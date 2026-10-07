#!/usr/bin/env python3
"""
Trail Names — a lightweight directory of hiker trail names.

Through-hikers earn a "trail name" on long trails (AT, PCT, CDT...). This app
lets hikers register their trail name with a hidden contact email, lets
visitors search the directory, and relays contact-form messages by email so
addresses are never exposed.

Stack: Flask + SQLite (stdlib). No build step, no database server.
Everything is configured through environment variables (see README.md).
"""

import os
import re
import secrets
import smtplib
import sqlite3
import time
from email.message import EmailMessage

from flask import (
    Flask, g, render_template, request, redirect, url_for, abort, flash,
)

# --------------------------------------------------------------------------
# Configuration (environment variables, with sane defaults)
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config.update(
    DATABASE=os.environ.get("DATABASE", os.path.join(BASE_DIR, "data", "trailnames.db")),
    SECRET_KEY=os.environ.get("SECRET_KEY", secrets.token_hex(32)),
    ADMIN_TOKEN=os.environ.get("ADMIN_TOKEN", secrets.token_hex(16)),
    SITE_NAME=os.environ.get("SITE_NAME", "Trail Names"),
    SITE_URL=os.environ.get("SITE_URL", "http://localhost:5000").rstrip("/"),
    SMTP_HOST=os.environ.get("SMTP_HOST", ""),
    SMTP_PORT=int(os.environ.get("SMTP_PORT", "587")),
    SMTP_USER=os.environ.get("SMTP_USER", ""),
    SMTP_PASS=os.environ.get("SMTP_PASS", ""),
    SMTP_FROM=os.environ.get("SMTP_FROM", ""),
    SMTP_USE_TLS=os.environ.get("SMTP_USE_TLS", "1") == "1",
)

# Email verification is required when we can actually send mail; otherwise
# entries go live immediately and the admin panel says so.
SMTP_CONFIGURED = bool(app.config["SMTP_HOST"] and app.config["SMTP_FROM"])
REQUIRE_VERIFICATION = (
    os.environ.get("REQUIRE_EMAIL_VERIFICATION", "auto").lower() in ("1", "true", "yes", "auto")
    and SMTP_CONFIGURED
)

# Known long trails offered as checkboxes.
TRAILS = [
    ("AT", "Appalachian Trail"),
    ("PCT", "Pacific Crest Trail"),
    ("CDT", "Continental Divide Trail"),
    ("AZT", "Arizona Trail"),
    ("CT", "Colorado Trail"),
    ("LT", "Long Trail (VT)"),
    ("JMT", "John Muir Trail"),
    ("TA", "Te Araroa (NZ)"),
    ("CAMINO", "Camino de Santiago"),
    ("OTHER", "Other / multiple"),
]
TRAIL_NAMES = dict(TRAILS)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
YEARS_RE = re.compile(r"^[\d,\s\-–/]+$")

# Simple in-memory rate limits: {key: [timestamps]}
_rate_buckets = {}


def rate_limited(key: str, limit: int, window_s: int) -> bool:
    now = time.time()
    bucket = _rate_buckets.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < window_s]
    if len(bucket) >= limit:
        return True
    bucket.append(now)
    return False


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def _hikers_table_sql(table: str) -> str:
    # One definition shared by the main schema and the migration rebuild,
    # so the two can never drift apart. Note: trail_name is deliberately
    # NOT unique — plenty of hikers share a name on the trail. Email is
    # the unique identity (enforced in the register flow, plus a unique
    # index added at startup when existing data is clean).
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trail_name   TEXT NOT NULL COLLATE NOCASE,
    email        TEXT NOT NULL,          -- never displayed publicly
    real_name    TEXT DEFAULT '',
    trails       TEXT DEFAULT '',        -- comma-wrapped codes, e.g. ",AT,PCT,"
    trail_custom TEXT DEFAULT '',        -- free-text trail when OTHER is picked
    years        TEXT DEFAULT '',
    location     TEXT DEFAULT '',
    bio          TEXT DEFAULT '',
    link         TEXT DEFAULT '',
    verified     INTEGER NOT NULL DEFAULT 0,
    verify_token TEXT DEFAULT '',
    edit_token   TEXT DEFAULT '',
    created_at   INTEGER NOT NULL
);"""


# Every hikers column, in table order. The migration's INSERT ... SELECT
# names columns explicitly so row ids (and everything else) survive.
HIKERS_COLUMNS = [
    "id", "trail_name", "email", "real_name", "trails", "trail_custom",
    "years", "location", "bio", "link", "verified", "verify_token",
    "edit_token", "created_at",
]

SCHEMA = _hikers_table_sql("hikers") + """
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    hiker_id     INTEGER NOT NULL REFERENCES hikers(id) ON DELETE CASCADE,
    sender_name  TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    body         TEXT NOT NULL,
    ip           TEXT DEFAULT '',
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hikers_verified ON hikers(verified);
CREATE TABLE IF NOT EXISTS site_settings (
    key   TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);
"""


def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _migrate_hikers_drop_unique_name(db):
    """Rebuild hikers without the old UNIQUE constraint on trail_name.

    SQLite can't drop a column constraint in place, so: copy every row
    (ids included — profile URLs are /hiker/<id>) into a fresh table,
    swap it in, and re-create the index. Runs inside one transaction;
    a no-op once the table no longer carries the constraint, so it is
    safe on every startup.
    """
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'hikers'"
    ).fetchone()
    if not row or not row["sql"] or "UNIQUE" not in row["sql"].upper():
        return  # fresh schema, or already migrated
    cols = ", ".join(HIKERS_COLUMNS)
    db.commit()  # close any open transaction so the FK pragma takes hold
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        db.execute("BEGIN")
        db.execute("DROP TABLE IF EXISTS hikers_new")
        db.execute(_hikers_table_sql("hikers_new"))
        db.execute(f"INSERT INTO hikers_new ({cols}) SELECT {cols} FROM hikers")
        db.execute("DROP TABLE hikers")
        db.execute("ALTER TABLE hikers_new RENAME TO hikers")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_hikers_verified ON hikers(verified)")
        db.execute("COMMIT")
    except Exception:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        db.execute("PRAGMA foreign_keys = ON")
    app.logger.info("Migrated hikers table: trail names are no longer unique.")


def _ensure_email_index(db):
    """Case-insensitive UNIQUE index on email, when existing data allows.

    One entry per email is enforced in the register flow regardless; the
    index is defense-in-depth. If legacy rows already share an email we
    skip the index (and log it) rather than fail startup trying.
    """
    dupe = db.execute(
        "SELECT email FROM hikers GROUP BY email COLLATE NOCASE "
        "HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if dupe:
        app.logger.warning(
            "Duplicate hiker emails present; skipping unique email index.")
        return
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_hikers_email "
        "ON hikers(email COLLATE NOCASE)")


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    # Lightweight migrations for databases created before a column existed.
    for stmt in (
        "ALTER TABLE hikers ADD COLUMN trail_custom TEXT DEFAULT ''",
        "ALTER TABLE hikers ADD COLUMN edit_token TEXT DEFAULT ''",
    ):
        try:
            db.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already present
    db.commit()
    # Structural migrations (no-ops once applied).
    _migrate_hikers_drop_unique_name(db)
    _ensure_email_index(db)
    db.commit()


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def send_email(to: str, subject: str, body: str, reply_to: str = "") -> str:
    """Send an email. Returns 'sent', 'logged' (demo mode), or 'failed'.

    Never raises: a mail-server outage shouldn't break registration/contact.
    """
    if not SMTP_CONFIGURED:
        # Demo mode: write the message where the operator can read it.
        outbox = os.path.join(BASE_DIR, "outbox")
        os.makedirs(outbox, exist_ok=True)
        fname = os.path.join(outbox, f"{int(time.time())}-{secrets.token_hex(4)}.txt")
        with open(fname, "w") as f:
            f.write(f"To: {to}\nSubject: {subject}\nReply-To: {reply_to or '-'}\n\n{body}\n")
        app.logger.info("SMTP not configured; wrote email to %s", fname)
        return "logged"

    try:
        msg = EmailMessage()
        msg["From"] = app.config["SMTP_FROM"]
        msg["To"] = to
        msg["Subject"] = subject
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(body)

        with smtplib.SMTP(app.config["SMTP_HOST"], app.config["SMTP_PORT"]) as s:
            if app.config["SMTP_USE_TLS"]:
                s.starttls()
            if app.config["SMTP_USER"]:
                s.login(app.config["SMTP_USER"], app.config["SMTP_PASS"])
            s.send_message(msg)
        return "sent"
    except Exception as e:  # noqa: BLE001 - report, don't crash the request
        app.logger.error("Email send failed: %s", e)
        return "failed"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def get_hiker(hiker_id: int, public_only: bool = True):
    db = get_db()
    if public_only:
        row = db.execute(
            "SELECT * FROM hikers WHERE id = ? AND verified = 1", (hiker_id,)
        ).fetchone()
    else:
        row = db.execute("SELECT * FROM hikers WHERE id = ?", (hiker_id,)).fetchone()
    return row


def trail_labels(codes: str):
    return [TRAIL_NAMES.get(c, c) for c in codes.strip(",").split(",") if c]


def _profile_form_data():
    """Parse and validate the profile fields shared by register + edit.

    Returns (data, errors). Keeping this in one place means the edit
    form enforces exactly the same rules as registration.
    """
    trail_name = request.form.get("trail_name", "").strip()
    real_name = request.form.get("real_name", "").strip()
    trails = [t for t in request.form.getlist("trails") if t in TRAIL_NAMES]
    trail_custom = request.form.get("trail_custom", "").strip()
    if "OTHER" not in trails:
        trail_custom = ""  # only kept when "Other / multiple" is picked
    years = request.form.get("years", "").strip()
    location = request.form.get("location", "").strip()
    bio = request.form.get("bio", "").strip()
    link = request.form.get("link", "").strip()

    errors = []
    if not trail_name or len(trail_name) > 60:
        errors.append("Trail name is required (max 60 characters).")
    if years and (not YEARS_RE.match(years) or len(years) > 40):
        errors.append("Years should look like '2022' or '2021, 2023'.")
    if link and not re.match(r"^https?://", link):
        errors.append("Link must start with http:// or https://.")
    if len(bio) > 500:
        errors.append("Bio must be under 500 characters.")
    if len(trail_custom) > 120:
        errors.append("Other trail must be under 120 characters.")

    data = {
        "trail_name": trail_name,
        "real_name": real_name,
        "trails_value": "," + ",".join(trails) + "," if trails else "",
        "trail_custom": trail_custom,
        "years": years,
        "location": location,
        "bio": bio,
        "link": link,
    }
    return data, errors


# --------------------------------------------------------------------------
# Site settings (editable content: about text/photo, support link, contact)
# --------------------------------------------------------------------------

UPLOAD_DIR = os.path.join(os.path.dirname(app.config["DATABASE"]), "uploads")

DEFAULT_SETTINGS = {
    "about_text": (
        "Hi, I'm Mateo — a hiker, writer, and web developer based in Massachusetts.\n"
        "\n"
        "I built Trail Names so hikers can find each other after the trail ends. "
        "If we shared miles somewhere out there, I hope you look me up."
    ),
    "about_photo": "/static/about-photo.jpg",
    "support_url": "",
    "contact_email": os.environ.get("CONTACT_EMAIL", "you@example.com"),
}


def get_setting(key: str) -> str:
    db = get_db()
    row = db.execute("SELECT value FROM site_settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key, "")
    return row["value"]


def set_setting(key: str, value: str):
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
        (key, value),
    )
    db.commit()


@app.context_processor
def inject_site_settings():
    return {"support_url": get_setting("support_url")}


def check_admin():
    token = request.args.get("token", "") or request.form.get("token", "")
    if not secrets.compare_digest(token, app.config["ADMIN_TOKEN"]):
        abort(403)


# --------------------------------------------------------------------------
# Public routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    trail = request.args.get("trail", "").strip()
    year = request.args.get("year", "").strip()
    location = request.args.get("location", "").strip()

    sql = "SELECT * FROM hikers WHERE verified = 1"
    params = []
    if q:
        sql += " AND trail_name LIKE ?"
        params.append(f"%{q}%")
    if trail and trail in TRAIL_NAMES:
        sql += " AND trails LIKE ?"
        params.append(f"%,{trail},%")
    if year:
        sql += " AND years LIKE ?"
        params.append(f"%{year}%")
    if location:
        sql += " AND location LIKE ?"
        params.append(f"%{location}%")
    sql += " ORDER BY trail_name COLLATE NOCASE, id"

    db = get_db()
    hikers = db.execute(sql, params).fetchall()
    total = db.execute("SELECT COUNT(*) AS c FROM hikers WHERE verified = 1").fetchone()["c"]

    # Browse mode (no search): cap the register so the footer stays
    # reachable as the directory grows; "Show more" pages 50 at a time.
    # Search results always show every match.
    searching = bool(q or trail or year or location)
    try:
        show = int(request.args.get("show", "50"))
    except ValueError:
        show = 50
    show = max(show, 50)
    match_count = len(hikers)
    if not searching:
        hikers = hikers[:show]

    return render_template(
        "index.html", hikers=hikers, total=total, trails=TRAILS,
        q=q, trail=trail, year=year, location=location,
        searching=searching, shown=len(hikers), match_count=match_count,
        next_show=show + 50,
        trail_labels=trail_labels,
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        # Honeypot: bots fill this hidden field; pretend success and stop.
        if request.form.get("website"):
            return render_template("registered.html", emailed=False)

        if rate_limited(f"reg:{request.remote_addr}", 10, 3600):
            flash("Too many registrations from this address. Try again later.")
            return render_template("register.html", trails=TRAILS), 429

        data, errors = _profile_form_data()
        email = request.form.get("email", "").strip()
        if not EMAIL_RE.match(email):
            errors.append("A valid contact email is required (it stays hidden).")

        # Email is the identity: one entry per email. A verified entry
        # blocks re-registering (with a pointer to the edit page); an
        # unverified one is simply refreshed — same row, fresh token —
        # so a lost confirmation email never locks anyone out. Trail
        # names, by contrast, may repeat freely.
        db = get_db()
        refresh_row = None
        duplicate_email = False
        if not errors:
            email_rows = db.execute(
                "SELECT * FROM hikers WHERE email = ? COLLATE NOCASE "
                "ORDER BY verified DESC, id",
                (email,),
            ).fetchall()
            if any(r["verified"] for r in email_rows):
                errors.append("This email already has an entry in the directory.")
                duplicate_email = True
            elif email_rows:
                refresh_row = email_rows[0]

        if errors:
            for e in errors:
                flash(e)
            return render_template(
                "register.html", trails=TRAILS,
                duplicate_email=duplicate_email), 400

        token = secrets.token_urlsafe(24)
        verified = 0 if REQUIRE_VERIFICATION else 1
        try:
            if refresh_row is not None:
                db.execute(
                    """UPDATE hikers
                       SET trail_name = ?, real_name = ?, trails = ?,
                           trail_custom = ?, years = ?, location = ?,
                           bio = ?, link = ?, verified = ?, verify_token = ?
                       WHERE id = ?""",
                    (data["trail_name"], data["real_name"], data["trails_value"],
                     data["trail_custom"], data["years"], data["location"],
                     data["bio"], data["link"], verified, token,
                     refresh_row["id"]),
                )
            else:
                db.execute(
                    """INSERT INTO hikers
                       (trail_name, email, real_name, trails, trail_custom,
                        years, location, bio, link, verified, verify_token,
                        created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (data["trail_name"], email, data["real_name"],
                     data["trails_value"], data["trail_custom"], data["years"],
                     data["location"], data["bio"], data["link"], verified,
                     token, int(time.time())),
                )
        except sqlite3.IntegrityError:
            # Lost a race with the unique email index.
            flash("This email already has an entry in the directory.")
            return render_template(
                "register.html", trails=TRAILS, duplicate_email=True), 400
        db.commit()

        emailed = False
        mail_ok = True
        if REQUIRE_VERIFICATION:
            link_verify = f"{app.config['SITE_URL']}/verify/{token}"
            mail_ok = send_email(
                email,
                f"Confirm your trail name: {data['trail_name']}",
                f"Howdy {data['trail_name']}!\n\n"
                f"Click to confirm your email and list your trail name in the directory:\n"
                f"{link_verify}\n\n"
                f"If you didn't register, just ignore this message.\n",
            ) != "failed"
            emailed = True
        if not mail_ok:
            flash("We couldn't send the confirmation email — the site admin can "
                  "verify your entry manually.")
        return render_template("registered.html", emailed=emailed)

    return render_template("register.html", trails=TRAILS)


@app.route("/verify/<token>")
def verify(token):
    db = get_db()
    row = db.execute(
        "SELECT * FROM hikers WHERE verify_token = ?", (token,)
    ).fetchone()
    if not row:
        abort(404)
    db.execute(
        "UPDATE hikers SET verified = 1, verify_token = '' WHERE id = ?", (row["id"],)
    )
    db.commit()
    return render_template("verified.html", hiker=row)


@app.route("/edit", methods=["GET", "POST"])
def edit_request():
    """Request a passwordless edit link by email.

    The response is identical whether or not the email has an entry,
    so the form can't be used to probe who's registered.
    """
    if request.method == "POST":
        # Honeypot: bots fill this hidden field; pretend success and stop.
        if request.form.get("website"):
            return render_template("edit_sent.html")

        if rate_limited(f"edit:{request.remote_addr}", 5, 3600):
            flash("Too many edit-link requests from this address. Try again later.")
            return render_template("edit_request.html"), 429

        email = request.form.get("email", "").strip()
        if EMAIL_RE.match(email):
            db = get_db()
            rows = db.execute(
                "SELECT * FROM hikers WHERE email = ? COLLATE NOCASE "
                "AND verified = 1 ORDER BY id",
                (email,),
            ).fetchall()
            if rows:
                links = []
                for row in rows:
                    token = secrets.token_urlsafe(24)
                    db.execute(
                        "UPDATE hikers SET edit_token = ? WHERE id = ?",
                        (token, row["id"]),
                    )
                    links.append(
                        (row["trail_name"],
                         f"{app.config['SITE_URL']}/edit/{token}"))
                db.commit()
                if len(links) == 1:
                    listing = f"your entry:\n\n{links[0][1]}"
                else:
                    listing = "your entries:\n\n" + "\n".join(
                        f"{name}: {url}" for name, url in links)
                send_email(
                    email,
                    "Edit your Trail Names entry",
                    "Howdy!\n\n"
                    "You (or someone) asked for a link to edit "
                    f"{listing}\n\n"
                    "The link doesn't expire — keep this email and reuse it "
                    "any time. If you didn't ask for it, just ignore this "
                    "message; nothing changes without the link.\n",
                )
        return render_template("edit_sent.html")

    return render_template("edit_request.html")


@app.route("/edit/<token>", methods=["GET", "POST"])
def edit_entry(token):
    db = get_db()
    row = db.execute(
        "SELECT * FROM hikers WHERE edit_token = ? AND edit_token != ''",
        (token,),
    ).fetchone()
    if not row:
        abort(404)

    if request.method == "POST":
        data, errors = _profile_form_data()
        if errors:
            for e in errors:
                flash(e)
            return render_template(
                "edit.html", hiker=row, trails=TRAILS,
                values=request.form,
                selected=request.form.getlist("trails")), 400
        db.execute(
            """UPDATE hikers
               SET trail_name = ?, real_name = ?, trails = ?,
                   trail_custom = ?, years = ?, location = ?, bio = ?,
                   link = ?
               WHERE id = ?""",
            (data["trail_name"], data["real_name"], data["trails_value"],
             data["trail_custom"], data["years"], data["location"],
             data["bio"], data["link"], row["id"]),
        )
        db.commit()
        flash("Your entry has been updated.")
        return redirect(url_for("profile", hiker_id=row["id"]))

    selected = [c for c in row["trails"].strip(",").split(",") if c]
    return render_template(
        "edit.html", hiker=row, trails=TRAILS, values=row, selected=selected)


@app.route("/hiker/<int:hiker_id>")
def profile(hiker_id):
    hiker = get_hiker(hiker_id)
    if not hiker:
        abort(404)
    return render_template("profile.html", hiker=hiker, trail_labels=trail_labels)


@app.route("/hiker/<int:hiker_id>/contact", methods=["GET", "POST"])
def contact(hiker_id):
    hiker = get_hiker(hiker_id)
    if not hiker:
        abort(404)

    if request.method == "POST":
        if request.form.get("website"):
            return render_template("contact_sent.html", hiker=hiker)

        if rate_limited(f"contact:{request.remote_addr}", 5, 3600):
            flash("Too many messages from this address. Try again later.")
            return render_template("contact.html", hiker=hiker), 429

        sender_name = request.form.get("sender_name", "").strip()
        sender_email = request.form.get("sender_email", "").strip()
        body = request.form.get("body", "").strip()

        errors = []
        if not sender_name or len(sender_name) > 80:
            errors.append("Your name is required.")
        if not EMAIL_RE.match(sender_email):
            errors.append("A valid email is required so they can reply.")
        if not body or len(body) > 2000:
            errors.append("Message is required (max 2000 characters).")
        if errors:
            for e in errors:
                flash(e)
            return render_template("contact.html", hiker=hiker), 400

        db = get_db()
        db.execute(
            """INSERT INTO messages
               (hiker_id, sender_name, sender_email, body, ip, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (hiker_id, sender_name, sender_email, body,
             request.remote_addr or "", int(time.time())),
        )
        db.commit()

        # Names can repeat now, so say which entry this is for.
        to_bits = trail_labels(hiker["trails"])
        if hiker["years"]:
            to_bits.append(hiker["years"])
        to_line = f"To trail name: {hiker['trail_name']}"
        if to_bits:
            to_line += f" ({' · '.join(to_bits)})"
        to_line += f" — entry #{hiker['id']}"

        mail_ok = send_email(
            hiker["email"],
            f"[{app.config['SITE_NAME']}] Message from {sender_name}",
            f"You've got a message via the {app.config['SITE_NAME']} directory.\n\n"
            f"From: {sender_name} <{sender_email}>\n"
            f"{to_line}\n\n"
            f"---\n{body}\n---\n\n"
            f"Reply directly to {sender_email} to answer.",
            reply_to=sender_email,
        ) != "failed"
        if not mail_ok:
            flash("Your message was saved, but email delivery failed — "
                  "the site admin can forward it.")
        return render_template("contact_sent.html", hiker=hiker)

    return render_template("contact.html", hiker=hiker)


@app.route("/contact", methods=["GET", "POST"])
def creator_contact():
    """Contact the site creator. The address stays private; the form relays."""
    if request.method == "POST":
        if request.form.get("website"):
            return render_template("contact_creator_sent.html")

        if rate_limited(f"creator-contact:{request.remote_addr}", 5, 3600):
            flash("Too many messages from this address. Try again later.")
            return render_template("contact_creator.html"), 429

        sender_name = request.form.get("sender_name", "").strip()
        sender_email = request.form.get("sender_email", "").strip()
        body = request.form.get("body", "").strip()

        errors = []
        if not sender_name or len(sender_name) > 80:
            errors.append("Your name is required.")
        if not EMAIL_RE.match(sender_email):
            errors.append("A valid email is required so I can reply.")
        if not body or len(body) > 2000:
            errors.append("Message is required (max 2000 characters).")
        if errors:
            for e in errors:
                flash(e)
            return render_template("contact_creator.html"), 400

        mail_ok = send_email(
            get_setting("contact_email"),
            f"[{app.config['SITE_NAME']}] Message from {sender_name}",
            f"Someone wrote via the {app.config['SITE_NAME']} contact form.\n\n"
            f"From: {sender_name} <{sender_email}>\n\n"
            f"---\n{body}\n---\n\n"
            f"Reply directly to {sender_email} to answer.",
            reply_to=sender_email,
        ) != "failed"
        if not mail_ok:
            flash("Your message couldn't be delivered right now. Please try again later.")
            return render_template("contact_creator.html"), 502
        return render_template("contact_creator_sent.html")

    return render_template("contact_creator.html")


@app.route("/about")
def about():
    text = get_setting("about_text")
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return render_template(
        "about.html",
        about_paragraphs=paragraphs,
        about_photo=get_setting("about_photo"),
    )


@app.route("/uploads/<path:filename>")
def uploads(filename):
    # Serves admin-uploaded files (the about photo). Traversal outside
    # the uploads dir is blocked by send_from_directory.
    return send_from_directory(UPLOAD_DIR, filename)


# --------------------------------------------------------------------------
# Admin (token in query string, e.g. /admin?token=...)
# --------------------------------------------------------------------------

@app.route("/admin")
def admin():
    check_admin()
    db = get_db()
    hikers = db.execute("SELECT * FROM hikers ORDER BY created_at DESC").fetchall()
    recent_msgs = db.execute(
        """SELECT m.*, h.trail_name FROM messages m
           JOIN hikers h ON h.id = m.hiker_id
           ORDER BY m.created_at DESC LIMIT 20"""
    ).fetchall()
    return render_template(
        "admin.html", hikers=hikers, messages=recent_msgs,
        token=request.args.get("token", ""),
        smtp_configured=SMTP_CONFIGURED,
        verification_on=REQUIRE_VERIFICATION,
    )


@app.route("/admin/delete/<int:hiker_id>", methods=["POST"])
def admin_delete(hiker_id):
    check_admin()
    db = get_db()
    db.execute("DELETE FROM messages WHERE hiker_id = ?", (hiker_id,))
    db.execute("DELETE FROM hikers WHERE id = ?", (hiker_id,))
    db.commit()
    flash("Entry deleted.")
    return redirect(url_for("admin", token=request.form.get("token", "")))


@app.route("/admin/verify/<int:hiker_id>", methods=["POST"])
def admin_verify(hiker_id):
    check_admin()
    db = get_db()
    db.execute(
        "UPDATE hikers SET verified = 1, verify_token = '' WHERE id = ?", (hiker_id,)
    )
    db.commit()
    flash("Entry verified.")
    return redirect(url_for("admin", token=request.form.get("token", "")))


@app.route("/admin/content")
def admin_content():
    check_admin()
    token = request.args.get("token", "")
    return render_template(
        "admin_content.html",
        token=token,
        about_text=get_setting("about_text"),
        about_photo=get_setting("about_photo"),
        support_url=get_setting("support_url"),
        contact_email=get_setting("contact_email"),
    )


@app.route("/admin/content", methods=["POST"])
def admin_content_save():
    check_admin()
    token = request.form.get("token", "")

    about_text = request.form.get("about_text", "").strip()
    if len(about_text) > 2000:
        flash("About text must be under 2000 characters.")
        return redirect(url_for("admin_content", token=token))
    set_setting("about_text", about_text)

    support_url = request.form.get("support_url", "").strip()
    if support_url and not re.match(r"^https?://", support_url):
        flash("Support link must start with http:// or https://.")
        return redirect(url_for("admin_content", token=token))
    set_setting("support_url", support_url)

    contact_email = request.form.get("contact_email", "").strip()
    if contact_email and not EMAIL_RE.match(contact_email):
        flash("Contact email doesn't look valid.")
        return redirect(url_for("admin_content", token=token))
    if contact_email:
        set_setting("contact_email", contact_email)

    if request.form.get("reset_photo"):
        set_setting("about_photo", DEFAULT_SETTINGS["about_photo"])
    else:
        file = request.files.get("about_photo")
        if file and file.filename:
            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp"):
                flash("Photo must be a JPG, PNG, or WebP file.")
                return redirect(url_for("admin_content", token=token))
            data = file.read()
            if len(data) > 8 * 1024 * 1024:
                flash("Photo must be under 8 MB.")
                return redirect(url_for("admin_content", token=token))
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            fname = "about" + (".jpg" if ext == ".jpeg" else ext)
            with open(os.path.join(UPLOAD_DIR, fname), "wb") as f:
                f.write(data)
            set_setting("about_photo", f"/uploads/{fname}")

    flash("Site content updated.")
    return redirect(url_for("admin_content", token=token))


# --------------------------------------------------------------------------
# Temporary seed data for testing search (admin only).
# /admin/seed?token=... inserts fake hikers (marked, verified, idempotent);
# /admin/unseed (POST) removes them. Remove these routes when done testing.
# --------------------------------------------------------------------------

_SEED_HIKERS = [
    # (trail_name, real_name, trails, years, location, bio)
    ("Sunshine", "Maya R.", ",AT,", "2019", "Virginia",
     "Chased daylight from Georgia to Maine. Still not tired of sunrises."),
    ("Bear Bait", "Tom K.", ",PCT,", "2021", "California",
     "Hung every food bag twice. The bears never got me."),
    ("Detour", "Priya S.", ",CDT,", "2018", "Colorado",
     "Took every alternate route. No regrets."),
    ("Moonshadow", "Ellis W.", ",AT,", "2022", "Georgia",
     "Night hiker. The stars were better company than the shelters."),
    ("Reboot", "Dan L.", ",PCT,", "2023", "Oregon",
     "Quit tech, walked to Canada, rebooted life."),
    ("Snapdragon", "Ana M.", ",JMT,", "2020", "California",
     "Named for the flowers at Evolution Lake."),
    ("Tortoise", "Greg H.", ",AT,", "2016", "Pennsylvania",
     "Slow and steady. Finished anyway."),
    ("Highbeam", "Jess T.", ",CDT,", "2022", "New Mexico",
     "Headlamp died in the Gila; hiked by moonlight instead."),
    ("Wandering Star", "Luc F.", ",CAMINO,", "2019", "Spain",
     "Walked to Santiago to get lost on purpose."),
    ("Packrat", "Nina P.", ",LT,", "2021", "Vermont",
     "Carried twelve pounds of 'just in case.' Used none of it."),
    ("Switchback", "Owen D.", ",CT,", "2023", "Colorado",
     "Counted 4,000 switchbacks. Probably miscounted."),
    ("Bluebird", "Sara J.", ",PCT,", "2017", "Washington",
     "Perfect weather the whole way. Suspicious, honestly."),
    ("Mosey", "Hank B.", ",AT,", "2020", "North Carolina",
     "Never hurried, never late."),
    ("Trail Mix", "Kim A.", ",AT,PCT,", "2018, 2022", "Tennessee",
     "A little bit of everything, just like the snack."),
    ("Cloudwalker", "Aroha N.", ",TA,", "2023", "New Zealand",
     "3,000 km of mud, ferns, and unreal huts."),
    ("Stonefly", "Chris V.", ",JMT,", "2021", "California",
     "Fly-fished every alpine lake. Ate well."),
    ("Night Owl", "Rae D.", ",CDT,", "2019", "Montana",
     "Hiked dawn to dusk, then journaled by headlamp."),
    ("Sunseeker", "Marco I.", ",AZT,", "2022", "Arizona",
     "Found the sun. It found me back."),
    ("Grit", "Beth C.", ",AT,", "2015", "Maine",
     "Finished Katahdin in a sleet storm. Would do it again."),
    ("Fernweh", "Lukas G.", ",PCT,", "2024", "California",
     "German for wanderlust. Earned it."),
    ("Marmot", "Dana W.", ",CT,", "2020", "Colorado",
     "Whistled at every marmot. They whistled back."),
    ("Biscuit", "Ray F.", ",AT,", "2023", "Virginia",
     "Ate a biscuit in every trail town. Scientific study."),
    ("Thunderhead", "Zoe M.", ",CDT,", "2021", "Wyoming",
     "Outran three storms in the Winds. Barely."),
    ("Pickles", "Sam O.", ",PCT,", "2019", "Oregon",
     "Craved pickles for 2,650 miles. Don't ask."),
    ("Wayfinder", "Claire B.", ",CAMINO,", "2017", "France",
     "Followed yellow arrows across a whole country."),
    ("Cricket", "Ben N.", ",LT,", "2022", "Vermont",
     "Chirped along the whole Long Trail."),
    ("Saguaro", "Rita E.", ",AZT,", "2023", "Arizona",
     "Standing tall through the desert, like the cactus."),
    ("Drifter", "Cole H.", ",AT,", "2018", "Maryland",
     "Went where the wind went. It went north."),
    ("Loon", "Maggie S.", ",OTHER,", "2021", "Minnesota",
     "Paddled and hiked the Boundary Waters."),
    ("Ember", "Jake P.", ",JMT,", "2022", "California",
     "Carried the spark all the way to Whitney."),
    ("Bearfoot", "Al Y.", ",AT,", "2020", "Georgia",
     "Lost a shoe on day three. Hiked on."),
    ("Sunbeam", "Tara Q.", ",PCT,", "2021", "Washington",
     "The one ray of sun in a week of rain."),
    ("Second Breakfast", "Finn G.", ",AT,", "2019", "Virginia",
     "Ate like a hobbit. Hiked like one too."),
    ("Trail Angel Tina", "Tina R.", ",OTHER,", "2016-2024", "New Hampshire",
     "Feeds hikers at the notch. Legend status."),
    ("Quicksilver", "Nate Z.", ",CDT,", "2023", "Colorado",
     "Fast, shiny, gone by morning."),
    ("Halfway", "Jo U.", ",PCT,", "2022", "California",
     "Celebrated the halfway point twice. Math was hard."),
]

_SEED_EMAIL_DOMAIN = "seed.trailnames.club"


@app.route("/admin/seed")
def admin_seed():
    check_admin()
    token = request.args.get("token", "")
    db = get_db()
    # Idempotent: clear any previous seed rows first.
    db.execute("DELETE FROM hikers WHERE email LIKE ?", (f"seed-%@{_SEED_EMAIL_DOMAIN}",))
    now = int(time.time())
    inserted = 0
    for i, (tname, rname, trails, years, loc, bio) in enumerate(_SEED_HIKERS):
        # Names are no longer unique, so check explicitly: never seed a
        # name that anyone (real hiker or live entry) already has.
        if db.execute(
            "SELECT 1 FROM hikers WHERE trail_name = ? COLLATE NOCASE",
            (tname,),
        ).fetchone():
            continue
        db.execute(
            "INSERT INTO hikers (trail_name, email, real_name, trails, years,"
            " location, bio, link, verified, verify_token, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, '', 1, '', ?)",
            (tname, f"seed-{i}@{_SEED_EMAIL_DOMAIN}", rname, trails,
             years, loc, bio, now),
        )
        inserted += 1
    db.commit()
    return render_template("seed.html", token=token, inserted=inserted, removed=None)


@app.route("/admin/unseed", methods=["POST"])
def admin_unseed():
    check_admin()
    token = request.form.get("token", "")
    db = get_db()
    cur = db.execute(
        "DELETE FROM hikers WHERE email LIKE ?", (f"seed-%@{_SEED_EMAIL_DOMAIN}",))
    db.commit()
    return render_template(
        "seed.html", token=token, inserted=None, removed=cur.rowcount)


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Initialize the database on startup (runs under gunicorn as well as
# `python app.py`, since module level code executes on import).

with app.app_context():
    init_db()

if __name__ == "__main__":
    print(f"Admin token: {app.config['ADMIN_TOKEN']}")
    print(f"Admin URL: http://localhost:5000/admin?token={app.config['ADMIN_TOKEN']}")
    if not SMTP_CONFIGURED:
        print("SMTP not configured: emails will be written to ./outbox/ (demo mode).")
    app.run(host="0.0.0.0", port=5000)
