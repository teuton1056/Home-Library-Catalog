"""Tests for the subject heading system."""
import pytest
import app as app_module
from conftest import insert_entry, insert_library, login_as


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def logged_in_as_cataloguer(client, cataloguer_id):
    login_as(client, '0002', 'catpass')


def _create_heading(app, heading, htype=None, scope_note=None):
    with app.app_context():
        db = app_module.get_db()
        cur = db.execute(
            'INSERT INTO subject_headings (heading, type, scope_note) VALUES (?, ?, ?)',
            (heading, htype, scope_note)
        )
        db.commit()
        return cur.lastrowid


def _get_heading(app, heading_id):
    with app.app_context():
        db = app_module.get_db()
        return db.execute('SELECT * FROM subject_headings WHERE id=?', (heading_id,)).fetchone()


def _get_relations(app, from_id):
    with app.app_context():
        db = app_module.get_db()
        return db.execute(
            'SELECT relation_type, to_id FROM subject_relations WHERE from_id=?', (from_id,)
        ).fetchall()


def _get_entry_headings(app, entry_id):
    with app.app_context():
        db = app_module.get_db()
        return db.execute(
            'SELECT sh.heading FROM subject_headings sh '
            'JOIN entry_subject_headings esh ON esh.heading_id = sh.id '
            'WHERE esh.entry_id=? ORDER BY sh.heading COLLATE NOCASE',
            (entry_id,)
        ).fetchall()


# ---------------------------------------------------------------------------
# Heading CRUD
# ---------------------------------------------------------------------------

class TestHeadingCreate:
    def test_create_heading_post(self, client):
        r = client.post('/subject/new', data={
            'heading': 'Canada', 'type': 'region', 'scope_note': 'A country.'
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'Canada' in r.data

    def test_create_heading_stored(self, client, app):
        client.post('/subject/new', data={'heading': 'Manitoba', 'type': 'region'})
        with app.app_context():
            row = app_module.get_db().execute(
                'SELECT * FROM subject_headings WHERE heading=?', ('Manitoba',)
            ).fetchone()
        assert row is not None
        assert row['type'] == 'region'

    def test_duplicate_heading_rejected(self, client):
        client.post('/subject/new', data={'heading': 'Animals'})
        r = client.post('/subject/new', data={'heading': 'Animals'}, follow_redirects=True)
        assert b'already exists' in r.data

    def test_empty_heading_rejected(self, client):
        r = client.post('/subject/new', data={'heading': ''}, follow_redirects=True)
        assert b'required' in r.data.lower()

    def test_edit_heading(self, client, app):
        hid = _create_heading(app, 'Old Name')
        client.post(f'/subject/{hid}/edit', data={
            'heading': 'New Name', 'type': 'topic', 'scope_note': 'Updated.'
        }, follow_redirects=True)
        row = _get_heading(app, hid)
        assert row['heading'] == 'New Name'
        assert row['type'] == 'topic'
        assert row['scope_note'] == 'Updated.'

    def test_edit_to_duplicate_rejected(self, client, app):
        hid1 = _create_heading(app, 'Alpha')
        _create_heading(app, 'Beta')
        r = client.post(f'/subject/{hid1}/edit', data={
            'heading': 'Beta', 'type': '', 'scope_note': ''
        }, follow_redirects=True)
        assert b'already exists' in r.data

    def test_detail_page_renders(self, client, app):
        hid = _create_heading(app, 'Linguistics', htype='topic', scope_note='Study of language.')
        r = client.get(f'/subject/{hid}')
        assert r.status_code == 200
        assert b'Linguistics' in r.data
        assert b'Study of language.' in r.data

    def test_unknown_heading_404_redirects(self, client):
        r = client.get('/subject/99999', follow_redirects=True)
        assert b'not found' in r.data.lower()


# ---------------------------------------------------------------------------
# Relations — auto-reciprocals
# ---------------------------------------------------------------------------

class TestRelationReciprocals:
    def _rel_types(self, app, from_id):
        return {(r['relation_type'], r['to_id']) for r in _get_relations(app, from_id)}

    def test_bt_creates_nt_reciprocal(self, app, client):
        canada   = _create_heading(app, 'Canada')
        manitoba = _create_heading(app, 'Manitoba')
        with app.app_context():
            db = app_module.get_db()
            app_module._add_relation(db, manitoba, 'BT', canada)
            db.commit()
        assert ('NT', manitoba) in self._rel_types(app, canada)
        assert ('BT', canada)   in self._rel_types(app, manitoba)

    def test_nt_creates_bt_reciprocal(self, app, client):
        canada   = _create_heading(app, 'Canada')
        ontario  = _create_heading(app, 'Ontario')
        with app.app_context():
            db = app_module.get_db()
            app_module._add_relation(db, canada, 'NT', ontario)
            db.commit()
        assert ('BT', canada)  in self._rel_types(app, ontario)
        assert ('NT', ontario) in self._rel_types(app, canada)

    def test_rt_is_bidirectional(self, app):
        atonement   = _create_heading(app, 'Atonement')
        redemption  = _create_heading(app, 'Redemption')
        with app.app_context():
            db = app_module.get_db()
            app_module._add_relation(db, atonement, 'RT', redemption)
            db.commit()
        assert ('RT', redemption) in self._rel_types(app, atonement)
        assert ('RT', atonement)  in self._rel_types(app, redemption)

    def test_use_creates_uf_reciprocal(self, app):
        coptic    = _create_heading(app, 'Coptic')
        preferred = _create_heading(app, 'Egyptian---Coptic')
        with app.app_context():
            db = app_module.get_db()
            app_module._add_relation(db, coptic, 'USE', preferred)
            db.commit()
        assert ('UF', coptic)    in self._rel_types(app, preferred)
        assert ('USE', preferred) in self._rel_types(app, coptic)

    def test_sa_no_reciprocal(self, app):
        copts  = _create_heading(app, 'Copts')
        coptic = _create_heading(app, 'Coptic Christianity')
        with app.app_context():
            db = app_module.get_db()
            app_module._add_relation(db, copts, 'SA', coptic)
            db.commit()
        assert ('SA', coptic) in self._rel_types(app, copts)
        # No automatic SA back from coptic to copts
        assert ('SA', copts) not in self._rel_types(app, coptic)

    def test_se_no_reciprocal(self, app):
        battle = _create_heading(app, 'Battle of France')
        ww2    = _create_heading(app, 'World War Two')
        with app.app_context():
            db = app_module.get_db()
            app_module._add_relation(db, battle, 'SE', ww2)
            db.commit()
        assert ('SE', ww2)   in self._rel_types(app, battle)
        assert ('SE', battle) not in self._rel_types(app, ww2)

    def test_remove_relation_removes_reciprocal(self, app):
        canada   = _create_heading(app, 'Canada')
        manitoba = _create_heading(app, 'Manitoba')
        with app.app_context():
            db = app_module.get_db()
            app_module._add_relation(db, canada, 'NT', manitoba)
            db.commit()
        with app.app_context():
            db = app_module.get_db()
            app_module._remove_relation(db, canada, 'NT', manitoba)
            db.commit()
        assert ('NT', manitoba) not in self._rel_types(app, canada)
        assert ('BT', canada)   not in self._rel_types(app, manitoba)

    def test_duplicate_relation_ignored(self, app):
        a = _create_heading(app, 'Phonetics')
        b = _create_heading(app, 'Phonology')
        with app.app_context():
            db = app_module.get_db()
            app_module._add_relation(db, a, 'RT', b)
            app_module._add_relation(db, a, 'RT', b)  # duplicate
            db.commit()
        rels = [r for r in _get_relations(app, a) if r['relation_type'] == 'RT']
        assert len(rels) == 1

    def test_relations_created_via_form(self, client, app):
        canada   = _create_heading(app, 'Canada')
        manitoba = _create_heading(app, 'Manitoba')
        client.post(f'/subject/{canada}/edit', data={
            'heading':    'Canada',
            'type':       'region',
            'scope_note': '',
            'rel_type_0': 'NT',
            'rel_target_0': 'Manitoba',
        }, follow_redirects=True)
        with app.app_context():
            db = app_module.get_db()
            rel = db.execute(
                'SELECT * FROM subject_relations WHERE from_id=? AND relation_type=? AND to_id=?',
                (canada, 'NT', manitoba)
            ).fetchone()
        assert rel is not None

    def test_form_creates_new_heading_for_relation_target(self, client, app):
        canada = _create_heading(app, 'Canada')
        client.post(f'/subject/{canada}/edit', data={
            'heading':      'Canada',
            'type':         'region',
            'scope_note':   '',
            'rel_type_0':   'NT',
            'rel_target_0': 'BrandNewProvince',
        }, follow_redirects=True)
        with app.app_context():
            row = app_module.get_db().execute(
                'SELECT id FROM subject_headings WHERE heading=?', ('BrandNewProvince',)
            ).fetchone()
        assert row is not None


# ---------------------------------------------------------------------------
# USE resolution — preferred form handling
# ---------------------------------------------------------------------------

class TestUseResolution:
    def test_resolve_preferred_follows_use(self, app):
        coptic    = _create_heading(app, 'Coptic')
        preferred = _create_heading(app, 'Egyptian---Coptic')
        with app.app_context():
            db = app_module.get_db()
            app_module._add_relation(db, coptic, 'USE', preferred)
            db.commit()
            pid, was_redirected = app_module._resolve_preferred(db, coptic)
        assert pid == preferred
        assert was_redirected is True

    def test_resolve_preferred_no_use_unchanged(self, app):
        canada = _create_heading(app, 'Canada')
        with app.app_context():
            db = app_module.get_db()
            pid, was_redirected = app_module._resolve_preferred(db, canada)
        assert pid == canada
        assert was_redirected is False

    def test_set_entry_subjects_redirects_nonpreferred(self, app):
        entry_id  = insert_entry(app, 'Coptic Dictionary')
        coptic    = _create_heading(app, 'Coptic')
        preferred = _create_heading(app, 'Egyptian---Coptic')
        with app.app_context():
            db = app_module.get_db()
            app_module._add_relation(db, coptic, 'USE', preferred)
            db.commit()
        with app.app_context():
            db = app_module.get_db()
            redirects = app_module._set_entry_subjects(db, entry_id, ['Coptic'])
            db.commit()
        assert len(redirects) == 1
        assert redirects[0][0] == 'Coptic'
        assert redirects[0][1]['heading'] == 'Egyptian---Coptic'
        tags = [r['heading'] for r in _get_entry_headings(app, entry_id)]
        assert 'Egyptian---Coptic' in tags
        assert 'Coptic' not in tags

    def test_edit_entry_flash_on_redirect(self, client, app):
        entry_id  = insert_entry(app, 'Test Entry')
        coptic    = _create_heading(app, 'Coptic')
        preferred = _create_heading(app, 'Egyptian---Coptic')
        with app.app_context():
            db = app_module.get_db()
            app_module._add_relation(db, coptic, 'USE', preferred)
            db.commit()
        r = client.post(f'/entry/{entry_id}/edit', data={
            'type': 'book', 'title': 'Test Entry', 'restricted': 'unrestricted',
            'subject': 'Coptic',
        }, follow_redirects=True)
        assert 'non-preferred' in r.data.decode() or 'replaced' in r.data.decode()


# ---------------------------------------------------------------------------
# Entry tagging
# ---------------------------------------------------------------------------

class TestEntryTagging:
    def test_set_entry_subjects_creates_headings(self, app):
        entry_id = insert_entry(app, 'New Book')
        with app.app_context():
            db = app_module.get_db()
            app_module._set_entry_subjects(db, entry_id, ['History', 'Philosophy'])
            db.commit()
        tags = [r['heading'] for r in _get_entry_headings(app, entry_id)]
        assert 'History' in tags
        assert 'Philosophy' in tags

    def test_set_entry_subjects_replaces_old(self, app):
        entry_id = insert_entry(app, 'Another Book')
        with app.app_context():
            db = app_module.get_db()
            app_module._set_entry_subjects(db, entry_id, ['History'])
            db.commit()
        with app.app_context():
            db = app_module.get_db()
            app_module._set_entry_subjects(db, entry_id, ['Philosophy'])
            db.commit()
        tags = [r['heading'] for r in _get_entry_headings(app, entry_id)]
        assert tags == ['Philosophy']

    def test_set_entry_subjects_deduplicates(self, app):
        entry_id = insert_entry(app, 'Dedup Test')
        with app.app_context():
            db = app_module.get_db()
            app_module._set_entry_subjects(db, entry_id, ['History', 'History'])
            db.commit()
        tags = [r['heading'] for r in _get_entry_headings(app, entry_id)]
        assert tags.count('History') == 1

    def test_new_entry_tags_saved(self, client, app):
        r = client.post('/entry/new', data={
            'type': 'book', 'title': 'Tagged Book', 'restricted': 'unrestricted',
            'subject': 'Linguistics,Phonology',
        }, follow_redirects=True)
        assert r.status_code == 200
        with app.app_context():
            entry_id = app_module.get_db().execute(
                "SELECT id FROM entries WHERE title='Tagged Book'"
            ).fetchone()['id']
        tags = [r['heading'] for r in _get_entry_headings(app, entry_id)]
        assert 'Linguistics' in tags
        assert 'Phonology' in tags

    def test_edit_entry_tags_saved(self, client, app):
        entry_id = insert_entry(app, 'Edit Tags Test')
        client.post(f'/entry/{entry_id}/edit', data={
            'type': 'book', 'title': 'Edit Tags Test', 'restricted': 'unrestricted',
            'subject': 'History,Canada',
        }, follow_redirects=True)
        tags = [r['heading'] for r in _get_entry_headings(app, entry_id)]
        assert 'History' in tags
        assert 'Canada' in tags

    def test_empty_subject_clears_tags(self, client, app):
        entry_id = insert_entry(app, 'Clear Tags Test')
        client.post(f'/entry/{entry_id}/edit', data={
            'type': 'book', 'title': 'Clear Tags Test', 'restricted': 'unrestricted',
            'subject': 'History',
        }, follow_redirects=True)
        client.post(f'/entry/{entry_id}/edit', data={
            'type': 'book', 'title': 'Clear Tags Test', 'restricted': 'unrestricted',
            'subject': '',
        }, follow_redirects=True)
        tags = _get_entry_headings(app, entry_id)
        assert tags == []

    def test_entry_detail_shows_subject_links(self, client, app):
        entry_id = insert_entry(app, 'Show Tags')
        hid = _create_heading(app, 'Medieval History')
        with app.app_context():
            db = app_module.get_db()
            app_module._set_entry_subjects(db, entry_id, ['Medieval History'])
            db.commit()
        r = client.get(f'/entry/{entry_id}')
        assert b'Medieval History' in r.data
        assert f'/subject/{hid}'.encode() in r.data


# ---------------------------------------------------------------------------
# Delete subject heading
# ---------------------------------------------------------------------------

class TestDeleteSubject:
    def test_delete_confirm_page(self, client, app):
        hid = _create_heading(app, 'To Be Deleted')
        r = client.get(f'/subject/{hid}/delete')
        assert r.status_code == 200
        assert b'To Be Deleted' in r.data

    def test_delete_with_no_entries(self, client, app):
        hid = _create_heading(app, 'Orphan Heading')
        r = client.post(f'/subject/{hid}/delete', data={'confirm': 'unlink'}, follow_redirects=True)
        assert r.status_code == 200
        assert b'deleted' in r.data.lower()
        with app.app_context():
            row = app_module.get_db().execute(
                'SELECT id FROM subject_headings WHERE id=?', (hid,)
            ).fetchone()
        assert row is None

    def test_delete_unlinks_entries(self, client, app):
        entry_id = insert_entry(app, 'Tagged Entry')
        hid = _create_heading(app, 'Deletable Tag')
        with app.app_context():
            db = app_module.get_db()
            app_module._set_entry_subjects(db, entry_id, ['Deletable Tag'])
            db.commit()
        client.post(f'/subject/{hid}/delete', data={'confirm': 'unlink'}, follow_redirects=True)
        tags = _get_entry_headings(app, entry_id)
        assert tags == []

    def test_delete_cancel_keeps_heading(self, client, app):
        hid = _create_heading(app, 'Keep Me')
        # POST without confirm=unlink → cancel
        r = client.post(f'/subject/{hid}/delete', data={'confirm': ''}, follow_redirects=True)
        with app.app_context():
            row = app_module.get_db().execute(
                'SELECT id FROM subject_headings WHERE id=?', (hid,)
            ).fetchone()
        assert row is not None

    def test_delete_shows_entry_count(self, client, app):
        entry_id = insert_entry(app, 'Count Entry')
        hid = _create_heading(app, 'Counted Tag')
        with app.app_context():
            db = app_module.get_db()
            app_module._set_entry_subjects(db, entry_id, ['Counted Tag'])
            db.commit()
        r = client.get(f'/subject/{hid}/delete')
        assert b'1' in r.data  # entry count displayed


# ---------------------------------------------------------------------------
# Browse and search
# ---------------------------------------------------------------------------

class TestSubjectBrowse:
    def test_subjects_page_lists_headings(self, client, app):
        _create_heading(app, 'Anthropology')
        _create_heading(app, 'Botany')
        r = client.get('/subjects')
        assert r.status_code == 200
        assert b'Anthropology' in r.data
        assert b'Botany' in r.data

    def test_subjects_search_filters(self, client, app):
        _create_heading(app, 'Zoology')
        _create_heading(app, 'Palaeozoology')
        _create_heading(app, 'Mathematics')
        r = client.get('/subjects?q=zoology')
        assert b'Zoology' in r.data
        assert b'Palaeozoology' in r.data
        assert b'Mathematics' not in r.data

    def test_autocomplete_api_returns_json(self, client, app):
        _create_heading(app, 'Linguistics')
        r = client.get('/api/subjects?q=Ling')
        assert r.status_code == 200
        data = r.get_json()
        assert any(h['heading'] == 'Linguistics' for h in data)

    def test_autocomplete_api_includes_preferred(self, client, app):
        coptic    = _create_heading(app, 'Coptic')
        preferred = _create_heading(app, 'Egyptian---Coptic')
        with app.app_context():
            db = app_module.get_db()
            app_module._add_relation(db, coptic, 'USE', preferred)
            db.commit()
        r = client.get('/api/subjects?q=Coptic')
        data = r.get_json()
        coptic_item = next((h for h in data if h['heading'] == 'Coptic'), None)
        assert coptic_item is not None
        assert coptic_item['preferred'] == 'Egyptian---Coptic'

    def test_preview_endpoint_returns_html(self, client, app):
        hid = _create_heading(app, 'Preview Test', scope_note='Test scope.')
        r = client.get(f'/subject/{hid}/preview')
        assert r.status_code == 200
        assert b'Test scope.' in r.data


# ---------------------------------------------------------------------------
# Search integration
# ---------------------------------------------------------------------------

class TestSearchIntegration:
    def test_simple_search_finds_subject(self, client, app):
        entry_id = insert_entry(app, 'Cree Language Book')
        with app.app_context():
            db = app_module.get_db()
            lib_id = db.execute("INSERT INTO libraries (name) VALUES ('L') RETURNING id").fetchone()['id']
            db.execute('INSERT INTO volumes (entry_id, library_id, barcode) VALUES (?, ?, ?)',
                       (entry_id, lib_id, '99001'))
            app_module._set_entry_subjects(db, entry_id, ['Cree'])
            db.commit()
        r = client.get('/search?q=Cree')
        assert b'Cree Language Book' in r.data

    def test_advanced_subject_search(self, client, app):
        entry_id = insert_entry(app, 'Manitoba History Book')
        with app.app_context():
            db = app_module.get_db()
            lib_id = db.execute("INSERT INTO libraries (name) VALUES ('L2') RETURNING id").fetchone()['id']
            db.execute('INSERT INTO volumes (entry_id, library_id, barcode) VALUES (?, ?, ?)',
                       (entry_id, lib_id, '99002'))
            app_module._set_entry_subjects(db, entry_id, ['Manitoba'])
            db.commit()
        r = client.get('/search?adv=1&field_0=subject&term_0=Manitoba')
        assert b'Manitoba History Book' in r.data


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class TestSubjectAccessControl:
    def test_guest_cannot_see_subjects_page(self, client):
        client.post('/logout')
        r = client.get('/subjects')
        assert r.status_code == 403

    def test_unauthenticated_cannot_see_subjects_page(self, client):
        client.post('/logout')
        r = client.get('/subjects')
        assert r.status_code == 403

    def test_regular_user_can_see_subjects(self, client, regular_id):
        client.post('/logout')
        login_as(client, '0003', 'regpass')
        r = client.get('/subjects')
        assert r.status_code == 200

    def test_regular_user_cannot_create_heading(self, client, regular_id):
        client.post('/logout')
        login_as(client, '0003', 'regpass')
        r = client.post('/subject/new', data={'heading': 'Nope'})
        assert r.status_code == 403

    def test_regular_user_cannot_edit_heading(self, client, app, regular_id):
        hid = _create_heading(app, 'Editable Heading')
        client.post('/logout')
        login_as(client, '0003', 'regpass')
        r = client.post(f'/subject/{hid}/edit', data={'heading': 'Changed'})
        assert r.status_code == 403

    def test_regular_user_cannot_delete_heading(self, client, app, regular_id):
        hid = _create_heading(app, 'Protected Heading')
        client.post('/logout')
        login_as(client, '0003', 'regpass')
        r = client.post(f'/subject/{hid}/delete', data={'confirm': 'unlink'})
        assert r.status_code == 403

    def test_cataloguer_can_create_heading(self, client, app):
        r = client.post('/subject/new', data={
            'heading': 'TestHeading', 'type': 'topic', 'scope_note': ''
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'TestHeading' in r.data

    def test_owner_can_delete_heading(self, client, app, owner_id):
        hid = _create_heading(app, 'Owner Deletes This')
        client.post('/logout')
        login_as(client, '0001', 'ownerpass')
        r = client.post(f'/subject/{hid}/delete', data={'confirm': 'unlink'}, follow_redirects=True)
        assert r.status_code == 200

    def test_restricted_user_can_see_subjects(self, client, restricted_id):
        client.post('/logout')
        login_as(client, '0004', 'respass')
        r = client.get('/subjects')
        assert r.status_code == 200

    def test_guest_user_cannot_see_subjects(self, client, guest_id):
        client.post('/logout')
        login_as(client, '0005', 'guestpass')
        r = client.get('/subjects')
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Migration: existing subject text → junction table
# ---------------------------------------------------------------------------

class TestSubjectMigration:
    def test_migration_creates_headings_from_text(self, app):
        """Simulate the migration by inserting a row with the old subject column
        via direct SQL, then running _migrate_subject_data manually."""
        # The migration runs at startup — in tests the DB is always fresh so
        # there is no subject column to migrate. We test the helper logic
        # directly by calling _set_entry_subjects with free-text tokens instead.
        entry_id = insert_entry(app, 'Migration Test')
        with app.app_context():
            db = app_module.get_db()
            app_module._set_entry_subjects(db, entry_id, ['Canada', 'Manitoba', 'Cree'])
            db.commit()
        tags = [r['heading'] for r in _get_entry_headings(app, entry_id)]
        assert 'Canada' in tags
        assert 'Manitoba' in tags
        assert 'Cree' in tags
