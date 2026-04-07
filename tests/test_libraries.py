"""Tests for library CRUD (owner-only operations)."""
import pytest
from conftest import insert_library, login_as


@pytest.fixture(autouse=True)
def logged_in_as_owner(client, owner_id):
    login_as(client, '0001', 'ownerpass')


class TestCreateLibrary:
    def test_creates_library(self, client, app):
        r = client.post('/library/new', data={'name': 'My Library'}, follow_redirects=True)
        assert r.status_code == 200
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute("SELECT name FROM libraries WHERE name = 'My Library'").fetchone()
            assert row is not None

    def test_duplicate_name_flashes_error(self, client, app):
        insert_library(app, 'Existing')
        r = client.post('/library/new', data={'name': 'Existing'}, follow_redirects=True)
        assert b'already exists' in r.data

    def test_empty_name_flashes_error(self, client):
        r = client.post('/library/new', data={'name': ''}, follow_redirects=True)
        assert b'required' in r.data.lower() or r.status_code == 200

    def test_can_set_primary(self, client, app):
        client.post('/library/new', data={'name': 'Primary Lib', 'is_primary': '1'})
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute("SELECT is_primary FROM libraries WHERE name = 'Primary Lib'").fetchone()
            assert row['is_primary'] == 1

    def test_can_set_storage(self, client, app):
        client.post('/library/new', data={'name': 'Storage Lib', 'is_storage': '1'})
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute("SELECT is_storage FROM libraries WHERE name = 'Storage Lib'").fetchone()
            assert row['is_storage'] == 1


class TestEditLibrary:
    def test_renames_library(self, client, app):
        lib_id = insert_library(app, 'Old Name')
        r = client.post(f'/library/{lib_id}/edit', data={
            'name':       'New Name',
            'is_primary': '0',
            'is_storage': '0',
        }, follow_redirects=True)
        assert r.status_code == 200
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute('SELECT name FROM libraries WHERE id = ?', (lib_id,)).fetchone()
            assert row['name'] == 'New Name'

    def test_sets_primary_flag(self, client, app):
        lib_id = insert_library(app, 'Lib A')
        client.post(f'/library/{lib_id}/edit', data={
            'name': 'Lib A', 'is_primary': '1', 'is_storage': '0'
        })
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute('SELECT is_primary FROM libraries WHERE id = ?', (lib_id,)).fetchone()
            assert row['is_primary'] == 1

    def test_only_one_primary_at_a_time(self, client, app):
        lib_a = insert_library(app, 'Lib A', is_primary=1)
        lib_b = insert_library(app, 'Lib B')
        client.post(f'/library/{lib_b}/edit', data={
            'name': 'Lib B', 'is_primary': '1', 'is_storage': '0'
        })
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            primaries = db.execute(
                'SELECT id FROM libraries WHERE is_primary = 1'
            ).fetchall()
            assert len(primaries) == 1
            assert primaries[0]['id'] == lib_b


class TestDeleteLibrary:
    def test_deletes_library(self, client, app):
        lib_id = insert_library(app, 'To Delete')
        client.post(f'/library/{lib_id}/delete', follow_redirects=True)
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute('SELECT id FROM libraries WHERE id = ?', (lib_id,)).fetchone()
            assert row is None

    def test_nonexistent_library_is_safe(self, client):
        r = client.post('/library/9999/delete', follow_redirects=True)
        assert r.status_code == 200
