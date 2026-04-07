"""Tests for the contributor (entry_authors) system."""
import pytest
import app as app_module
from conftest import insert_entry, insert_contributor, login_as


@pytest.fixture(autouse=True)
def logged_in_as_cataloguer(client, cataloguer_id):
    login_as(client, '0002', 'catpass')


@pytest.fixture()
def entry_id(app):
    return insert_entry(app, title='Test Book')


def _add(client, entry_id, name, role='author'):
    return client.post(
        f'/entry/{entry_id}/contributor/add',
        data={'name': name, 'role': role},
        follow_redirects=True,
    )


def _get_contributors(app, entry_id):
    with app.app_context():
        db = app_module.get_db()
        return db.execute(
            'SELECT * FROM entry_authors WHERE entry_id = ? ORDER BY sort_order, id',
            (entry_id,)
        ).fetchall()


def _get_author_field(app, entry_id):
    with app.app_context():
        db = app_module.get_db()
        return db.execute(
            'SELECT author FROM entries WHERE id = ?', (entry_id,)
        ).fetchone()['author']


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class TestContributorAccess:
    def test_unauthenticated_add_gets_403(self, client, entry_id):
        client.post('/logout')
        r = client.post(f'/entry/{entry_id}/contributor/add',
                        data={'name': 'X', 'role': 'author'})
        assert r.status_code == 403

    def test_regular_user_add_gets_403(self, client, entry_id, regular_id):
        client.post('/logout')
        login_as(client, '0003', 'regpass')
        r = client.post(f'/entry/{entry_id}/contributor/add',
                        data={'name': 'X', 'role': 'author'})
        assert r.status_code == 403

    def test_unauthenticated_delete_gets_403(self, client, app, entry_id):
        cid = insert_contributor(app, entry_id, 'Smith, John')
        client.post('/logout')
        r = client.post(f'/contributor/{cid}/delete')
        assert r.status_code == 403

    def test_unauthenticated_edit_gets_403(self, client, app, entry_id):
        cid = insert_contributor(app, entry_id, 'Smith, John')
        client.post('/logout')
        r = client.post(f'/contributor/{cid}/edit',
                        data={'name': 'Other', 'role': 'editor'})
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Adding contributors
# ---------------------------------------------------------------------------

class TestAddContributor:
    def test_add_inserts_row(self, client, app, entry_id):
        _add(client, entry_id, 'Smith, John')
        contribs = _get_contributors(app, entry_id)
        assert len(contribs) == 1
        assert contribs[0]['name'] == 'Smith, John'
        assert contribs[0]['role'] == 'author'

    def test_add_with_role(self, client, app, entry_id):
        _add(client, entry_id, 'Jones, Alice', role='editor')
        contribs = _get_contributors(app, entry_id)
        assert contribs[0]['role'] == 'editor'

    def test_all_valid_roles_accepted(self, client, app, entry_id):
        for role in ('author', 'editor', 'translator', 'compiler', 'illustrator', 'contributor'):
            _add(client, entry_id, f'Name {role}', role=role)
        contribs = _get_contributors(app, entry_id)
        roles = [c['role'] for c in contribs]
        for role in ('author', 'editor', 'translator', 'compiler', 'illustrator', 'contributor'):
            assert role in roles

    def test_invalid_role_defaults_to_author(self, client, app, entry_id):
        _add(client, entry_id, 'Jones, Bob', role='wizard')
        contribs = _get_contributors(app, entry_id)
        assert contribs[0]['role'] == 'author'

    def test_empty_name_rejected(self, client, app, entry_id):
        _add(client, entry_id, '')
        assert len(_get_contributors(app, entry_id)) == 0

    def test_multiple_contributors_added(self, client, app, entry_id):
        _add(client, entry_id, 'Smith, John')
        _add(client, entry_id, 'Jones, Alice', role='editor')
        contribs = _get_contributors(app, entry_id)
        assert len(contribs) == 2

    def test_sort_order_increments(self, client, app, entry_id):
        _add(client, entry_id, 'First')
        _add(client, entry_id, 'Second')
        contribs = _get_contributors(app, entry_id)
        assert contribs[0]['sort_order'] < contribs[1]['sort_order']

    def test_nonexistent_entry_redirects(self, client):
        r = client.post('/entry/9999/contributor/add',
                        data={'name': 'X', 'role': 'author'},
                        follow_redirects=True)
        assert r.status_code == 200

    def test_add_shown_on_entry_detail(self, client, entry_id):
        _add(client, entry_id, 'Smith, John')
        r = client.get(f'/entry/{entry_id}')
        assert b'Smith, John' in r.data

    def test_role_label_shown_on_entry_detail(self, client, entry_id):
        _add(client, entry_id, 'Jones, Alice', role='editor')
        r = client.get(f'/entry/{entry_id}')
        assert b'Editor' in r.data


# ---------------------------------------------------------------------------
# Deleting contributors
# ---------------------------------------------------------------------------

class TestDeleteContributor:
    def test_delete_removes_row(self, client, app, entry_id):
        cid = insert_contributor(app, entry_id, 'Smith, John')
        client.post(f'/contributor/{cid}/delete', follow_redirects=True)
        assert len(_get_contributors(app, entry_id)) == 0

    def test_delete_nonexistent_is_safe(self, client):
        r = client.post('/contributor/9999/delete', follow_redirects=True)
        assert r.status_code == 200

    def test_delete_only_removes_target(self, client, app, entry_id):
        cid1 = insert_contributor(app, entry_id, 'Smith, John')
        cid2 = insert_contributor(app, entry_id, 'Jones, Alice', role='editor')
        client.post(f'/contributor/{cid1}/delete')
        contribs = _get_contributors(app, entry_id)
        assert len(contribs) == 1
        assert contribs[0]['id'] == cid2


# ---------------------------------------------------------------------------
# Editing contributors
# ---------------------------------------------------------------------------

class TestEditContributor:
    def test_edit_updates_name(self, client, app, entry_id):
        cid = insert_contributor(app, entry_id, 'Old Name')
        client.post(f'/contributor/{cid}/edit',
                    data={'name': 'New Name', 'role': 'author'})
        contribs = _get_contributors(app, entry_id)
        assert contribs[0]['name'] == 'New Name'

    def test_edit_updates_role(self, client, app, entry_id):
        cid = insert_contributor(app, entry_id, 'Smith, John', role='author')
        client.post(f'/contributor/{cid}/edit',
                    data={'name': 'Smith, John', 'role': 'translator'})
        contribs = _get_contributors(app, entry_id)
        assert contribs[0]['role'] == 'translator'

    def test_edit_invalid_role_keeps_original(self, client, app, entry_id):
        cid = insert_contributor(app, entry_id, 'Smith, John', role='author')
        client.post(f'/contributor/{cid}/edit',
                    data={'name': 'Smith, John', 'role': 'wizard'})
        contribs = _get_contributors(app, entry_id)
        assert contribs[0]['role'] == 'author'

    def test_edit_nonexistent_is_safe(self, client):
        r = client.post('/contributor/9999/edit',
                        data={'name': 'X', 'role': 'author'},
                        follow_redirects=True)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Reordering contributors
# ---------------------------------------------------------------------------

class TestReorderContributors:
    def test_reorder_changes_sort_order(self, client, app, entry_id):
        cid1 = insert_contributor(app, entry_id, 'First')
        cid2 = insert_contributor(app, entry_id, 'Second')
        # Post reversed order
        client.post(f'/entry/{entry_id}/contributor/reorder',
                    data={'order[]': [str(cid2), str(cid1)]},
                    follow_redirects=True)
        contribs = _get_contributors(app, entry_id)
        by_id = {c['id']: c for c in contribs}
        assert by_id[cid2]['sort_order'] < by_id[cid1]['sort_order']

    def test_reorder_ignores_non_numeric_ids(self, client, app, entry_id):
        insert_contributor(app, entry_id, 'Only')
        r = client.post(f'/entry/{entry_id}/contributor/reorder',
                        data={'order[]': ['abc', 'xyz']},
                        follow_redirects=True)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Denormalized entries.author cache (_sync_author_field)
# ---------------------------------------------------------------------------

class TestAuthorFieldSync:
    def test_add_sets_author_field(self, client, app, entry_id):
        _add(client, entry_id, 'Smith, John')
        assert _get_author_field(app, entry_id) == 'Smith, John'

    def test_author_field_reflects_first_contributor(self, client, app, entry_id):
        _add(client, entry_id, 'Smith, John')
        _add(client, entry_id, 'Jones, Alice')
        assert _get_author_field(app, entry_id) == 'Smith, John'

    def test_delete_updates_author_field_to_next(self, client, app, entry_id):
        cid1 = insert_contributor(app, entry_id, 'Smith, John')
        insert_contributor(app, entry_id, 'Jones, Alice')
        client.post(f'/contributor/{cid1}/delete')
        assert _get_author_field(app, entry_id) == 'Jones, Alice'

    def test_delete_last_clears_author_field(self, client, app, entry_id):
        cid = insert_contributor(app, entry_id, 'Smith, John')
        client.post(f'/contributor/{cid}/delete')
        assert _get_author_field(app, entry_id) is None

    def test_reorder_updates_author_field(self, client, app, entry_id):
        cid1 = insert_contributor(app, entry_id, 'First Author')
        cid2 = insert_contributor(app, entry_id, 'Second Author')
        client.post(f'/entry/{entry_id}/contributor/reorder',
                    data={'order[]': [str(cid2), str(cid1)]})
        assert _get_author_field(app, entry_id) == 'Second Author'

    def test_edit_name_updates_author_field(self, client, app, entry_id):
        cid = insert_contributor(app, entry_id, 'Old Name')
        client.post(f'/contributor/{cid}/edit',
                    data={'name': 'New Name', 'role': 'author'})
        assert _get_author_field(app, entry_id) == 'New Name'


# ---------------------------------------------------------------------------
# Migration: existing entries.author → entry_authors
# ---------------------------------------------------------------------------

class TestAuthorMigration:
    def test_existing_author_migrated(self, app):
        """An entry with author set before migration appears in entry_authors."""
        with app.app_context():
            db = app_module.get_db()
            # Insert directly with the old author field set
            cur = db.execute(
                "INSERT INTO entries (type, title, author) VALUES ('book', 'Legacy Book', 'Legacy, Author')"
            )
            entry_id = cur.lastrowid
            # Simulate first-run migration (clear entry_authors so it re-runs)
            db.execute('DELETE FROM entry_authors WHERE entry_id = ?', (entry_id,))
            db.commit()
            # Call the migration function directly
            app_module._migrate_authors_to_table(db)
            db.commit()
            contribs = db.execute(
                'SELECT * FROM entry_authors WHERE entry_id = ?', (entry_id,)
            ).fetchall()
            assert len(contribs) == 1
            assert contribs[0]['name'] == 'Legacy, Author'
            assert contribs[0]['role'] == 'author'

    def test_migration_is_idempotent(self, app):
        """Running migration twice does not duplicate rows."""
        with app.app_context():
            db = app_module.get_db()
            cur = db.execute(
                "INSERT INTO entries (type, title, author) VALUES ('book', 'Idempotent Book', 'Test, Person')"
            )
            entry_id = cur.lastrowid
            db.execute('DELETE FROM entry_authors WHERE entry_id = ?', (entry_id,))
            db.commit()
            app_module._migrate_authors_to_table(db)
            db.commit()
            # Run again — should not add more rows because table is now non-empty globally
            app_module._migrate_authors_to_table(db)
            db.commit()
            count = db.execute(
                'SELECT COUNT(*) FROM entry_authors WHERE entry_id = ?', (entry_id,)
            ).fetchone()[0]
            assert count == 1


# ---------------------------------------------------------------------------
# Citation integration
# ---------------------------------------------------------------------------

class TestCitationWithContributors:
    def test_author_appears_in_citation(self, client, app, entry_id):
        insert_contributor(app, entry_id, 'Smith, John')
        r = client.get(f'/entry/{entry_id}')
        assert b'Smith, John' in r.data

    def test_editor_gets_ed_suffix_in_citation(self, client, app, entry_id):
        insert_contributor(app, entry_id, 'Jones, Alice', role='editor')
        r = client.get(f'/entry/{entry_id}')
        # eds. or ed. should appear somewhere in the citation
        assert b'ed.' in r.data

    def test_no_contributor_no_citation_author(self, client, entry_id):
        r = client.get(f'/entry/{entry_id}')
        # No contributor means only the title should appear in citation
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Search integration
# ---------------------------------------------------------------------------

class TestAuthorSearch:
    def test_search_finds_by_contributor_name(self, client, app):
        eid = insert_entry(app, title='Search Target')
        from conftest import insert_library, insert_volume
        lib_id = insert_library(app, 'Test Library')
        insert_volume(app, eid, library_id=lib_id)
        insert_contributor(app, eid, 'Findable, Person')
        r = client.get('/search?q=Findable')
        assert b'Search Target' in r.data

    def test_search_does_not_find_wrong_author(self, client, app):
        eid = insert_entry(app, title='Other Book')
        from conftest import insert_library, insert_volume
        lib_id = insert_library(app, 'Test Library')
        insert_volume(app, eid, library_id=lib_id)
        insert_contributor(app, eid, 'Smith, Jane')
        r = client.get('/search?q=NoSuchAuthorXYZ')
        assert b'Other Book' not in r.data

    def test_adv_search_author_field(self, client, app):
        eid = insert_entry(app, title='Advanced Target')
        from conftest import insert_library, insert_volume
        lib_id = insert_library(app, 'Test Library')
        insert_volume(app, eid, library_id=lib_id)
        insert_contributor(app, eid, 'Specific, Writer')
        r = client.get('/search?adv=1&field_0=author&op_0=AND&term_0=Specific')
        assert b'Advanced Target' in r.data
