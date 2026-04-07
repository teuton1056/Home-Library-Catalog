"""Tests for search functionality."""
import pytest
from conftest import insert_library, insert_entry, insert_volume, login_as


@pytest.fixture()
def library_id(app):
    return insert_library(app, 'Test Library')


@pytest.fixture()
def populated_db(app, library_id):
    """A small set of entries covering different fields and types."""
    entries = [
        insert_entry(app, title='Python Programming', etype='book'),
        insert_entry(app, title='Flask Web Development', etype='book'),
        insert_entry(app, title='Science Journal', etype='journal'),
    ]
    for i, eid in enumerate(entries):
        insert_volume(app, eid, library_id=library_id, barcode=f'0010{i}')
    return entries


class TestBasicSearch:
    def test_search_page_renders(self, client):
        r = client.get('/search')
        assert r.status_code == 200

    def test_search_finds_by_title(self, client, populated_db):
        r = client.get('/search?q=Python')
        assert b'Python Programming' in r.data
        assert b'Flask Web Development' not in r.data

    def test_search_is_case_insensitive(self, client, populated_db):
        r = client.get('/search?q=python')
        assert b'Python Programming' in r.data

    def test_search_partial_match(self, client, populated_db):
        r = client.get('/search?q=Web')
        assert b'Flask Web Development' in r.data

    def test_search_no_results(self, client, populated_db):
        r = client.get('/search?q=xyzzy_no_match')
        assert b'Python' not in r.data

    def test_empty_query_shows_no_results(self, client, populated_db):
        r = client.get('/search?q=')
        assert b'Python Programming' not in r.data

    def test_filter_by_type(self, client, populated_db):
        r = client.get('/search?q=Science&type=journal')
        assert b'Science Journal' in r.data
        assert b'Python Programming' not in r.data

    def test_filter_by_library(self, client, app, populated_db, library_id):
        other_lib = insert_library(app, 'Other Library')
        other_entry = insert_entry(app, title='Other Book')
        insert_volume(app, other_entry, library_id=other_lib, barcode='00200')
        r = client.get(f'/search?q=Book&library_id={library_id}')
        assert b'Flask Web Development' in r.data
        assert b'Other Book' not in r.data


class TestAdvancedSearch:
    def test_adv_search_by_title(self, client, populated_db):
        r = client.get('/search?adv=1&field_0=title&op_0=AND&term_0=Python')
        assert b'Python Programming' in r.data
        assert b'Flask' not in r.data

    def test_adv_search_by_type(self, client, populated_db):
        r = client.get('/search?adv=1&field_0=type&op_0=AND&term_0=journal')
        assert b'Science Journal' in r.data
        assert b'Python Programming' not in r.data

    def test_adv_search_not_operator(self, client, populated_db):
        r = client.get(
            '/search?adv=1'
            '&field_0=any&op_0=AND&term_0=Book'
            '&field_1=title&op_1=NOT&term_1=Flask'
        )
        assert b'Flask Web Development' not in r.data

    def test_adv_search_empty_rows_ignored(self, client, populated_db):
        # Submitting with no terms should return no results
        r = client.get('/search?adv=1&field_0=title&op_0=AND&term_0=')
        assert b'Python Programming' not in r.data


class TestSearchVisibilityIntegration:
    """Confirm restricted-filter is applied inside search results."""

    @pytest.fixture()
    def mixed_entries(self, app, library_id):
        ids = {}
        for level in ('unrestricted', 'restricted', 'hidden'):
            eid = insert_entry(app, title=f'{level.title()} Item', restricted=level)
            insert_volume(app, eid, library_id=library_id)
            ids[level] = eid
        return ids

    def test_unauthenticated_sees_only_unrestricted(self, client, mixed_entries):
        r = client.get('/search?q=Item')
        assert b'Unrestricted Item' in r.data
        assert b'Restricted Item' not in r.data
        assert b'Hidden Item' not in r.data

    def test_regular_user_sees_restricted_not_hidden(self, client, mixed_entries, regular_id):
        login_as(client, '0003', 'regpass')
        r = client.get('/search?q=Item')
        assert b'Restricted Item' in r.data
        assert b'Hidden Item' not in r.data

    def test_cataloguer_sees_all(self, client, mixed_entries, cataloguer_id):
        login_as(client, '0002', 'catpass')
        r = client.get('/search?q=Item')
        assert b'Hidden Item' in r.data
