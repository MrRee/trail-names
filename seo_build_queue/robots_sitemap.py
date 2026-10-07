# Queued for the next consolidated Trail Names build batch — NOT yet installed.
#
# SEO: /robots.txt + /sitemap.xml so Google can index trailnames.club.
# As of 2026-10-05: /robots.txt and /sitemap.xml both 404, and a Google
# "site:trailnames.club" search returns zero results — the site is not
# indexed yet. Reddit modmail outreach is already underway, so getting
# indexable now is well-timed (indexing takes a few days to bite).
#
# TO INSTALL (in the next batch, alongside Mateo's other queued changes):
#   1. In app.py, add Response to the flask import line.
#   2. Paste the two routes below next to the other public routes.
#   3. Deploy as part of the consolidated batch (never a solo deploy).
#   4. In Google Search Console (Mateo's Google account): add property
#      trailnames.club, verify via DNS TXT at Porkbun (he's done DKIM/
#      SPF/DMARC there before — same flow), then submit the sitemap URL.
#
# Note: the sitemap lists verified entries only, which currently include the
# disclosed dummy seed entries. When Mateo unseeds, rows drop out of the
# sitemap automatically — no manual pruning needed.

from flask import Response  # add to the existing flask import in app.py


@app.route("/robots.txt")
def robots_txt():
    site = app.config["SITE_URL"].rstrip("/")
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /edit\n"
        f"Sitemap: {site}/sitemap.xml\n"
    )
    return Response(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    db = get_db()
    site = app.config["SITE_URL"].rstrip("/")
    urls = [f"{site}/", f"{site}/register", f"{site}/about", f"{site}/contact"]
    rows = db.execute(
        "SELECT id FROM hikers WHERE verified = 1 ORDER BY id"
    ).fetchall()
    urls.extend(f"{site}/hiker/{row['id']}" for row in rows)
    items = "\n".join(f"  <url><loc>{u}</loc></url>" for u in urls)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{items}\n"
        "</urlset>"
    )
    return Response(xml, mimetype="application/xml")
