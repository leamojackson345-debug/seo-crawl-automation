#!/usr/bin/env python3
"""
Free, simple site crawler (no Screaming Frog needed).

Crawls one site, follows internal links, records status codes, redirects,
robots directives and canonicals, and writes an `internal_all.csv` that
seo_audit.py can analyse (use: seo_audit.py --skip-crawl --crawl-dir <out>).

Limits: does not run JavaScript (pages that only show links/canonicals after JS
are not seen). Respects robots.txt.

Usage:
  python crawler.py --url https://example.com/ --out crawl-output/latest --max-pages 2000
"""
from __future__ import annotations

import argparse
import csv
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

# Links matching any of these regexes are not crawled (language switchers, login, filter pages...)
DEFAULT_EXCLUDES = [
    r"/change/(language|currency)",
    r"/(login|logout|favorites|auth)(/|\?|$)",
    r"/used-cars/search\?",
]

FIELDS = [
    "Address", "Content Type", "Status Code", "Status", "Indexability",
    "Meta Robots 1", "X-Robots-Tag 1", "Canonical Link Element 1",
    "Canonical Link Element 2", "Crawl Depth", "Inlinks", "Redirect URL",
]


def host_key(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def clean(url: str) -> str:
    return urldefrag(url.strip())[0]


def fetch(session, url, timeout, ua, delay):
    """Fetch one URL without following redirects. Returns (record, links)."""
    if delay:
        time.sleep(delay)
    rec = {k: "" for k in FIELDS}
    rec["Address"] = url
    links: list[str] = []
    r = None
    for attempt in (1, 2):  # retry once on connection problems only
        try:
            r = session.get(url, timeout=timeout, allow_redirects=False,
                            headers={"User-Agent": ua}, stream=True)
            break
        except requests.RequestException as exc:
            if attempt == 2:
                rec.update({"Status Code": 0, "Status": f"No response: {type(exc).__name__}"})
                return rec, links

    rec["Status Code"] = r.status_code
    rec["Status"] = r.reason or ""
    ctype = r.headers.get("Content-Type", "").split(";")[0].strip()
    rec["Content Type"] = ctype
    rec["X-Robots-Tag 1"] = r.headers.get("X-Robots-Tag", "")

    if 300 <= r.status_code < 400 and r.headers.get("Location"):
        target = clean(urljoin(url, r.headers["Location"]))
        rec["Redirect URL"] = target
        links.append(target)
        r.close()
        return rec, links

    if r.status_code == 200 and "html" in ctype.lower():
        body = r.content[:3_000_000]
        soup = BeautifulSoup(body, "html.parser")
        robots = [m.get("content", "") for m in soup.find_all("meta")
                  if (m.get("name") or "").lower() in ("robots", "googlebot")]
        rec["Meta Robots 1"] = ", ".join(x for x in robots if x)
        canons = [clean(urljoin(url, l["href"])) for l in soup.find_all("link", href=True)
                  if "canonical" in [x.lower() for x in (l.get("rel") or [])]]
        rec["Canonical Link Element 1"] = canons[0] if canons else ""
        rec["Canonical Link Element 2"] = canons[1] if len(canons) > 1 else ""
        directives = f"{rec['Meta Robots 1']} {rec['X-Robots-Tag 1']}".lower()
        rec["Indexability"] = "Non-Indexable" if "noindex" in directives else "Indexable"
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
                continue
            links.append(clean(urljoin(url, href)))
    r.close()
    return rec, links


def crawl(start, max_pages, workers, delay, timeout, ua, excludes=()):
    start = clean(start)
    root = host_key(start)
    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=workers))
    session.mount("http://", requests.adapters.HTTPAdapter(pool_maxsize=workers))

    rp = robotparser.RobotFileParser()
    try:
        resp = session.get(urljoin(start, "/robots.txt"), timeout=timeout, headers={"User-Agent": ua})
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
            print("robots.txt loaded")
        else:
            rp = None  # 403/404/etc: no usable robots.txt, so allow everything
            print(f"robots.txt returned {resp.status_code}: ignoring it")
    except requests.RequestException as exc:
        rp = None
        print(f"robots.txt not reachable ({type(exc).__name__}): ignoring it")

    depth = {start: 0}
    records: dict[str, dict] = {}
    inlinks: dict[str, set[str]] = {}
    queue = [start]

    def allowed(u):
        return rp is None or rp.can_fetch(ua, u)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while queue and len(records) < max_pages:
            batch = queue[: max_pages - len(records)]
            queue = queue[len(batch):]
            to_fetch = []
            for u in batch:
                if allowed(u):
                    to_fetch.append(u)
                else:
                    rec = {k: "" for k in FIELDS}
                    rec.update({"Address": u, "Status Code": 0, "Status": "Blocked by robots.txt",
                                "Crawl Depth": depth[u]})
                    records[u] = rec
            results = list(pool.map(lambda u: (u, *fetch(session, u, timeout, ua, delay)), to_fetch))
            for u, rec, links in results:
                rec["Crawl Depth"] = depth[u]
                records[u] = rec
                for link in links:
                    if not link.startswith(("http://", "https://")) or host_key(link) != root:
                        continue
                    if any(rx.search(link) for rx in excludes):
                        continue
                    inlinks.setdefault(link, set()).add(u)
                    if link not in depth:
                        depth[link] = depth[u] + 1
                        queue.append(link)
            print(f"crawled {len(records)} URLs, {len(queue)} queued", flush=True)

    for u, rec in records.items():
        rec["Inlinks"] = len(inlinks.get(u, ()))
    return records, inlinks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", default="crawl-output/latest")
    ap.add_argument("--max-pages", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--delay", type=float, default=0.0, help="Seconds to wait before each request, per worker")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--exclude", action="append", default=[], help="Regex of URLs not to crawl (repeatable)")
    ap.add_argument("--no-default-excludes", action="store_true")
    ap.add_argument("--user-agent", default="SEO-Audit-Bot/1.0 (+technical SEO monitoring)")
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    patterns = a.exclude + ([] if a.no_default_excludes else DEFAULT_EXCLUDES)
    excludes = [re.compile(x) for x in patterns]
    records, inlinks = crawl(a.url, a.max_pages, a.workers, a.delay, a.timeout, a.user_agent, excludes)
    if len(records) <= 1:
        first = next(iter(records.values()), {})
        print(f"WARNING: only {len(records)} URL(s) crawled. First URL: status={first.get('Status Code')} "
              f"{first.get('Status')}. The site may be blocking this crawler.")

    with open(out / "internal_all.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(records.values())

    # Linking pages for broken URLs (used to show "where to fix it")
    with open(out / "broken_inlinks.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Source", "Destination"])
        for dst, rec in records.items():
            if int(rec["Status Code"] or 0) >= 400:
                for src in sorted(inlinks.get(dst, ()))[:5]:
                    w.writerow([src, dst])
    print(f"Done: {len(records)} URLs -> {out/'internal_all.csv'}")


if __name__ == "__main__":
    main()
