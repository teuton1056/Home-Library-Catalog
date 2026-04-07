import configparser
import csv
import io
import logging
import logging.handlers
import re
import sqlite3
import os
import json
import glob
import html as html_module
import sys
import zipfile
import urllib.request
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, g, flash, has_request_context, send_file, jsonify, session, abort
from werkzeug.security import generate_password_hash, check_password_hash
from typing import Any

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# ---------------------------------------------------------------------------
# Config file — loaded once at startup; all other constants derive from it
# ---------------------------------------------------------------------------

_APP_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_APP_DIR, 'config.ini')

_cfg = configparser.ConfigParser()
_cfg.read(CONFIG_PATH)

def _cfg_path(section: str, key: str, default: str) -> str:
    """Return a config value resolved to an absolute path."""
    raw = _cfg.get(section, key, fallback=default)
    return raw if os.path.isabs(raw) else os.path.join(_APP_DIR, raw)

# [database]
DATABASE = _cfg_path('database', 'path', 'library.db')

# [backup]
BACKUP_DIR        = _cfg_path('backup', 'backup_dir', 'backups')
SHORT_BACKUP_DIR  = os.path.join(BACKUP_DIR, 'short')
DAILY_BACKUP_DIR  = os.path.join(BACKUP_DIR, 'daily')
SHORT_BACKUP_KEEP = _cfg.getint('backup', 'short_keep', fallback=5)
DAILY_BACKUP_KEEP = _cfg.getint('backup', 'daily_keep', fallback=30)

CLASSIFICATION_DIR = os.path.join(_APP_DIR, 'classification_tables')


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    """Configure and return the application logger from config.ini.

    File handler: DEBUG by default — captures full diagnostics on disk.
    Stdout handler: INFO by default — readable during normal operation.
    Both levels, the log path, rotation size, and backup count are all
    overridable in the [logging] section of config.ini.
    """
    S = 'logging'

    def _level(key: str, default: str) -> int:
        raw = _cfg.get(S, key, fallback=default)
        return getattr(logging, raw.upper(), logging.DEBUG)

    file_enabled   = _cfg.getboolean(S, 'file_enabled',  fallback=True)
    stdout_enabled = _cfg.getboolean(S, 'stdout_enabled', fallback=True)
    file_level     = _level('file_level',   'DEBUG')
    stdout_level   = _level('stdout_level', 'INFO')
    log_file       = _cfg.get(S, 'log_file',      fallback='logs/app.log')
    max_bytes      = _cfg.getint(S, 'max_bytes',   fallback=5 * 1024 * 1024)
    backup_count   = _cfg.getint(S, 'backup_count', fallback=3)

    # Resolve relative paths against the directory that contains app.py
    if not os.path.isabs(log_file):
        log_file = os.path.join(os.path.dirname(__file__), log_file)

    log = logging.getLogger('library')
    log.setLevel(logging.DEBUG)          # handlers do their own level filtering
    log.propagate = False

    fmt = logging.Formatter(
        '%(asctime)s  %(levelname)-8s  %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    if file_enabled:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8'
        )
        fh.setLevel(file_level)
        fh.setFormatter(fmt)
        log.addHandler(fh)

    if stdout_enabled:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(stdout_level)
        sh.setFormatter(fmt)
        log.addHandler(sh)

    return log


logger = setup_logging()

if app.secret_key == 'dev-secret-key-change-in-production':
    logger.critical('Using default secret key — this should be overridden in production for security!')

# Load the LOCUS prefix → rank mapping from the JSON file (used for sorting by LOCUS call number)
# Isn't there a better way to do this?
_locus_prefix_order_path = os.path.join(os.path.dirname(__file__), 'LOCUS_Prefix_Order.json')
with open(_locus_prefix_order_path) as _f:
    LOCUS_PREFIX_RANK: dict[str, int] = json.load(_f)

_YEAR_RE = re.compile(r'^\d{4}(?:[/\-]\d{4})?$')

def _parse_year(raw: str):
    """Accept YYYY, YYYY-YYYY, or YYYY/YYYY. Returns normalised text or None."""
    s = raw.strip()
    if not s:
        return None
    if _YEAR_RE.match(s):
        return s.replace('/', '-')
    return None

ENTRY_TYPES = [
    ('book',                  'Book'),
    ('book_section',          'Book Section'),
    ('journal',               'Journal'),
    ('journal_article',       'Journal Article'),
    ('map',                   'Map'),
    ('pamphlet',              'Pamphlet'),
    ('dvd',                   'DVD'),
    ('cd',                    'CD'),
    ('vhs',                   'VHS Tape'),
    ('conference_proceedings', 'Conference Proceedings'),
    ('government_document',   'Government Document'),
]
TYPE_LABELS = dict(ENTRY_TYPES)

DIGITAL_RESOURCE_TYPES = [
    ('full_text', 'Full Text'),
    ('toc',       'Table of Contents'),
]
DIGITAL_RESOURCE_TYPE_LABELS = dict(DIGITAL_RESOURCE_TYPES)

# Contributor roles — (key, label)
CONTRIBUTOR_ROLES = [
    ('author',      'Author'),
    ('editor',      'Editor'),
    ('translator',  'Translator'),
    ('compiler',    'Compiler'),
    ('illustrator', 'Illustrator'),
    ('contributor', 'Contributor'),
]
CONTRIBUTOR_ROLE_LABELS = dict(CONTRIBUTOR_ROLES)

# Advanced search: ordered field list (value, label) used in both the UI and SQL dispatch
ADV_SEARCH_FIELDS = [
    ('any',         'Any field'),
    ('title',       'Title'),
    ('author',      'Author'),
    ('year',        'Year'),
    ('subject',     'Subject'),
    ('publisher',   'Publisher'),
    ('isbn',        'ISBN'),
    ('issn',        'ISSN'),
    ('series',      'Series'),
    ('language',    'Language'),
    ('locus_call',  'LOCUS call no.'),
    ('locus_code',  'LOCUS code'),
    ('barcode',     'Barcode'),
    ('book_title',  'Book title'),
    ('publication', 'Publication'),
    ('type',        'Entry type'),
]

# Maps each field key to the SQL column expressions it searches
_ADV_FIELD_SQL: dict = {
    'any': [
        'e.title',
        '(SELECT GROUP_CONCAT(ea.name, \' \') FROM entry_authors ea WHERE ea.entry_id = e.id)',
        'CAST(e.year AS TEXT)', 'e.language',
        'e.publisher', 'e.publisher_location', 'e.type',
        'e.isbn', 'e.issn', 'e.series',
        'v.barcode', 'v.lcc_number', 'v.locus_call_number', 'v.date', 'v.volume_number',
        'e.publication', 'e.pub_volume', 'e.pub_issue',
        'e.pages', 'e.book_title', 'e.locus_code',
        'dr.url', 'dr.call_number',
        '(SELECT GROUP_CONCAT(sh.heading, \' \') FROM entry_subject_headings esh2 '
        ' JOIN subject_headings sh ON sh.id = esh2.heading_id WHERE esh2.entry_id = e.id)',
    ],
    'title':       ['e.title'],
    'author':      ['(SELECT GROUP_CONCAT(ea.name, \' \') FROM entry_authors ea WHERE ea.entry_id = e.id)'],
    'year':        ['CAST(e.year AS TEXT)'],
    'subject':     [
        '(SELECT GROUP_CONCAT(sh.heading, \' \') FROM entry_subject_headings esh2 '
        ' JOIN subject_headings sh ON sh.id = esh2.heading_id WHERE esh2.entry_id = e.id)'
    ],
    'publisher':   ['e.publisher'],
    'isbn':        ['e.isbn'],
    'issn':        ['e.issn'],
    'series':      ['e.series'],
    'language':    ['e.language'],
    'lcc_number':  ['v.lcc_number'],
    'locus_call':  ['v.locus_call_number'],
    'locus_code':  ['e.locus_code'],
    'barcode':     ['v.barcode'],
    'book_title':  ['e.book_title'],
    'publication': ['e.publication'],
    'type':        ['e.type'],
}

# Patron roles — (key, label)
PATRON_ROLES = [
    ('owner',      'Owner'),
    ('cataloguer', 'Cataloguer'),
    ('regular',    'Regular User'),
    ('restricted', 'Restricted User'),
    ('guest',      'Guest'),
]
PATRON_ROLE_LABELS = dict(PATRON_ROLES)

# Entry types that typically include a publisher location
TYPES_WITH_PUB_LOCATION = {
    'book', 'book_section', 'pamphlet', 'map', 'conference_proceedings', 'government_document'
}

# Entry list sort options — (key, label)
SORT_OPTIONS = [
    ('title',  'Title'),
    ('author', 'Author'),
    ('year',   'Year'),
    ('locus',  'LOCUS'),
]

# Maps sort key → SQL ORDER BY clause (entries aliased as e, first_call_number available)
_SORT_ORDER_BY = {
    'title':  'e.title COLLATE NOCASE',
    'author': '(SELECT ea.name FROM entry_authors ea WHERE ea.entry_id = e.id ORDER BY ea.sort_order, ea.id LIMIT 1) IS NULL, (SELECT ea.name FROM entry_authors ea WHERE ea.entry_id = e.id ORDER BY ea.sort_order, ea.id LIMIT 1) COLLATE NOCASE, e.title COLLATE NOCASE',
    'year':   'e.year IS NULL, SUBSTR(CAST(e.year AS TEXT), 1, 4), e.title COLLATE NOCASE',
    'locus':  'first_call_number IS NULL, first_call_number COLLATE NOCASE, e.title COLLATE NOCASE',
    'type':   'e.type, e.title COLLATE NOCASE',
}


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def init_db():
    with app.app_context():
        db = get_db()
        with open(os.path.join(os.path.dirname(__file__), 'schema.sql')) as f:
            db.executescript(f.read())
        _commit(db)
    logger.debug('Database initialised from schema.sql')


@app.cli.command('init-db')
def init_db_command():
    """Initialise the database tables."""
    init_db()
    logger.info('Database initialised via CLI command.')


def _migrate_authors_to_table(db):
    """One-time migration: copy entries.author into entry_authors for existing rows."""
    already = db.execute('SELECT COUNT(*) FROM entry_authors').fetchone()[0]
    if already > 0:
        return
    rows = db.execute(
        "SELECT id, author FROM entries WHERE author IS NOT NULL AND author != ''"
    ).fetchall()
    for row in rows:
        db.execute(
            'INSERT INTO entry_authors (entry_id, name, role, sort_order) VALUES (?, ?, ?, ?)',
            (row['id'], row['author'], 'author', 0)
        )


def migrate_db():
    """Apply incremental schema migrations to existing databases."""
    logger.debug('Checking database schema...')
    db = get_db()
    logger.debug('Ensuring article/section columns in entries...')
    # If the entries table is missing the new article/section columns, recreate it.
    existing_cols = {row[1] for row in db.execute('PRAGMA table_info(entries)').fetchall()}
    if 'publication' not in existing_cols:
        db.execute('PRAGMA foreign_keys = OFF')
        db.execute(
            'CREATE TABLE entries_new ('
            '  id INTEGER PRIMARY KEY AUTOINCREMENT,'
            '  type TEXT NOT NULL,'
            '  title TEXT, author TEXT, year TEXT, language TEXT,'
            '  publisher TEXT, publisher_location TEXT, subject TEXT,'
            '  isbn TEXT, issn TEXT, series TEXT,'
            '  publication TEXT, pub_volume TEXT, pub_issue TEXT, pages TEXT, book_title TEXT'
            ')'
        )
        db.execute(
            'INSERT INTO entries_new '
            '(id, type, title, author, year, language, publisher, publisher_location, '
            ' subject, isbn, issn, series) '
            'SELECT id, type, title, author, year, language, publisher, publisher_location, '
            '       subject, isbn, issn, series FROM entries'
        )
        db.execute('DROP TABLE entries')
        db.execute('ALTER TABLE entries_new RENAME TO entries')
        db.execute('PRAGMA foreign_keys = ON')

    logger.debug('Ensuring patrons table exists...')
    # Ensure the patrons table exists (idempotent)
    db.execute(
        'CREATE TABLE IF NOT EXISTS patrons ('
        '  id             INTEGER PRIMARY KEY AUTOINCREMENT,'
        '  patron_number  TEXT NOT NULL UNIQUE,'
        '  name           TEXT NOT NULL,'
        '  email          TEXT,'
        "  role           TEXT NOT NULL DEFAULT 'regular'"
        "  CHECK(role IN ('owner', 'cataloguer', 'regular', 'restricted', 'guest'))"
        ')'
    )

    logger.debug('Ensuring digital_resources table exists...')
    # Ensure the digital_resources table exists (idempotent)
    db.execute(
        'CREATE TABLE IF NOT EXISTS digital_resources ('
        '  id            INTEGER PRIMARY KEY AUTOINCREMENT,'
        '  entry_id      INTEGER NOT NULL,'
        '  url           TEXT,'
        '  call_number   TEXT,'
        "  resource_type TEXT NOT NULL DEFAULT 'full_text',"
        '  FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE'
        ')'
    )

    logger.debug('Ensuring volumes table has correct columns...')
    # Rename volumes.shelfmark → lcc_number and add locus_call_number (table recreation)
    vol_cols = {row[1] for row in db.execute('PRAGMA table_info(volumes)').fetchall()}
    if 'lcc_number' not in vol_cols:
        db.execute('PRAGMA foreign_keys = OFF')
        db.execute(
            'CREATE TABLE volumes_new ('
            '  id                INTEGER PRIMARY KEY AUTOINCREMENT,'
            '  entry_id          INTEGER NOT NULL,'
            '  library_id        INTEGER,'
            '  barcode           TEXT,'
            '  lcc_number        TEXT,'
            '  locus_call_number TEXT,'
            '  date              TEXT,'
            '  volume_number     TEXT,'
            '  checked_out       INTEGER NOT NULL DEFAULT 0,'
            '  box_number        TEXT,'
            '  audit_scanned     INTEGER NOT NULL DEFAULT 0,'
            '  FOREIGN KEY (entry_id)   REFERENCES entries(id)   ON DELETE CASCADE,'
            '  FOREIGN KEY (library_id) REFERENCES libraries(id) ON DELETE SET NULL'
            ')'
        )
        if 'audit_scanned' in vol_cols:
            db.execute(
                'INSERT INTO volumes_new '
                '(id, entry_id, library_id, barcode, lcc_number, date, volume_number, '
                ' checked_out, box_number, audit_scanned) '
                'SELECT id, entry_id, library_id, barcode, shelfmark, date, volume_number, '
                '       checked_out, box_number, audit_scanned FROM volumes'
            )
        else:
            db.execute(
                'INSERT INTO volumes_new '
                '(id, entry_id, library_id, barcode, lcc_number, date, volume_number, '
                ' checked_out, box_number) '
                'SELECT id, entry_id, library_id, barcode, shelfmark, date, volume_number, '
                '       checked_out, box_number FROM volumes'
            )
        db.execute('DROP TABLE volumes')
        db.execute('ALTER TABLE volumes_new RENAME TO volumes')
        db.execute('PRAGMA foreign_keys = ON')

    # Simple column additions for volumes / libraries / entries
    migrations = [
        'ALTER TABLE volumes    ADD COLUMN checked_out       INTEGER NOT NULL DEFAULT 0',
        'ALTER TABLE volumes    ADD COLUMN box_number        TEXT',
        'ALTER TABLE volumes    ADD COLUMN audit_scanned     INTEGER NOT NULL DEFAULT 0',
        'ALTER TABLE volumes    ADD COLUMN locus_call_number TEXT',
        'ALTER TABLE volumes    ADD COLUMN is_oversize       INTEGER NOT NULL DEFAULT 0',
        'ALTER TABLE volumes    ADD COLUMN is_missing        INTEGER NOT NULL DEFAULT 0',
        'ALTER TABLE volumes    ADD COLUMN is_fragile        INTEGER NOT NULL DEFAULT 0',
        'ALTER TABLE libraries  ADD COLUMN is_primary        INTEGER NOT NULL DEFAULT 0',
        'ALTER TABLE libraries  ADD COLUMN is_storage        INTEGER NOT NULL DEFAULT 0',
        'ALTER TABLE entries    ADD COLUMN locus_code        TEXT',
        'ALTER TABLE entries    ADD COLUMN subtitle           TEXT',
        'ALTER TABLE entries    ADD COLUMN notes              TEXT',
        'ALTER TABLE entries    ADD COLUMN condition          TEXT',
        'ALTER TABLE entries    ADD COLUMN acquisition_date    TEXT',
        'ALTER TABLE entries    ADD COLUMN acquisition_source  TEXT',
        'ALTER TABLE entries    ADD COLUMN conference_name     TEXT',
        'ALTER TABLE entries    ADD COLUMN conference_date     TEXT',
        'ALTER TABLE entries    ADD COLUMN conference_location TEXT',
        'ALTER TABLE entries    ADD COLUMN original_year       TEXT',
        'ALTER TABLE entries    ADD COLUMN original_publisher  TEXT',
        'ALTER TABLE entries    ADD COLUMN edition             TEXT',
        'ALTER TABLE entries    ADD COLUMN series_number       TEXT',
        'ALTER TABLE entries    ADD COLUMN abstract            TEXT',
        "ALTER TABLE entries    ADD COLUMN restricted          TEXT NOT NULL DEFAULT 'unrestricted' CHECK(restricted IN ('unrestricted', 'restricted', 'hidden'))",
        'ALTER TABLE volumes    ADD COLUMN checked_out_to      INTEGER REFERENCES patrons(id) ON DELETE SET NULL',
        'ALTER TABLE patrons    ADD COLUMN password_hash       TEXT',
        'ALTER TABLE volumes    ADD COLUMN phys_dimensions     TEXT',
        'ALTER TABLE volumes    ADD COLUMN phys_weight         TEXT',
        'ALTER TABLE volumes    ADD COLUMN phys_pages          TEXT',
        'ALTER TABLE volumes    ADD COLUMN phys_binding        TEXT',
        'ALTER TABLE volumes    ADD COLUMN phys_color          TEXT',
        'ALTER TABLE volumes    ADD COLUMN phys_material       TEXT',
        'ALTER TABLE volumes    ADD COLUMN phys_notes          TEXT',
    ]

    logger.debug('Ensuring checkouts and entry_authors tables exist...')
    # Ensure the checkouts table exists (idempotent)
    db.execute(
        'CREATE TABLE IF NOT EXISTS checkouts ('
        '  id             INTEGER PRIMARY KEY AUTOINCREMENT,'
        '  volume_id      INTEGER NOT NULL,'
        '  patron_id      INTEGER NOT NULL,'
        '  checked_out_at TEXT NOT NULL,'
        '  returned_at    TEXT,'
        '  FOREIGN KEY (volume_id) REFERENCES volumes(id) ON DELETE CASCADE,'
        '  FOREIGN KEY (patron_id) REFERENCES patrons(id) ON DELETE CASCADE'
        ')'
    )

    # Ensure the entry_authors table exists (idempotent)
    db.execute(
        'CREATE TABLE IF NOT EXISTS entry_authors ('
        '  id         INTEGER PRIMARY KEY AUTOINCREMENT,'
        '  entry_id   INTEGER NOT NULL,'
        '  name       TEXT    NOT NULL,'
        '  role       TEXT    NOT NULL DEFAULT \'author\','
        '  sort_order INTEGER NOT NULL DEFAULT 0,'
        '  FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE'
        ')'
    )

    logger.debug('Ensuring existing author data is in entry_authors table...')
    # Migrate existing entries.author values into entry_authors (run once)
    _migrate_authors_to_table(db)

    for sql in migrations:
        try:
            db.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists

    # Backfill any NULL restricted values (rows created before the column existed)
    db.execute("UPDATE entries SET restricted = 'unrestricted' WHERE restricted IS NULL")

    logger.debug('Ensuring subject heading system tables exist...')

    # ── Subject heading system migration ────────────────────────────────────
    # Create the three new tables unconditionally (IF NOT EXISTS guards).
    db.execute(
        'CREATE TABLE IF NOT EXISTS subject_headings ('
        '  id         INTEGER PRIMARY KEY AUTOINCREMENT,'
        '  heading    TEXT    NOT NULL UNIQUE,'
        '  type       TEXT,'
        '  scope_note TEXT'
        ')'
    )
    db.execute(
        'CREATE TABLE IF NOT EXISTS subject_relations ('
        '  id            INTEGER PRIMARY KEY AUTOINCREMENT,'
        '  from_id       INTEGER NOT NULL,'
        "  relation_type TEXT    NOT NULL CHECK(relation_type IN ('USE','UF','BT','NT','RT','SA','SE','PARENT','CHILD')),"
        '  to_id         INTEGER NOT NULL,'
        '  UNIQUE(from_id, relation_type, to_id),'
        '  FOREIGN KEY (from_id) REFERENCES subject_headings(id) ON DELETE CASCADE,'
        '  FOREIGN KEY (to_id)   REFERENCES subject_headings(id) ON DELETE CASCADE'
        ')'
    )
    db.execute(
        'CREATE TABLE IF NOT EXISTS entry_subject_headings ('
        '  id         INTEGER PRIMARY KEY AUTOINCREMENT,'
        '  entry_id   INTEGER NOT NULL,'
        '  heading_id INTEGER NOT NULL,'
        '  UNIQUE(entry_id, heading_id),'
        '  FOREIGN KEY (entry_id)   REFERENCES entries(id)          ON DELETE CASCADE,'
        '  FOREIGN KEY (heading_id) REFERENCES subject_headings(id) ON DELETE CASCADE'
        ')'
    )

    logger.debug('Ensuring existing subject data is in subject_headings and entry_subject_headings tables...')

    # Migrate entries.subject → entry_subject_headings, then drop the column.
    # Guard: only run if entries still has the 'subject' column.
    entry_cols = {row[1] for row in db.execute('PRAGMA table_info(entries)').fetchall()}
    if 'subject' in entry_cols:
        logger.debug('Found legacy subject column in entries — migrating to structured subject headings...')
        # Commit all preceding DDL/DML so the backup sees a clean, fully-committed state.
        # Without this, db.backup() deadlocks because db still holds its write lock.
        db.commit()
        _migrate_backup(db)

        # Populate subject_headings and entry_subject_headings from free-text subject strings
        rows = db.execute("SELECT id, subject FROM entries WHERE subject IS NOT NULL").fetchall()
        logger.debug('Migrating subjects for %d entries...', len(rows))
        for row in rows:
            for tag in row['subject'].split(','):
                tag = tag.strip()
                if not tag:
                    continue
                existing = db.execute(
                    'SELECT id FROM subject_headings WHERE heading = ?', (tag,)
                ).fetchone()
                if existing:
                    hid = existing['id']
                else:
                    cur2 = db.execute(
                        'INSERT INTO subject_headings (heading) VALUES (?)', (tag,)
                    )
                    hid = cur2.lastrowid
                db.execute(
                    'INSERT OR IGNORE INTO entry_subject_headings (entry_id, heading_id) VALUES (?, ?)',
                    (row['id'], hid)
                )

        logger.debug('Recreating entries table without subject column...')
        # Commit the subject-heading INSERT work before issuing the PRAGMA.
        # PRAGMA foreign_keys is a no-op inside an active transaction, so we must
        # ensure no transaction is open or the subsequent DROP TABLE entries will
        # cascade-delete all volumes/entry_authors/digital_resources rows via their
        # ON DELETE CASCADE foreign keys.
        db.commit()
        db.execute('PRAGMA foreign_keys = OFF')
        db.execute(
            'CREATE TABLE entries_no_subject ('
            '  id                 INTEGER PRIMARY KEY AUTOINCREMENT,'
            '  type               TEXT NOT NULL,'
            '  title              TEXT, author TEXT, year TEXT, language TEXT,'
            '  publisher          TEXT, publisher_location TEXT,'
            '  isbn               TEXT, issn TEXT, series TEXT,'
            '  publication        TEXT, pub_volume TEXT, pub_issue TEXT,'
            '  pages              TEXT, book_title TEXT, locus_code TEXT,'
            '  subtitle           TEXT, notes TEXT, condition TEXT,'
            '  acquisition_date   TEXT, acquisition_source TEXT,'
            '  conference_name    TEXT, conference_date TEXT, conference_location TEXT,'
            '  original_year      TEXT, original_publisher TEXT,'
            '  edition            TEXT, series_number TEXT, abstract TEXT,'
            "  restricted         TEXT NOT NULL DEFAULT 'unrestricted'"
            "                     CHECK(restricted IN ('unrestricted','restricted','hidden'))"
            ')'
        )
        db.execute(
            'INSERT INTO entries_no_subject '
            '(id, type, title, author, year, language, publisher, publisher_location, '
            ' isbn, issn, series, publication, pub_volume, pub_issue, pages, book_title, '
            ' locus_code, subtitle, notes, condition, acquisition_date, acquisition_source, '
            ' conference_name, conference_date, conference_location, original_year, '
            ' original_publisher, edition, series_number, abstract, restricted) '
            'SELECT id, type, title, author, year, language, publisher, publisher_location, '
            '       isbn, issn, series, publication, pub_volume, pub_issue, pages, book_title, '
            '       locus_code, subtitle, notes, condition, acquisition_date, acquisition_source, '
            '       conference_name, conference_date, conference_location, original_year, '
            '       original_publisher, edition, series_number, abstract, restricted '
            'FROM entries'
        )
        logger.debug('Dropping old entries table and renaming new one...')
        db.execute('DROP TABLE entries')
        db.execute('ALTER TABLE entries_no_subject RENAME TO entries')
        db.execute('PRAGMA foreign_keys = ON')

    logger.debug('Ensuring subject_relations CHECK constraint includes PARENT/CHILD...')
    sr_sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='subject_relations'"
    ).fetchone()
    if sr_sql and 'PARENT' not in sr_sql['sql']:
        logger.debug('Recreating subject_relations table with updated CHECK constraint...')
        db.commit()
        db.execute('PRAGMA foreign_keys = OFF')
        db.execute(
            'CREATE TABLE subject_relations_new ('
            '  id            INTEGER PRIMARY KEY AUTOINCREMENT,'
            '  from_id       INTEGER NOT NULL,'
            "  relation_type TEXT    NOT NULL"
            "                CHECK(relation_type IN ('USE','UF','BT','NT','RT','SA','SE','PARENT','CHILD')),"
            '  to_id         INTEGER NOT NULL,'
            '  UNIQUE(from_id, relation_type, to_id),'
            '  FOREIGN KEY (from_id) REFERENCES subject_headings(id) ON DELETE CASCADE,'
            '  FOREIGN KEY (to_id)   REFERENCES subject_headings(id) ON DELETE CASCADE'
            ')'
        )
        db.execute(
            'INSERT INTO subject_relations_new (id, from_id, relation_type, to_id) '
            'SELECT id, from_id, relation_type, to_id FROM subject_relations'
        )
        db.execute('DROP TABLE subject_relations')
        db.execute('ALTER TABLE subject_relations_new RENAME TO subject_relations')
        db.execute('PRAGMA foreign_keys = ON')
        logger.debug('subject_relations table updated.')

    logger.debug('Ensuring existing compound subject headings have PARENT/CHILD relations...')
    compound_rows = db.execute(
        "SELECT id, heading FROM subject_headings WHERE heading LIKE '%---%' OR heading LIKE '%\u2014%'"
    ).fetchall()
    for row in compound_rows:
        _ensure_subdivision_parent(db, row['id'], row['heading'])

    logger.debug('Ensuring existing volume barcodes are normalised...')
    # Normalise all-numeric barcodes to 5-digit zero-padded strings (idempotent)
    db.execute(
        "UPDATE volumes "
        "SET barcode = printf('%05d', CAST(barcode AS INTEGER)) "
        "WHERE barcode GLOB '[0-9]*' AND length(barcode) < 5"
    )
    logger.debug('Database schema is up to date.')
    _commit(db)


def _migrate_backup(db):
    """Write a one-off backup of the DB before a destructive schema migration."""
    if not os.path.exists(DATABASE):
        return
    os.makedirs(SHORT_BACKUP_DIR, exist_ok=True)
    ts   = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = os.path.join(SHORT_BACKUP_DIR, f'library_premigration_{ts}.db')
    dst  = sqlite3.connect(path)
    try:
        db.backup(dst)
    finally:
        dst.close()


def _sync_author_field(db, entry_id):
    """Keep entries.author in sync with the first author in entry_authors (for sort/display)."""
    first = db.execute(
        'SELECT name FROM entry_authors WHERE entry_id = ? ORDER BY sort_order, id LIMIT 1',
        (entry_id,)
    ).fetchone()
    db.execute('UPDATE entries SET author = ? WHERE id = ?',
               (first['name'] if first else None, entry_id))


def _commit(db):
    """Commit and flag that a backup should be made after the request."""
    db.commit()
    if has_request_context():
        g._db_modified = True


def make_backup():
    """
    Short-term: one backup per write, keep last SHORT_BACKUP_KEEP copies.
    Daily:      one backup per calendar day, keep last DAILY_BACKUP_KEEP copies.
    Uses SQLite's built-in backup API so the copy is always consistent.
    """
    src_db = getattr(g, '_database', None)
    if src_db is None or not os.path.exists(DATABASE):
        return

    os.makedirs(SHORT_BACKUP_DIR, exist_ok=True)
    os.makedirs(DAILY_BACKUP_DIR, exist_ok=True)

    now = datetime.now()
    ts  = now.strftime('%Y%m%d_%H%M%S')

    # --- short-term backup ---
    short_path = os.path.join(SHORT_BACKUP_DIR, f'library_{ts}.db')
    dst = sqlite3.connect(short_path)
    try:
        src_db.backup(dst)
    finally:
        dst.close()
    logger.debug('Short-term backup written: %s', short_path)

    # Rotate: remove oldest beyond the keep limit
    for old in sorted(glob.glob(os.path.join(SHORT_BACKUP_DIR, 'library_*.db')))[:-SHORT_BACKUP_KEEP]:
        try:
            os.remove(old)
            logger.debug('Rotated old short-term backup: %s', old)
        except OSError:
            pass

    # --- daily backup ---
    today_str  = now.strftime('%Y%m%d')
    daily_path = os.path.join(DAILY_BACKUP_DIR, f'library_{today_str}.db')
    if not os.path.exists(daily_path):
        dst = sqlite3.connect(daily_path)
        try:
            src_db.backup(dst)
        finally:
            dst.close()
        logger.debug('Daily backup written: %s', daily_path)

        for old in sorted(glob.glob(os.path.join(DAILY_BACKUP_DIR, 'library_*.db')))[:-DAILY_BACKUP_KEEP]:
            try:
                os.remove(old)
                logger.debug('Rotated old daily backup: %s', old)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Template context
# ---------------------------------------------------------------------------

@app.after_request
def after_write(response):
    if getattr(g, '_db_modified', False):
        try:
            make_backup()
        except Exception:
            logger.exception('Backup failed after write — database is intact but no backup was made')
    return response


@app.before_request
def load_logged_in_patron():
    patron_id = session.get('patron_id')
    if patron_id:
        try:
            g.patron = get_db().execute(
                'SELECT * FROM patrons WHERE id = ?', (patron_id,)
            ).fetchone()
        except Exception:
            g.patron = None
    else:
        g.patron = None


@app.context_processor
def inject_globals():
    try:
        libraries = get_db().execute(
            'SELECT * FROM libraries ORDER BY name'
        ).fetchall()
    except Exception:
        libraries = []
    return {'all_libraries': libraries, 'current_patron': g.get('patron')}


def _restricted_filter():
    """Return SQL condition fragment (no leading AND) to filter entries by patron visibility.
    Returns empty string for owner/cataloguer (no filter)."""
    patron = g.get('patron')
    if patron and patron['role'] in ('owner', 'cataloguer'):
        return ''
    elif patron and patron['role'] in ('regular', 'restricted'):
        return "e.restricted != 'hidden'"
    else:
        return "e.restricted = 'unrestricted'"


def require_role(*roles):
    """Decorator: abort(403) if current patron's role is not in roles."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            patron = g.get('patron')
            if not patron or patron['role'] not in roles:
                patron_id = patron['id'] if patron else None
                role      = patron['role'] if patron else 'unauthenticated'
                logger.debug(
                    'Access denied to %s — patron=%s role=%s required=%s',
                    request.endpoint, patron_id, role, roles,
                )
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ---------------------------------------------------------------------------
# Citation generator (Chicago style)
# ---------------------------------------------------------------------------

def generate_citation(entry, volumes, contributors=None):
    title     = entry['title'] or 'Untitled'
    year      = entry['year']
    publisher = entry['publisher']
    pub_loc   = entry['publisher_location']
    etype     = entry['type']
    year_str  = str(year) if year else 'n.d.'

    parts = []

    # Build author/editor string from structured contributors
    if contributors:
        authors   = [c for c in contributors if c['role'] == 'author']
        editors   = [c for c in contributors if c['role'] == 'editor']
        primary   = authors or editors
        is_editor = bool(editors) and not authors
        if primary:
            names = ', '.join(c['name'] for c in primary)
            author_str = names.rstrip('.')
            if etype == 'conference_proceedings' or is_editor:
                author_str += ', ed.' if len(primary) == 1 else ', eds.'
            else:
                author_str += '.'
            parts.append(author_str)
    elif entry['author']:
        # Fallback to legacy field if no structured contributors yet
        author_str = entry['author'].rstrip('.')
        if etype == 'conference_proceedings':
            author_str += ', ed.'
        else:
            author_str += '.'
        parts.append(author_str)

    if etype == 'journal_article':
        publication = entry['publication'] or 'Unknown Journal'
        pub_vol     = entry['pub_volume']
        pub_issue   = entry['pub_issue']
        pages       = entry['pages']
        parts.append(f'"{title}."')
        ref = f'<em>{publication}</em>'
        if pub_vol:
            ref += f' {pub_vol}'
        if pub_issue:
            ref += f', no. {pub_issue}'
        if year:
            ref += f' ({year_str})'
        if pages:
            ref += f': {pages}'
        parts.append(ref + '.')
        return ' '.join(parts)

    if etype == 'book_section':
        book_title = entry['book_title'] or 'Unknown Book'
        pages      = entry['pages']
        parts.append(f'"{title}."')
        parts.append(f'In <em>{book_title}</em>.')
        if pub_loc and publisher:
            parts.append(f'{pub_loc}: {publisher}, {year_str}.')
        elif publisher:
            parts.append(f'{publisher}, {year_str}.')
        elif year:
            parts.append(f'{year_str}.')
        if pages:
            parts.append(f'Pp. {pages}.')
        return ' '.join(parts)

    # Default path (book, journal, map, etc.)
    parts.append(f'<em>{title}</em>.')

    if len(volumes) > 1:
        parts.append(f'{len(volumes)} vols.')

    if pub_loc and publisher:
        parts.append(f'{pub_loc}: {publisher}, {year_str}.')
    elif publisher:
        parts.append(f'{publisher}, {year_str}.')
    elif year:
        parts.append(f'{year_str}.')
    else:
        parts.append('n.d.')

    return ' '.join(parts)


# ---------------------------------------------------------------------------
# Subject heading helpers
# ---------------------------------------------------------------------------

SUBJECT_HEADING_TYPES = [
    ('region',   'Region'),
    ('people',   'People'),
    ('person',   'Person'),
    ('topic',    'Topic'),
    ('event',    'Event'),
    ('language', 'Language'),
    ('work',     'Work'),
]
SUBJECT_HEADING_TYPE_LABELS = dict(SUBJECT_HEADING_TYPES)

# All relation types with display labels (used on detail/preview pages)
SUBJECT_RELATION_TYPES = [
    ('PARENT', 'Parent Heading'),
    ('CHILD',  'Child Heading'),
    ('USE',    'Use'),
    ('UF',     'Use For'),
    ('BT',     'Broader Term'),
    ('NT',     'Narrower Term'),
    ('RT',     'Related Term'),
    ('SA',     'See Also'),
    ('SE',     'See Especially'),
]
SUBJECT_RELATION_TYPE_LABELS = dict(SUBJECT_RELATION_TYPES)

# Relation types available in the manual relation editor (PARENT/CHILD are
# auto-managed from the heading name and must not be set by hand)
SUBJECT_FORM_RELATION_TYPES = [r for r in SUBJECT_RELATION_TYPES if r[0] not in ('PARENT', 'CHILD')]

# Automatic reciprocals: adding (A, rel, B) also adds (B, recip, A)
_RELATION_RECIPROCALS = {
    'PARENT': 'CHILD',
    'CHILD':  'PARENT',
    'BT':     'NT',
    'NT':     'BT',
    'RT':     'RT',
    'USE':    'UF',
    'UF':     'USE',
    # SA and SE have no automatic reciprocal
}

# Matches the subdivision separator in compound headings (e.g. "Hebrew---Biblical"
# or "Hebrew\u2014Biblical").  The three-hyphen form mirrors LaTeX \textemdash
# and is the primary convention; the Unicode em-dash is also accepted.
_SUBDIVISION_SEP_RE = re.compile(r'---|—')


def _ensure_subdivision_parent(db, heading_id, heading_text):
    """If heading_text contains a subdivision separator (--- or —), ensure a PARENT
    relation exists to the immediate parent heading (and a CHILD reciprocal on the
    parent), creating the parent heading if absent.
    Recurses naturally via _get_or_create_heading for multi-level compounds."""
    matches = list(_SUBDIVISION_SEP_RE.finditer(heading_text))
    if not matches:
        return
    parent_text = heading_text[:matches[-1].start()].strip()
    if not parent_text:
        return
    parent_id = _get_or_create_heading(db, parent_text)
    _add_relation(db, heading_id, 'PARENT', parent_id)


def _get_or_create_heading(db, text):
    """Return the id of the subject heading with the given text, creating it if absent.
    When a new compound heading is created (contains --- or —) a PARENT relation to its
    immediate parent is added automatically."""
    text = text.strip()
    row = db.execute('SELECT id FROM subject_headings WHERE heading = ?', (text,)).fetchone()
    if row:
        return row['id']
    cur = db.execute('INSERT INTO subject_headings (heading) VALUES (?)', (text,))
    heading_id = cur.lastrowid
    _ensure_subdivision_parent(db, heading_id, text)
    return heading_id


def _add_relation(db, from_id, rel_type, to_id):
    """Insert a relation and its automatic reciprocal (if any). Silently ignores duplicates."""
    if from_id == to_id:
        return
    try:
        db.execute(
            'INSERT INTO subject_relations (from_id, relation_type, to_id) VALUES (?, ?, ?)',
            (from_id, rel_type, to_id)
        )
    except sqlite3.IntegrityError:
        pass
    recip = _RELATION_RECIPROCALS.get(rel_type)
    if recip:
        try:
            db.execute(
                'INSERT INTO subject_relations (from_id, relation_type, to_id) VALUES (?, ?, ?)',
                (to_id, recip, from_id)
            )
        except sqlite3.IntegrityError:
            pass


def _remove_relation(db, from_id, rel_type, to_id):
    """Delete a relation and its automatic reciprocal (if any)."""
    db.execute(
        'DELETE FROM subject_relations WHERE from_id=? AND relation_type=? AND to_id=?',
        (from_id, rel_type, to_id)
    )
    recip = _RELATION_RECIPROCALS.get(rel_type)
    if recip:
        db.execute(
            'DELETE FROM subject_relations WHERE from_id=? AND relation_type=? AND to_id=?',
            (to_id, recip, from_id)
        )


def _resolve_preferred(db, heading_id):
    """Follow USE chain to find the preferred heading.
    Returns (preferred_id, was_redirected: bool).
    Stops after 10 hops to guard against loops."""
    current = heading_id
    for _ in range(10):
        use_rel = db.execute(
            'SELECT to_id FROM subject_relations WHERE from_id=? AND relation_type=?',
            (current, 'USE')
        ).fetchone()
        if not use_rel:
            break
        current = use_rel['to_id']
    return current, (current != heading_id)


def _set_entry_subjects(db, entry_id, names):
    """Replace all subject headings for an entry with the given list of heading names.
    Each name is resolved to its preferred form (via USE chain); new headings are created.
    Returns a list of (original_name, preferred_heading_row) tuples that were redirected."""
    redirects = []
    new_ids = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        hid = _get_or_create_heading(db, name)
        preferred_id, was_redirected = _resolve_preferred(db, hid)
        if was_redirected:
            pref_row = db.execute(
                'SELECT * FROM subject_headings WHERE id=?', (preferred_id,)
            ).fetchone()
            redirects.append((name, pref_row))
        new_ids.append(preferred_id)
    db.execute('DELETE FROM entry_subject_headings WHERE entry_id=?', (entry_id,))
    for hid in dict.fromkeys(new_ids):  # deduplicate while preserving first-seen order
        try:
            db.execute(
                'INSERT INTO entry_subject_headings (entry_id, heading_id) VALUES (?, ?)',
                (entry_id, hid)
            )
        except sqlite3.IntegrityError:
            pass
    return redirects


# ---------------------------------------------------------------------------
# Routes — Subject Headings
# ---------------------------------------------------------------------------

def _require_subject_access():
    """Abort 403 for guests and unauthenticated visitors."""
    patron = g.get('patron')
    if not patron or patron['role'] == 'guest':
        abort(403)


@app.route('/subjects')
def subjects():
    _require_subject_access()
    db = get_db()
    q  = request.args.get('q', '').strip()
    if q:
        rows = db.execute(
            'SELECT sh.*, COUNT(esh.entry_id) AS entry_count '
            'FROM subject_headings sh '
            'LEFT JOIN entry_subject_headings esh ON esh.heading_id = sh.id '
            'WHERE sh.heading LIKE ? '
            'GROUP BY sh.id ORDER BY sh.heading COLLATE NOCASE',
            (f'%{q}%',)
        ).fetchall()
    else:
        rows = db.execute(
            'SELECT sh.*, COUNT(esh.entry_id) AS entry_count '
            'FROM subject_headings sh '
            'LEFT JOIN entry_subject_headings esh ON esh.heading_id = sh.id '
            'GROUP BY sh.id ORDER BY sh.heading COLLATE NOCASE'
        ).fetchall()
    return render_template('subjects.html', headings=rows, query=q,
                           relation_type_labels=SUBJECT_RELATION_TYPE_LABELS)


@app.route('/subject/new', methods=['GET', 'POST'])
@require_role('owner', 'cataloguer')
def new_subject():
    if request.method == 'POST':
        heading_text = request.form.get('heading', '').strip()
        if not heading_text:
            flash('Heading text is required.')
            return render_template('subject_form.html',
                                   heading=None, editing=False,
                                   heading_types=SUBJECT_HEADING_TYPES,
                                   relation_types=SUBJECT_FORM_RELATION_TYPES)
        db = get_db()
        try:
            cur = db.execute(
                'INSERT INTO subject_headings (heading, type, scope_note) VALUES (?, ?, ?)',
                (
                    heading_text,
                    request.form.get('type', '').strip() or None,
                    request.form.get('scope_note', '').strip() or None,
                )
            )
            heading_id = cur.lastrowid
        except sqlite3.IntegrityError:
            flash(f'A heading "{heading_text}" already exists.')
            return render_template('subject_form.html',
                                   heading=None, editing=False,
                                   heading_types=SUBJECT_HEADING_TYPES,
                                   relation_types=SUBJECT_FORM_RELATION_TYPES)
        _save_subject_relations(db, heading_id, request.form)
        _ensure_subdivision_parent(db, heading_id, heading_text)
        _commit(db)
        logger.info('Subject heading created: id=%d heading=%r', heading_id, heading_text)
        flash(f'Subject heading "{heading_text}" created.')
        return redirect(url_for('subject_detail', heading_id=heading_id))
    return render_template('subject_form.html',
                           heading=None, editing=False,
                           heading_types=SUBJECT_HEADING_TYPES,
                           relation_types=SUBJECT_FORM_RELATION_TYPES)


@app.route('/subject/<int:heading_id>')
def subject_detail(heading_id):
    _require_subject_access()
    db      = get_db()
    heading = db.execute('SELECT * FROM subject_headings WHERE id=?', (heading_id,)).fetchone()
    if not heading:
        flash('Subject heading not found.')
        return redirect(url_for('subjects'))
    relations = db.execute(
        'SELECT sr.relation_type, sh.id AS target_id, sh.heading AS target_heading '
        'FROM subject_relations sr '
        'JOIN subject_headings sh ON sh.id = sr.to_id '
        'WHERE sr.from_id = ? '
        'ORDER BY sr.relation_type, sh.heading COLLATE NOCASE',
        (heading_id,)
    ).fetchall()
    rf = _restricted_filter()
    rf_sql = f' AND {rf}' if rf else ''
    entries = db.execute(
        'SELECT e.id, e.title, e.type, e.year, e.restricted, '
        '(SELECT ea.name FROM entry_authors ea WHERE ea.entry_id = e.id '
        ' ORDER BY ea.sort_order, ea.id LIMIT 1) AS first_author '
        'FROM entries e '
        'JOIN entry_subject_headings esh ON esh.entry_id = e.id '
        f'WHERE esh.heading_id = ?{rf_sql} '
        'ORDER BY e.title COLLATE NOCASE',
        (heading_id,)
    ).fetchall()
    return render_template('subject_detail.html', heading=heading, relations=relations,
                           entries=entries, type_labels=TYPE_LABELS,
                           heading_type_labels=SUBJECT_HEADING_TYPE_LABELS,
                           relation_type_labels=SUBJECT_RELATION_TYPE_LABELS)


@app.route('/subject/<int:heading_id>/preview')
def subject_preview(heading_id):
    _require_subject_access()
    db      = get_db()
    heading = db.execute('SELECT * FROM subject_headings WHERE id=?', (heading_id,)).fetchone()
    if not heading:
        return '<p class="preview-empty">Heading not found.</p>', 404
    relations = db.execute(
        'SELECT sr.relation_type, sh.id AS target_id, sh.heading AS target_heading '
        'FROM subject_relations sr '
        'JOIN subject_headings sh ON sh.id = sr.to_id '
        'WHERE sr.from_id = ? '
        'ORDER BY sr.relation_type, sh.heading COLLATE NOCASE',
        (heading_id,)
    ).fetchall()
    rf = _restricted_filter()
    rf_sql = f' AND {rf}' if rf else ''
    entries = db.execute(
        'SELECT e.id, e.title, e.type, '
        '(SELECT ea.name FROM entry_authors ea WHERE ea.entry_id = e.id '
        ' ORDER BY ea.sort_order, ea.id LIMIT 1) AS first_author '
        'FROM entries e '
        'JOIN entry_subject_headings esh ON esh.entry_id = e.id '
        f'WHERE esh.heading_id = ?{rf_sql} '
        'ORDER BY e.title COLLATE NOCASE LIMIT 10',
        (heading_id,)
    ).fetchall()
    return render_template('subject_preview.html', heading=heading, relations=relations,
                           entries=entries, type_labels=TYPE_LABELS,
                           heading_type_labels=SUBJECT_HEADING_TYPE_LABELS,
                           relation_type_labels=SUBJECT_RELATION_TYPE_LABELS)


@app.route('/subject/<int:heading_id>/edit', methods=['GET', 'POST'])
@require_role('owner', 'cataloguer')
def edit_subject(heading_id):
    db      = get_db()
    heading = db.execute('SELECT * FROM subject_headings WHERE id=?', (heading_id,)).fetchone()
    if not heading:
        flash('Subject heading not found.')
        return redirect(url_for('subjects'))
    if request.method == 'POST':
        heading_text = request.form.get('heading', '').strip()
        if not heading_text:
            flash('Heading text is required.')
        else:
            try:
                db.execute(
                    'UPDATE subject_headings SET heading=?, type=?, scope_note=? WHERE id=?',
                    (
                        heading_text,
                        request.form.get('type', '').strip() or None,
                        request.form.get('scope_note', '').strip() or None,
                        heading_id,
                    )
                )
            except sqlite3.IntegrityError:
                flash(f'A heading "{heading_text}" already exists.')
                relations = db.execute(
                    'SELECT sr.relation_type, sh.id AS target_id, sh.heading AS target_heading '
                    'FROM subject_relations sr JOIN subject_headings sh ON sh.id=sr.to_id '
                    'WHERE sr.from_id=? ORDER BY sr.relation_type, sh.heading COLLATE NOCASE',
                    (heading_id,)
                ).fetchall()
                return render_template('subject_form.html',
                                       heading=heading, relations=relations,
                                       editing=True,
                                       heading_types=SUBJECT_HEADING_TYPES,
                                       relation_types=SUBJECT_FORM_RELATION_TYPES)
            # Replace all relations for this heading
            db.execute('DELETE FROM subject_relations WHERE from_id=?', (heading_id,))
            _save_subject_relations(db, heading_id, request.form)
            _ensure_subdivision_parent(db, heading_id, heading_text)
            _commit(db)
            logger.info('Subject heading updated: id=%d heading=%r', heading_id, heading_text)
            flash('Subject heading updated.')
            return redirect(url_for('subject_detail', heading_id=heading_id))
    relations = db.execute(
        'SELECT sr.relation_type, sh.id AS target_id, sh.heading AS target_heading '
        'FROM subject_relations sr JOIN subject_headings sh ON sh.id=sr.to_id '
        'WHERE sr.from_id=? ORDER BY sr.relation_type, sh.heading COLLATE NOCASE',
        (heading_id,)
    ).fetchall()
    return render_template('subject_form.html',
                           heading=heading, relations=relations,
                           editing=True,
                           heading_types=SUBJECT_HEADING_TYPES,
                           relation_types=SUBJECT_FORM_RELATION_TYPES)


def _save_subject_relations(db, heading_id, form):
    """Parse relation rows from a submitted form and insert them.
    Each row is encoded as rel_type_N and rel_target_N fields.
    Also removes the old reciprocals before inserting new ones (edit path calls
    DELETE FROM subject_relations WHERE from_id=? before calling this).
    """
    valid_types = set(dict(SUBJECT_RELATION_TYPES))
    i = 0
    while True:
        rel_type = form.get(f'rel_type_{i}', '').strip().upper()
        target   = form.get(f'rel_target_{i}', '').strip()
        i += 1
        if rel_type == '' and target == '':
            if i > 50:
                break
            continue
        if i > 50:
            break
        if not rel_type or not target:
            continue
        if rel_type not in valid_types:
            continue
        target_id = _get_or_create_heading(db, target)
        _add_relation(db, heading_id, rel_type, target_id)


@app.route('/subject/<int:heading_id>/delete', methods=['GET', 'POST'])
@require_role('owner', 'cataloguer')
def delete_subject(heading_id):
    db      = get_db()
    heading = db.execute('SELECT * FROM subject_headings WHERE id=?', (heading_id,)).fetchone()
    if not heading:
        flash('Subject heading not found.')
        return redirect(url_for('subjects'))
    if request.method == 'POST':
        confirm = request.form.get('confirm', '')
        if confirm == 'unlink':
            db.execute('DELETE FROM entry_subject_headings WHERE heading_id=?', (heading_id,))
            db.execute('DELETE FROM subject_headings WHERE id=?', (heading_id,))
            _commit(db)
            logger.info('Subject heading deleted: id=%d heading=%r', heading_id, heading['heading'])
            flash(f'Heading "{heading["heading"]}" deleted and unlinked from all entries.')
            return redirect(url_for('subjects'))
        # Any other value or missing confirm → cancel
        return redirect(url_for('subject_detail', heading_id=heading_id))
    entry_count = db.execute(
        'SELECT COUNT(*) FROM entry_subject_headings WHERE heading_id=?', (heading_id,)
    ).fetchone()[0]
    return render_template('subject_delete_confirm.html',
                           heading=heading, entry_count=entry_count)


@app.route('/api/subjects')
def api_subjects():
    _require_subject_access()
    q  = request.args.get('q', '').strip()
    db = get_db()
    if q:
        rows = db.execute(
            'SELECT id, heading, type FROM subject_headings '
            'WHERE heading LIKE ? ORDER BY heading COLLATE NOCASE LIMIT 20',
            (f'%{q}%',)
        ).fetchall()
    else:
        rows = db.execute(
            'SELECT id, heading, type FROM subject_headings '
            'ORDER BY heading COLLATE NOCASE LIMIT 20'
        ).fetchall()
    result = []
    for r in rows:
        # Check if it's a non-preferred form (has USE relation)
        use_rel = db.execute(
            'SELECT sh.heading AS preferred FROM subject_relations sr '
            'JOIN subject_headings sh ON sh.id = sr.to_id '
            'WHERE sr.from_id=? AND sr.relation_type=?',
            (r['id'], 'USE')
        ).fetchone()
        result.append({
            'id':        r['id'],
            'heading':   r['heading'],
            'type':      r['type'],
            'preferred': use_rel['preferred'] if use_rel else None,
        })
    return jsonify(result)


# ---------------------------------------------------------------------------
# Routes — Libraries
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    db = get_db()
    if not request.args.get('all'):
        primary = db.execute(
            'SELECT id FROM libraries WHERE is_primary = 1 LIMIT 1'
        ).fetchone()
        if primary:
            return redirect(url_for('library', library_id=primary['id']))
    rows = db.execute(
        'SELECT l.*, COUNT(DISTINCT v.entry_id) AS entry_count '
        'FROM libraries l LEFT JOIN volumes v ON v.library_id = l.id '
        'GROUP BY l.id ORDER BY l.name'
    ).fetchall()
    return render_template('index.html', libraries=rows)


@app.route('/library/new', methods=['GET', 'POST'])
@require_role('owner')
def new_library():
    if request.method == 'POST':
        name       = request.form.get('name', '').strip()
        is_primary = 1 if request.form.get('is_primary') else 0
        is_storage = 1 if request.form.get('is_storage') else 0
        if not name:
            flash('Library name is required.')
            return render_template('new_library.html')
        try:
            db = get_db()
            if is_primary:
                db.execute('UPDATE libraries SET is_primary = 0')
            db.execute(
                'INSERT INTO libraries (name, is_primary, is_storage) VALUES (?, ?, ?)',
                (name, is_primary, is_storage)
            )
            _commit(db)
            flash(f'Library "{name}" created.')
            return redirect(url_for('index'))
        except sqlite3.IntegrityError:
            flash(f'A library named "{name}" already exists.')
    return render_template('new_library.html')


@app.route('/library/<int:library_id>/edit', methods=['POST'])
@require_role('owner')
def edit_library(library_id):
    db  = get_db()
    lib = db.execute('SELECT * FROM libraries WHERE id = ?', (library_id,)).fetchone()
    if not lib:
        flash('Library not found.')
        return redirect(url_for('index', all=1))
    is_primary = 1 if request.form.get('is_primary') else 0
    is_storage = 1 if request.form.get('is_storage') else 0
    if is_primary:
        db.execute('UPDATE libraries SET is_primary = 0')
    db.execute(
        'UPDATE libraries SET name=?, is_primary=?, is_storage=? WHERE id=?',
        (
            request.form.get('name', lib['name']).strip() or lib['name'],
            is_primary,
            is_storage,
            library_id,
        )
    )
    _commit(db)
    flash('Library settings updated.')
    return redirect(url_for('library', library_id=library_id))


@app.route('/library/<int:library_id>/delete', methods=['POST'])
@require_role('owner')
def delete_library(library_id):
    db  = get_db()
    lib = db.execute('SELECT name FROM libraries WHERE id = ?', (library_id,)).fetchone()
    if lib:
        db.execute('DELETE FROM libraries WHERE id = ?', (library_id,))
        _commit(db)
        flash(f'Library \"{lib["name"]}\" deleted.')
    return redirect(url_for('index'))


@app.route('/library/<int:library_id>')
def library(library_id):
    db  = get_db()
    lib = db.execute('SELECT * FROM libraries WHERE id = ?', (library_id,)).fetchone()
    if not lib:
        flash('Library not found.')
        return redirect(url_for('index'))
    sort = request.args.get('sort', 'title').strip()
    if sort not in _SORT_ORDER_BY:
        sort = 'title'
    order_by = _SORT_ORDER_BY[sort]
    if sort == 'title':
        # library view default: group by type first, then title within each group
        order_by = 'e.type, ' + order_by
    rf = _restricted_filter()
    rf_sql = f' AND {rf}' if rf else ''
    entries = db.execute(
        'SELECT e.*, COUNT(v.id) AS volume_count, '
        '(SELECT COUNT(*) FROM digital_resources dr WHERE dr.entry_id = e.id) AS digital_count, '
        '(SELECT v2.locus_call_number '
        ' FROM volumes v2 WHERE v2.entry_id = e.id AND v2.library_id = ? '
        ' ORDER BY CAST(v2.volume_number AS INTEGER), v2.volume_number LIMIT 1) AS first_call_number '
        'FROM entries e JOIN volumes v ON v.entry_id = e.id '
        f'WHERE v.library_id = ?{rf_sql} '
        f'GROUP BY e.id ORDER BY {order_by}',
        (library_id, library_id)
    ).fetchall()
    open_settings = bool(request.args.get('settings'))
    return render_template('library.html', library=lib, entries=entries,
                           type_labels=TYPE_LABELS, open_settings=open_settings,
                           sort=sort, sort_options=SORT_OPTIONS)


# ---------------------------------------------------------------------------
# Routes — Entries
# ---------------------------------------------------------------------------

@app.route('/entry/new', methods=['GET', 'POST'])
@require_role('owner', 'cataloguer')
def new_entry():
    db = get_db()

    if request.method == 'POST':
        etype = request.form.get('type', '').strip()
        if not etype:
            flash('Entry type is required.')
            return render_template('entry_form.html',
                                   entry_types=ENTRY_TYPES,
                                   types_with_pub_location=TYPES_WITH_PUB_LOCATION,
                                   hint_library_id=request.form.get('hint_library_id', ''))

        year = _parse_year(request.form.get('year', ''))

        cur = db.execute(
            'INSERT INTO entries '
            '(type, title, subtitle, year, language, publisher, publisher_location, isbn, issn, series, '
            'publication, pub_volume, pub_issue, pages, book_title, locus_code, '
            'notes, condition, acquisition_date, acquisition_source, '
            'conference_name, conference_date, conference_location, '
            'original_year, original_publisher, edition, series_number, abstract, restricted) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                etype,
                request.form.get('title', '').strip()               or None,
                request.form.get('subtitle', '').strip()            or None,
                year,
                request.form.get('language', '').strip()            or None,
                request.form.get('publisher', '').strip()           or None,
                request.form.get('publisher_location', '').strip()  or None,
                request.form.get('isbn', '').strip()                or None,
                request.form.get('issn', '').strip()                or None,
                request.form.get('series', '').strip()              or None,
                request.form.get('publication', '').strip()         or None,
                request.form.get('pub_volume', '').strip()          or None,
                request.form.get('pub_issue', '').strip()           or None,
                request.form.get('pages', '').strip()               or None,
                request.form.get('book_title', '').strip()          or None,
                request.form.get('locus_code', '').strip()          or None,
                request.form.get('notes', '').strip()               or None,
                request.form.get('condition', '').strip()           or None,
                request.form.get('acquisition_date', '').strip()    or None,
                request.form.get('acquisition_source', '').strip()  or None,
                request.form.get('conference_name', '').strip()     or None,
                request.form.get('conference_date', '').strip()     or None,
                request.form.get('conference_location', '').strip() or None,
                request.form.get('original_year', '').strip()       or None,
                request.form.get('original_publisher', '').strip()  or None,
                request.form.get('edition', '').strip()             or None,
                request.form.get('series_number', '').strip()       or None,
                request.form.get('abstract', '').strip()            or None,
                request.form.get('restricted', 'unrestricted'),
            )
        )
        entry_id = cur.lastrowid
        subject_names = [s for s in request.form.get('subject', '').split(',') if s.strip()]
        redirects = _set_entry_subjects(db, entry_id, subject_names)
        for orig, pref in redirects:
            flash(f'"{orig}" is a non-preferred form — replaced with "{pref["heading"]}".')
            logger.debug('Entry %d: subject redirect %r → %r', entry_id, orig, pref['heading'])
        _commit(db)
        logger.info('Entry created: id=%d type=%s title=%r', entry_id, etype, request.form.get('title', '').strip())
        flash('Entry added. Add a volume below to assign it to a library.')
        hint = request.form.get('hint_library_id', '').strip()
        return redirect(url_for('entry_detail', entry_id=entry_id,
                                default_library=hint if hint else None))

    hint_library_id = request.args.get('library_id', '')
    return render_template('entry_form.html',
                           entry_types=ENTRY_TYPES,
                           types_with_pub_location=TYPES_WITH_PUB_LOCATION,
                           hint_library_id=hint_library_id)


@app.route('/entry/<int:entry_id>')
def entry_detail(entry_id):
    db    = get_db()
    entry = db.execute('SELECT * FROM entries WHERE id = ?', (entry_id,)).fetchone()
    if not entry:
        flash('Entry not found.')
        return redirect(url_for('index'))
    # Access control based on restricted field
    patron = g.get('patron')
    role = patron['role'] if patron else None
    if entry['restricted'] == 'hidden' and role not in ('owner', 'cataloguer'):
        abort(403)
    elif entry['restricted'] == 'restricted' and role not in ('owner', 'cataloguer', 'regular', 'restricted'):
        abort(403)
    volumes = db.execute(
        'SELECT v.*, l.name AS library_name, '
        'COALESCE(l.is_storage, 0) AS library_is_storage '
        'FROM volumes v '
        'LEFT JOIN libraries l ON l.id = v.library_id '
        'WHERE v.entry_id = ? '
        'ORDER BY CAST(v.volume_number AS INTEGER), v.volume_number, v.date',
        (entry_id,)
    ).fetchall()
    locations = db.execute(
        'SELECT DISTINCT l.* FROM libraries l '
        'JOIN volumes v ON v.library_id = l.id '
        'WHERE v.entry_id = ? ORDER BY l.name',
        (entry_id,)
    ).fetchall()
    libraries        = db.execute('SELECT * FROM libraries ORDER BY name').fetchall()
    storage_lib_ids  = [l['id'] for l in libraries if l['is_storage']]
    digital_resources = db.execute(
        'SELECT * FROM digital_resources WHERE entry_id = ? ORDER BY resource_type, id',
        (entry_id,)
    ).fetchall()
    contributors = db.execute(
        'SELECT * FROM entry_authors WHERE entry_id = ? ORDER BY sort_order, id',
        (entry_id,)
    ).fetchall()
    subject_headings = db.execute(
        'SELECT sh.* FROM subject_headings sh '
        'JOIN entry_subject_headings esh ON esh.heading_id = sh.id '
        'WHERE esh.entry_id = ? ORDER BY sh.heading COLLATE NOCASE',
        (entry_id,)
    ).fetchall()
    default_library  = request.args.get('default_library', '')
    citation         = generate_citation(entry, volumes, contributors)
    return render_template('entry_detail.html', entry=entry, volumes=volumes,
                           locations=locations, libraries=libraries,
                           storage_lib_ids=storage_lib_ids,
                           digital_resources=digital_resources,
                           digital_resource_types=DIGITAL_RESOURCE_TYPES,
                           digital_resource_type_labels=DIGITAL_RESOURCE_TYPE_LABELS,
                           default_library=default_library,
                           contributors=contributors,
                           contributor_roles=CONTRIBUTOR_ROLES,
                           subject_headings=subject_headings,
                           citation=citation, type_labels=TYPE_LABELS)


@app.route('/entry/<int:entry_id>/edit', methods=['GET', 'POST'])
@require_role('owner', 'cataloguer')
def edit_entry(entry_id):
    db    = get_db()
    entry = db.execute('SELECT * FROM entries WHERE id = ?', (entry_id,)).fetchone()
    if not entry:
        flash('Entry not found.')
        return redirect(url_for('index'))
    if request.method == 'POST':
        etype = request.form.get('type', '').strip()
        year  = _parse_year(request.form.get('year', ''))

        db.execute(
            'UPDATE entries SET type=?, title=?, subtitle=?, year=?, '
            'language=?, publisher=?, publisher_location=?, isbn=?, issn=?, series=?, '
            'publication=?, pub_volume=?, pub_issue=?, pages=?, book_title=?, locus_code=?, '
            'notes=?, condition=?, acquisition_date=?, acquisition_source=?, '
            'conference_name=?, conference_date=?, conference_location=?, '
            'original_year=?, original_publisher=?, edition=?, series_number=?, abstract=?, restricted=? WHERE id=?',
            (
                etype,
                request.form.get('title', '').strip()               or None,
                request.form.get('subtitle', '').strip()            or None,
                year,
                request.form.get('language', '').strip()            or None,
                request.form.get('publisher', '').strip()           or None,
                request.form.get('publisher_location', '').strip()  or None,
                request.form.get('isbn', '').strip()                or None,
                request.form.get('issn', '').strip()                or None,
                request.form.get('series', '').strip()              or None,
                request.form.get('publication', '').strip()         or None,
                request.form.get('pub_volume', '').strip()          or None,
                request.form.get('pub_issue', '').strip()           or None,
                request.form.get('pages', '').strip()               or None,
                request.form.get('book_title', '').strip()          or None,
                request.form.get('locus_code', '').strip()          or None,
                request.form.get('notes', '').strip()               or None,
                request.form.get('condition', '').strip()           or None,
                request.form.get('acquisition_date', '').strip()    or None,
                request.form.get('acquisition_source', '').strip()  or None,
                request.form.get('conference_name', '').strip()     or None,
                request.form.get('conference_date', '').strip()     or None,
                request.form.get('conference_location', '').strip() or None,
                request.form.get('original_year', '').strip()       or None,
                request.form.get('original_publisher', '').strip()  or None,
                request.form.get('edition', '').strip()             or None,
                request.form.get('series_number', '').strip()       or None,
                request.form.get('abstract', '').strip()            or None,
                request.form.get('restricted', 'unrestricted'),
                entry_id,
            )
        )
        subject_names = [s for s in request.form.get('subject', '').split(',') if s.strip()]
        redirects = _set_entry_subjects(db, entry_id, subject_names)
        for orig, pref in redirects:
            flash(f'"{orig}" is a non-preferred form — replaced with "{pref["heading"]}".')
            logger.debug('Entry %d: subject redirect %r → %r', entry_id, orig, pref['heading'])
        _commit(db)
        logger.info('Entry updated: id=%d', entry_id)
        flash('Entry updated.')
        return redirect(url_for('entry_detail', entry_id=entry_id))

    subject_headings = db.execute(
        'SELECT sh.* FROM subject_headings sh '
        'JOIN entry_subject_headings esh ON esh.heading_id = sh.id '
        'WHERE esh.entry_id = ? ORDER BY sh.heading COLLATE NOCASE',
        (entry_id,)
    ).fetchall()
    return render_template('entry_form.html',
                           entry_types=ENTRY_TYPES,
                           types_with_pub_location=TYPES_WITH_PUB_LOCATION,
                           entry=entry, editing=True,
                           subject_headings=subject_headings)


@app.route('/entry/<int:entry_id>/delete', methods=['POST'])
@require_role('owner', 'cataloguer')
def delete_entry(entry_id):
    db = get_db()
    if db.execute('SELECT id FROM entries WHERE id = ?', (entry_id,)).fetchone():
        db.execute('DELETE FROM entries WHERE id = ?', (entry_id,))
        _commit(db)
        logger.info('Entry deleted: id=%d', entry_id)
        flash('Entry deleted.')
    return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# Routes — Contributors
# ---------------------------------------------------------------------------

@app.route('/entry/<int:entry_id>/contributor/add', methods=['POST'])
@require_role('owner', 'cataloguer')
def add_contributor(entry_id):
    db = get_db()
    if not db.execute('SELECT id FROM entries WHERE id = ?', (entry_id,)).fetchone():
        flash('Entry not found.')
        return redirect(url_for('index'))
    name = request.form.get('name', '').strip()
    role = request.form.get('role', 'author').strip()
    if not name:
        flash('Contributor name is required.')
        return redirect(url_for('entry_detail', entry_id=entry_id))
    if role not in dict(CONTRIBUTOR_ROLES):
        role = 'author'
    next_order = db.execute(
        'SELECT COALESCE(MAX(sort_order), -1) + 1 FROM entry_authors WHERE entry_id = ?',
        (entry_id,)
    ).fetchone()[0]
    db.execute(
        'INSERT INTO entry_authors (entry_id, name, role, sort_order) VALUES (?, ?, ?, ?)',
        (entry_id, name, role, next_order)
    )
    _sync_author_field(db, entry_id)
    _commit(db)
    return redirect(url_for('entry_detail', entry_id=entry_id))


@app.route('/contributor/<int:contributor_id>/delete', methods=['POST'])
@require_role('owner', 'cataloguer')
def delete_contributor(contributor_id):
    db = get_db()
    row = db.execute('SELECT entry_id FROM entry_authors WHERE id = ?', (contributor_id,)).fetchone()
    if row:
        entry_id = row['entry_id']
        db.execute('DELETE FROM entry_authors WHERE id = ?', (contributor_id,))
        _sync_author_field(db, entry_id)
        _commit(db)
        return redirect(url_for('entry_detail', entry_id=entry_id))
    return redirect(url_for('index'))


@app.route('/contributor/<int:contributor_id>/edit', methods=['POST'])
@require_role('owner', 'cataloguer')
def edit_contributor(contributor_id):
    db = get_db()
    row = db.execute('SELECT * FROM entry_authors WHERE id = ?', (contributor_id,)).fetchone()
    if not row:
        return redirect(url_for('index'))
    entry_id = row['entry_id']
    name = request.form.get('name', '').strip() or row['name']
    role = request.form.get('role', row['role']).strip()
    if role not in dict(CONTRIBUTOR_ROLES):
        role = row['role']
    db.execute('UPDATE entry_authors SET name = ?, role = ? WHERE id = ?',
               (name, role, contributor_id))
    _sync_author_field(db, entry_id)
    _commit(db)
    return redirect(url_for('entry_detail', entry_id=entry_id))


@app.route('/entry/<int:entry_id>/contributor/reorder', methods=['POST'])
@require_role('owner', 'cataloguer')
def reorder_contributors(entry_id):
    """Accept a posted list of contributor ids in the desired order."""
    db = get_db()
    ids = request.form.getlist('order[]')
    for i, cid_str in enumerate(ids):
        if cid_str.isdigit():
            db.execute(
                'UPDATE entry_authors SET sort_order = ? WHERE id = ? AND entry_id = ?',
                (i, int(cid_str), entry_id)
            )
    _sync_author_field(db, entry_id)
    _commit(db)
    return redirect(url_for('entry_detail', entry_id=entry_id))


# ---------------------------------------------------------------------------
# Routes — Volumes
# ---------------------------------------------------------------------------

@app.route('/entry/<int:entry_id>/volume/new', methods=['POST'])
@require_role('owner', 'cataloguer')
def add_volume(entry_id):
    db = get_db()
    lib_id_str  = request.form.get('library_id', '').strip()
    checked_out = 1 if request.form.get('checked_out') else 0
    is_oversize = 1 if request.form.get('is_oversize') else 0
    is_missing  = 1 if request.form.get('is_missing')  else 0
    is_fragile  = 1 if request.form.get('is_fragile')  else 0
    db.execute(
        'INSERT INTO volumes (entry_id, library_id, barcode, lcc_number, locus_call_number, '
        '                    date, volume_number, checked_out, box_number, '
        '                    is_oversize, is_missing, is_fragile) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            entry_id,
            int(lib_id_str) if lib_id_str.isdigit() else None,
            request.form.get('barcode',           '').strip() or None,
            request.form.get('lcc_number',        '').strip() or None,
            request.form.get('locus_call_number', '').strip() or None,
            request.form.get('date',              '').strip() or None,
            request.form.get('volume_number',     '').strip() or None,
            checked_out,
            request.form.get('box_number',        '').strip() or None,
            is_oversize, is_missing, is_fragile,
        )
    )
    _commit(db)
    logger.info('Volume added: entry_id=%d', entry_id)
    flash('Volume added.')
    return redirect(url_for('entry_detail', entry_id=entry_id))


@app.route('/volume/<int:volume_id>/edit', methods=['POST'])
@require_role('owner', 'cataloguer')
def edit_volume(volume_id):
    db  = get_db()
    vol = db.execute('SELECT entry_id FROM volumes WHERE id = ?', (volume_id,)).fetchone()
    if vol:
        lib_id_str  = request.form.get('library_id', '').strip()
        checked_out = 1 if request.form.get('checked_out') else 0
        is_oversize = 1 if request.form.get('is_oversize') else 0
        is_missing  = 1 if request.form.get('is_missing')  else 0
        is_fragile  = 1 if request.form.get('is_fragile')  else 0
        db.execute(
            'UPDATE volumes SET library_id=?, volume_number=?, lcc_number=?, locus_call_number=?, '
            '                  barcode=?, date=?, checked_out=?, box_number=?, '
            '                  is_oversize=?, is_missing=?, is_fragile=?, '
            '                  phys_dimensions=?, phys_weight=?, phys_pages=?, '
            '                  phys_binding=?, phys_color=?, phys_material=?, phys_notes=? '
            'WHERE id=?',
            (
                int(lib_id_str) if lib_id_str.isdigit() else None,
                request.form.get('volume_number',     '').strip() or None,
                request.form.get('lcc_number',        '').strip() or None,
                request.form.get('locus_call_number', '').strip() or None,
                request.form.get('barcode',           '').strip() or None,
                request.form.get('date',              '').strip() or None,
                checked_out,
                request.form.get('box_number',        '').strip() or None,
                is_oversize, is_missing, is_fragile,
                request.form.get('phys_dimensions',   '').strip() or None,
                request.form.get('phys_weight',       '').strip() or None,
                request.form.get('phys_pages',        '').strip() or None,
                request.form.get('phys_binding',      '').strip() or None,
                request.form.get('phys_color',        '').strip() or None,
                request.form.get('phys_material',     '').strip() or None,
                request.form.get('phys_notes',        '').strip() or None,
                volume_id,
            )
        )
        _commit(db)
        logger.info('Volume updated: id=%d', volume_id)
        flash('Volume updated.')
        return redirect(url_for('entry_detail', entry_id=vol['entry_id']))
    return redirect(url_for('index'))


@app.route('/volume/<int:volume_id>/delete', methods=['POST'])
@require_role('owner', 'cataloguer')
def delete_volume(volume_id):
    db  = get_db()
    vol = db.execute('SELECT entry_id FROM volumes WHERE id = ?', (volume_id,)).fetchone()
    if vol:
        entry_id = vol['entry_id']
        db.execute('DELETE FROM volumes WHERE id = ?', (volume_id,))
        _commit(db)
        logger.info('Volume deleted: id=%d entry_id=%d', volume_id, entry_id)
        flash('Volume deleted.')
        return redirect(url_for('entry_detail', entry_id=entry_id))
    return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# Routes — Digital Resources
# ---------------------------------------------------------------------------

@app.route('/entry/<int:entry_id>/digital/new', methods=['POST'])
@require_role('owner', 'cataloguer')
def add_digital_resource(entry_id):
    db = get_db()
    db.execute(
        'INSERT INTO digital_resources (entry_id, url, call_number, resource_type) VALUES (?, ?, ?, ?)',
        (
            entry_id,
            request.form.get('url',           '').strip() or None,
            request.form.get('call_number',   '').strip() or None,
            request.form.get('resource_type', 'full_text').strip(),
        )
    )
    _commit(db)
    flash('Digital resource added.')
    return redirect(url_for('entry_detail', entry_id=entry_id))


@app.route('/digital/<int:dr_id>/edit', methods=['POST'])
@require_role('owner', 'cataloguer')
def edit_digital_resource(dr_id):
    db = get_db()
    dr = db.execute('SELECT entry_id FROM digital_resources WHERE id = ?', (dr_id,)).fetchone()
    if dr:
        db.execute(
            'UPDATE digital_resources SET url=?, call_number=?, resource_type=? WHERE id=?',
            (
                request.form.get('url',           '').strip() or None,
                request.form.get('call_number',   '').strip() or None,
                request.form.get('resource_type', 'full_text').strip(),
                dr_id,
            )
        )
        _commit(db)
        flash('Digital resource updated.')
        return redirect(url_for('entry_detail', entry_id=dr['entry_id']))
    return redirect(url_for('index'))


@app.route('/digital/<int:dr_id>/delete', methods=['POST'])
@require_role('owner', 'cataloguer')
def delete_digital_resource(dr_id):
    db = get_db()
    dr = db.execute('SELECT entry_id FROM digital_resources WHERE id = ?', (dr_id,)).fetchone()
    if dr:
        entry_id = dr['entry_id']
        db.execute('DELETE FROM digital_resources WHERE id = ?', (dr_id,))
        _commit(db)
        flash('Digital resource deleted.')
        return redirect(url_for('entry_detail', entry_id=entry_id))
    return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# Routes — Management
# ---------------------------------------------------------------------------

@app.route('/manage')
@require_role('owner', 'cataloguer')
def manage():
    db = get_db()
    libraries = db.execute(
        'SELECT l.*, COUNT(DISTINCT v.entry_id) AS entry_count '
        'FROM libraries l LEFT JOIN volumes v ON v.library_id = l.id '
        'GROUP BY l.id ORDER BY l.name'
    ).fetchall()

    numeric_barcodes = [
        row[0] for row in db.execute(
            "SELECT CAST(barcode AS INTEGER) FROM volumes "
            "WHERE barcode GLOB '[0-9]*' ORDER BY CAST(barcode AS INTEGER)"
        ).fetchall()
    ]

    if numeric_barcodes:
        next_highest = numeric_barcodes[-1] + 1
        expected = 10
        next_unused = None
        for b in numeric_barcodes:
            if b > expected:
                next_unused = expected
                break
            if b == expected:
                expected += 1
        if next_unused is None:
            next_unused = expected
    else:
        next_highest = 10
        next_unused  = 10

    patrons = db.execute(
        'SELECT * FROM patrons ORDER BY patron_number'
    ).fetchall()

    return render_template('manage.html', libraries=libraries,
                           next_unused=next_unused, next_highest=next_highest,
                           patrons=patrons, patron_roles=PATRON_ROLES,
                           patron_role_labels=PATRON_ROLE_LABELS)


# ---------------------------------------------------------------------------
# Routes — Barcode check
# ---------------------------------------------------------------------------

@app.route('/manage/barcode-check')
@require_role('owner', 'cataloguer')
def barcode_check():
    db = get_db()

    dup_barcodes = db.execute(
        """
        SELECT barcode, COUNT(*) AS cnt
        FROM volumes
        WHERE barcode IS NOT NULL AND barcode != ''
        GROUP BY barcode
        HAVING COUNT(*) > 1
        ORDER BY barcode
        """
    ).fetchall()

    unbarcoded = db.execute(
        """
        SELECT v.id AS volume_id, v.volume_number,
               l.name AS library_name,
               e.id AS entry_id, e.title, e.author, e.year
        FROM volumes v
        JOIN entries e ON v.entry_id = e.id
        LEFT JOIN libraries l ON v.library_id = l.id
        WHERE v.barcode IS NULL OR v.barcode = ''
        ORDER BY e.title
        """
    ).fetchall()

    if not dup_barcodes and not unbarcoded:
        flash('No barcode issues found.')
        return redirect(url_for('manage'))

    # Fetch full details for each duplicated barcode
    duplicates = []
    for row in dup_barcodes:
        vols = db.execute(
            """
            SELECT v.id AS volume_id, v.barcode, v.volume_number,
                   l.name AS library_name,
                   e.id AS entry_id, e.title, e.author, e.year
            FROM volumes v
            JOIN entries e ON v.entry_id = e.id
            LEFT JOIN libraries l ON v.library_id = l.id
            WHERE v.barcode = ?
            ORDER BY v.id
            """,
            (row['barcode'],)
        ).fetchall()
        duplicates.append({'barcode': row['barcode'], 'volumes': vols})

    return render_template('barcode_check.html', duplicates=duplicates,
                           unbarcoded=unbarcoded)


# ---------------------------------------------------------------------------
# Routes — Patrons
# ---------------------------------------------------------------------------

@app.route('/manage/patron/add', methods=['POST'])
@require_role('owner')
def add_patron():
    db    = get_db()
    name  = request.form.get('name', '').strip()
    role  = request.form.get('role', 'regular').strip()
    email = request.form.get('email', '').strip() or None
    password = request.form.get('password', '').strip()

    if not name:
        flash('Patron name is required.')
        return redirect(url_for('manage'))

    valid_roles = {r for r, _ in PATRON_ROLES}
    if role not in valid_roles:
        role = 'regular'

    if role == 'owner':
        existing_owner = db.execute(
            "SELECT id FROM patrons WHERE role = 'owner'"
        ).fetchone()
        if existing_owner:
            flash('An Owner already exists. There can only be one Owner.')
            return redirect(url_for('manage'))

    if not password:
        flash('A password is required.')
        return redirect(url_for('manage'))

    # Assign next patron number (4-digit zero-padded)
    row = db.execute(
        "SELECT MAX(CAST(patron_number AS INTEGER)) FROM patrons"
    ).fetchone()
    next_num = (row[0] or 0) + 1
    patron_number = f'{next_num:04d}'

    pw_hash = generate_password_hash(password)
    db.execute(
        'INSERT INTO patrons (patron_number, name, email, role, password_hash) VALUES (?, ?, ?, ?, ?)',
        (patron_number, name, email, role, pw_hash)
    )
    _commit(db)
    flash(f'Patron {patron_number} — {name} added.')
    return redirect(url_for('manage'))


@app.route('/manage/patron/<int:patron_id>/delete', methods=['POST'])
@require_role('owner')
def delete_patron(patron_id):
    db = get_db()
    patron = db.execute('SELECT * FROM patrons WHERE id = ?', (patron_id,)).fetchone()
    if patron:
        db.execute('DELETE FROM patrons WHERE id = ?', (patron_id,))
        _commit(db)
        flash(f'Patron {patron["patron_number"]} — {patron["name"]} deleted.')
    return redirect(url_for('manage'))


# ---------------------------------------------------------------------------
# Routes — Auth
# ---------------------------------------------------------------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        patron_number = request.form.get('patron_number', '').strip()
        password      = request.form.get('password', '')
        db = get_db()
        patron = db.execute(
            'SELECT * FROM patrons WHERE patron_number = ?', (patron_number,)
        ).fetchone()
        if patron and patron['password_hash'] and check_password_hash(patron['password_hash'], password):
            session.clear()
            session['patron_id'] = patron['id']
            logger.info('Login: patron %s (%s) authenticated', patron['patron_number'], patron['name'])
            flash(f'Welcome, {patron["name"]}.')
            return redirect(request.form.get('next') or url_for('index'))
        logger.warning('Login: failed attempt for patron number %r', patron_number)
        flash('Invalid patron number or password.')
    return render_template('login.html', next=request.args.get('next', ''))


@app.route('/logout', methods=['POST'])
def logout():
    patron = g.get('patron')
    if patron:
        logger.info('Logout: patron %s (%s)', patron['patron_number'], patron['name'])
    session.clear()
    flash('You have been logged out.')
    return redirect(url_for('index'))


@app.route('/manage/patron/<int:patron_id>/set-password', methods=['POST'])
@require_role('owner')
def set_patron_password(patron_id):
    db = get_db()
    patron = db.execute('SELECT * FROM patrons WHERE id = ?', (patron_id,)).fetchone()
    if not patron:
        flash('Patron not found.')
        return redirect(url_for('manage'))
    password = request.form.get('password', '').strip()
    if len(password) < 4:
        flash('Password must be at least 4 characters.')
        return redirect(url_for('manage'))
    db.execute(
        'UPDATE patrons SET password_hash = ? WHERE id = ?',
        (generate_password_hash(password), patron_id)
    )
    _commit(db)
    flash(f'Password updated for {patron["name"]}.')
    return redirect(url_for('manage'))


# ---------------------------------------------------------------------------
# Routes — Checkout desk
# ---------------------------------------------------------------------------

def _checkout_bin():
    """Return the current bin as a list of volume ids (ints) from the session."""
    return session.get('checkout_bin', [])


def _bin_volumes(db, bin_ids):
    """Fetch full volume+entry info for all ids in the bin, preserving order."""
    if not bin_ids:
        return []
    placeholders = ','.join('?' * len(bin_ids))
    rows = db.execute(
        f'SELECT v.id, v.barcode, v.locus_call_number, v.checked_out, v.checked_out_to, '
        f'       v.library_id, v.box_number, '
        f'       e.title, e.author, '
        f'       l.name AS library_name, COALESCE(l.is_storage, 0) AS library_is_storage '
        f'FROM volumes v '
        f'JOIN entries e ON e.id = v.entry_id '
        f'LEFT JOIN libraries l ON l.id = v.library_id '
        f'WHERE v.id IN ({placeholders})',
        bin_ids
    ).fetchall()
    # re-order to match bin insertion order
    order = {vid: i for i, vid in enumerate(bin_ids)}
    return sorted(rows, key=lambda r: order[r['id']])


@app.route('/checkout')
@require_role('owner', 'cataloguer')
def checkout_desk():
    db       = get_db()
    bin_ids  = _checkout_bin()
    volumes  = _bin_volumes(db, bin_ids)
    libraries = db.execute('SELECT * FROM libraries ORDER BY name').fetchall()
    storage_lib_ids = [l['id'] for l in libraries if l['is_storage']]
    return render_template('checkout.html',
                           volumes=volumes,
                           libraries=libraries,
                           storage_lib_ids=storage_lib_ids)


@app.route('/checkout/scan', methods=['POST'])
@require_role('owner', 'cataloguer')
def checkout_scan():
    barcode = request.form.get('barcode', '').strip()
    if not barcode:
        flash('Please enter a barcode.')
        return redirect(url_for('checkout_desk'))

    db  = get_db()
    vol = db.execute(
        'SELECT v.id, e.title FROM volumes v '
        'JOIN entries e ON e.id = v.entry_id '
        'WHERE v.barcode = ?',
        (barcode,)
    ).fetchone()

    if not vol:
        flash(f'No volume found with barcode {barcode}.')
        return redirect(url_for('checkout_desk'))

    bin_ids = _checkout_bin()
    if vol['id'] in bin_ids:
        flash(f'"{vol["title"]}" is already in the bin.')
    else:
        bin_ids.append(vol['id'])
        session['checkout_bin'] = bin_ids
        flash(f'Added: {vol["title"]}')
    return redirect(url_for('checkout_desk'))


@app.route('/checkout/remove/<int:volume_id>', methods=['POST'])
@require_role('owner', 'cataloguer')
def checkout_remove(volume_id):
    bin_ids = _checkout_bin()
    if volume_id in bin_ids:
        bin_ids.remove(volume_id)
        session['checkout_bin'] = bin_ids
    return redirect(url_for('checkout_desk'))


@app.route('/checkout/clear', methods=['POST'])
@require_role('owner', 'cataloguer')
def checkout_clear():
    session.pop('checkout_bin', None)
    return redirect(url_for('checkout_desk'))


@app.route('/checkout/do-checkout', methods=['POST'])
@require_role('owner', 'cataloguer')
def checkout_do_checkout():
    patron_number = request.form.get('patron_number', '').strip()
    db      = get_db()
    bin_ids = _checkout_bin()

    if not bin_ids:
        flash('The bin is empty.')
        return redirect(url_for('checkout_desk'))

    if not patron_number:
        flash('Patron number is required to check out.')
        return redirect(url_for('checkout_desk'))

    patron = db.execute(
        'SELECT * FROM patrons WHERE patron_number = ?', (patron_number,)
    ).fetchone()

    if not patron:
        flash(f'No patron found with number {patron_number}.')
        return redirect(url_for('checkout_desk'))

    allowed_roles = {'owner', 'cataloguer', 'regular', 'restricted'}
    if patron['role'] not in allowed_roles:
        flash(f'Patron {patron_number} ({patron["name"]}) has role "{patron["role"]}" and is not permitted to borrow items.')
        return redirect(url_for('checkout_desk'))

    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    for vid in bin_ids:
        db.execute(
            'UPDATE volumes SET checked_out = 1, checked_out_to = ? WHERE id = ?',
            (patron['id'], vid)
        )
        db.execute(
            'INSERT INTO checkouts (volume_id, patron_id, checked_out_at) VALUES (?, ?, ?)',
            (vid, patron['id'], now)
        )

    _commit(db)
    session.pop('checkout_bin', None)
    logger.info('Checkout: %d volume(s) checked out to patron %s (%s)', len(bin_ids), patron_number, patron['name'])
    flash(f'{len(bin_ids)} volume(s) checked out to {patron["name"]} ({patron_number}).')
    return redirect(url_for('checkout_desk'))


@app.route('/checkout/do-checkin', methods=['POST'])
@require_role('owner', 'cataloguer')
def checkout_do_checkin():
    db      = get_db()
    bin_ids = _checkout_bin()

    if not bin_ids:
        flash('The bin is empty.')
        return redirect(url_for('checkout_desk'))

    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    checked_in = 0
    for vid in bin_ids:
        vol = db.execute('SELECT checked_out, checked_out_to FROM volumes WHERE id = ?', (vid,)).fetchone()
        if vol and vol['checked_out']:
            db.execute(
                'UPDATE volumes SET checked_out = 0, checked_out_to = NULL WHERE id = ?',
                (vid,)
            )
            # close the most recent open checkout record for this volume
            db.execute(
                'UPDATE checkouts SET returned_at = ? '
                'WHERE id = ('
                '  SELECT id FROM checkouts '
                '  WHERE volume_id = ? AND returned_at IS NULL '
                '  ORDER BY checked_out_at DESC LIMIT 1'
                ')',
                (now, vid)
            )
            checked_in += 1

    _commit(db)
    session.pop('checkout_bin', None)
    not_checked_out = len(bin_ids) - checked_in
    logger.info('Checkin: %d volume(s) checked in (%d skipped — not checked out)', checked_in, not_checked_out)
    msg = f'{checked_in} volume(s) checked in.'
    if not_checked_out:
        msg += f' ({not_checked_out} were not marked as checked out and were ignored.)'
    flash(msg)
    return redirect(url_for('checkout_desk'))


@app.route('/checkout/do-transfer', methods=['POST'])
@require_role('owner', 'cataloguer')
def checkout_do_transfer():
    db      = get_db()
    bin_ids = _checkout_bin()

    if not bin_ids:
        flash('The bin is empty.')
        return redirect(url_for('checkout_desk'))

    lib_id_str = request.form.get('library_id', '').strip()
    if not lib_id_str or not lib_id_str.isdigit():
        flash('Please select a destination library.')
        return redirect(url_for('checkout_desk'))

    lib_id  = int(lib_id_str)
    library = db.execute('SELECT * FROM libraries WHERE id = ?', (lib_id,)).fetchone()
    if not library:
        flash('Library not found.')
        return redirect(url_for('checkout_desk'))

    box_number = None
    if library['is_storage']:
        box_number = request.form.get('box_number', '').strip() or None
        if not box_number:
            flash('Box number is required when transferring to a storage library.')
            return redirect(url_for('checkout_desk'))

    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    for vid in bin_ids:
        vol = db.execute('SELECT checked_out FROM volumes WHERE id = ?', (vid,)).fetchone()
        # If checked out, auto-check in before transferring
        if vol and vol['checked_out']:
            db.execute(
                'UPDATE volumes SET checked_out = 0, checked_out_to = NULL WHERE id = ?',
                (vid,)
            )
            db.execute(
                'UPDATE checkouts SET returned_at = ? '
                'WHERE id = ('
                '  SELECT id FROM checkouts '
                '  WHERE volume_id = ? AND returned_at IS NULL '
                '  ORDER BY checked_out_at DESC LIMIT 1'
                ')',
                (now, vid)
            )
        db.execute(
            'UPDATE volumes SET library_id = ?, box_number = ? WHERE id = ?',
            (lib_id, box_number, vid)
        )

    _commit(db)
    session.pop('checkout_bin', None)
    logger.info('Transfer: %d volume(s) transferred to library %d (%s)', len(bin_ids), lib_id, library['name'])
    flash(f'{len(bin_ids)} volume(s) transferred to {library["name"]}.')
    return redirect(url_for('checkout_desk'))


# ---------------------------------------------------------------------------
# Routes — Checkout record
# ---------------------------------------------------------------------------

@app.route('/checkouts')
@require_role('owner', 'cataloguer')
def checkouts_overview():
    db = get_db()
    checked_out = db.execute(
        '''SELECT v.id AS volume_id, v.barcode, v.volume_number,
                  e.id AS entry_id, e.title, e.type,
                  p.id AS patron_id, p.name AS patron_name, p.patron_number,
                  l.name AS library_name,
                  c.checked_out_at AS checkout_time
           FROM volumes v
           JOIN entries  e ON e.id = v.entry_id
           JOIN patrons  p ON p.id = v.checked_out_to
           LEFT JOIN libraries l ON l.id = v.library_id
           LEFT JOIN checkouts c ON c.id = (
               SELECT id FROM checkouts
               WHERE volume_id = v.id AND returned_at IS NULL
               ORDER BY checked_out_at DESC LIMIT 1
           )
           WHERE v.checked_out = 1
           ORDER BY c.checked_out_at DESC'''
    ).fetchall()
    return render_template('checkouts_overview.html', checked_out=checked_out)


@app.route('/patron/<int:patron_id>/checkouts')
@require_role('owner', 'cataloguer')
def patron_checkout_history(patron_id):
    db = get_db()
    patron = db.execute('SELECT * FROM patrons WHERE id = ?', (patron_id,)).fetchone()
    if not patron:
        flash('Patron not found.')
        return redirect(url_for('manage'))
    history = db.execute(
        '''SELECT c.id, c.checked_out_at, c.returned_at,
                  v.id AS volume_id, v.barcode, v.volume_number,
                  e.id AS entry_id, e.title, e.type,
                  l.name AS library_name
           FROM checkouts c
           JOIN volumes  v ON v.id = c.volume_id
           JOIN entries  e ON e.id = v.entry_id
           LEFT JOIN libraries l ON l.id = v.library_id
           WHERE c.patron_id = ?
           ORDER BY c.checked_out_at DESC''',
        (patron_id,)
    ).fetchall()
    return render_template('patron_checkouts.html', patron=patron, history=history)


# ---------------------------------------------------------------------------
# Routes — Audit
# ---------------------------------------------------------------------------

@app.route('/audit')
@require_role('owner', 'cataloguer')
def audit():
    db = get_db()
    libraries = db.execute('SELECT * FROM libraries ORDER BY name').fetchall()
    return render_template('audit.html', libraries=libraries)


@app.route('/audit/start', methods=['POST'])
@require_role('owner', 'cataloguer')
def audit_start():
    library_id = request.form.get('library_id', '').strip()
    if not library_id or not library_id.isdigit():
        flash('Please select a library.')
        return redirect(url_for('audit'))
    library_id = int(library_id)
    db  = get_db()
    lib = db.execute('SELECT id FROM libraries WHERE id = ?', (library_id,)).fetchone()
    if not lib:
        flash('Library not found.')
        return redirect(url_for('audit'))
    db.execute('UPDATE volumes SET audit_scanned = 0 WHERE library_id = ?', (library_id,))
    _commit(db)
    return redirect(url_for('audit_session', library_id=library_id))


@app.route('/audit/<int:library_id>')
@require_role('owner', 'cataloguer')
def audit_session(library_id):
    db  = get_db()
    lib = db.execute('SELECT * FROM libraries WHERE id = ?', (library_id,)).fetchone()
    if not lib:
        flash('Library not found.')
        return redirect(url_for('audit'))
    total   = db.execute(
        'SELECT COUNT(*) FROM volumes WHERE library_id = ? AND barcode IS NOT NULL',
        (library_id,)
    ).fetchone()[0]
    scanned = db.execute(
        'SELECT COUNT(*) FROM volumes WHERE library_id = ? AND barcode IS NOT NULL AND audit_scanned = 1',
        (library_id,)
    ).fetchone()[0]
    no_barcode = db.execute(
        'SELECT COUNT(*) FROM volumes WHERE library_id = ? AND barcode IS NULL',
        (library_id,)
    ).fetchone()[0]
    return render_template('audit_session.html', library=lib,
                           total=total, scanned=scanned, no_barcode=no_barcode)


@app.route('/audit/<int:library_id>/scan', methods=['POST'])
@require_role('owner', 'cataloguer')
def audit_scan(library_id):
    barcode = request.form.get('barcode', '').strip()
    if not barcode:
        return jsonify({'found': False, 'error': 'No barcode entered.'})

    db  = get_db()
    vol = db.execute(
        'SELECT v.*, e.title, e.author, e.type FROM volumes v '
        'JOIN entries e ON e.id = v.entry_id '
        'WHERE v.barcode = ? AND v.library_id = ?',
        (barcode, library_id)
    ).fetchone()

    if not vol:
        return jsonify({'found': False,
                        'error': f'Barcode "{barcode}" not found in this library.'})

    already_scanned = bool(vol['audit_scanned'])
    if not already_scanned:
        db.execute('UPDATE volumes SET audit_scanned = 1 WHERE id = ?', (vol['id'],))
        _commit(db)

    total   = db.execute(
        'SELECT COUNT(*) FROM volumes WHERE library_id = ? AND barcode IS NOT NULL',
        (library_id,)
    ).fetchone()[0]
    scanned = db.execute(
        'SELECT COUNT(*) FROM volumes WHERE library_id = ? AND barcode IS NOT NULL AND audit_scanned = 1',
        (library_id,)
    ).fetchone()[0]

    return jsonify({
        'found':           True,
        'already_scanned': already_scanned,
        'complete':        total > 0 and scanned >= total,
        'scanned':         scanned,
        'total':           total,
        'entry': {
            'title':         vol['title'] or '(Untitled)',
            'author':        vol['author'] or '',
            'volume_number': vol['volume_number'] or '',
            'type':          vol['type'],
        },
    })


@app.route('/audit/<int:library_id>/stop', methods=['POST'])
@require_role('owner', 'cataloguer')
def audit_stop(library_id):
    return redirect(url_for('audit_results', library_id=library_id))


@app.route('/audit/<int:library_id>/results')
@require_role('owner', 'cataloguer')
def audit_results(library_id):
    db  = get_db()
    lib = db.execute('SELECT * FROM libraries WHERE id = ?', (library_id,)).fetchone()
    if not lib:
        flash('Library not found.')
        return redirect(url_for('audit'))
    unscanned = db.execute(
        'SELECT v.*, e.title, e.author, e.year, e.type '
        'FROM volumes v JOIN entries e ON e.id = v.entry_id '
        'WHERE v.library_id = ? AND v.barcode IS NOT NULL AND v.audit_scanned = 0 '
        'ORDER BY COALESCE(v.locus_call_number, v.lcc_number) COLLATE NOCASE, e.title COLLATE NOCASE',
        (library_id,)
    ).fetchall()
    no_barcode = db.execute(
        'SELECT v.*, e.title, e.author, e.year, e.type '
        'FROM volumes v JOIN entries e ON e.id = v.entry_id '
        'WHERE v.library_id = ? AND v.barcode IS NULL '
        'ORDER BY COALESCE(v.locus_call_number, v.lcc_number) COLLATE NOCASE, e.title COLLATE NOCASE',
        (library_id,)
    ).fetchall()
    total   = db.execute(
        'SELECT COUNT(*) FROM volumes WHERE library_id = ? AND barcode IS NOT NULL',
        (library_id,)
    ).fetchone()[0]
    scanned = total - len(unscanned)
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    return render_template('audit_results.html', library=lib,
                           unscanned=unscanned, no_barcode=no_barcode,
                           total=total, scanned=scanned,
                           type_labels=TYPE_LABELS, now=now)


# ---------------------------------------------------------------------------
# Routes — Cataloguing API
# ---------------------------------------------------------------------------

_classification_data_cache = None  # reset on import to pick up any new JSON files

def _load_classification_data():
    global _classification_data_cache
    if _classification_data_cache is None:
        def _load(name):
            with open(os.path.join(CLASSIFICATION_DIR, name), encoding='utf-8') as f:
                return json.load(f)
        _classification_data_cache = {
            'regions':         _load('region_codes.json'),
            'domains':         _load('domain_codes.json'),
            'periods':         _load('period_codes.json'),
            'rulings':         _load('editorial_rulings.json'),
            'regional_domains': _load('regional_domain_codes.json'),
        }
    return _classification_data_cache


@app.route('/api/classification/data')
def api_classification_data():
    data = _load_classification_data()
    resp = jsonify(data)
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


@app.route('/api/catalogue/barcode-next')
@require_role('owner', 'cataloguer')
def api_barcode_next():
    db  = get_db()
    row = db.execute(
        "SELECT MAX(CAST(barcode AS INTEGER)) FROM volumes WHERE barcode GLOB '[0-9]*'"
    ).fetchone()
    return jsonify({'next': (row[0] or 0) + 1})


@app.route('/api/catalogue/check-collision')
@require_role('owner', 'cataloguer')
def api_check_collision():
    shelfmark  = request.args.get('shelfmark', '').strip()
    workmark   = request.args.get('workmark', '').strip()
    exclude_id = request.args.get('exclude_id', type=int)
    if not shelfmark or not workmark:
        return jsonify({'collision': False})
    call_number = f'{shelfmark} / {workmark}'
    sql    = 'SELECT id, title, locus_code FROM entries WHERE locus_code = ?'
    params: list[Any] = [call_number]
    if exclude_id:
        sql    += ' AND id != ?'
        params.append(exclude_id)
    row = get_db().execute(sql, params).fetchone()
    if row:
        return jsonify({'collision': True,
                        'entry_id':  row['id'],
                        'title':     row['title'],
                        'locus_code': row['locus_code']})
    return jsonify({'collision': False})


@app.route('/api/catalogue/isbn-lookup', methods=['POST'])
@require_role('owner', 'cataloguer')
def api_isbn_lookup():
    data = request.get_json(force=True)
    isbn = (data.get('isbn') or '').strip().replace('-', '').replace(' ', '')
    if not isbn:
        return jsonify({'error': 'ISBN required'}), 400

    url = f'https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'HomeLibraryCatalog/1.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read().decode('utf-8'))
    except Exception:
        return jsonify({'error': 'Lookup failed'}), 502

    book = result.get(f'ISBN:{isbn}')
    if not book:
        return jsonify({'error': 'Not found'}), 404

    # Title
    title = book.get('title', '')

    # Author: Open Library returns "Forename Surname"; convert to "Surname, Forename"
    author = ''
    authors = book.get('authors', [])
    if authors:
        name  = authors[0].get('name', '').strip()
        parts = name.split()
        if len(parts) >= 2:
            author = f"{parts[-1]}, {' '.join(parts[:-1])}"
        else:
            author = name

    # Year: extract 4-digit year from publish_date string
    year = ''
    year_match = re.search(r'\b(1[0-9]{3}|20[0-9]{2})\b', book.get('publish_date', ''))
    if year_match:
        year = year_match.group(1)

    # Publisher
    publisher = ''
    publishers = book.get('publishers', [])
    if publishers:
        publisher = publishers[0].get('name', '')

    return jsonify({'title': title, 'author': author, 'year': year, 'publisher': publisher})


@app.route('/api/catalogue/unclassified')
@require_role('owner', 'cataloguer')
def api_unclassified():
    rows = get_db().execute(
        "SELECT id, title, author, year FROM entries "
        "WHERE locus_code IS NULL OR locus_code = '' ORDER BY title"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/catalogue/save', methods=['POST'])
@require_role('owner', 'cataloguer')
def api_catalogue_save():
    data       = request.get_json(force=True)
    mode       = data.get('mode', 'new')
    entry_id   = data.get('entry_id')
    locus_code = (data.get('locus_code') or '').strip()
    volumes_in = data.get('volumes', [])

    if not locus_code:
        return jsonify({'error': 'locus_code is required'}), 400

    db = get_db()

    # Assign barcodes server-side for any volume missing one
    row = db.execute(
        "SELECT MAX(CAST(barcode AS INTEGER)) FROM volumes WHERE barcode GLOB '[0-9]*'"
    ).fetchone()
    next_bc = (row[0] or 0) + 1

    assigned_barcodes = []
    for v in volumes_in:
        bc = (v.get('barcode') or '').strip()
        if not bc:
            bc = str(next_bc)
            next_bc += 1
        assigned_barcodes.append(bc)

    if mode == 'new':
        # Create a new entry with the provided metadata
        author    = (data.get('author')    or '').strip() or None
        year_raw  = (data.get('year')      or '').strip()
        title     = (data.get('title')     or '').strip() or None
        publisher = (data.get('publisher') or '').strip() or None
        year      = _parse_year(year_raw)
        cur = db.execute(
            'INSERT INTO entries (type, title, year, publisher, locus_code) '
            'VALUES (?, ?, ?, ?, ?)',
            ('book', title, year, publisher, locus_code)
        )
        entry_id = cur.lastrowid
        if author:
            db.execute(
                'INSERT INTO entry_authors (entry_id, name, role, sort_order) VALUES (?, ?, ?, ?)',
                (entry_id, author, 'author', 0)
            )
            db.execute('UPDATE entries SET author = ? WHERE id = ?', (author, entry_id))
    else:
        if not entry_id:
            return jsonify({'error': 'entry_id required for classify/batch mode'}), 400
        db.execute('UPDATE entries SET locus_code = ? WHERE id = ?', (locus_code, entry_id))

    # Update existing volumes or insert new ones
    for v, bc in zip(volumes_in, assigned_barcodes):
        mark    = (v.get('mark')    or '').strip() or None
        vol_id  = v.get('id')
        if vol_id:
            db.execute(
                'UPDATE volumes SET locus_call_number = ?, volume_number = ?, barcode = ? '
                'WHERE id = ? AND entry_id = ?',
                (locus_code, mark, bc, vol_id, entry_id)
            )
        else:
            db.execute(
                'INSERT INTO volumes (entry_id, locus_call_number, volume_number, barcode) '
                'VALUES (?, ?, ?, ?)',
                (entry_id, locus_code, mark, bc)
            )

    _commit(db)
    return jsonify({'entry_id': entry_id, 'barcodes': assigned_barcodes})


# ---------------------------------------------------------------------------
# Routes — Cataloguing Tool
# ---------------------------------------------------------------------------

@app.route('/cataloguing/new')
@require_role('owner', 'cataloguer')
def cataloguing_new():
    return render_template('cataloguing.html',
                           mode='new', entry=None,
                           queue_pos=None, queue_total=None)


@app.route('/cataloguing/classify/<int:entry_id>')
@require_role('owner', 'cataloguer')
def cataloguing_classify(entry_id):
    db    = get_db()
    entry = db.execute('SELECT * FROM entries WHERE id = ?', (entry_id,)).fetchone()
    if not entry:
        flash('Entry not found.')
        return redirect(url_for('manage'))
    vols = db.execute(
        'SELECT id, volume_number, barcode FROM volumes WHERE entry_id = ? '
        'ORDER BY CAST(volume_number AS INTEGER), volume_number',
        (entry_id,)
    ).fetchall()
    existing_volumes = [
        {'id': v['id'], 'mark': v['volume_number'] or '', 'barcode': v['barcode'] or ''}
        for v in vols
    ]
    return render_template('cataloguing.html',
                           mode='classify', entry=entry,
                           existing_volumes=existing_volumes,
                           queue_pos=None, queue_total=None)


@app.route('/cataloguing/batch')
@require_role('owner', 'cataloguer')
def cataloguing_batch():
    db = get_db()

    # Add current entry to the deferred list if skip requested
    skip_id = request.args.get('skip', type=int)
    if skip_id:
        deferred = list(session.get('batch_skip_ids', []))
        if skip_id not in deferred:
            deferred.append(skip_id)
        session['batch_skip_ids'] = deferred
        session.modified = True

    all_unclassified = db.execute(
        "SELECT * FROM entries WHERE locus_code IS NULL OR locus_code = '' ORDER BY title"
    ).fetchall()

    if not all_unclassified:
        session.pop('batch_skip_ids', None)
        flash('No unclassified entries remaining.')
        return redirect(url_for('manage'))

    skip_list = session.get('batch_skip_ids', [])
    active   = [e for e in all_unclassified if e['id'] not in skip_list]
    deferred = [e for e in all_unclassified if e['id'] in skip_list]

    # All active entries done — reset and process deferred as active
    in_deferred_phase = False
    if not active and deferred:
        session.pop('batch_skip_ids', None)
        session.modified = True
        active            = deferred
        deferred          = []
        in_deferred_phase = True

    entry = active[0]

    vols = db.execute(
        'SELECT id, volume_number, barcode FROM volumes WHERE entry_id = ? '
        'ORDER BY CAST(volume_number AS INTEGER), volume_number',
        (entry['id'],)
    ).fetchall()
    existing_volumes = [
        {'id': v['id'], 'mark': v['volume_number'] or '', 'barcode': v['barcode'] or ''}
        for v in vols
    ]
    return render_template('cataloguing.html',
                           mode='batch', entry=entry,
                           existing_volumes=existing_volumes,
                           queue_remaining=len(all_unclassified),
                           queue_deferred=len(deferred),
                           in_deferred_phase=in_deferred_phase,
                           queue_pos=None, queue_total=len(all_unclassified))


# ---------------------------------------------------------------------------
# Routes — About
# ---------------------------------------------------------------------------

@app.route('/about')
def about():
    db = get_db()
    stats = db.execute(
        'SELECT '
        '  (SELECT COUNT(*) FROM libraries)                                      AS library_count, '
        '  (SELECT COUNT(*) FROM entries)                                        AS entry_count, '
        '  (SELECT COUNT(*) FROM volumes)                                        AS volume_count, '
        '  (SELECT COUNT(*) FROM digital_resources)                              AS digital_count, '
        "  (SELECT COUNT(*) FROM entries WHERE locus_code IS NOT NULL AND locus_code != '') AS locus_count, "
        '  (SELECT COUNT(*) FROM subject_headings)                              AS subject_count'
    ).fetchone()
    return render_template('about.html', stats=stats)


# ---------------------------------------------------------------------------
# Routes — Export
# ---------------------------------------------------------------------------

@app.route('/export')
@require_role('owner', 'cataloguer')
def export_csv():
    db  = get_db()
    now = datetime.now().strftime('%Y%m%d_%H%M%S')

    def rows_to_csv(rows):
        """Serialise a list of sqlite3.Row objects to a UTF-8-encoded CSV bytes."""
        buf = io.StringIO()
        if not rows:
            return buf.getvalue().encode('utf-8')
        writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
        return buf.getvalue().encode('utf-8')

    libraries         = db.execute('SELECT * FROM libraries ORDER BY name').fetchall()
    entries           = db.execute('SELECT * FROM entries   ORDER BY type, title COLLATE NOCASE').fetchall()
    volumes           = db.execute('SELECT * FROM volumes   ORDER BY entry_id, CAST(volume_number AS INTEGER), volume_number').fetchall()
    digital_resources = db.execute('SELECT * FROM digital_resources ORDER BY entry_id, id').fetchall()

    # Joined catalog — one row per volume, all fields in one place
    catalog = db.execute(
        'SELECT '
        '  l.name          AS library, '
        '  e.type, e.title, e.author, e.year, e.language, '
        '  e.publisher, e.publisher_location, '
        '  (SELECT GROUP_CONCAT(sh.heading, ", ") FROM entry_subject_headings esh '
        '   JOIN subject_headings sh ON sh.id = esh.heading_id '
        '   WHERE esh.entry_id = e.id) AS subject, '
        '  e.isbn, e.issn, e.series, '
        '  v.volume_number, v.lcc_number, v.locus_call_number, v.barcode, v.date, '
        '  CASE v.checked_out WHEN 1 THEN "yes" ELSE "no" END AS checked_out '
        'FROM volumes v '
        'JOIN entries e ON e.id = v.entry_id '
        'LEFT JOIN libraries l ON l.id = v.library_id '
        'ORDER BY l.name, e.type, e.title COLLATE NOCASE, '
        '         CAST(v.volume_number AS INTEGER), v.volume_number'
    ).fetchall()

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f'libraries_{now}.csv',         rows_to_csv(libraries))
        zf.writestr(f'entries_{now}.csv',           rows_to_csv(entries))
        zf.writestr(f'volumes_{now}.csv',           rows_to_csv(volumes))
        zf.writestr(f'digital_resources_{now}.csv', rows_to_csv(digital_resources))
        zf.writestr(f'catalog_{now}.csv',           rows_to_csv(catalog))

    zip_buf.seek(0)
    return send_file(
        zip_buf,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'library_export_{now}.zip',
    )


# ---------------------------------------------------------------------------
# Routes — Search
# ---------------------------------------------------------------------------

@app.route('/barcode')
@require_role('owner', 'cataloguer')
def barcode_search():
    q = request.args.get('q', '').strip()
    if q:
        vol = get_db().execute(
            'SELECT entry_id FROM volumes WHERE barcode = ?', (q,)
        ).fetchone()
        if vol:
            return redirect(url_for('entry_detail', entry_id=vol['entry_id']))
        return render_template('barcode_search.html', query=q, not_found=True)
    return render_template('barcode_search.html', query='', not_found=False)


def _build_advanced_where(rows: list) -> tuple:
    """Build a WHERE clause from advanced search rows.

    Each row is a dict with keys 'field', 'op' ('AND'|'OR'|'NOT'), 'term'.
    The first valid row's op is always ignored (treated as the initial term).
    Rows with empty terms are skipped.
    Returns (sql_fragment, params) or (None, []).
    """
    parts: list[str] = []
    params: list     = []
    first = True
    for row in rows:
        term = (row.get('term') or '').strip()
        if not term:
            continue
        field = row.get('field', 'any')
        if field not in _ADV_FIELD_SQL:
            field = 'any'
        op = (row.get('op') or 'AND').upper()
        if op not in ('AND', 'OR', 'NOT'):
            op = 'AND'
        cols = _ADV_FIELD_SQL[field]
        like_term = f'%{term}%'
        sub = ' OR '.join(f'{col} LIKE ?' for col in cols)
        if first:
            parts.append(f'({sub})')
            first = False
        elif op == 'NOT':
            parts.append(f'AND NOT ({sub})')
        elif op == 'OR':
            parts.append(f'OR ({sub})')
        else:
            parts.append(f'AND ({sub})')
        params.extend([like_term] * len(cols))
    if not parts:
        return None, []
    return ' '.join(parts), params


@app.route('/search')
def search():
    q          = request.args.get('q',          '').strip()
    library_id = request.args.get('library_id', '').strip()
    etype      = request.args.get('type',        '').strip()
    adv        = request.args.get('adv',         '0') == '1'

    db         = get_db()
    results    = []
    adv_rows: list[dict] = []

    if adv:
        for i in range(6):
            field = request.args.get(f'field_{i}', '').strip()
            if not field:
                continue
            if field not in _ADV_FIELD_SQL:
                field = 'any'
            op   = request.args.get(f'op_{i}', 'AND').strip().upper()
            if op not in ('AND', 'OR', 'NOT'):
                op = 'AND'
            term = request.args.get(f'term_{i}', '').strip()
            adv_rows.append({'field': field, 'op': op, 'term': term})

    # Build WHERE — only when a user term is present
    conditions: list[str] = []
    params: list[Any]     = []

    if adv:
        adv_clause, adv_params = _build_advanced_where(adv_rows)
        if adv_clause:
            conditions.append(adv_clause)
            params.extend(adv_params)
    elif q:
        all_cols = _ADV_FIELD_SQL['any']
        like_q   = f'%{q}%'
        conditions.append('(' + ' OR '.join(f'{c} LIKE ?' for c in all_cols) + ')')
        params.extend([like_q] * len(all_cols))

    is_searched = bool(conditions)

    if is_searched:
        if library_id and library_id.isdigit():
            conditions.append('v.library_id = ?')
            params.append(int(library_id))
        if etype:
            conditions.append('e.type = ?')
            params.append(etype)
        rf = _restricted_filter()
        if rf:
            conditions.append(rf)
        where = ' AND '.join(conditions)
        sort = request.args.get('sort', 'title').strip()
        if sort not in _SORT_ORDER_BY:
            sort = 'title'
        order_by = _SORT_ORDER_BY[sort]
        results = db.execute(
            'SELECT DISTINCT e.*, '
            'GROUP_CONCAT(DISTINCT l.name) AS library_names, '
            'COUNT(DISTINCT v.id) AS volume_count, '
            'SUM(CASE WHEN v.checked_out = 1 THEN 1 ELSE 0 END) AS checked_out_count, '
            '(SELECT v2.locus_call_number FROM volumes v2 '
            ' WHERE v2.entry_id = e.id '
            ' ORDER BY CAST(v2.volume_number AS INTEGER), v2.volume_number LIMIT 1) AS first_call_number, '
            '(SELECT COUNT(*) FROM digital_resources dr2 WHERE dr2.entry_id = e.id) AS digital_count '
            'FROM entries e '
            'LEFT JOIN volumes v ON v.entry_id = e.id '
            'LEFT JOIN libraries l ON l.id = v.library_id '
            'LEFT JOIN digital_resources dr ON dr.entry_id = e.id '
            f'WHERE {where} GROUP BY e.id ORDER BY {order_by}',
            params,
        ).fetchall()
    else:
        sort = 'title'

    return render_template('search.html', results=results, query=q,
                           selected_library=library_id, selected_type=etype,
                           entry_types=ENTRY_TYPES, type_labels=TYPE_LABELS,
                           adv=adv, adv_rows=adv_rows, adv_fields=ADV_SEARCH_FIELDS,
                           is_searched=is_searched, sort=sort, sort_options=SORT_OPTIONS)


# ---------------------------------------------------------------------------
# LibraryThing import
# ---------------------------------------------------------------------------

LT_IMPORT_PATH = os.path.join(os.path.dirname(__file__), 'librarything.json')


def _parse_lt_entry(raw):
    """Convert one LibraryThing JSON record into our entry + volume data."""
    # Unescape any HTML entities in text fields
    def clean(s):
        return html_module.unescape(s).strip() if s else None

    title = clean(raw.get('title', ''))

    # Determine type: 'journal' when any tag contains the word "journal"
    tags = raw.get('tags') or []
    etype = 'journal' if any('journal' in t.lower() for t in tags) else 'book'

    # Author
    author = clean(raw.get('primaryauthor', ''))

    # Year — use the 'date' field (4-digit string or first 4 chars)
    date_str = str(raw.get('date', '') or '')
    year = date_str[:4] if len(date_str) >= 4 and date_str[:4].isdigit() else None

    # Language — first element of the list
    langs = raw.get('language') or []
    language = langs[0] if langs else None

    # Publisher — strip year/edition suffix from 'publication' field
    pub_field = clean(raw.get('publication', '') or '')
    publisher = None
    if pub_field:
        idx = pub_field.find('(')
        publisher = clean(pub_field[:idx]) if idx > 0 else pub_field

    # ISBN — field may be a dict {"0": "...", "2": "..."} or a list
    isbn_raw = raw.get('isbn')
    isbn = None
    if isinstance(isbn_raw, dict) and isbn_raw:
        isbn = next(iter(isbn_raw.values()))
    elif isinstance(isbn_raw, list) and isbn_raw:
        isbn = isbn_raw[0]

    # Series — list, take first element
    series_raw = raw.get('series')
    series = series_raw[0] if isinstance(series_raw, list) and series_raw else None
    if series:
        series = clean(series)

    # Subject — use user-defined tags (stored as a list for junction table insertion)
    subject = tags  # kept as list; caller passes to _set_entry_subjects

    # Physical volumes count
    try:
        volumes_count = int(raw.get('volumes') or 1)
    except (ValueError, TypeError):
        volumes_count = 1

    # LCC shelfmark
    lcc_raw = raw.get('lcc') or {}
    lcc = clean(lcc_raw.get('code', '')) or None

    return {
        'type':               etype,
        'title':              title or None,
        'author':             author,
        'year':               year,
        'language':           language,
        'publisher':          publisher,
        'publisher_location': None,
        'isbn':               isbn,
        'issn':               None,
        'series':             series,
        'subject_tags':       subject if subject else [],  # list of strings
        'volumes_count':      volumes_count,
        'lcc':                lcc,
    }


@app.route('/import/librarything', methods=['GET', 'POST'])
@require_role('owner', 'cataloguer')
def import_librarything():
    if not os.path.exists(LT_IMPORT_PATH):
        flash('librarything.json was not found in the application directory.')
        return redirect(url_for('index'))

    with open(LT_IMPORT_PATH, encoding='utf-8') as f:
        raw_data = json.load(f)

    db        = get_db()
    libraries = db.execute('SELECT * FROM libraries ORDER BY name').fetchall()

    if request.method == 'POST':
        library_id = request.form.get('library_id', '').strip()
        if not library_id:
            flash('Please select a library.')
            return redirect(url_for('import_librarything'))

        library_id = int(library_id)
        added      = 0
        vols_added = 0
        flagged    = []   # multi-volume entries needing manual attention

        # Sequential barcode assignment — start from the highest existing
        # numeric barcode, with a floor of 9 so the first assigned is >= 10.
        # Barcodes 0-9 are reserved.
        row = db.execute(
            "SELECT MAX(CAST(barcode AS INTEGER)) FROM volumes "
            "WHERE barcode GLOB '[0-9]*'"
        ).fetchone()
        next_barcode = max((row[0] or 0), 9) + 1

        for raw in raw_data.values():
            parsed        = _parse_lt_entry(raw)
            volumes_count = parsed.pop('volumes_count')
            lcc           = parsed.pop('lcc')

            subject_tags = parsed.pop('subject_tags') or []
            cur = db.execute(
                'INSERT INTO entries '
                '(type, title, year, language, '
                'publisher, publisher_location, isbn, issn, series) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    parsed['type'],   parsed['title'],
                    parsed['year'],   parsed['language'], parsed['publisher'],
                    parsed['publisher_location'],
                    parsed['isbn'],   parsed['issn'],
                    parsed['series'],
                )
            )
            entry_id = cur.lastrowid
            added   += 1
            if subject_tags:
                _set_entry_subjects(db, entry_id, subject_tags)
            if parsed['author']:
                db.execute(
                    'INSERT INTO entry_authors (entry_id, name, role, sort_order) VALUES (?, ?, ?, ?)',
                    (entry_id, parsed['author'], 'author', 0)
                )
                db.execute('UPDATE entries SET author = ? WHERE id = ?',
                           (parsed['author'], entry_id))

            if volumes_count == 1:
                db.execute(
                    'INSERT INTO volumes (entry_id, library_id, barcode, lcc_number, volume_number) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (entry_id, library_id, str(next_barcode), lcc, '1')
                )
                next_barcode += 1
                vols_added   += 1
            elif parsed['type'] == 'book':
                # Auto-create numbered volumes for multi-volume books
                for vol_num in range(1, volumes_count + 1):
                    lcc_num = f'{lcc} v. {vol_num}' if lcc else None
                    db.execute(
                        'INSERT INTO volumes (entry_id, library_id, barcode, lcc_number, volume_number) '
                        'VALUES (?, ?, ?, ?, ?)',
                        (entry_id, library_id, str(next_barcode), lcc_num, str(vol_num))
                    )
                    next_barcode += 1
                vols_added += volumes_count
            else:
                flagged.append({
                    'entry_id': entry_id,
                    'title':    parsed['title'] or '(Untitled)',
                    'volumes':  volumes_count,
                    'lcc':      lcc,
                    'type':     parsed['type'],
                })

        highest_barcode = next_barcode - 1
        _commit(db)
        return render_template('import_results.html',
                               added=added, vols_added=vols_added,
                               flagged=flagged,
                               highest_barcode=highest_barcode)

    # GET — show confirmation form with a summary of what will be imported
    parsed_all       = [_parse_lt_entry(v) for v in raw_data.values()]
    single_vol       = [p for p in parsed_all if p['volumes_count'] == 1]
    multi_vol_books  = [p for p in parsed_all if p['volumes_count'] > 1 and p['type'] == 'book']
    multi_vol_other  = [p for p in parsed_all if p['volumes_count'] > 1 and p['type'] != 'book']
    type_counts      = {}
    for p in parsed_all:
        type_counts[p['type']] = type_counts.get(p['type'], 0) + 1

    return render_template('import_lt.html',
                           libraries=libraries,
                           total=len(parsed_all),
                           single_vol_count=len(single_vol),
                           multi_vol_books=multi_vol_books,
                           multi_vol_other=multi_vol_other,
                           type_counts=type_counts,
                           type_labels=TYPE_LABELS)


# ---------------------------------------------------------------------------
# Shelf browse
# ---------------------------------------------------------------------------

@app.route('/shelf-browse')
def shelf_browse():
    db     = get_db()
    prefix = request.args.get('prefix', '').strip().upper()

    # Distinct two-letter prefixes present in the collection, sorted by canonical order
    prefix_rows = db.execute(
        """
        SELECT DISTINCT UPPER(SUBSTR(v.locus_call_number, 1, 2)) AS pfx
        FROM volumes v
        WHERE v.locus_call_number IS NOT NULL
          AND v.locus_call_number != ''
          AND v.locus_call_number GLOB '[A-Za-z][A-Za-z]*'
        """
    ).fetchall()
    _max_rank = len(LOCUS_PREFIX_RANK)
    prefixes = sorted(
        [r['pfx'] for r in prefix_rows],
        key=lambda p: LOCUS_PREFIX_RANK.get(p, _max_rank)
    )

    params = []
    where  = (
        "v.locus_call_number IS NOT NULL "
        "AND v.locus_call_number != '' "
        "AND v.locus_call_number GLOB '[A-Za-z][A-Za-z]*'"
    )
    if prefix and prefix in prefixes:
        where += " AND UPPER(SUBSTR(v.locus_call_number, 1, 2)) = ?"
        params.append(prefix)

    # Fetch all matching volumes; sort in Python using canonical prefix rank
    # then by the remainder of the call number for within-prefix ordering.
    rows = db.execute(
        f"""
        SELECT
            v.id            AS volume_id,
            v.locus_call_number,
            v.volume_number,
            l.name          AS library_name,
            e.id            AS entry_id,
            e.title,
            e.author,
            e.year,
            e.type
        FROM volumes v
        JOIN entries  e ON v.entry_id   = e.id
        LEFT JOIN libraries l ON v.library_id = l.id
        WHERE {where}
        """,
        params
    ).fetchall()

    def _vol_sort_key(v):
        cn = v['locus_call_number'] or ''
        pfx = cn[:2].upper()
        return (LOCUS_PREFIX_RANK.get(pfx, _max_rank), cn.upper())

    volumes = sorted(rows, key=_vol_sort_key)

    return render_template(
        'shelf_browse.html',
        volumes=volumes,
        prefixes=prefixes,
        active_prefix=prefix,
        type_labels=TYPE_LABELS,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    _host  = _cfg.get    ('server', 'host',  fallback='127.0.0.1')
    _port  = _cfg.getint ('server', 'port',  fallback=5000)
    _debug = _cfg.getboolean('server', 'debug', fallback=True)

    logger.info('Starting Home Library Catalog...')
    if not os.path.exists(DATABASE):
        logger.info('Database not found — initialising from schema...')
        init_db()
    else:
        logger.info('Existing database found — running migrations...')
        with app.app_context():
            migrate_db()
    logger.info('Ready — starting Flask development server on %s:%d (debug=%s)', _host, _port, _debug)
    app.run(host=_host, port=_port, debug=_debug)
