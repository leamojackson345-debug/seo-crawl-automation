# Free automated technical SEO crawl (no Screaming Frog)

Every week: crawl your site with a built-in Python crawler, flag critical issues, post the summary to Slack and/or email.

Files:
- `crawler.py`: free crawler (requests + BeautifulSoup)
- `seo_audit.py`: finds the issues and sends Slack/email
- `important_pages.txt`: which pages count as important
- `.github/workflows/seo-crawl.yml`: weekly schedule
- `requirements.txt`

(`seo_audit.py` also contains optional Screaming Frog support; ignore it.)

## What gets flagged
- 4xx and 5xx pages (plus pages that don't respond), with the pages linking to them
- Redirect chains of more than 1 hop, and redirect loops
- noindex / nofollow on important pages (meta tag or X-Robots-Tag header)
- Missing canonicals, canonicals pointing to a different URL, canonicals pointing to a broken/redirecting/noindex URL, conflicting canonicals

## Setup (about 10 minutes)
1. Create a GitHub repo and upload all files, keeping the folder layout (workflow file inside `.github/workflows/`).
2. Edit `important_pages.txt` with your key pages (e.g. `/pricing`, `/products/*`).
3. Repo -> Settings -> Secrets and variables -> Actions.
   - Variables: `SITE_URL` (e.g. `https://example.com/`), optional `MAX_PAGES` (default 2000)
   - Secrets for Slack: `SLACK_WEBHOOK_URL`
   - Secrets for email: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM`, `EMAIL_TO`
   (Slack or email, you don't need both.)
4. Actions tab -> "Weekly technical SEO crawl" -> Run workflow. Check Slack/email.

Slack webhook: api.slack.com/apps -> Create app -> Incoming Webhooks -> add to your channel -> copy the URL.
Gmail SMTP: `smtp.gmail.com`, port 587, use an App Password.

## Run it on your own computer
```bash
pip install -r requirements.txt
python crawler.py --url https://example.com/ --out crawl-output/latest --max-pages 500
python seo_audit.py --skip-crawl --crawl-dir crawl-output/latest --important-pages important_pages.txt --no-notify
```
Results: `crawl-output/latest/issues.csv`.

## Cron alternative (server)
```
0 3 * * 1 cd /opt/seo-crawl && SITE_URL=https://example.com/ SLACK_WEBHOOK_URL=... python3 crawler.py --url $SITE_URL --out crawl-output/latest && python3 seo_audit.py --skip-crawl --crawl-dir crawl-output/latest
```

## Limits to know
- It does not run JavaScript. Sites that build links or canonicals only with JS will be under-reported.
- It respects robots.txt, follows internal links only, and stops at `--max-pages`.
- Be gentle with small servers: lower `--workers` or add `--delay 0.5`.
- GitHub-hosted runs are free for public repos and use free minutes on private ones.
