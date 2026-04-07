"""Tests for the checkouts overview and patron checkout history pages."""
import pytest
from conftest import insert_library, insert_entry, insert_volume, insert_patron, login_as


@pytest.fixture()
def library_id(app):
    return insert_library(app, 'Main Library')


@pytest.fixture()
def borrower_id(app):
    return insert_patron(app, '0010', 'Alice Borrower', 'regular', 'alicepass')


@pytest.fixture()
def volume_id(app, library_id):
    eid = insert_entry(app, title='Borrowed Book')
    return insert_volume(app, eid, library_id=library_id, barcode='00100')


def _checkout(client, barcode, patron_number):
    """Scan a barcode then perform checkout."""
    client.post('/checkout/scan', data={'barcode': barcode})
    client.post('/checkout/do-checkout', data={'patron_number': patron_number})


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class TestCheckoutsOverviewAccess:
    def test_unauthenticated_gets_403(self, client):
        r = client.get('/checkouts')
        assert r.status_code == 403

    def test_regular_user_gets_403(self, client, regular_id):
        login_as(client, '0003', 'regpass')
        r = client.get('/checkouts')
        assert r.status_code == 403

    def test_cataloguer_can_access(self, client, cataloguer_id):
        login_as(client, '0002', 'catpass')
        r = client.get('/checkouts')
        assert r.status_code == 200

    def test_owner_can_access(self, client, owner_id):
        login_as(client, '0001', 'ownerpass')
        r = client.get('/checkouts')
        assert r.status_code == 200


class TestPatronHistoryAccess:
    def test_unauthenticated_gets_403(self, client, borrower_id):
        r = client.get(f'/patron/{borrower_id}/checkouts')
        assert r.status_code == 403

    def test_regular_user_gets_403(self, client, borrower_id, regular_id):
        login_as(client, '0003', 'regpass')
        r = client.get(f'/patron/{borrower_id}/checkouts')
        assert r.status_code == 403

    def test_cataloguer_can_access(self, client, borrower_id, cataloguer_id):
        login_as(client, '0002', 'catpass')
        r = client.get(f'/patron/{borrower_id}/checkouts')
        assert r.status_code == 200

    def test_owner_can_access(self, client, borrower_id, owner_id):
        login_as(client, '0001', 'ownerpass')
        r = client.get(f'/patron/{borrower_id}/checkouts')
        assert r.status_code == 200

    def test_nonexistent_patron_redirects(self, client, owner_id):
        login_as(client, '0001', 'ownerpass')
        r = client.get('/patron/9999/checkouts', follow_redirects=True)
        assert r.status_code == 200
        assert b'not found' in r.data.lower()


# ---------------------------------------------------------------------------
# Checkouts overview content
# ---------------------------------------------------------------------------

class TestCheckoutsOverviewContent:
    @pytest.fixture(autouse=True)
    def logged_in(self, client, cataloguer_id):
        login_as(client, '0002', 'catpass')

    def test_empty_state_message(self, client):
        r = client.get('/checkouts')
        assert b'No items are currently checked out' in r.data

    def test_shows_checked_out_item(self, client, volume_id, borrower_id):
        _checkout(client, '00100', '0010')
        r = client.get('/checkouts')
        assert b'Borrowed Book' in r.data
        assert b'Alice Borrower' in r.data

    def test_shows_patron_number(self, client, volume_id, borrower_id):
        _checkout(client, '00100', '0010')
        r = client.get('/checkouts')
        assert b'0010' in r.data

    def test_shows_barcode(self, client, volume_id, borrower_id):
        _checkout(client, '00100', '0010')
        r = client.get('/checkouts')
        assert b'00100' in r.data

    def test_shows_library_name(self, client, volume_id, borrower_id):
        _checkout(client, '00100', '0010')
        r = client.get('/checkouts')
        assert b'Main Library' in r.data

    def test_checked_in_item_not_shown(self, client, volume_id, borrower_id):
        _checkout(client, '00100', '0010')
        # check back in
        client.post('/checkout/scan', data={'barcode': '00100'})
        client.post('/checkout/do-checkin')
        r = client.get('/checkouts')
        # empty-state message confirms nothing is in the table
        assert b'No items are currently checked out' in r.data

    def test_multiple_items_shown(self, client, app, library_id, borrower_id, volume_id):
        eid2 = insert_entry(app, title='Second Book')
        insert_volume(app, eid2, library_id=library_id, barcode='00101')
        _checkout(client, '00100', '0010')
        # scan second book and checkout in same transaction
        client.post('/checkout/scan', data={'barcode': '00101'})
        client.post('/checkout/do-checkout', data={'patron_number': '0010'})
        r = client.get('/checkouts')
        assert b'Borrowed Book' in r.data
        assert b'Second Book' in r.data

    def test_patron_name_links_to_history(self, client, volume_id, borrower_id):
        _checkout(client, '00100', '0010')
        r = client.get('/checkouts')
        assert f'/patron/{borrower_id}/checkouts'.encode() in r.data


# ---------------------------------------------------------------------------
# Patron checkout history content
# ---------------------------------------------------------------------------

class TestPatronCheckoutHistory:
    @pytest.fixture(autouse=True)
    def logged_in(self, client, cataloguer_id):
        login_as(client, '0002', 'catpass')

    def test_empty_history_message(self, client, borrower_id):
        r = client.get(f'/patron/{borrower_id}/checkouts')
        assert b'No checkout history' in r.data

    def test_shows_patron_name(self, client, borrower_id):
        r = client.get(f'/patron/{borrower_id}/checkouts')
        assert b'Alice Borrower' in r.data

    def test_shows_patron_number(self, client, borrower_id):
        r = client.get(f'/patron/{borrower_id}/checkouts')
        assert b'0010' in r.data

    def test_shows_checkout_record(self, client, volume_id, borrower_id):
        _checkout(client, '00100', '0010')
        r = client.get(f'/patron/{borrower_id}/checkouts')
        assert b'Borrowed Book' in r.data

    def test_out_badge_when_not_returned(self, client, volume_id, borrower_id):
        _checkout(client, '00100', '0010')
        r = client.get(f'/patron/{borrower_id}/checkouts')
        assert b'Out' in r.data

    def test_returned_badge_after_checkin(self, client, volume_id, borrower_id):
        _checkout(client, '00100', '0010')
        client.post('/checkout/scan', data={'barcode': '00100'})
        client.post('/checkout/do-checkin')
        r = client.get(f'/patron/{borrower_id}/checkouts')
        assert b'Returned' in r.data

    def test_shows_multiple_records(self, client, volume_id, borrower_id):
        # checkout and return, then checkout again
        _checkout(client, '00100', '0010')
        client.post('/checkout/scan', data={'barcode': '00100'})
        client.post('/checkout/do-checkin')
        _checkout(client, '00100', '0010')
        r = client.get(f'/patron/{borrower_id}/checkouts')
        assert r.data.count(b'Borrowed Book') >= 2

    def test_only_shows_this_patrons_records(self, client, app, volume_id,
                                              borrower_id, library_id):
        other_id = insert_patron(app, '0011', 'Bob Other', 'regular', 'bobpass')
        _checkout(client, '00100', '0010')
        # check in and re-checkout to other patron
        client.post('/checkout/scan', data={'barcode': '00100'})
        client.post('/checkout/do-checkin')
        _checkout(client, '00100', '0011')

        # drain flash messages before loading Alice's history
        client.get('/checkouts')
        r = client.get(f'/patron/{borrower_id}/checkouts')
        # Alice has one record; Bob's name must not appear in the table
        assert b'Alice Borrower' in r.data
        assert b'Bob Other' not in r.data
