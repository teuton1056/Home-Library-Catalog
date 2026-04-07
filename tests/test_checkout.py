"""Tests for the checkout desk: scanning, checkout, check-in, and transfer."""
import pytest
from conftest import insert_library, insert_entry, insert_volume, insert_patron, login_as


@pytest.fixture(autouse=True)
def logged_in_as_cataloguer(client, cataloguer_id):
    login_as(client, '0002', 'catpass')


@pytest.fixture()
def library_id(app):
    return insert_library(app, 'Main Library')


@pytest.fixture()
def storage_id(app):
    return insert_library(app, 'Storage', is_storage=1)


@pytest.fixture()
def entry_id(app):
    return insert_entry(app, title='Checkout Test Book')


@pytest.fixture()
def volume_id(app, entry_id, library_id):
    return insert_volume(app, entry_id, library_id=library_id, barcode='00042')


@pytest.fixture()
def borrower_id(app):
    """A regular patron who can borrow items."""
    return insert_patron(app, '0010', 'Borrower', 'regular', 'borrowpass')


@pytest.fixture()
def guest_patron_id(app):
    return insert_patron(app, '0011', 'Guest Patron', 'guest', 'guestpass')


def _scan(client, barcode):
    return client.post('/checkout/scan', data={'barcode': barcode},
                       follow_redirects=True)


def _bin_has(client, volume_id):
    """Check if volume_id is currently in the session bin."""
    with client.session_transaction() as sess:
        return volume_id in sess.get('checkout_bin', [])


# ---------------------------------------------------------------------------
# Desk rendering
# ---------------------------------------------------------------------------

class TestCheckoutDesk:
    def test_desk_renders(self, client):
        r = client.get('/checkout')
        assert r.status_code == 200

    def test_empty_bin_message(self, client):
        r = client.get('/checkout')
        # Action cards should not appear when bin is empty
        assert b'do-checkout' not in r.data


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

class TestScan:
    def test_scan_known_barcode_adds_to_bin(self, client, volume_id):
        _scan(client, '00042')
        assert _bin_has(client, volume_id)

    def test_scan_unknown_barcode_flashes_error(self, client):
        r = _scan(client, '99999')
        assert b'No volume found' in r.data
        with client.session_transaction() as sess:
            assert sess.get('checkout_bin', []) == []

    def test_scan_duplicate_not_added_twice(self, client, volume_id):
        _scan(client, '00042')
        _scan(client, '00042')
        with client.session_transaction() as sess:
            bin_ids = sess.get('checkout_bin', [])
            assert bin_ids.count(volume_id) == 1

    def test_scan_empty_barcode_flashes_error(self, client):
        r = _scan(client, '')
        assert b'barcode' in r.data.lower()

    def test_multiple_volumes_in_bin(self, client, app, entry_id, library_id, volume_id):
        v2 = insert_volume(app, entry_id, library_id=library_id, barcode='00043')
        _scan(client, '00042')
        _scan(client, '00043')
        with client.session_transaction() as sess:
            assert len(sess.get('checkout_bin', [])) == 2


# ---------------------------------------------------------------------------
# Remove / clear
# ---------------------------------------------------------------------------

class TestBinManagement:
    def test_remove_from_bin(self, client, volume_id):
        _scan(client, '00042')
        assert _bin_has(client, volume_id)
        client.post(f'/checkout/remove/{volume_id}', follow_redirects=True)
        assert not _bin_has(client, volume_id)

    def test_remove_nonexistent_id_is_safe(self, client):
        r = client.post('/checkout/remove/9999', follow_redirects=True)
        assert r.status_code == 200

    def test_clear_empties_bin(self, client, volume_id):
        _scan(client, '00042')
        client.post('/checkout/clear', follow_redirects=True)
        with client.session_transaction() as sess:
            assert sess.get('checkout_bin', []) == []


# ---------------------------------------------------------------------------
# Check out
# ---------------------------------------------------------------------------

class TestDoCheckout:
    def test_checkout_marks_volume_checked_out(self, client, app, volume_id, borrower_id):
        _scan(client, '00042')
        client.post('/checkout/do-checkout', data={'patron_number': '0010'},
                    follow_redirects=True)
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            vol = db.execute('SELECT checked_out, checked_out_to FROM volumes WHERE id = ?',
                             (volume_id,)).fetchone()
            assert vol['checked_out'] == 1
            assert vol['checked_out_to'] == borrower_id

    def test_checkout_creates_audit_record(self, client, app, volume_id, borrower_id):
        _scan(client, '00042')
        client.post('/checkout/do-checkout', data={'patron_number': '0010'})
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            record = db.execute(
                'SELECT * FROM checkouts WHERE volume_id = ?', (volume_id,)
            ).fetchone()
            assert record is not None
            assert record['returned_at'] is None

    def test_checkout_clears_bin(self, client, volume_id, borrower_id):
        _scan(client, '00042')
        client.post('/checkout/do-checkout', data={'patron_number': '0010'})
        with client.session_transaction() as sess:
            assert sess.get('checkout_bin', []) == []

    def test_checkout_to_guest_refused(self, client, app, volume_id, guest_patron_id):
        _scan(client, '00042')
        r = client.post('/checkout/do-checkout', data={'patron_number': '0011'},
                        follow_redirects=True)
        assert b'not permitted' in r.data
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            vol = db.execute('SELECT checked_out FROM volumes WHERE id = ?',
                             (volume_id,)).fetchone()
            assert vol['checked_out'] == 0

    def test_checkout_unknown_patron_refused(self, client, volume_id):
        _scan(client, '00042')
        r = client.post('/checkout/do-checkout', data={'patron_number': '9999'},
                        follow_redirects=True)
        assert b'No patron found' in r.data

    def test_checkout_empty_bin_flashes_error(self, client):
        r = client.post('/checkout/do-checkout', data={'patron_number': '0010'},
                        follow_redirects=True)
        assert b'empty' in r.data.lower()

    def test_checkout_missing_patron_number_flashes_error(self, client, volume_id):
        _scan(client, '00042')
        r = client.post('/checkout/do-checkout', data={'patron_number': ''},
                        follow_redirects=True)
        assert b'required' in r.data.lower()


# ---------------------------------------------------------------------------
# Check in
# ---------------------------------------------------------------------------

class TestDoCheckin:
    def test_checkin_clears_checked_out_flag(self, client, app, volume_id, borrower_id):
        # First check out
        _scan(client, '00042')
        client.post('/checkout/do-checkout', data={'patron_number': '0010'})
        # Then check in
        _scan(client, '00042')
        client.post('/checkout/do-checkin', follow_redirects=True)
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            vol = db.execute('SELECT checked_out, checked_out_to FROM volumes WHERE id = ?',
                             (volume_id,)).fetchone()
            assert vol['checked_out'] == 0
            assert vol['checked_out_to'] is None

    def test_checkin_closes_checkout_record(self, client, app, volume_id, borrower_id):
        _scan(client, '00042')
        client.post('/checkout/do-checkout', data={'patron_number': '0010'})
        _scan(client, '00042')
        client.post('/checkout/do-checkin')
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            record = db.execute(
                'SELECT returned_at FROM checkouts WHERE volume_id = ?', (volume_id,)
            ).fetchone()
            assert record['returned_at'] is not None

    def test_checkin_not_checked_out_volume_is_ignored(self, client, app, volume_id):
        _scan(client, '00042')
        r = client.post('/checkout/do-checkin', follow_redirects=True)
        assert b'ignored' in r.data or b'not marked' in r.data
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            vol = db.execute('SELECT checked_out FROM volumes WHERE id = ?',
                             (volume_id,)).fetchone()
            assert vol['checked_out'] == 0

    def test_checkin_empty_bin_flashes_error(self, client):
        r = client.post('/checkout/do-checkin', follow_redirects=True)
        assert b'empty' in r.data.lower()


# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------

class TestDoTransfer:
    def test_transfer_updates_library(self, client, app, volume_id, library_id):
        dest_id = insert_library(app, 'Destination')
        _scan(client, '00042')
        client.post('/checkout/do-transfer',
                    data={'library_id': str(dest_id)}, follow_redirects=True)
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            vol = db.execute('SELECT library_id FROM volumes WHERE id = ?',
                             (volume_id,)).fetchone()
            assert vol['library_id'] == dest_id

    def test_transfer_to_storage_requires_box(self, client, volume_id, storage_id):
        _scan(client, '00042')
        r = client.post('/checkout/do-transfer',
                        data={'library_id': str(storage_id)},
                        follow_redirects=True)
        assert b'Box number is required' in r.data

    def test_transfer_to_storage_with_box(self, client, app, volume_id, storage_id):
        _scan(client, '00042')
        client.post('/checkout/do-transfer',
                    data={'library_id': str(storage_id), 'box_number': 'A1'})
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            vol = db.execute('SELECT library_id, box_number FROM volumes WHERE id = ?',
                             (volume_id,)).fetchone()
            assert vol['library_id'] == storage_id
            assert vol['box_number'] == 'A1'

    def test_transfer_auto_checks_in_borrowed_volume(self, client, app, volume_id,
                                                     library_id, borrower_id):
        dest_id = insert_library(app, 'New Home')
        _scan(client, '00042')
        client.post('/checkout/do-checkout', data={'patron_number': '0010'})
        _scan(client, '00042')
        client.post('/checkout/do-transfer', data={'library_id': str(dest_id)})
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            vol = db.execute('SELECT checked_out FROM volumes WHERE id = ?',
                             (volume_id,)).fetchone()
            assert vol['checked_out'] == 0
            record = db.execute(
                'SELECT returned_at FROM checkouts WHERE volume_id = ?', (volume_id,)
            ).fetchone()
            assert record['returned_at'] is not None

    def test_transfer_empty_bin_flashes_error(self, client, library_id):
        r = client.post('/checkout/do-transfer',
                        data={'library_id': str(library_id)}, follow_redirects=True)
        assert b'empty' in r.data.lower()

    def test_transfer_no_library_selected_flashes_error(self, client, volume_id):
        _scan(client, '00042')
        r = client.post('/checkout/do-transfer', data={}, follow_redirects=True)
        assert b'select' in r.data.lower() or b'library' in r.data.lower()
