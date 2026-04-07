"""Tests for restricted-entry visibility filtering by role."""
import pytest
from conftest import (
    insert_library, insert_entry, insert_volume, login_as
)


@pytest.fixture()
def library_id(app):
    return insert_library(app, 'Main Library')


@pytest.fixture()
def entries(app, library_id):
    """Three entries with different restricted values, each with one volume."""
    ids = {}
    for restricted in ('unrestricted', 'restricted', 'hidden'):
        eid = insert_entry(app, title=f'{restricted.title()} Book', restricted=restricted)
        insert_volume(app, eid, library_id=library_id)
        ids[restricted] = eid
    return ids


# ---------------------------------------------------------------------------
# Library view
# ---------------------------------------------------------------------------

class TestLibraryViewVisibility:
    def test_guest_sees_only_unrestricted(self, client, library_id, entries):
        r = client.get(f'/library/{library_id}')
        assert b'Unrestricted Book' in r.data
        assert b'Restricted Book' not in r.data
        assert b'Hidden Book' not in r.data

    def test_regular_sees_unrestricted_and_restricted(self, client, library_id, entries, regular_id):
        login_as(client, '0003', 'regpass')
        r = client.get(f'/library/{library_id}')
        assert b'Unrestricted Book' in r.data
        assert b'Restricted Book' in r.data
        assert b'Hidden Book' not in r.data

    def test_restricted_patron_sees_same_as_regular(self, client, library_id, entries, restricted_id):
        login_as(client, '0004', 'respass')
        r = client.get(f'/library/{library_id}')
        assert b'Restricted Book' in r.data
        assert b'Hidden Book' not in r.data

    def test_cataloguer_sees_all(self, client, library_id, entries, cataloguer_id):
        login_as(client, '0002', 'catpass')
        r = client.get(f'/library/{library_id}')
        assert b'Unrestricted Book' in r.data
        assert b'Restricted Book' in r.data
        assert b'Hidden Book' in r.data

    def test_owner_sees_all(self, client, library_id, entries, owner_id):
        login_as(client, '0001', 'ownerpass')
        r = client.get(f'/library/{library_id}')
        assert b'Hidden Book' in r.data

    def test_guest_role_sees_only_unrestricted(self, client, library_id, entries, guest_id):
        login_as(client, '0005', 'guestpass')
        r = client.get(f'/library/{library_id}')
        assert b'Restricted Book' not in r.data
        assert b'Hidden Book' not in r.data


# ---------------------------------------------------------------------------
# Entry detail access
# ---------------------------------------------------------------------------

class TestEntryDetailAccess:
    def test_guest_can_view_unrestricted(self, client, entries):
        r = client.get(f'/entry/{entries["unrestricted"]}')
        assert r.status_code == 200

    def test_guest_cannot_view_restricted(self, client, entries):
        r = client.get(f'/entry/{entries["restricted"]}')
        assert r.status_code == 403

    def test_guest_cannot_view_hidden(self, client, entries):
        r = client.get(f'/entry/{entries["hidden"]}')
        assert r.status_code == 403

    def test_regular_can_view_restricted(self, client, entries, regular_id):
        login_as(client, '0003', 'regpass')
        r = client.get(f'/entry/{entries["restricted"]}')
        assert r.status_code == 200

    def test_regular_cannot_view_hidden(self, client, entries, regular_id):
        login_as(client, '0003', 'regpass')
        r = client.get(f'/entry/{entries["hidden"]}')
        assert r.status_code == 403

    def test_cataloguer_can_view_hidden(self, client, entries, cataloguer_id):
        login_as(client, '0002', 'catpass')
        r = client.get(f'/entry/{entries["hidden"]}')
        assert r.status_code == 200

    def test_owner_can_view_hidden(self, client, entries, owner_id):
        login_as(client, '0001', 'ownerpass')
        r = client.get(f'/entry/{entries["hidden"]}')
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Search visibility
# ---------------------------------------------------------------------------

class TestSearchVisibility:
    def test_guest_search_excludes_restricted(self, client, entries):
        r = client.get('/search?q=Book')
        assert b'Unrestricted Book' in r.data
        assert b'Restricted Book' not in r.data
        assert b'Hidden Book' not in r.data

    def test_regular_search_excludes_hidden(self, client, entries, regular_id):
        login_as(client, '0003', 'regpass')
        r = client.get('/search?q=Book')
        assert b'Restricted Book' in r.data
        assert b'Hidden Book' not in r.data

    def test_cataloguer_search_includes_all(self, client, entries, cataloguer_id):
        login_as(client, '0002', 'catpass')
        r = client.get('/search?q=Book')
        assert b'Restricted Book' in r.data
        assert b'Hidden Book' in r.data
