"""Tests for role-based access control (403 enforcement)."""
import pytest
from conftest import insert_patron, login_as


# Routes that require owner OR cataloguer
CATALOGUER_ROUTES = [
    ('GET',  '/entry/new'),
    ('GET',  '/entry/9999/edit'),
    ('GET',  '/manage'),
    ('GET',  '/checkout'),
    ('GET',  '/barcode'),
    ('GET',  '/manage/barcode-check'),
    ('GET',  '/audit'),
]

# Routes that require owner only
OWNER_ONLY_ROUTES = [
    ('POST', '/library/new'),
    ('POST', '/manage/patron/add'),
    ('POST', '/manage/patron/9999/delete'),
    ('POST', '/manage/patron/9999/set-password'),
]


def _do(client, method, path):
    if method == 'GET':
        return client.get(path)
    return client.post(path, data={})


class TestUnauthenticatedAccess:
    @pytest.mark.parametrize('method,path', CATALOGUER_ROUTES)
    def test_403_unauthenticated(self, client, method, path):
        r = _do(client, method, path)
        assert r.status_code == 403

    @pytest.mark.parametrize('method,path', OWNER_ONLY_ROUTES)
    def test_403_unauthenticated_owner_routes(self, client, method, path):
        r = _do(client, method, path)
        assert r.status_code == 403

    def test_public_routes_accessible(self, client):
        for path in ['/', '/search', '/about', '/shelf-browse', '/login']:
            r = client.get(path)
            assert r.status_code in (200, 302), f'{path} returned {r.status_code}'


class TestRegularUserAccess:
    @pytest.fixture(autouse=True)
    def _login(self, client, regular_id):
        login_as(client, '0003', 'regpass')

    @pytest.mark.parametrize('method,path', CATALOGUER_ROUTES)
    def test_403_regular_user(self, client, method, path):
        r = _do(client, method, path)
        assert r.status_code == 403

    @pytest.mark.parametrize('method,path', OWNER_ONLY_ROUTES)
    def test_403_regular_user_owner_routes(self, client, method, path):
        r = _do(client, method, path)
        assert r.status_code == 403

    def test_can_browse_public(self, client):
        r = client.get('/search')
        assert r.status_code == 200


class TestGuestRoleAccess:
    """A patron with role='guest' should behave like unauthenticated for protected routes."""
    @pytest.fixture(autouse=True)
    def _login(self, client, guest_id):
        login_as(client, '0005', 'guestpass')

    @pytest.mark.parametrize('method,path', CATALOGUER_ROUTES)
    def test_403_guest_role(self, client, method, path):
        r = _do(client, method, path)
        assert r.status_code == 403


class TestCataloguerAccess:
    @pytest.fixture(autouse=True)
    def _login(self, client, cataloguer_id):
        login_as(client, '0002', 'catpass')

    def test_can_access_manage(self, client):
        r = client.get('/manage')
        assert r.status_code == 200

    def test_can_access_new_entry(self, client):
        r = client.get('/entry/new')
        assert r.status_code == 200

    def test_can_access_checkout(self, client):
        r = client.get('/checkout')
        assert r.status_code == 200

    def test_cannot_create_library(self, client):
        r = client.post('/library/new', data={'name': 'X'})
        assert r.status_code == 403

    def test_cannot_add_patron(self, client):
        r = client.post('/manage/patron/add', data={
            'name': 'X', 'role': 'regular', 'password': 'pass1'
        })
        assert r.status_code == 403

    def test_cannot_delete_patron(self, client):
        r = client.post('/manage/patron/9999/delete')
        assert r.status_code == 403

    def test_cannot_set_patron_password(self, client):
        r = client.post('/manage/patron/9999/set-password', data={'password': 'newpass'})
        assert r.status_code == 403


class TestOwnerAccess:
    @pytest.fixture(autouse=True)
    def _login(self, client, owner_id):
        login_as(client, '0001', 'ownerpass')

    def test_can_access_manage(self, client):
        r = client.get('/manage')
        assert r.status_code == 200

    def test_can_create_library(self, client):
        r = client.post('/library/new', data={'name': 'New Lib'}, follow_redirects=True)
        assert r.status_code == 200

    def test_can_add_patron(self, client):
        r = client.post('/manage/patron/add', data={
            'name': 'Test', 'role': 'regular', 'password': 'pass123'
        }, follow_redirects=True)
        assert r.status_code == 200

    def test_can_access_new_entry(self, client):
        r = client.get('/entry/new')
        assert r.status_code == 200
