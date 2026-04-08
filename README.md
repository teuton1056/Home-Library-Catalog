# Home Library Catalog Webapp

A Python/Flask web application for cataloguing a personal library across multiple physical collections, with a controlled vocabulary subject index, patron management, and a circulation desk.

## Features

### Multiple Collections
Organize holdings into named libraries (e.g. "Home Library", "Office", "Storage"). Collections can be marked as **Primary** (opens by default on the home page) or **Storage** (exposes a Box Number field and shows a distinct amber indicator on volumes). Library management — creating, configuring, and deleting libraries — is restricted to the owner account and is accessed from the **Library Management** page.

### Entry Types
Eleven entry types are supported, each with type-appropriate fields:

| Type | Additional Fields |
|---|---|
| Book | Publisher, Publisher Location, ISBN, ISSN, Series, Series Number, Edition, Original Year, Original Publisher |
| Book Section | Book Title, Pages, Publisher, Publisher Location, ISBN, Series |
| Journal | Publisher, ISSN |
| Journal Article | Publication, Volume, Issue, Pages, ISSN |
| Map | Publisher, Publisher Location |
| Pamphlet | Publisher, Publisher Location |
| DVD | — |
| CD | — |
| VHS Tape | — |
| Conference Proceedings | Conference Name, Conference Date, Conference Location, Publisher, Publisher Location |
| Government Document | Publisher, Publisher Location |

All entries also carry: Title, Subtitle, Year, Language, Notes, Condition, Acquisition Date, Acquisition Source, Abstract, and a **LOCUS Code** field. A **Restricted** field controls visibility per entry (see Access Control below). Subject access points are linked controlled vocabulary headings rather than free-text tags.

**Contributors** are tracked as a separate structured list on each entry — any number of people, each with an explicit role (Author, Editor, Translator, Compiler, Illustrator, Contributor). Contributors can be reordered and edited independently from the entry detail page, and feed directly into the Chicago citation generator.

### Volumes & Copies
Each catalog entry is a pure bibliographic record. Physical copies are tracked separately as **volumes**, which belong to a specific library. One entry can have copies in multiple collections with no duplication of bibliographic data.

Each volume records:
- **Library** — which collection it lives in
- **LCC Number** — Library of Congress call number
- **LOCUS Call Number** — LOCUS system call number
- **Barcode**
- **Volume Number** — for multi-volume works
- **Date** — acquisition or edition date
- **Box Number** — for volumes held in storage boxes
- **Oversize / Missing / Fragile** — condition flags
- **Physical Description** — optional collapsible panel with Dimensions, Weight, Pages, Binding, Colour, Material, and Notes

### Digital Resources
Digital copies are tracked alongside physical volumes. Each digital resource records a URL (`https://` or `zotero://`), an optional call number, and a type of either *Full Text* or *Table of Contents*.

### Subject Headings
Subject access is managed through a controlled vocabulary modelled on the LOCUS subject heading system. Each heading is a distinct record with an optional **type** (Region, People, Person, Topic, Event, Language, or Work), a **scope note**, and structured relations to other headings:

| Relation | Meaning | Reciprocated? |
|---|---|---|
| BT | Broader Term | Yes → NT on target |
| NT | Narrower Term | Yes → BT on target |
| RT | Related Term | Yes → RT on target |
| USE | Preferred form | Yes → UF on target |
| UF | Non-preferred form | Yes → USE on target |
| SA | See Also | No |
| SE | See Entry | No |

Entries may only be tagged with preferred forms. If a non-preferred (USE) heading is entered, the system automatically substitutes the preferred form and notifies the cataloguer. New headings are created on save when a heading text does not yet exist.

The subject browse page and heading detail pages are accessible to Regular patrons and above. Guests and unauthenticated visitors do not have access to the subject index. Creating, editing, and deleting headings is restricted to Owner and Cataloguer accounts.

### Patron Management & Access Control
The system supports multiple named patron accounts, each with one of five roles:

| Role | Permissions |
|---|---|
| **Owner** | Full access: all entries, all management, patron and library administration. Only one owner is permitted. |
| **Cataloguer** | Can view all entries and make all cataloguing changes. Cannot add, remove, or configure libraries or patrons. |
| **Regular** | Browse and search unrestricted and restricted entries; browse the subject index. Cannot make changes. |
| **Restricted** | Same as Regular for now. |
| **Guest** | Browse and search unrestricted entries only. No access to the subject index. |

Unauthenticated visitors have guest-level access. Login is by **patron number** and password (bcrypt-hashed). The owner sets and resets patron passwords from the Manage page.

Entry visibility is controlled per entry by a **Restricted** field:
- **Unrestricted** — visible to everyone including anonymous visitors
- **Restricted** — visible to regular, restricted, cataloguer, and owner roles
- **Hidden** — visible to cataloguer and owner only

### Circulation Desk
The checkout desk (`/checkout`) provides a full circulation workflow for owner and cataloguer accounts:

- **Scan** — add volumes to a bin by barcode
- **Check Out** — assign all binned volumes to a patron by patron number; guests cannot borrow
- **Check In** — mark binned volumes as returned and close their checkout record
- **Transfer** — move binned volumes to a different library; checked-out volumes are automatically checked in

The **On Loan** page (`/checkouts`) lists every currently checked-out item with the borrowing patron and checkout time. Each patron's full checkout history is accessible at `/patron/<id>/checkouts`.

### Physical Audit
Audit mode verifies which volumes are physically present in a given library using a barcode scanner or keyboard input. The tool gives immediate colour-coded feedback (found, already scanned, not found) and tracks progress with a live counter. A printer-friendly report at the end lists all volumes that were not scanned.

### Search
Full-text search across all bibliographic and physical fields: title, author, year, language, publisher, subject headings, series, ISBN, ISSN, LOCUS call number, LOCUS code, barcode, volume number, and date. Results can be filtered by **collection** and **entry type**. An **Advanced Search** mode allows multi-field boolean queries with AND/NOT operators.

**Barcode Scan** mode accepts a single barcode and jumps directly to the matching entry. Designed for use with hardware barcode scanners.

Search results respect the access control level of the logged-in patron; hidden or restricted entries are filtered automatically.

### Citations
A Chicago-style citation is automatically generated for every entry and shown at the top of its detail page.

### Import & Export
- **LibraryThing import** — place `librarything.json` in the application directory. Single-volume entries are imported with their LCC shelfmark and a sequential barcode; multi-volume books are split into numbered volumes; other multi-volume entries are flagged for manual review.
- **CSV export** — exports a ZIP archive containing `libraries.csv`, `entries.csv`, `volumes.csv`, and a fully joined `catalog.csv` with one row per volume.

### Automatic Backups
Every write operation saves a database backup. Short-term backups (last 5 writes) are kept in `backups/short/`; daily backups (last 30 days) in `backups/daily/`.

## Data Model

```
patrons               id, patron_number, name, email, password_hash, role

libraries             id, name, is_primary, is_storage

entries               id, type, title, subtitle, author, year, language,
                      publisher, publisher_location, isbn, issn,
                      series, series_number, edition, publication, pub_volume,
                      pub_issue, pages, book_title, locus_code, notes,
                      condition, acquisition_date, acquisition_source,
                      conference_name, conference_date, conference_location,
                      original_year, original_publisher, abstract, restricted

entry_authors         id, entry_id, name, role, sort_order

volumes               id, entry_id, library_id, barcode, lcc_number,
                      locus_call_number, date, volume_number, checked_out,
                      checked_out_to, box_number, audit_scanned,
                      is_oversize, is_missing, is_fragile,
                      phys_dimensions, phys_weight, phys_pages,
                      phys_binding, phys_color, phys_material, phys_notes

digital_resources     id, entry_id, url, call_number, resource_type

checkouts             id, volume_id, patron_id, checked_out_at, returned_at

subject_headings      id, heading, type, scope_note

subject_relations     id, from_id, relation_type, to_id

entry_subject_headings  id, entry_id, heading_id
```

## Stack

- Python 3 / Flask 3.x
- SQLite 3 (raw `sqlite3` module, no ORM)
- Jinja2 templates
- Plain CSS (no framework)
- Vanilla JavaScript (no build step)

## Running

```bash
pip install flask
python app.py
```

The database (`library.db`) is created automatically on first run. Schema migrations run automatically on startup so existing databases are updated in place. To reinitialise from scratch via the CLI:

```bash
flask init-db
```

## Tests

```bash
pip install pytest pytest-flask
py -m pytest tests/
```

The test suite uses per-test isolated SQLite databases via `tmp_path` and covers authentication, role-based access control, entry/volume CRUD, library management, patron management, checkout desk operations, search, checkout record views, the contributor system (CRUD, reordering, author field sync, migration, citation integration), volume physical descriptions, and the full controlled vocabulary subject heading system (heading CRUD, relation reciprocals, USE resolution, entry tagging with auto-redirect, delete/unlink, browse, search integration, access control, and data migration from legacy free-text tags).

# Roadmap

- A Localization system, allowing users to rename various parts of the app, esp. LOCUS, which is very tightly coupled to the original usecase.
- Improvements to the physical management, especially regarding multiple types of call number or finding systems.
- Improved export capabilities, including various "fancy" PDF exports.
