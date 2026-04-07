"""Tests for patron management (owner-only) and password operations."""
import pytest
from conftest import insert_patron, login_as


@pytest.fixture(autouse=True)
def logged_in_as_owner(client, owner_id):
    login_as(client, '0001', 'ownerpass')


class TestAddPatron:
    def test_adds_patron(self, client, app):
        r = client.post('/manage/patron/add', data={
            'name':     'Alice',
            'role':     'regular',
            'password': 'pass123',
        }, follow_redirects=True)
        assert r.status_code == 200
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute("SELECT name FROM patrons WHERE name = 'Alice'").fetchone()
            assert row is not None

    def test_patron_number_auto_assigned(self, client, app, owner_id):
        # owner is 0001, next should be 0002
        client.post('/manage/patron/add', data={
            'name': 'Bob', 'role': 'regular', 'password': 'pass123'
        })
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute("SELECT patron_number FROM patrons WHERE name = 'Bob'").fetchone()
            assert row['patron_number'] == '0002'

    def test_patron_numbers_sequential(self, client, app, owner_id):
        for name in ('P1', 'P2', 'P3'):
            client.post('/manage/patron/add', data={
                'name': name, 'role': 'regular', 'password': 'pass'
            })
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            nums = [r['patron_number'] for r in
                    db.execute("SELECT patron_number FROM patrons WHERE name IN ('P1','P2','P3') "
                               "ORDER BY patron_number").fetchall()]
            assert nums == ['0002', '0003', '0004']

    def test_password_is_hashed(self, client, app):
        client.post('/manage/patron/add', data={
            'name': 'Carol', 'role': 'regular', 'password': 'plaintext'
        })
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute("SELECT password_hash FROM patrons WHERE name = 'Carol'").fetchone()
            assert row['password_hash'] != 'plaintext'
            assert row['password_hash'].startswith('pbkdf2') or row['password_hash'].startswith('scrypt')

    def test_requires_password(self, client, app):
        r = client.post('/manage/patron/add', data={
            'name': 'NoPw', 'role': 'regular', 'password': ''
        }, follow_redirects=True)
        assert b'password is required' in r.data.lower() or b'Password' in r.data
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute("SELECT id FROM patrons WHERE name = 'NoPw'").fetchone()
            assert row is None

    def test_requires_name(self, client, app):
        client.post('/manage/patron/add', data={
            'name': '', 'role': 'regular', 'password': 'pass'
        })
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            count = db.execute("SELECT COUNT(*) FROM patrons WHERE name = ''").fetchone()[0]
            assert count == 0

    def test_can_add_email(self, client, app):
        client.post('/manage/patron/add', data={
            'name': 'Dave', 'role': 'regular',
            'password': 'pass', 'email': 'dave@example.com'
        })
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute("SELECT email FROM patrons WHERE name = 'Dave'").fetchone()
            assert row['email'] == 'dave@example.com'

    def test_all_roles_accepted(self, client, app, owner_id):
        for role in ('cataloguer', 'regular', 'restricted', 'guest'):
            client.post('/manage/patron/add', data={
                'name': f'Patron {role}', 'role': role, 'password': 'pass'
            })
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            for role in ('cataloguer', 'regular', 'restricted', 'guest'):
                row = db.execute(
                    'SELECT role FROM patrons WHERE name = ?', (f'Patron {role}',)
                ).fetchone()
                assert row['role'] == role


class TestOwnerUniqueness:
    def test_cannot_add_second_owner(self, client, owner_id):
        # owner_id is already the first owner
        r = client.post('/manage/patron/add', data={
            'name': 'Second Owner', 'role': 'owner', 'password': 'pass'
        }, follow_redirects=True)
        assert b'Owner already exists' in r.data or b'only one' in r.data.lower()

    def test_second_owner_not_inserted(self, client, app, owner_id):
        client.post('/manage/patron/add', data={
            'name': 'Second Owner', 'role': 'owner', 'password': 'pass'
        })
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            count = db.execute("SELECT COUNT(*) FROM patrons WHERE role = 'owner'").fetchone()[0]
            assert count == 1


class TestDeletePatron:
    def test_deletes_patron(self, client, app):
        pid = insert_patron(app, '0010', 'To Delete', 'regular', 'pass')
        client.post(f'/manage/patron/{pid}/delete', follow_redirects=True)
        with app.app_context():
            import app as app_module
            db = app_module.get_db()
            row = db.execute('SELECT id FROM patrons WHERE id = ?', (pid,)).fetchone()
            assert row is None

    def test_nonexistent_patron_is_safe(self, client):
        r = client.post('/manage/patron/9999/delete', follow_redirects=True)
        assert r.status_code == 200


class TestSetPassword:
    def test_sets_password_hash(self, client, app):
        pid = insert_patron(app, '0010', 'PwTest', 'regular', 'oldpass')
        client.post(f'/manage/patron/{pid}/set-password', data={'password': 'newpass123'})
        with app.app_context():
            import app as app_module
            from werkzeug.security import check_password_hash
            db = app_module.get_db()
            row = db.execute('SELECT password_hash FROM patrons WHERE id = ?', (pid,)).fetchone()
            assert check_password_hash(row['password_hash'], 'newpass123')

    def test_new_password_works_for_login(self, client, app):
        pid = insert_patron(app, '0010', 'PwLogin', 'regular', 'oldpass')
        client.post(f'/manage/patron/{pid}/set-password', data={'password': 'brand-new'})
        # Log out owner, log in as updated patron
        client.post('/logout')
        r = login_as(client, '0010', 'brand-new')
        assert b'Welcome' in r.data

    def test_short_password_rejected(self, client, app):
        pid = insert_patron(app, '0010', 'ShortPw', 'regular', 'oldpass')
        r = client.post(f'/manage/patron/{pid}/set-password',
                        data={'password': 'ab'}, follow_redirects=True)
        assert b'at least 4' in r.data

    def test_nonexistent_patron_is_safe(self, client):
        r = client.post('/manage/patron/9999/set-password',
                        data={'password': 'valid123'}, follow_redirects=True)
        assert r.status_code == 200
