# PSLF Job Digest

Finds **remote** jobs at **government and nonprofit** employers every morning and
delivers new matches as a daily email and a landing page you can bookmark. It runs
free on GitHub Actions.

## How jobs are filtered

1. **Remote (mandatory).** The posting must clearly say remote, work from home, or
   anywhere in the U.S., and must not say hybrid. A title or location that says on-site
   or in-office is also rejected. Location and salary are otherwise ignored.
2. **Employer (mandatory).** Government employers always pass. Any other employer
   passes only if its name matches an IRS 501(c)(3) record in ProPublica's Nonprofit
   Explorer. Set `include_worth_checking: true` to also see uncertain matches.
3. **Keywords.** The title or description must contain at least one of your keywords.
   Jobs with more matches are listed first, and each job shows which words matched.

## Sources

| Source | Covers | Key |
| --- | --- | --- |
| USAJOBS API | Remote federal jobs | Free, by request; usually arrives within a day |
| Adzuna API | Remote jobs nationwide; nonprofits found here are checked against IRS records | Free, instant; about 1,000 calls/month |
| ProPublica Nonprofit Explorer | IRS 501(c)(3) records for the employer check | None |
| NYC Jobs (NYC Open Data) | City agency jobs; off by default because they are rarely fully remote | None |

Always confirm an employer with the PSLF Help Tool at studentaid.gov before relying
on it for PSLF.

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

- `keywords` is your topic list. Add or remove words freely; it costs nothing extra.
- `include_worth_checking` shows employers that are probably nonprofit or public but
  didn't match an IRS record exactly, which is common for hospital systems.
- `employer_overrides` fixes any employer the automatic check gets wrong.
- `adzuna_pages` controls how many Adzuna results are scanned each day (50 per page).

## Run on your own computer

```
pip install -r requirements.txt
python job_digest.py --demo        # sample page in docs/index.html, no keys needed
export ADZUNA_APP_ID=... ADZUNA_APP_KEY=...   # plus any other keys
python job_digest.py --no-email    # real search, page only
```

## Known limits

- Adzuna descriptions are short excerpts, so a remote job that only mentions it deep in
  the full posting can be missed. Strictness here is deliberate: it keeps hybrid and
  on-site jobs out.
- Hospital systems often file taxes under a different legal name, so they may fail the
  employer check. Add them to `employer_overrides` once you've confirmed them.
- Salaries marked "(est.)" are Adzuna's estimates.
