"""
Shared fixtures and helpers for the Home Library Catalog test suite.
"""
import os
import sys

import pytest
from werkzeug.security import generate_password_hash

# Ensure the project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module
from app import app as flask_app


# ---------------------------------------------------------------------------
# Core fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app(tmp_path, monkeypatch):
    """Flask app with a fresh, isolated SQLite DB for each test."""
    db_path = str(tmp_path / 'test.db')
    monkeypatch.setattr(app_module, 'DATABASE', db_path)
    # Disable backups so tests don't write to the real backup directory
    monkeypatch.setattr(app_module, 'make_backup', lambda: None)

    flask_app.config.update(TESTING=True, SECRET_KEY='test-secret')

    with flask_app.app_context():
        app_module.init_db()

    yield flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# DB insertion helpers (run outside a request context)
# ---------------------------------------------------------------------------

def insert_patron(app, patron_number, name, role, password, email=None):
    with app.app_context():
        db = app_module.get_db()
        db.execute(
            'INSERT INTO patrons (patron_number, name, email, role, password_hash) '
            'VALUES (?, ?, ?, ?, ?)',
            (patron_number, name, email, role, generate_password_hash(password)),
        )
        db.commit()
        return db.execute(
            'SELECT id FROM patrons WHERE patron_number = ?', (patron_number,)
        ).fetchone()['id']


def insert_library(app, name, is_storage=0, is_primary=0):
    with app.app_context():
        db = app_module.get_db()
        db.execute(
            'INSERT INTO libraries (name, is_storage, is_primary) VALUES (?, ?, ?)',
            (name, is_storage, is_primary),
        )
        db.commit()
        return db.execute(
            'SELECT id FROM libraries WHERE name = ?', (name,)
        ).fetchone()['id']


def insert_entry(app, title='Test Book', etype='book', restricted='unrestricted'):
    with app.app_context():
        db = app_module.get_db()
        db.execute(
            'INSERT INTO entries (type, title, restricted) VALUES (?, ?, ?)',
            (etype, title, restricted),
        )
        db.commit()
        return db.execute(
            'SELECT id FROM entries WHERE title = ? ORDER BY id DESC LIMIT 1', (title,)
        ).fetchone()['id']


def insert_contributor(app, entry_id, name, role='author'):
    with app.app_context():
        db = app_module.get_db()
        next_order = db.execute(
            'SELECT COALESCE(MAX(sort_order), -1) + 1 FROM entry_authors WHERE entry_id = ?',
            (entry_id,)
        ).fetchone()[0]
        db.execute(
            'INSERT INTO entry_authors (entry_id, name, role, sort_order) VALUES (?, ?, ?, ?)',
            (entry_id, name, role, next_order),
        )
        # keep denormalized cache in sync
        first = db.execute(
            'SELECT name FROM entry_authors WHERE entry_id = ? ORDER BY sort_order, id LIMIT 1',
            (entry_id,)
        ).fetchone()
        db.execute('UPDATE entries SET author = ? WHERE id = ?',
                   (first['name'] if first else None, entry_id))
        db.commit()
        return db.execute(
            'SELECT id FROM entry_authors WHERE entry_id = ? ORDER BY id DESC LIMIT 1',
            (entry_id,)
        ).fetchone()['id']


def insert_volume(app, entry_id, library_id=None, barcode=None, locus_call_number=None):
    with app.app_context():
        db = app_module.get_db()
        db.execute(
            'INSERT INTO volumes (entry_id, library_id, barcode, locus_call_number) '
            'VALUES (?, ?, ?, ?)',
            (entry_id, library_id, barcode, locus_call_number),
        )
        db.commit()
        return db.execute(
            'SELECT id FROM volumes WHERE entry_id = ? ORDER BY id DESC LIMIT 1', (entry_id,)
        ).fetchone()['id']


# ---------------------------------------------------------------------------
# Role fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def owner_id(app):
    return insert_patron(app, '0001', 'Owner', 'owner', 'ownerpass')


@pytest.fixture()
def cataloguer_id(app):
    return insert_patron(app, '0002', 'Cataloguer', 'cataloguer', 'catpass')


@pytest.fixture()
def regular_id(app):
    return insert_patron(app, '0003', 'Regular', 'regular', 'regpass')


@pytest.fixture()
def restricted_id(app):
    return insert_patron(app, '0004', 'Restricted', 'restricted', 'respass')


@pytest.fixture()
def guest_id(app):
    return insert_patron(app, '0005', 'Guest', 'guest', 'guestpass')


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def login_as(client, patron_number, password):
    """POST to /login and follow redirects. Returns the final response."""
    return client.post(
        '/login',
        data={'patron_number': patron_number, 'password': password},
        follow_redirects=True,
    )
