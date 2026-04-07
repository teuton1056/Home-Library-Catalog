"""Tests for volume physical description fields."""
import pytest
import app as app_module
from conftest import insert_entry, insert_library, insert_volume, login_as


@pytest.fixture(autouse=True)
def logged_in_as_cataloguer(client, cataloguer_id):
    login_as(client, '0002', 'catpass')


@pytest.fixture()
def library_id(app):
    return insert_library(app, 'Test Library')


@pytest.fixture()
def entry_id(app):
    return insert_entry(app, title='Physical Test Book')


@pytest.fixture()
def volume_id(app, entry_id, library_id):
    return insert_volume(app, entry_id, library_id=library_id, barcode='00050')


def _get_volume(app, volume_id):
    with app.app_context():
        db = app_module.get_db()
        return db.execute('SELECT * FROM volumes WHERE id = ?', (volume_id,)).fetchone()


def _edit_volume(client, volume_id, **kwargs):
    """POST to edit_volume with the given physical (or other) fields."""
    data = {
        'volume_number': '',
        'library_id': '',
        'locus_call_number': '',
        'barcode': '00050',
        'date': '',
        'box_number': '',
    }
    data.update(kwargs)
    return client.post(
        f'/volume/{volume_id}/edit',
        data=data,
        follow_redirects=True,
    )


# ---------------------------------------------------------------------------
# Default state
# ---------------------------------------------------------------------------

class TestPhysicalFieldDefaults:
    def test_new_volume_has_no_phys_fields(self, app, volume_id):
        vol = _get_volume(app, volume_id)
        assert vol['phys_dimensions'] is None
        assert vol['phys_weight']     is None
        assert vol['phys_pages']      is None
        assert vol['phys_binding']    is None
        assert vol['phys_color']      is None
        assert vol['phys_material']   is None
        assert vol['phys_notes']      is None


# ---------------------------------------------------------------------------
# Saving physical fields
# ---------------------------------------------------------------------------

class TestSavePhysicalFields:
    def test_dimensions_saved(self, client, app, volume_id):
        _edit_volume(client, volume_id, phys_dimensions='24 x 16 x 3 cm')
        assert _get_volume(app, volume_id)['phys_dimensions'] == '24 x 16 x 3 cm'

    def test_weight_saved(self, client, app, volume_id):
        _edit_volume(client, volume_id, phys_weight='450 g')
        assert _get_volume(app, volume_id)['phys_weight'] == '450 g'

    def test_pages_saved(self, client, app, volume_id):
        _edit_volume(client, volume_id, phys_pages='xii + 340')
        assert _get_volume(app, volume_id)['phys_pages'] == 'xii + 340'

    def test_binding_saved(self, client, app, volume_id):
        _edit_volume(client, volume_id, phys_binding='Hardcover')
        assert _get_volume(app, volume_id)['phys_binding'] == 'Hardcover'

    def test_color_saved(self, client, app, volume_id):
        _edit_volume(client, volume_id, phys_color='Full color')
        assert _get_volume(app, volume_id)['phys_color'] == 'Full color'

    def test_material_saved(self, client, app, volume_id):
        _edit_volume(client, volume_id, phys_material='Cloth')
        assert _get_volume(app, volume_id)['phys_material'] == 'Cloth'

    def test_notes_saved(self, client, app, volume_id):
        _edit_volume(client, volume_id, phys_notes='Spine faded')
        assert _get_volume(app, volume_id)['phys_notes'] == 'Spine faded'

    def test_all_fields_saved_together(self, client, app, volume_id):
        _edit_volume(client, volume_id,
                     phys_dimensions='30 x 20 x 2 cm',
                     phys_weight='600 g',
                     phys_pages='256',
                     phys_binding='Paperback',
                     phys_color='B&W',
                     phys_material='Paper',
                     phys_notes='Some foxing on pages 10-15')
        vol = _get_volume(app, volume_id)
        assert vol['phys_dimensions'] == '30 x 20 x 2 cm'
        assert vol['phys_weight']     == '600 g'
        assert vol['phys_pages']      == '256'
        assert vol['phys_binding']    == 'Paperback'
        assert vol['phys_color']      == 'B&W'
        assert vol['phys_material']   == 'Paper'
        assert vol['phys_notes']      == 'Some foxing on pages 10-15'

    def test_partial_fields_leaves_others_null(self, client, app, volume_id):
        _edit_volume(client, volume_id, phys_binding='Hardcover', phys_pages='200')
        vol = _get_volume(app, volume_id)
        assert vol['phys_binding']    == 'Hardcover'
        assert vol['phys_pages']      == '200'
        assert vol['phys_dimensions'] is None
        assert vol['phys_weight']     is None
        assert vol['phys_color']      is None
        assert vol['phys_material']   is None
        assert vol['phys_notes']      is None

    def test_edit_returns_200(self, client, volume_id):
        r = _edit_volume(client, volume_id, phys_dimensions='20 x 15 cm')
        assert r.status_code == 200

    def test_clearing_field_sets_null(self, client, app, volume_id):
        # Set a value first, then clear it
        _edit_volume(client, volume_id, phys_binding='Hardcover')
        assert _get_volume(app, volume_id)['phys_binding'] == 'Hardcover'
        _edit_volume(client, volume_id, phys_binding='')
        assert _get_volume(app, volume_id)['phys_binding'] is None

    def test_whitespace_only_stored_as_null(self, client, app, volume_id):
        _edit_volume(client, volume_id, phys_dimensions='   ')
        assert _get_volume(app, volume_id)['phys_dimensions'] is None


# ---------------------------------------------------------------------------
# Display on entry detail page
# ---------------------------------------------------------------------------

class TestPhysicalDescriptionDisplay:
    def test_phys_data_appears_on_detail_page(self, client, app, entry_id, volume_id):
        _edit_volume(client, volume_id, phys_dimensions='24 x 16 cm', phys_binding='Hardcover')
        r = client.get(f'/entry/{entry_id}')
        assert b'24 x 16 cm' in r.data
        assert b'Hardcover'  in r.data

    def test_physical_button_present_when_data_exists(self, client, app, entry_id, volume_id):
        _edit_volume(client, volume_id, phys_dimensions='20 x 14 cm')
        r = client.get(f'/entry/{entry_id}')
        assert b'Physical' in r.data

    def test_physical_view_row_absent_when_no_data(self, client, entry_id, volume_id):
        # No physical data — the phys-desc-panel (view row) should not be rendered
        r = client.get(f'/entry/{entry_id}')
        assert b'phys-desc-panel' not in r.data

    def test_notes_appear_on_detail_page(self, client, app, entry_id, volume_id):
        _edit_volume(client, volume_id, phys_notes='Water damage on back cover')
        r = client.get(f'/entry/{entry_id}')
        assert b'Water damage on back cover' in r.data

    def test_all_field_labels_shown_when_set(self, client, app, entry_id, volume_id):
        _edit_volume(client, volume_id,
                     phys_dimensions='20 x 14 cm',
                     phys_weight='300 g',
                     phys_pages='128',
                     phys_binding='Paperback',
                     phys_color='Full color',
                     phys_material='Paper',
                     phys_notes='Good condition')
        r = client.get(f'/entry/{entry_id}')
        for label in (b'Dimensions', b'Weight', b'Pages', b'Binding', b'Colour', b'Material', b'Notes'):
            assert label in r.data


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class TestPhysicalDescriptionAccess:
    def test_unauthenticated_edit_gets_403(self, client, volume_id):
        client.post('/logout')
        r = client.post(f'/volume/{volume_id}/edit',
                        data={'phys_dimensions': '20 x 14 cm'})
        assert r.status_code == 403

    def test_regular_user_edit_gets_403(self, client, volume_id, regular_id):
        client.post('/logout')
        login_as(client, '0003', 'regpass')
        r = client.post(f'/volume/{volume_id}/edit',
                        data={'phys_dimensions': '20 x 14 cm'})
        assert r.status_code == 403
