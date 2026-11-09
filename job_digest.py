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
  The Jobs API (thejobsapi.com) - paid key with a 7-day trial; jobs from employers'
                                 own career sites, checked the same way as Adzuna

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
THEJOBSAPI_URL = "https://api.thejobsapi.com/v1/jobs"
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
               closes="", salary="", category="", note="", text="", hits=[], apply="")
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


DEFAULTS = {
    "keywords": ["health", "clinical", "population", "analyst", "nurse", "nursing", "equity", "perioperative", "data"],
    "exclude_title_words": [], "exclude_employers": [], "watch_boards": [], "employer_overrides": {},
    "include_worth_checking": False, "max_employer_lookups": 150,
    "sources": {"usajobs": True, "adzuna": True, "thejobsapi": True, "watch_boards": True, "nyc": False},
    "lookback_days": 3, "adzuna_pages": 10, "thejobsapi_max_jobs": 70, "thejobsapi_max_requests": 5,
    "thejobsapi_window": "24h", "page_days": 14, "send_empty_digest": False,
}


def load_config(path=None):
    """config.json is written by the Settings page; config.yaml is the original hand-edited file."""
    if path is None:
        path = ROOT / "config.json" if (ROOT / "config.json").exists() else ROOT / "config.yaml"
    path = Path(path)
    text = path.read_text()
    loaded = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    cfg = {**DEFAULTS, **(loaded or {})}
    cfg["sources"] = {**DEFAULTS["sources"], **(cfg.get("sources") or {})}
    cfg["keywords"] = [k for k in cfg["keywords"] if str(k).strip()] or DEFAULTS["keywords"]
    log(f"Settings loaded from {path.name}")
    return cfg


# ---------------------------------------------------------------- filters
REMOTE_YES = [r"\bremote\b", r"work from home", r"\bwfh\b", r"telework from anywhere",
              r"telecommut", r"anywhere in the u\.?s"]
REMOTE_NO_ANYWHERE = [r"\bhybrid\b", r"not (a )?remote", r"remote not available"]
REMOTE_NO_HEADLINE = [r"on-?site", r"in[- ]office", r"in[- ]person"]  # checked in title/location only


# "Remote / San Diego, CA / Hybrid" or "Remote or Hybrid": remote offered as one of the options
REMOTE_OPTION = r"\bremote\b\s*(/|\||,|\bor\b)|(/|\||,|\bor\b)\s*\bremote\b"


def is_remote(headline, description=""):
    """headline = title + location. Needs a remote signal and no hybrid/on-site wording,
    unless the title/location lists fully remote as one of the options."""
    head, body = (headline or "").lower(), (description or "").lower()
    if re.search(REMOTE_OPTION, head) and not re.search(r"not (a )?remote", head + " " + body):
        return True
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


def fetch_thejobsapi(cfg):
    key = os.getenv("JOBSAPI_KEY")
    if not key:
        log("The Jobs API: no key set, skipping")
        return []
    headers = {"Authorization": f"Bearer {key}"}
    max_jobs = cfg.get("thejobsapi_max_jobs", 70)
    query = " OR ".join(f"{k}*" for k in cfg["keywords"])  # any keyword, prefix match
    base = {"q": query, "remote": "true", "window": cfg.get("thejobsapi_window", "24h"),
            "description": "text", "country": "US"}
    jobs, dropped, cursor, requests_made = [], 0, None, 0
    while len(jobs) + dropped < max_jobs and requests_made < cfg.get("thejobsapi_max_requests", 5):
        params = dict(base, limit=min(100, max_jobs - len(jobs) - dropped))
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(THEJOBSAPI_URL, headers=headers, params=params, timeout=30)
            requests_made += 1
            if r.status_code == 400 and "country" in base and not cursor:
                log(f"The Jobs API rejected country=US ({r.json().get('detail')}); retrying with location")
                base.pop("country")
                base["location"] = "United States"
                continue
            if r.status_code in (402, 403):
                log("The Jobs API: trial ended or plan inactive (see thejobsapi.com dashboard), skipping")
                break
            if r.status_code == 429:
                log(f"The Jobs API: allowance used up or rate limited ({r.json().get('quota_exceeded') or 'rate'}), skipping")
                break
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log(f"The Jobs API error: {e}")
            break
        for a in data.get("jobs", []):
            if a.get("work_arrangement") not in (None, "remote") or a.get("remote") is not True:
                dropped += 1  # filter 1: only fully remote, never hybrid
                continue
            places = ", ".join(l.get("label", "") for l in (a.get("locations") or [])[:2] if l.get("label"))
            sal = a.get("salary") or {}
            period = {"YEAR": "", "HOUR": " /hr", "MONTH": " /month", "WEEK": " /week", "DAY": " /day"}
            jobs.append(make_job(
                id=f"thejobsapi:{a.get('id')}", source="The Jobs API", title=a.get("title"),
                employer=a.get("organization"), location=f"Remote ({places})" if places else "Remote",
                url=a.get("url") or a.get("apply_url"),
                apply=a.get("apply_url") if a.get("apply_url") and a.get("apply_url") != a.get("url") else "",
                posted=(a.get("posted_at") or a.get("first_seen_at") or "")[:10],
                closes=(a.get("valid_through") or "")[:10],
                salary=money(sal.get("min"), sal.get("max"), period.get(sal.get("period"), "")) if sal else "",
                text=a.get("description_text")))
        cursor = data.get("next_cursor")
        if not cursor:
            break
        time.sleep(1)
    log(f"The Jobs API: {len(jobs)} remote postings ({dropped} hybrid/unclear dropped, {requests_made} requests)")
    return jobs


BOARD_PATTERNS = [
    ("rippling", r"ats\.rippling\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)"),
    ("greenhouse", r"greenhouse\.io/(?:embed/job_board\?for=)?([^/?#&]+)"),
    ("lever", r"jobs\.lever\.co/([^/?#]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([^/?#]+)"),
]


def parse_board(entry):
    """A watch_boards entry (a link, or a dict with url/name/category) -> (system, slug, name, category)."""
    if isinstance(entry, str):
        entry = {"url": entry}
    url = (entry.get("url") or "").strip()
    for system, pattern in BOARD_PATTERNS:
        m = re.search(pattern, url)
        if m:
            slug = m.group(1)
            name = entry.get("name") or slug.replace("-", " ").replace("_", " ").title()
            category = entry.get("category") if entry.get("category") in ("government", "nonprofit") else ""
            return system, slug, name, category
    return None


def board_jobs(system, slug):
    """Returns (id, title, url, location, remote, posted, text) tuples for one employer's board."""
    out = []
    if system == "rippling":
        r = requests.get(f"https://api.rippling.com/platform/api/ats/v2/board/{slug}/jobs",
                         params={"groupJobsByLocation": "true", "page": 0, "pageSize": 1000}, timeout=30)
        r.raise_for_status()
        for it in r.json().get("items", []):
            locs = it.get("locations") or []
            remote = any(l.get("workplaceType") == "REMOTE" for l in locs)
            def place(l):
                wt = (l.get("workplaceType") or "").replace("_", "-").lower()
                return l["name"] if not wt or wt in l["name"].lower() else f"{l['name']} ({wt})"
            where = "; ".join(place(l) for l in locs[:3] if l.get("name"))
            out.append((it.get("id"), (it.get("name") or "").strip(), it.get("url"), where, remote, "", ""))
    elif system == "greenhouse":
        r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=30)
        r.raise_for_status()
        for it in r.json().get("jobs", []):
            where = (it.get("location") or {}).get("name") or ""
            out.append((it.get("id"), it.get("title"), it.get("absolute_url"), where,
                        is_remote(f"{it.get('title')} {where}"), (it.get("first_published") or it.get("updated_at") or "")[:10], ""))
    elif system == "lever":
        r = requests.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, timeout=30)
        r.raise_for_status()
        for it in r.json():
            where = (it.get("categories") or {}).get("location") or ""
            wt = (it.get("workplaceType") or "").lower()
            remote = wt == "remote" or (wt in ("", "unspecified") and is_remote(f"{it.get('text')} {where}"))
            posted = dt.datetime.fromtimestamp(it["createdAt"] / 1000).date().isoformat() if it.get("createdAt") else ""
            out.append((it.get("id"), it.get("text"), it.get("hostedUrl"), where, remote, posted, it.get("descriptionPlain") or ""))
    elif system == "ashby":
        r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=30)
        r.raise_for_status()
        for it in r.json().get("jobs", []):
            wt = (it.get("workplaceType") or "").lower()
            remote = wt == "remote" or (not wt and it.get("isRemote") is True)
            out.append((it.get("id"), it.get("title"), it.get("jobUrl"), it.get("location") or "", remote,
                        (it.get("publishedAt") or "")[:10], it.get("descriptionPlain") or ""))
    return out


def fetch_boards(cfg):
    jobs = []
    for entry in cfg.get("watch_boards") or []:
        parsed = parse_board(entry)
        if not parsed:
            log(f"Watched boards: couldn't read {entry!r}; use an ats.rippling.com, greenhouse.io, "
                "jobs.lever.co or jobs.ashbyhq.com link")
            continue
        system, slug, name, category = parsed
        try:
            rows = board_jobs(system, slug)
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            log(f"Watched boards: {name} ({system}) failed: {e}")
            continue
        remote_rows = [row for row in rows if row[4]]
        for jid, title, url, where, _, posted, text in remote_rows:
            jobs.append(make_job(
                id=f"{system}:{slug}:{jid}", source=f"{name} careers", title=title, employer=name,
                location=where if "remote" in where.lower() else (f"Remote ({where})" if where else "Remote"), url=url, posted=posted, text=text,
                category=category, note="Set in your watch list" if category else ""))
        log(f"Watched boards: {name}: {len(remote_rows)} remote of {len(rows)} open postings")
        time.sleep(1)
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
                job["category"], job["note"], job["deferred"] = "unknown", "Lookup failed; will retry next run", True
        else:
            job["category"], job["note"], job["deferred"] = "unknown", "Lookup limit reached; will check next run", True
    save_json(EMPLOYER_CACHE, cache)
    log(f"Employer checks: {lookups} new ProPublica lookups")
    return jobs


# ---------------------------------------------------------------- output
PAGE_CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#59636e;--card:#f6f8fa;--line:#d1d9e0;--accent:#0b5cad;--ok:#1a7f37;--chip:#eaeef2}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--card:#151b23;--line:#3d444d;--accent:#4493f8;--ok:#3fb950;--chip:#262c36}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:860px;margin:auto;padding:24px 16px}h1{font-size:1.6rem;margin:0 0 4px}
h2{font-size:1.15rem;margin:28px 0 4px;border-bottom:1px solid var(--line);padding-bottom:4px}
h3{font-size:1rem;margin:18px 0 2px}h3+p{margin:0}.muted{color:var(--muted);font-size:.9rem}
.job{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin:8px 0}
.job a{color:var(--accent);font-weight:600;text-decoration:none}.job a.apply{font-weight:400;font-size:.85rem;margin-left:8px}.job a:hover{text-decoration:underline}
.actions{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
button{font:inherit;font-size:.85rem;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:4px 10px;cursor:pointer}
button:hover{border-color:var(--accent)}button.primary{color:var(--ok);border-color:var(--ok)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:18px 0 6px}
.tab{padding:6px 12px;border-radius:999px;background:var(--chip);border-color:transparent;font-weight:600}
.tab[aria-selected="true"]{background:var(--accent);color:var(--bg)}
.tab span{font-weight:400;margin-left:4px}
.tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
[hidden]{display:none!important}
.nav{display:flex;gap:18px;margin-bottom:14px;font-weight:600}.nav a{color:var(--muted);text-decoration:none;padding-bottom:2px}
.nav a[aria-current="page"]{color:var(--fg);border-bottom:2px solid var(--accent)}.nav a:hover{color:var(--accent)}
"""

PAGE_JS = r"""
(function () {
  var KEY = "pslf-digest-marks-v1";
  var today = new Date().toISOString().slice(0, 10);
  var marks = {}, canSave = true, view = "review";
  try { marks = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) { canSave = false; }
  var cutoff = new Date(Date.now() - 90 * 864e5).toISOString().slice(0, 10);
  Object.keys(marks).forEach(function (id) {   // forget old "hide" marks; applied ones are kept
    var m = marks[id];
    if (m && m.hidden && !m.applied && m.hidden < cutoff) delete marks[id];
  });
  function $(s) { return document.querySelector(s); }
  function $$(s, root) { return Array.prototype.slice.call((root || document).querySelectorAll(s)); }
  function save() {
    if (!canSave) { $("#storage-warn").hidden = false; return; }
    try { localStorage.setItem(KEY, JSON.stringify(marks)); }
    catch (e) { canSave = false; $("#storage-warn").hidden = false; }
  }
  function fmt(d) {
    try { return new Date(d + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }); }
    catch (e) { return d; }
  }
  function el(tag, cls, text) { var n = document.createElement(tag); if (cls) n.className = cls; if (text) n.textContent = text; return n; }

  function render() {
    var cards = $$(".job[data-id]"), toReview = 0, hidden = 0;
    cards.forEach(function (card) {
      var m = marks[card.dataset.id] || {};
      var isApplied = !!m.applied, isHidden = !!m.hidden && !isApplied;
      if (isHidden) hidden++; else if (!isApplied) toReview++;
      card.hidden = view === "review" ? (isApplied || isHidden) : view === "hidden" ? !isHidden : true;
      card.querySelector('[data-act="hide"]').textContent = isHidden ? "Unhide" : "Hide";
    });
    $$(".group").forEach(function (g) {
      var n = $$(".job:not([hidden])", g).length;
      g.hidden = !n; g.querySelector(".gcount").textContent = n;
    });
    $$(".day").forEach(function (d) {
      var n = $$(".job:not([hidden])", d).length;
      d.hidden = !n; d.querySelector(".count").textContent = n + (view === "hidden" ? " hidden" : " to review");
    });
    var applied = Object.keys(marks).filter(function (id) { return marks[id] && marks[id].applied; })
      .sort(function (a, b) { return marks[b].applied.localeCompare(marks[a].applied); });
    $("#count-review").textContent = toReview;
    $("#count-applied").textContent = applied.length;
    $("#count-hidden").textContent = hidden;

    var list = $("#applied-list");
    list.hidden = view !== "applied";
    list.textContent = "";
    if (view === "applied") {
      if (!applied.length) list.appendChild(el("p", "muted", "Nothing marked as applied yet."));
      applied.forEach(function (id) {
        var m = marks[id], card = el("div", "job");
        var link = el("a", "", m.title || "Untitled job");
        if (/^https?:\/\//.test(m.url || "")) link.href = m.url;
        card.appendChild(link);
        card.appendChild(el("div", "muted", (m.employer ? m.employer + " · " : "") + "Applied " + fmt(m.applied)));
        var actions = el("div", "actions"), undo = el("button", "", "Undo");
        undo.type = "button"; undo.dataset.undo = id;
        actions.appendChild(undo); card.appendChild(actions); list.appendChild(card);
      });
    }
    var empty = $("#empty");
    empty.hidden = view === "applied" || cards.some(function (c) { return !c.hidden; });
    empty.textContent = view === "hidden" ? "No hidden jobs." : "You're all caught up. New jobs appear here each morning.";
    $$(".tab").forEach(function (t) { t.setAttribute("aria-selected", String(t.dataset.view === view)); });
  }

  document.addEventListener("click", function (e) {
    var tab = e.target.closest(".tab");
    if (tab) { view = tab.dataset.view; render(); return; }
    var btn = e.target.closest("button[data-act]");
    if (btn) {
      var card = btn.closest(".job"), id = card.dataset.id, m = marks[id] || {};
      if (btn.dataset.act === "applied") {
        marks[id] = { applied: today, title: card.dataset.title, employer: card.dataset.employer, url: card.dataset.url };
      } else if (m.hidden) {
        delete m.hidden;
        if (Object.keys(m).length) marks[id] = m; else delete marks[id];
      } else {
        m.hidden = today; marks[id] = m;
      }
      save(); render(); return;
    }
    var undo = e.target.closest("button[data-undo]");
    if (undo) { delete marks[undo.dataset.undo]; save(); render(); }
  });

  $("#export").addEventListener("click", function () {
    var blob = new Blob([JSON.stringify(marks, null, 1)], { type: "application/json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "pslf-digest-marks-" + today + ".json";
    document.body.appendChild(a); a.click(); a.remove();
  });
  $("#import").addEventListener("click", function () { $("#import-file").click(); });
  $("#import-file").addEventListener("change", function (e) {
    var file = e.target.files[0]; if (!file) return;
    var reader = new FileReader();
    reader.onload = function () {
      try {
        var data = JSON.parse(reader.result);
        if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("bad file");
        Object.keys(data).forEach(function (id) { if (data[id] && typeof data[id] === "object") marks[id] = data[id]; });
        save(); render(); alert("Marks imported.");
      } catch (err) { alert("That file doesn't look like an export from this page."); }
      e.target.value = "";
    };
    reader.readAsText(file);
  });
  if (!canSave) $("#storage-warn").hidden = false;
  render();
})();
"""


def allowed_categories(cfg):
    return {"government", "nonprofit"} | ({"check"} if cfg.get("include_worth_checking") else set())


def render(jobs_by_day, cfg, page=False):
    esc = html.escape
    st = (lambda style: "") if page else (lambda style: f" style='{style}'")  # inline styles only for email
    muted = st("color:#59636e;font-size:13px")
    allowed = allowed_categories(cfg)
    out = []
    for day, jobs in jobs_by_day:
        day_jobs = [j for j in jobs if j["category"] in allowed]
        if page:
            if not day_jobs:
                continue
            out.append(f"<section class='day'><h2>{esc(day)} · <span class='count'>{len(day_jobs)} new</span></h2>")
        for cat, heading, blurb in SECTIONS:
            if cat not in allowed:
                continue
            group = sorted((j for j in day_jobs if j["category"] == cat),
                           key=lambda j: (-len(j.get("hits") or []), j["title"].lower()))
            if not group:
                continue
            if page:
                out.append(f"<section class='group'><h3>{heading} (<span class='gcount'>{len(group)}</span>)</h3>"
                           f"<p class='muted'>{blurb}</p>")
            else:
                out.append(f"<h3>{heading} ({len(group)})</h3><p{st('margin:0;color:#59636e;font-size:13px')}>{blurb}</p>")
            for j in group:
                bits = [j["employer"], j["location"], j["salary"],
                        f"closes {j['closes']}" if j["closes"] else "", j["source"]]
                if j.get("hits"):
                    bits.append("matches: " + ", ".join(j["hits"]))
                meta = " · ".join(esc(b) for b in bits if b)
                apply = (f" <a class='apply' href='{esc(j['apply'])}'{st('color:#0b5cad;font-size:13px')}>Apply directly</a>"
                         if j.get("apply") else "")
                data = (f" data-id='{esc(j['id'])}' data-title='{esc(j['title'])}'"
                        f" data-employer='{esc(j['employer'])}' data-url='{esc(j['url'])}'") if page else ""
                actions = ("<div class='actions'><button type='button' class='primary' data-act='applied'>Mark applied</button>"
                           "<button type='button' data-act='hide'>Hide</button></div>") if page else ""
                out.append(
                    f"<div class='job'{data}{st('border:1px solid #d1d9e0;border-radius:8px;padding:10px 14px;margin:8px 0')}>"
                    f"<a href='{esc(j['url'])}'{st('color:#0b5cad;font-weight:600')}>{esc(j['title'])}</a>{apply}"
                    f"<div class='muted'{muted}>{meta}</div>"
                    f"<div class='muted'{muted}>PSLF: {esc(j['note'])}</div>{actions}</div>")
            if page:
                out.append("</section>")
        if page:
            out.append("</section>")
    footer = (f"<p class='muted'{st('color:#59636e;font-size:13px;margin-top:28px')}>PSLF eligibility depends on the "
              "employer and on working full time (30+ hours/week). Confirm any employer with the PSLF Help Tool at "
              "studentaid.gov before relying on it.</p>")
    title = "PSLF Job Digest"
    if not page:
        if not out:
            out.append("<p>No new remote government or nonprofit jobs matched today.</p>")
        doc = f"<h2 style='margin-top:0'>{title} · {TODAY:%b %d}</h2>" + "".join(out) + footer
        return f"<div style='font-family:Arial,sans-serif;max-width:720px'>{doc}</div>"
    header = ("<nav class='nav'><a href='./' aria-current='page'>Results</a><a href='settings.html'>Settings</a></nav>"
              f"<h1>{title}</h1><p class='muted'>Remote only · government and nonprofit · Updated "
              f"{TODAY:%A, %B %d, %Y} · last {cfg.get('page_days', 14)} days</p>"
              "<div class='tabs' role='tablist'>"
              "<button type='button' class='tab' role='tab' data-view='review'>To review<span id='count-review'></span></button>"
              "<button type='button' class='tab' role='tab' data-view='applied'>Applied<span id='count-applied'></span></button>"
              "<button type='button' class='tab' role='tab' data-view='hidden'>Hidden<span id='count-hidden'></span></button></div>"
              "<div class='tools'><span class='muted'>Marks are saved in this browser only.</span>"
              "<button type='button' id='export'>Export marks</button><button type='button' id='import'>Import marks</button>"
              "<input type='file' id='import-file' accept='.json,application/json' hidden></div>"
              "<p id='storage-warn' class='muted' hidden>This browser is blocking storage, so marks won't be saved after you leave the page.</p>")
    doc = header + "".join(out) + "<div id='applied-list' hidden></div><p id='empty' class='muted' hidden></p>" + footer
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{title}</title>"
            f"<style>{PAGE_CSS}</style></head><body><main>{doc}</main><script>{PAGE_JS}</script></body></html>")


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
    ap.add_argument("--config", default=None, help="defaults to config.json (saved by the Settings page), else config.yaml")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--demo", action="store_true", help="render sample jobs without calling any API")
    args = ap.parse_args()
    cfg = load_config(args.config)

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
             (fetch_adzuna(cfg) if src.get("adzuna", True) else []) +
             (fetch_thejobsapi(cfg) if src.get("thejobsapi", True) else []) +
             (fetch_boards(cfg) if src.get("watch_boards", True) else []))
    # Filter 1 (remote) happened inside each fetcher.
    seen = load_json(SEEN_FILE, {})
    unique, skipped = {}, 0
    for job in found:
        if job["id"] in seen or job["id"] in unique:
            continue
        if excluded(job, cfg):
            skipped += 1
            continue
        job["hits"] = keyword_hits(job, cfg)          # filter 3, checked before the slower employer lookups
        if job["hits"]:
            unique[job["id"]] = job
    log(f"Exclusions: {skipped} new jobs dropped by exclude_title_words/exclude_employers "
        f"(currently {cfg.get('exclude_title_words') or []})")
    candidates = classify(list(unique.values()), cfg)  # filter 2
    allowed = allowed_categories(cfg)
    new = [j for j in candidates if j["category"] in allowed]
    log(f"{len(found)} remote postings → {len(unique)} keyword matches → {len(new)} at government/nonprofit employers")

    deferred = 0
    for job in candidates:  # mark checked jobs as seen; jobs whose employer wasn't checked yet come back next run
        if job.pop("deferred", False):
            deferred += 1
            continue
        seen[job["id"]] = TODAY.isoformat()
    if deferred:
        log(f"{deferred} jobs held for the next run (employer not checked yet)")
    cutoff = (TODAY - dt.timedelta(days=120)).isoformat()
    save_json(SEEN_FILE, {k: v for k, v in seen.items() if v >= cutoff})

    for job in new:
        job.pop("text", None)
    recent = load_json(RECENT_FILE, {})
    recent[TODAY.isoformat()] = recent.get(TODAY.isoformat(), []) + new
    keep = (TODAY - dt.timedelta(days=cfg.get("page_days", 14))).isoformat()
    recent = {d: js for d, js in recent.items() if d >= keep}
    before = sum(len(js) for js in recent.values())  # re-apply current exclusions to jobs saved earlier
    recent = {d: [x for x in js if not excluded(x, cfg)] for d, js in recent.items()}
    removed = before - sum(len(js) for js in recent.values())
    if removed:
        log(f"Removed {removed} previously saved jobs that match your current exclusions")
    save_json(RECENT_FILE, recent)
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(render(sorted(recent.items(), reverse=True), cfg, page=True))
    log("Landing page updated: docs/index.html")

    if not args.no_email and (new or cfg.get("send_empty_digest")):
        send_email(render([(TODAY.isoformat(), new)], cfg), len(new))


if __name__ == "__main__":
    sys.exit(main())
