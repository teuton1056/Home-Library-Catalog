CREATE TABLE IF NOT EXISTS patrons (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    patron_number  TEXT    NOT NULL UNIQUE,
    name           TEXT    NOT NULL,
    email          TEXT,
    password_hash  TEXT,
    role           TEXT    NOT NULL DEFAULT 'regular'
                   CHECK(role IN ('owner', 'cataloguer', 'regular', 'restricted', 'guest'))
);

CREATE TABLE IF NOT EXISTS libraries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    is_primary INTEGER NOT NULL DEFAULT 0,
    is_storage INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS entries (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    type               TEXT NOT NULL CHECK(type IN (
                           'book', 'journal', 'map', 'pamphlet',
                           'dvd', 'cd', 'vhs',
                           'conference_proceedings', 'government_document',
                           'journal_article', 'book_section'
                       )),
    title              TEXT,
    author             TEXT,
    year               TEXT,
    language           TEXT,
    publisher          TEXT,
    publisher_location TEXT,
    subject            TEXT,
    isbn               TEXT,
    issn               TEXT,
    series             TEXT,
    publication        TEXT,
    pub_volume         TEXT,
    pub_issue          TEXT,
    pages              TEXT,
    book_title         TEXT,
    locus_code         TEXT,
    subtitle           TEXT,
    notes              TEXT,
    condition          TEXT,
    acquisition_date    TEXT,
    acquisition_source  TEXT,
    conference_name     TEXT,
    conference_date     TEXT,
    conference_location TEXT,
    original_year       TEXT,
    original_publisher  TEXT,
    edition             TEXT,
    series_number       TEXT,
    abstract            TEXT,
    restricted          TEXT NOT NULL DEFAULT 'unrestricted'
                        CHECK(restricted IN ('unrestricted', 'restricted', 'hidden'))
);



CREATE TABLE IF NOT EXISTS digital_resources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id      INTEGER NOT NULL,
    url           TEXT,
    call_number   TEXT,
    resource_type TEXT NOT NULL DEFAULT 'full_text'
                  CHECK(resource_type IN ('full_text', 'toc')),
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS volumes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id      INTEGER NOT NULL,
    library_id    INTEGER,
    barcode           TEXT,
    lcc_number        TEXT,
    locus_call_number TEXT,
    date              TEXT,
    volume_number     TEXT,
    checked_out       INTEGER NOT NULL DEFAULT 0,
    box_number        TEXT,
    audit_scanned     INTEGER NOT NULL DEFAULT 0,
    is_oversize       INTEGER NOT NULL DEFAULT 0,
    is_missing        INTEGER NOT NULL DEFAULT 0,
    is_fragile        INTEGER NOT NULL DEFAULT 0,
    checked_out_to    INTEGER,
    phys_dimensions   TEXT,
    phys_weight       TEXT,
    phys_pages        TEXT,
    phys_binding      TEXT,
    phys_color        TEXT,
    phys_material     TEXT,
    phys_notes        TEXT,
    FOREIGN KEY (entry_id)      REFERENCES entries(id)   ON DELETE CASCADE,
    FOREIGN KEY (library_id)    REFERENCES libraries(id) ON DELETE SET NULL,
    FOREIGN KEY (checked_out_to) REFERENCES patrons(id)  ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS entry_authors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    role        TEXT    NOT NULL DEFAULT 'author',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (entry_id) REFERENCES entries(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS checkouts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id       INTEGER NOT NULL,
    patron_id       INTEGER NOT NULL,
    checked_out_at  TEXT    NOT NULL,
    returned_at     TEXT,
    FOREIGN KEY (volume_id) REFERENCES volumes(id) ON DELETE CASCADE,
    FOREIGN KEY (patron_id) REFERENCES patrons(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS subject_headings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    heading    TEXT    NOT NULL UNIQUE,
    type       TEXT,
    scope_note TEXT
);

CREATE TABLE IF NOT EXISTS subject_relations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id       INTEGER NOT NULL,
    relation_type TEXT    NOT NULL
                  CHECK(relation_type IN ('USE','UF','BT','NT','RT','SA','SE','PARENT','CHILD')),
    to_id         INTEGER NOT NULL,
    UNIQUE(from_id, relation_type, to_id),
    FOREIGN KEY (from_id) REFERENCES subject_headings(id) ON DELETE CASCADE,
    FOREIGN KEY (to_id)   REFERENCES subject_headings(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS entry_subject_headings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id   INTEGER NOT NULL,
    heading_id INTEGER NOT NULL,
    UNIQUE(entry_id, heading_id),
    FOREIGN KEY (entry_id)   REFERENCES entries(id)          ON DELETE CASCADE,
    FOREIGN KEY (heading_id) REFERENCES subject_headings(id) ON DELETE CASCADE
);
