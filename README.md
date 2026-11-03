# PSLF Job Digest

Searches public job sources every morning for health care, government, education,
and nonprofit roles, checks whether each employer is likely to qualify for Public
Service Loan Forgiveness (PSLF), and delivers new matches two ways: a daily email and
a landing page you can bookmark. It runs free on GitHub Actions.

## Sources

| Source | Covers | Key | PSLF status |
| --- | --- | --- | --- |
| USAJOBS API | Federal jobs (VA, HHS, CDC, NIH and more) | Free, by request; usually arrives within a day | All government |
| NYC Jobs (NYC Open Data) | City agency jobs, including the Health Department | None | All government |
| Adzuna API | Hospitals, universities, nonprofits, and private employers | Free, instant; about 1,000 calls/month | Checked per employer |
| ProPublica Nonprofit Explorer | IRS 501(c)(3) records, used to check Adzuna employers | None | — |

Each Adzuna employer is looked up once in ProPublica and the result is cached, so
lookups get rarer over time. Jobs are sorted into four groups: government, verified
501(c)(3) nonprofit, worth checking, and unverified (hidden unless you turn on
`show_unverified`). Always confirm an employer with the PSLF Help Tool at
studentaid.gov before relying on it.

## Setup (about 30 minutes, plus waiting for the USAJOBS key)

1. **Request keys.**
   - USAJOBS: https://developer.usajobs.gov/apirequest/ — you'll use the same email as `USAJOBS_EMAIL`.
   - Adzuna: https://developer.adzuna.com — copy the app ID and app key.
   - Optional: an NYC Open Data app token raises rate limits but isn't needed for one run a day.
2. **Create a Gmail app password** for sending the email (Google Account → Security →
   2-Step Verification → App passwords). Your normal password won't work.
3. **Create a GitHub repository** and upload everything in this folder, keeping the
   `.github/workflows` folder structure.
4. **Add secrets** in the repo under Settings → Secrets and variables → Actions:
   `USAJOBS_API_KEY`, `USAJOBS_EMAIL`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`,
   `SMTP_USER` (your Gmail address), `SMTP_PASSWORD` (the app password),
   `EMAIL_TO` (where the digest goes), and optionally `NYC_APP_TOKEN`.
   Any source without keys is skipped, so you can start with NYC Jobs alone.
5. **Turn on the landing page:** Settings → Pages → Deploy from a branch → `main`, folder `/docs`.
   On a free account the repo must be public for Pages to work. The page shows only job
   listings; your keys and email address stay in secrets.
6. **Run it once now:** Actions tab → Daily job digest → Run workflow. After that it runs
   every morning automatically.

## Tuning

Edit `config.yaml`. The main settings:

- `search_terms` are sent to the APIs. Each term costs one Adzuna call per location per
  day, plus one for remote, so 8 terms ≈ 480 calls a month, within the free tier.
- `title_keywords` decide which results you see. Add as many as you like.
- `employer_overrides` fixes any employer the automatic check gets wrong.

## Run on your own computer

```
pip install -r requirements.txt
python job_digest.py --demo        # sample page in docs/index.html, no keys needed
export ADZUNA_APP_ID=... ADZUNA_APP_KEY=...   # plus any other keys
python job_digest.py --no-email    # real search, page only
```

## Known limits

- NYC Jobs listings have no direct link, so the digest links to a City Jobs search for
  the Job ID. If a link doesn't land on the posting, search the title on cityjobs.nyc.gov.
- NYC Health + Hospitals, the NYC Department of Education, CUNY, and New York State
  agencies post on their own sites and may not appear in NYC Jobs. Adzuna picks up some of these.
- Adzuna descriptions are excerpts, and salaries marked "(est.)" are Adzuna's estimates.
- Hospital systems often file under a different legal name, so they tend to land in
  "Worth checking"; add them to `employer_overrides` once confirmed.
