"""Tests for login, logout, and session behaviour."""
import pytest
from conftest import insert_patron, login_as


class TestLoginPage:
    def test_renders(self, client):
        r = client.get('/login')
        assert r.status_code == 200
        assert b'Log In' in r.data

    def test_valid_login_redirects(self, client, owner_id):
        r = login_as(client, '0001', 'ownerpass')
        assert r.status_code == 200
        assert b'Welcome' in r.data

    def test_valid_login_sets_session(self, client, app, owner_id):
        with client:
            login_as(client, '0001', 'ownerpass')
            with client.session_transaction() as sess:
                assert 'patron_id' in sess
                assert sess['patron_id'] == owner_id

    def test_wrong_password_rejected(self, client, owner_id):
        r = login_as(client, '0001', 'wrongpass')
        assert b'Invalid' in r.data
        with client.session_transaction() as sess:
            assert 'patron_id' not in sess

    def test_unknown_patron_number_rejected(self, client):
        r = login_as(client, '9999', 'anything')
        assert b'Invalid' in r.data

    def test_patron_without_password_cannot_login(self, client, app):
        # Insert patron with no password_hash
        with app.app_context():
            db = __import__('app').get_db()
            db.execute(
                "INSERT INTO patrons (patron_number, name, role) VALUES ('0010', 'NoPw', 'regular')"
            )
            db.commit()
        r = login_as(client, '0010', 'anything')
        assert b'Invalid' in r.data

    def test_header_shows_login_button_when_logged_out(self, client):
        r = client.get('/')
        assert b'Log In' in r.data

    def test_header_shows_name_when_logged_in(self, client, owner_id):
        login_as(client, '0001', 'ownerpass')
        r = client.get('/')
        assert b'Owner' in r.data
        assert b'logged in' in r.data


class TestLogout:
    def test_logout_clears_session(self, client, owner_id):
        login_as(client, '0001', 'ownerpass')
        client.post('/logout', follow_redirects=True)
        with client.session_transaction() as sess:
            assert 'patron_id' not in sess

    def test_logout_redirects_to_index(self, client, owner_id):
        login_as(client, '0001', 'ownerpass')
        r = client.post('/logout', follow_redirects=True)
        assert r.status_code == 200
        assert b'logged out' in r.data

    def test_logout_requires_post(self, client):
        r = client.get('/logout')
        assert r.status_code == 405
