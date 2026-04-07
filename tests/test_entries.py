"""Tests for entry and volume CRUD operations."""
import pytest
from conftest import insert_library, insert_entry, insert_volume, login_as


@pytest.fixture(autouse=True)
def logged_in_as_cataloguer(client, cataloguer_id):
    login_as(client, '0002', 'catpass')


@pytest.fixture()
def library_id(app):
    return insert_library(app, 'Test Library')


@pytest.fixture()
def entry_id(app):
    return insert_entry(app, title='Original Title')


@pytest.fixture()
def volume_id(app, entry_id, library_id):
    return insert_volume(app, entry_id, library_id=library_id, barcode='00010')


# ---------------------------------------------------------------------------
# Entry creation
# ---------------------------------------------------------------------------

class TestNewEntry:
    def test_form_renders(self, client):
        r = client.get('/entry/new')
        assert r.status_code == 200
        assert b'New Entry' in r.data or b'entry' in r.data.lower()

    def test_create_book(self, client, library_id):
        r = client.post('/entry/new', data={
            'type':      'book',
            'title':     'A New Book',
            'year':      '2020',
            'restricted': 'unrestricted',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'A New Book' in r.data

    def test_create_journal_article(self, client):
        r = client.post('/entry/new', data={
            'type':        'journal_article',
            'title':       'An Article',
            'publication': 'Test Journal',
            'restricted':  'unrestricted',
        }, follow_redirects=True)
        assert r.status_code == 200

    def test_year_range_accepted(self, client):
        r = client.post('/entry/new', data={
            'type':      'book',
            'title':     'Multi-Year',
            'year':      '1990-1993',
            'restricted': 'unrestricted',
        }, follow_redirects=True)
        assert r.status_code == 200

    def test_invalid_year_not_stored(self, client, app):
        client.post('/entry/new', data={
            'type':      'book',
            'title':     'Bad Year',
            'year':      'not-a-year',
            'restricted': 'unrestricted',
        }, follow_redirects=True)
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute("SELECT year FROM entries WHERE title = 'Bad Year'").fetchone()
            assert row is None or row['year'] is None


# ---------------------------------------------------------------------------
# Entry editing
# ---------------------------------------------------------------------------

class TestEditEntry:
    def test_edit_form_renders(self, client, entry_id):
        r = client.get(f'/entry/{entry_id}/edit')
        assert r.status_code == 200
        assert b'Original Title' in r.data

    def test_edit_updates_title(self, client, app, entry_id):
        r = client.post(f'/entry/{entry_id}/edit', data={
            'type':      'book',
            'title':     'Updated Title',
            'restricted': 'unrestricted',
        }, follow_redirects=True)
        assert r.status_code == 200
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute('SELECT title FROM entries WHERE id = ?', (entry_id,)).fetchone()
            assert row['title'] == 'Updated Title'

    def test_edit_restricted_field(self, client, app, entry_id):
        client.post(f'/entry/{entry_id}/edit', data={
            'type':      'book',
            'title':     'Original Title',
            'restricted': 'hidden',
        })
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute('SELECT restricted FROM entries WHERE id = ?', (entry_id,)).fetchone()
            assert row['restricted'] == 'hidden'


# ---------------------------------------------------------------------------
# Entry deletion
# ---------------------------------------------------------------------------

class TestDeleteEntry:
    def test_delete_removes_entry(self, client, app, entry_id):
        client.post(f'/entry/{entry_id}/delete')
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute('SELECT id FROM entries WHERE id = ?', (entry_id,)).fetchone()
            assert row is None

    def test_delete_nonexistent_entry_is_safe(self, client):
        r = client.post('/entry/9999/delete', follow_redirects=True)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Entry detail
# ---------------------------------------------------------------------------

class TestEntryDetail:
    def test_displays_metadata(self, client, app, library_id):
        eid = insert_entry(app, title='Detail Test')
        insert_volume(app, eid, library_id=library_id)
        r = client.get(f'/entry/{eid}')
        assert r.status_code == 200
        assert b'Detail Test' in r.data

    def test_missing_entry_redirects(self, client):
        r = client.get('/entry/9999', follow_redirects=True)
        assert r.status_code == 200  # redirected to index with flash


# ---------------------------------------------------------------------------
# Volume operations
# ---------------------------------------------------------------------------

class TestVolumes:
    def test_add_volume(self, client, app, entry_id, library_id):
        r = client.post(f'/entry/{entry_id}/volume/new', data={
            'library_id':   str(library_id),
            'barcode':      '00020',
            'volume_number': '1',
        }, follow_redirects=True)
        assert r.status_code == 200
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            vol = db.execute(
                "SELECT * FROM volumes WHERE barcode = '00020'"
            ).fetchone()
            assert vol is not None

    def test_edit_volume(self, client, app, volume_id):
        r = client.post(f'/volume/{volume_id}/edit', data={
            'volume_number':     '2',
            'locus_call_number': 'CS.A1',
            'barcode':           '00010',
        }, follow_redirects=True)
        assert r.status_code == 200
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            vol = db.execute('SELECT * FROM volumes WHERE id = ?', (volume_id,)).fetchone()
            assert vol['volume_number'] == '2'
            assert vol['locus_call_number'] == 'CS.A1'

    def test_delete_volume(self, client, app, volume_id):
        client.post(f'/volume/{volume_id}/delete')
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute('SELECT id FROM volumes WHERE id = ?', (volume_id,)).fetchone()
            assert row is None
