# Trail Names — build notes, 2026-10-03

Two features: **duplicate trail names allowed** (email is now the unique
identity) and **passwordless self-editing** via emailed links.

## What changed

### `app.py`
- **Schema**: `hikers.trail_name` no longer `UNIQUE`; new column
  `edit_token TEXT DEFAULT ''`. The hikers table definition lives in one
  helper (`_hikers_table_sql`) shared by the schema and the migration so
  they can't drift.
- **Startup migration** (`_migrate_hikers_drop_unique_name`, runs in
  `init_db()` on every boot): if the live table still has the old UNIQUE
  constraint, it rebuilds the table inside one transaction — new table,
  `INSERT ... SELECT` with every column named (row **ids preserved
  exactly**, so `/hiker/<id>` URLs and the AUTOINCREMENT sequence
  survive), drop/rename, index recreated. Detected via `sqlite_master`,
  so it fires exactly once and is a no-op on every later boot and on
  fresh installs. Also adds `trail_custom`/`edit_token` columns first if
  an older DB lacks them.
- **Email index** (`_ensure_email_index`): adds a case-insensitive
  UNIQUE index on `email` when existing rows are clean; if legacy rows
  already share an email it logs a warning and skips the index instead
  of failing startup. Enforcement of one-entry-per-email happens in the
  register flow either way (index is defense-in-depth; insert races fall
  back to the same friendly error).
- **Register**: shared profile-field parsing/validation extracted to
  `_profile_form_data()` (identical rules for register and edit).
  - Duplicate trail names register freely.
  - Email already attached to a **verified** entry → friendly block:
    "This email already has an entry in the directory." plus a link to
    the edit page (case-insensitive match).
  - Email attached only to an **unverified** entry → the row is
    refreshed in place (same id, new form data, fresh verify token) and
    the confirmation email is resent — a lost confirmation email no
    longer locks anyone out.
- **Edit my entry**:
  - `GET/POST /edit` — request a link by email. Response is always the
    same "If that email has an entry…" page (hit, miss, and
    unverified-only all look identical; nothing sent on a miss).
    Honeypot + rate limit (5/hour/IP), same patterns as the contact form.
  - The email carries `{SITE_URL}/edit/<token>`; a single email lists
    one link per entry if an address somehow has several.
  - `GET/POST /edit/<token>` — prefilled form, all registration fields
    editable **except email** (shown read-only with a "contact the site
    to change it" note). Validates via `_profile_form_data()`; on save,
    flashes "Your entry has been updated." and redirects to the profile.
    Tokens are persistent: regenerated only when a new link is requested.
- **Contact relay**: "To trail name: X" now reads e.g.
  `To trail name: Wanderer (Pacific Crest Trail · John Muir Trail · 2022, 2025) — entry #7`
  so duplicate names stay unambiguous.
- **Search**: `ORDER BY trail_name COLLATE NOCASE, id` (stable order
  among same-name entries).
- **Seed** (`/admin/seed`): explicit name-existence check instead of
  relying on `IntegrityError` — a name anyone already has is skipped, so
  seeds never duplicate real entries.

### Templates
- `edit_request.html`, `edit_sent.html`, `edit.html` — new.
- `base.html` — "Edit my entry" link in the footer nav.
- `about.html` — the "Trail names are unique here…" line replaced:
  duplicates welcomed, disambiguation explained, edit link pointed out.
- `register.html` — trail-name hint ("sharing a name with another hiker
  is fine") and a duplicate-email block linking to the edit page.

## Tests performed (all local, throwaway DBs, nothing on Fly touched)

Env: Python 3.12 venv, Flask 3.1; two instances — one demo-mode (no
SMTP, mail → outbox), one against a local SMTP sink with email
verification required.

**Migration (old-schema DBs)** — *pass*
- Built old-schema DBs (UNIQUE trail_name; one with, one without
  `trail_custom`) with rows at ids 5/9/42, mixed verified/unverified,
  including a pending verify token. App boot migrated both:
  UNIQUE gone, `edit_token`/`trail_custom` added, every id and value
  preserved (verified flag and pending token included).
- Post-migration, next registered row got id 43 — AUTOINCREMENT intact.
- Restart on the migrated DB: migration is a no-op, app serves normally,
  no `hikers_new` leftovers.

**Flows** — *pass*
- Register duplicate trail name, different email → allowed; second entry
  verifies and both appear in search (with their trails/years/location).
- Register with an already-verified email (different case) → 400 with
  the friendly message + edit-page link; no new row created.
- Unverified re-register → same row id, fields replaced, verify token
  rotated; old verify link 404s, new one verifies; profile shows the
  refreshed data.
- Edit link: unknown email and unverified-only email → generic page, no
  mail sent. Known email → link emailed (verified via sink), form opens
  prefilled, invalid submission (bad years) → 400 with the validation
  error, valid save → 302 to profile, flash shown, profile reflects the
  change. Bad token → 404. Honeypot submission → generic page, no mail.
  6th request within the hour → 429.
- Contact relay mail body shows
  `To trail name: Wanderer (Pacific Crest Trail · John Muir Trail · 2022, 2025) — entry #1`.
- Footer shows "Edit my entry"; About page shows the new copy;
  `/admin` loads; duplicate profiles render independently.

## Decisions worth knowing
- Email uniqueness is enforced **in code**, with the DB unique index
  added only when legacy data is clean. If the index was skipped you'd
  see a "Duplicate hiker emails present" warning in the app log at boot —
  worth a glance after deploy.
- Changing a contact email is intentionally not self-serve yet: the edit
  form says to contact the site. (Building re-verification for email
  changes is a reasonable v2.)
- Edit tokens don't expire; requesting a new link rotates the token.

## Deploy (Mateo runs this)
1. Unzip `trail-names.zip`, `cd trail-names`.
2. `fly deploy`
3. That's it — the DB migration runs automatically on first boot.
   Existing entries, ids, and profile URLs are untouched; entries still
   unverified keep their pending state (their original verify links
   were replaced only if they re-register).
4. Optional sanity check after deploy: load the homepage, open one
   existing profile, and try the footer "Edit my entry" with your own
   registered address.
