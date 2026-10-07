# Trail Names

The code behind [trailnames.club](https://trailnames.club) — now open source.

A lightweight directory of hiker trail names. Through-hikers earn a "trail
name" on long trails (AT, PCT, CDT...) — this lets them register it, lets
others search by name / trail / year / location, and relays contact-form
messages by email so addresses are never exposed.

**Stack:** Python + Flask + SQLite. One small app, no build step, no database
server. Designed to be cheap to run.

## Run it locally

```bash
cd trail-names
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python app.py
```

Open http://localhost:5000. On first start it prints your **admin token** and
admin URL — save it. Without SMTP configured, the app runs in demo mode:
"sent" emails are written as text files to `./outbox/` instead.

## Configuration (environment variables)

| Variable | Default | What it does |
|---|---|---|
| `DATABASE` | `./data/trailnames.db` | SQLite file location |
| `SECRET_KEY` | random per start | Session signing — **set a fixed value in production** |
| `ADMIN_TOKEN` | random, printed at startup | Token for `/admin` (moderation) |
| `SITE_NAME` | `Trail Names` | Used in emails/titles |
| `SITE_URL` | `http://localhost:5000` | Base URL used in verification emails |
| `SMTP_HOST` / `SMTP_PORT` | (unset) | Outgoing mail server |
| `SMTP_USER` / `SMTP_PASS` | (unset) | SMTP credentials |
| `SMTP_FROM` | (unset) | `From:` address for relayed mail |
| `SMTP_USE_TLS` | `1` | Use STARTTLS |
| `REQUIRE_EMAIL_VERIFICATION` | `auto` | `auto` = on when SMTP is set, off otherwise |

Without `SMTP_HOST`/`SMTP_FROM`, email verification is skipped (entries go
live immediately) and contact messages are logged to `./outbox/` — fine for
testing, not for production.

### Email on the cheap

The app only needs plain SMTP. Free/low-cost options:

- **Your own Gmail**: create an [App Password](https://myaccount.google.com/apppasswords),
  set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER=you@gmail.com`,
  `SMTP_PASS=<app password>`, `SMTP_FROM=you@gmail.com`. Free, fine for a
  small directory. (Gmail caps ~500 emails/day.)
- **Resend / Brevo / Mailgun free tiers**: give you SMTP credentials with a
  few hundred–few thousand free emails/month — more than enough here.

## Deploying (cheap options)

The app is a single container / single process with a SQLite file — the
cheapest possible shape to host.

**Option A — Fly.io** (~$0–5/mo, free allowances usually cover this):
```bash
fly launch   # answer the prompts; it detects the Dockerfile
fly secrets set SECRET_KEY=$(python3 -c "import secrets;print(secrets.token_hex(32))") \
  ADMIN_TOKEN=$(python3 -c "import secrets;print(secrets.token_hex(16))") \
  SITE_URL=https://your-app.fly.dev \
  SMTP_HOST=... SMTP_FROM=... # etc.
fly volumes create trail_data --size 1   # persist the SQLite file!
```
Then set `DATABASE=/data/trailnames.db` as a secret and mount the volume at
`/data`. (Without the volume, registrations vanish on each deploy.)

**Option B — any small VPS** (Hetzner, DigitalOcean droplet ~$4–6/mo):
install Python, `pip install -r requirements.txt gunicorn`, and run
`gunicorn -w 2 -b 127.0.0.1:8000 app:app` behind Caddy or nginx. Back up
`data/trailnames.db` regularly — that's the whole site.

**Option C — Render/Railway free tier**: works, but free tiers sleep and may
wipe local disk — attach their persistent disk offering if you go this way.

## Admin

Visit `/admin?token=YOUR_ADMIN_TOKEN` to see all entries (verified or not),
manually verify, delete entries, and review recent contact messages.

## Backups

Everything lives in one file: `data/trailnames.db`. Copy it anywhere to back
up or move hosts.

## Privacy & abuse notes

- Emails are stored but **never rendered** in any public page or API.
- Contact form: honeypot field + 5 messages/hour per IP rate limit; messages
  are stored for moderation and relayed with `Reply-To` set to the sender.
- Trail names are unique (case-insensitive), first come first served.
- Consider adding a CAPTCHA (e.g. Cloudflare Turnstile, free) if spam becomes
  a problem — the form has a single place to hook it in.

## License

MIT — see [LICENSE](LICENSE).
