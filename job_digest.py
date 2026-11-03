#!/usr/bin/env python3
"""PSLF Job Digest: finds REMOTE jobs at government and nonprofit employers.

Filters, in order:
  1. Remote: the posting must clearly say remote and must not say hybrid.
  2. Employer: government, or a verified IRS 501(c)(3) nonprofit.
  3. Keywords: the title or description must contain one of your words.

Sources
  USAJOBS (federal)            - free key, every job qualifies as government
  NYC Jobs (NYC Open Data)     - no key, every job qualifies as government
  Adzuna (aggregator)          - free key, employer checked against IRS 501(c)(3)
                                 records via ProPublica Nonprofit Explorer (no key)

Run:  python job_digest.py            fetch, update the landing page, send email
      python job_digest.py --no-email fetch and update the page only
      python job_digest.py --demo     render sample data, no network calls
"""
import argparse
import datetime as dt
import difflib
import html
import json
import os
import re
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote_plus

import requests
import yaml

ROOT = Path(__file__).parent
STATE, DOCS = ROOT / "state", ROOT / "docs"
SEEN_FILE, RECENT_FILE = STATE / "seen.json", STATE / "recent.json"
EMPLOYER_CACHE = STATE / "employer_cache.json"

USAJOBS_URL = "https://data.usajobs.gov/api/search"
NYC_URL = "https://data.cityofnewyork.us/resource/kpav-sd4t.json"
NYC_LINK = "https://cityjobs.nyc.gov/jobs?keyword={job_id}"  # dataset has no direct link; this searches by Job ID
ADZUNA_URL = "https://api.adzuna.com/v1/api/jobs/us/search/{page}"
PROPUBLICA_URL = "https://projects.propublica.org/nonprofits/api/v2/search.json"

TODAY = dt.date.today()

# PSLF categories, in the order they appear in the digest
SECTIONS = [
    ("government", "Government employers", "Remote roles at federal, state, and city agencies."),
    ("nonprofit", "Verified 501(c)(3) nonprofits", "Remote roles; employer matched an IRS 501(c)(3) record."),
    ("check", "Worth checking", "Likely public or nonprofit, but the match is not certain."),
    ("unknown", "Unverified employers", "No public or 501(c)(3) match found."),
]
GOV_NAME_PATTERNS = [
    r"\bdepartment of\b", r"\bcity of\b", r"\bcounty of\b", r"\bstate of\b", r"\bnyc\b",
    r"new york city", r"health \+ hospitals", r"\bcuny\b", r"\bsuny\b", r"city university of new york",
    r"state university of new york", r"veterans affairs", r"public schools?", r"board of education",
    r"\bnew york state\b", r"\bnys\b",
]


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True))


def norm(text):
    text = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())
    drop = {"the", "inc", "incorporated", "llc", "corp", "corporation", "co", "of", "and", "at"}
    return " ".join(w for w in text.split() if w not in drop)


def make_job(**kw):
    job = dict(id="", source="", title="", employer="", location="", url="", posted="",
               closes="", salary="", category="", note="", text="", hits=[])
    job.update({k: (v or "") if isinstance(v, str) or v is None else v for k, v in kw.items()})
    return job


def money(lo, hi, suffix=""):
    try:
        lo, hi = float(lo or 0), float(hi or 0)
    except ValueError:
        return ""
    if not lo and not hi:
        return ""
    fmt = lambda x: f"${x:,.0f}"
    return (f"{fmt(lo)} – {fmt(hi)}" if lo and hi and lo != hi else fmt(lo or hi)) + suffix


# ---------------------------------------------------------------- filters
REMOTE_YES = [r"\bremote\b", r"work from home", r"\bwfh\b", r"telework from anywhere",
              r"telecommut", r"anywhere in the u\.?s"]
REMOTE_NO_ANYWHERE = [r"\bhybrid\b", r"not (a )?remote", r"remote not available"]
REMOTE_NO_HEADLINE = [r"on-?site", r"in[- ]office", r"in[- ]person"]  # checked in title/location only


def is_remote(headline, description=""):
    """headline = title + location. Needs a remote signal and no hybrid/on-site wording."""
    head, body = (headline or "").lower(), (description or "").lower()
    if any(re.search(p, head + " " + body) for p in REMOTE_NO_ANYWHERE):
        return False
    if any(re.search(p, head) for p in REMOTE_NO_HEADLINE):
        return False
    return any(re.search(p, head + " " + body) for p in REMOTE_YES)


def keyword_hits(job, cfg):
    """Return matched keywords; title matches are listed first."""
    title, body = job["title"].lower(), job.get("text", "").lower()
    in_title, in_body = [], []
    for k in cfg["keywords"]:
        pat = r"\b" + re.escape(k.lower())
        if re.search(pat, title):
            in_title.append(k)
        elif re.search(pat, body):
            in_body.append(k)
    return in_title + in_body


def excluded(job, cfg):
    title = job["title"].lower()
    return (any(w.lower() in title for w in cfg.get("exclude_title_words") or []) or
            any(e.lower() in job["employer"].lower() for e in cfg.get("exclude_employers") or []))


# ---------------------------------------------------------------- sources
def fetch_usajobs(cfg):
    key, email = os.getenv("USAJOBS_API_KEY"), os.getenv("USAJOBS_EMAIL")
    if not (key and email):
        log("USAJOBS: no key set, skipping")
        return []
    headers = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key}
    jobs, dropped = [], 0
    for page in range(1, 6):
        params = {"RemoteIndicator": "True", "DatePosted": cfg["lookback_days"],
                  "ResultsPerPage": 500, "Page": page}
        try:
            r = requests.get(USAJOBS_URL, headers=headers, params=params, timeout=60)
            r.raise_for_status()
            items = r.json().get("SearchResult", {}).get("SearchResultItems", [])
        except (requests.RequestException, ValueError) as e:
            log(f"USAJOBS error: {e}")
            break
        for item in items:
            d = item.get("MatchedObjectDescriptor", {})
            details = (d.get("UserArea") or {}).get("Details") or {}
            flag = details.get("RemoteIndicator")
            location = d.get("PositionLocationDisplay") or ""
            remote = flag is True or str(flag).lower() == "true" or is_remote(f"{d.get('PositionTitle')} {location}")
            if not remote:
                dropped += 1
                continue
            pay = (d.get("PositionRemuneration") or [{}])[0]
            jobs.append(make_job(
                id=f"usajobs:{item.get('MatchedObjectId')}", source="USAJOBS",
                title=d.get("PositionTitle"), employer=d.get("OrganizationName"),
                location=location, url=d.get("PositionURI"),
                posted=(d.get("PublicationStartDate") or "")[:10],
                closes=(d.get("ApplicationCloseDate") or "")[:10],
                salary=money(pay.get("MinimumRange"), pay.get("MaximumRange"),
                             "" if pay.get("RateIntervalCode") == "PA" else " /hr"),
                text=" ".join(str(x) for x in (details.get("JobSummary"), d.get("QualificationSummary")) if x),
                category="government", note="Federal agency"))
        if len(items) < 500:
            break
        time.sleep(1)
    log(f"USAJOBS: {len(jobs)} remote postings ({dropped} non-remote dropped)")
    return jobs


def fetch_nyc(cfg):
    since = (TODAY - dt.timedelta(days=cfg["lookback_days"])).isoformat()
    params = {"$where": f"posting_type='External' AND posting_date >= '{since}T00:00:00'",
              "$limit": 5000, "$order": "posting_date DESC"}
    headers = {"X-App-Token": os.getenv("NYC_APP_TOKEN")} if os.getenv("NYC_APP_TOKEN") else {}
    try:
        r = requests.get(NYC_URL, params=params, headers=headers, timeout=60)
        r.raise_for_status()
        rows = r.json()
    except (requests.RequestException, ValueError) as e:
        log(f"NYC Jobs error: {e}")
        return []
    jobs = []
    for row in rows:
        text = " ".join(str(row.get(k) or "") for k in ("job_description", "preferred_skills", "additional_information"))
        if not is_remote(f"{row.get('business_title')} {row.get('work_location')}", text):
            continue
        annual = (row.get("salary_frequency") or "").lower() == "annual"
        jobs.append(make_job(
            id=f"nyc:{row.get('job_id')}", source="NYC Jobs",
            title=row.get("business_title"), employer=(row.get("agency") or "").title(),
            location=row.get("work_location") or "New York, NY",
            url=NYC_LINK.format(job_id=quote_plus(str(row.get("job_id", "")))),
            posted=(row.get("posting_date") or "")[:10], closes=(row.get("post_until") or "")[:10],
            salary=money(row.get("salary_range_from"), row.get("salary_range_to"), "" if annual else " /hr"),
            text=text, category="government", note=f"NYC agency · Job ID {row.get('job_id')}"))
    log(f"NYC Jobs: {len(jobs)} remote postings out of {len(rows)}")
    return jobs


def fetch_adzuna(cfg):
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        log("Adzuna: no key set, skipping")
        return []
    jobs, dropped = [], 0
    for page in range(1, cfg.get("adzuna_pages", 10) + 1):
        params = {"app_id": app_id, "app_key": app_key, "what": "remote",
                  "what_or": " ".join(cfg["keywords"]), "results_per_page": 50,
                  "max_days_old": cfg["lookback_days"], "sort_by": "date"}
        try:
            r = requests.get(ADZUNA_URL.format(page=page), params=params, timeout=30)
            r.raise_for_status()
            results = r.json().get("results", [])
        except (requests.RequestException, ValueError) as e:
            log(f"Adzuna error on page {page}: {e}")
            break
        for a in results:
            location = (a.get("location") or {}).get("display_name") or ""
            if not is_remote(f"{a.get('title')} {location}", a.get("description")):
                dropped += 1
                continue
            est = " (est.)" if str(a.get("salary_is_predicted")) == "1" else ""
            jobs.append(make_job(
                id=f"adzuna:{a.get('id')}", source="Adzuna", title=a.get("title"),
                employer=(a.get("company") or {}).get("display_name"), location=location,
                url=a.get("redirect_url"), posted=(a.get("created") or "")[:10], text=a.get("description"),
                salary=money(a.get("salary_min"), a.get("salary_max")) + est if a.get("salary_min") else ""))
        if len(results) < 50:
            break
        time.sleep(1)
    log(f"Adzuna: {len(jobs)} remote postings ({dropped} without a clear remote signal dropped)")
    return jobs


# ---------------------------------------------------------------- PSLF check
def name_score(employer, org_name):
    a, b = norm(employer), norm(org_name)
    if not a or not b:
        return 0.0
    if set(a.split()) <= set(b.split()):
        return max(0.85, difflib.SequenceMatcher(None, a, b).ratio())
    return difflib.SequenceMatcher(None, a, b).ratio()


def propublica_check(employer, state):
    params = {"q": employer, "c_code[id]": 3}
    if state:
        params["state[id]"] = state
    try:
        r = requests.get(PROPUBLICA_URL, params=params, timeout=30)
        orgs = r.json().get("organizations", []) if r.status_code == 200 else []
    except (requests.RequestException, ValueError):
        return None  # network problem: don't cache, try again next run
    if not orgs:
        return ("unknown", "No 501(c)(3) match found; verify with the PSLF Help Tool")
    best = max(orgs, key=lambda o: name_score(employer, o.get("name", "")))
    score = name_score(employer, best.get("name", ""))
    label = f"{best.get('name')} (EIN {best.get('ein')})"
    if score >= 0.8:
        return ("nonprofit", f"IRS 501(c)(3): {label}")
    return ("check", f"Closest 501(c)(3) match: {label}; verify")


def classify(jobs, cfg):
    cache = load_json(EMPLOYER_CACHE, {})
    overrides = {norm(k): v for k, v in (cfg.get("employer_overrides") or {}).items()}
    lookups = 0
    for job in jobs:
        if job["category"]:
            continue
        key = norm(job["employer"])
        if not key:
            job["category"], job["note"] = "unknown", "Employer not listed"
        elif key in overrides:
            job["category"], job["note"] = overrides[key], "Set in your config"
        elif key in cache:
            job["category"], job["note"] = cache[key]
        elif any(re.search(p, job["employer"].lower()) for p in GOV_NAME_PATTERNS):
            job["category"], job["note"] = "check", "Name suggests a public employer; verify"
        elif lookups < cfg.get("max_employer_lookups", 40):
            result = propublica_check(job["employer"], cfg.get("nonprofit_state"))
            lookups += 1
            time.sleep(1)
            if result:
                cache[key] = list(result)
                job["category"], job["note"] = result
            else:
                job["category"], job["note"] = "unknown", "Lookup failed; will retry next run"
        else:
            job["category"], job["note"] = "unknown", "Lookup limit reached; will check next run"
    save_json(EMPLOYER_CACHE, cache)
    log(f"Employer checks: {lookups} new ProPublica lookups")
    return jobs


# ---------------------------------------------------------------- output
def allowed_categories(cfg):
    return {"government", "nonprofit"} | ({"check"} if cfg.get("include_worth_checking") else set())


def render(jobs_by_day, cfg, page=False):
    css = """
:root{--bg:#fff;--fg:#1f2328;--muted:#59636e;--card:#f6f8fa;--line:#d1d9e0;--accent:#0b5cad}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--card:#151b23;--line:#3d444d;--accent:#4493f8}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:860px;margin:auto;padding:24px 16px}h1{font-size:1.6rem;margin:0 0 4px}
h2{font-size:1.15rem;margin:28px 0 4px;border-bottom:1px solid var(--line);padding-bottom:4px}
h3{font-size:1rem;margin:18px 0 2px}h3+p{margin:0}.muted{color:var(--muted);font-size:.9rem}
.job{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin:8px 0}
.job a{color:var(--accent);font-weight:600;text-decoration:none}.job a:hover{text-decoration:underline}
""" if page else ""
    st = (lambda style: "") if page else (lambda style: f" style='{style}'")  # inline styles only for email
    muted = st("color:#59636e;font-size:13px")
    out = []
    for day, jobs in jobs_by_day:
        if page:
            shown = sum(j["category"] in allowed_categories(cfg) for j in jobs)
            out.append(f"<h2>{html.escape(day)} · {shown} new</h2>")
        for cat, heading, blurb in SECTIONS:
            if cat not in allowed_categories(cfg):
                continue
            group = sorted((j for j in jobs if j["category"] == cat),
                           key=lambda j: (-len(j.get("hits") or []), j["title"].lower()))
            if not group:
                continue
            out.append(f"<h3>{heading} ({len(group)})</h3><p class='muted'{st("margin:0;color:#59636e;font-size:13px")}>{blurb}</p>")
            for j in group:
                bits = [j["employer"], j["location"], j["salary"],
                        f"closes {j['closes']}" if j["closes"] else "", j["source"]]
                if j.get("hits"):
                    bits.append("matches: " + ", ".join(j["hits"]))
                meta = " · ".join(html.escape(b) for b in bits if b)
                out.append(
                    f"<div class='job'{st('border:1px solid #d1d9e0;border-radius:8px;padding:10px 14px;margin:8px 0')}>"
                    f"<a href='{html.escape(j['url'])}'{st('color:#0b5cad;font-weight:600')}>{html.escape(j['title'])}</a>"
                    f"<div class='muted'{muted}>{meta}</div>"
                    f"<div class='muted'{muted}>PSLF: {html.escape(j['note'])}</div></div>")
    if not out:
        out.append("<p>No new remote government or nonprofit jobs matched today.</p>")
    footer = (f"<p class='muted'{st('color:#59636e;font-size:13px;margin-top:28px')}>PSLF eligibility depends on the "
              "employer and on working full time (30+ hours/week). Confirm any employer with the PSLF Help Tool at "
              "studentaid.gov before relying on it.</p>")
    title = "PSLF Job Digest"
    body = (f"<h1>{title}</h1><p class='muted'>Remote only · government and nonprofit · Updated {TODAY:%A, %B %d, %Y} · "
            f"last {cfg.get('page_days', 14)} days</p>" if page else f"<h2 style='margin-top:0'>{title} · {TODAY:%b %d}</h2>")
    doc = body + "".join(out) + footer
    if not page:
        return f"<div style='font-family:Arial,sans-serif;max-width:720px'>{doc}</div>"
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{title}</title>"
            f"<style>{css}</style></head><body><main>{doc}</main></body></html>")


def send_email(html_body, count):
    need = ["SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"]
    if not all(os.getenv(k) for k in need):
        log("Email: SMTP settings missing, skipping")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"PSLF Job Digest: {count} new job{'s' if count != 1 else ''} · {TODAY:%b %d}"
    msg["From"], msg["To"] = os.getenv("SMTP_USER"), os.getenv("EMAIL_TO")
    msg.attach(MIMEText("Your digest is best viewed as HTML.", "plain"))
    msg.attach(MIMEText(html_body, "html"))
    host, port = os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"))
        s.send_message(msg)
    log(f"Email sent to {os.getenv('EMAIL_TO')}")


def demo_jobs():
    j = make_job
    return [
        j(id="d1", source="USAJOBS", title="Sample: Public Health Data Analyst",
          employer="Example Federal Agency", location="Anywhere in the U.S. (remote job)", url="https://www.usajobs.gov/",
          hits=["health", "analyst", "data"], salary="$74,536", closes="2026-11-12", category="government", note="Federal agency"),
        j(id="d2", source="USAJOBS", title="Health Science Specialist", employer="Veterans Health Administration",
          location="Anywhere in the U.S. (remote job)", url="https://www.usajobs.gov/", hits=["health"], salary="$86,962 – $113,047",
          category="government", note="Federal agency"),
        j(id="d3", source="Adzuna", title="Population Health Analyst", employer="Example Community Health Center",
          location="Remote", url="https://www.adzuna.com/", hits=["population", "health", "analyst"], salary="$80,000 – $95,000 (est.)",
          category="nonprofit", note="IRS 501(c)(3): EXAMPLE COMMUNITY HEALTH CENTER INC (EIN 000000000)"),
        j(id="d4", source="Adzuna", title="Clinical Informatics Specialist", employer="Example University Hospital",
          location="Remote", url="https://www.adzuna.com/", category="check", hits=["clinical"],
          note="Closest 501(c)(3) match: EXAMPLE UNIVERSITY MEDICAL CENTER (EIN 000000000); verify"),
    ]


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--demo", action="store_true", help="render sample jobs without calling any API")
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())

    if args.demo:
        jobs = demo_jobs()
        DOCS.mkdir(exist_ok=True)
        (DOCS / "index.html").write_text(render([(TODAY.isoformat(), jobs)], cfg, page=True))
        (ROOT / "demo_email.html").write_text(render([(TODAY.isoformat(), jobs)], cfg))
        log("Demo written to docs/index.html and demo_email.html")
        return

    src = cfg.get("sources") or {}
    found = ((fetch_usajobs(cfg) if src.get("usajobs", True) else []) +
             (fetch_nyc(cfg) if src.get("nyc", False) else []) +
             (fetch_adzuna(cfg) if src.get("adzuna", True) else []))
    # Filter 1 (remote) happened inside each fetcher.
    seen = load_json(SEEN_FILE, {})
    unique = {}
    for job in found:
        if job["id"] in seen or job["id"] in unique or excluded(job, cfg):
            continue
        job["hits"] = keyword_hits(job, cfg)          # filter 3, checked before the slower employer lookups
        if job["hits"]:
            unique[job["id"]] = job
    candidates = classify(list(unique.values()), cfg)  # filter 2
    allowed = allowed_categories(cfg)
    new = [j for j in candidates if j["category"] in allowed]
    log(f"{len(found)} remote postings → {len(unique)} keyword matches → {len(new)} at government/nonprofit employers")

    for job in candidates:  # mark everything checked as seen, so rejects aren't re-checked tomorrow
        seen[job["id"]] = TODAY.isoformat()
    cutoff = (TODAY - dt.timedelta(days=120)).isoformat()
    save_json(SEEN_FILE, {k: v for k, v in seen.items() if v >= cutoff})

    for job in new:
        job.pop("text", None)
    recent = load_json(RECENT_FILE, {})
    recent[TODAY.isoformat()] = recent.get(TODAY.isoformat(), []) + new
    keep = (TODAY - dt.timedelta(days=cfg.get("page_days", 14))).isoformat()
    recent = {d: js for d, js in recent.items() if d >= keep}
    save_json(RECENT_FILE, recent)
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(render(sorted(recent.items(), reverse=True), cfg, page=True))
    log("Landing page updated: docs/index.html")

    if not args.no_email and (new or cfg.get("send_empty_digest")):
        send_email(render([(TODAY.isoformat(), new)], cfg), len(new))


if __name__ == "__main__":
    sys.exit(main())
