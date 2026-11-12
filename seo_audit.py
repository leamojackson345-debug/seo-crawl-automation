#!/usr/bin/env python3
"""
Scheduled technical SEO audit: run Screaming Frog headless, flag critical issues,
notify Slack and/or email. Standard library only (no pip install needed).

Flags:
  1. 4xx / 5xx (and no-response) internal URLs
  2. Redirect chains (> 1 hop) and loops
  3. noindex / nofollow on IMPORTANT pages
  4. Missing canonicals, canonical mismatches, canonicals pointing at bad URLs

Environment variables (all optional except SITE_URL / --url):
  SITE_URL, SLACK_WEBHOOK_URL,
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_FROM, EMAIL_TO (comma separated),
  RUN_URL (link to the CI run, added to messages)
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2}

CATEGORIES = {
    "http_5xx": "5xx server errors",
    "http_4xx": "4xx client errors",
    "no_response": "No response / connection errors",
    "redirect_loop": "Redirect loops",
    "redirect_chain": "Redirect chains (more than 1 hop)",
    "noindex": "noindex on important pages",
    "nofollow": "nofollow on important pages",
    "canonical_missing": "Missing canonical tag",
    "canonical_mismatch": "Canonical points to a different URL",
    "canonical_bad_target": "Canonical points to a redirecting / error / noindex URL",
    "canonical_multiple": "Multiple conflicting canonicals",
}

# Screaming Frog exports (names must match the SF UI / `--help export-tabs` output)
EXPORT_TABS = [
    "Internal:All",
    "Response Codes:Client Error (4xx)",
    "Response Codes:Server Error (5xx)",
    "Response Codes:Redirection (3xx)",
]
BULK_EXPORTS = [
    "Response Codes:Client Error (4xx) Inlinks",
    "Response Codes:Server Error (5xx) Inlinks",
]
REPORTS = ["Redirects:Redirect Chains"]


# --------------------------------------------------------------------------- #
# Crawl
# --------------------------------------------------------------------------- #
def run_crawl(args, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        args.sf_bin,
        "--crawl", args.url,
        "--headless",
        "--overwrite",
        "--output-folder", str(out_dir),
        "--export-format", "csv",
        "--export-tabs", ",".join(EXPORT_TABS),
        "--bulk-export", ",".join(BULK_EXPORTS),
        "--save-report", ",".join(REPORTS),
    ]
    if args.config:
        if Path(args.config).exists():
            cmd += ["--config", args.config]
        else:
            print(f"WARNING: config file {args.config} not found, using defaults", file=sys.stderr)
    if args.xvfb:
        cmd = ["xvfb-run", "-a"] + cmd
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True, timeout=args.crawl_timeout)


# --------------------------------------------------------------------------- #
# Loading the export
# --------------------------------------------------------------------------- #
@dataclass
class Page:
    address: str
    status: int
    status_text: str
    content_type: str
    indexability: str
    meta_robots: str
    x_robots: str
    canonical: str
    canonical2: str
    depth: int
    inlinks: int
    redirect_url: str

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type.lower()

    @property
    def directives(self) -> str:
        return f"{self.meta_robots} {self.x_robots}".lower()


def to_int(value: str, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def norm_url(url: str) -> str:
    """Normalise for comparison only: lowercase scheme/host, drop fragment and trailing slash."""
    parts = urlsplit(url.strip())
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def load_pages(path: Path) -> list[Page]:
    rows = read_csv(path)
    if not rows:
        sys.exit(f"{path} is empty - did the crawl fail?")
    header_map = {k.strip().lower(): k for k in rows[0] if k}
    for needed in ("address", "status code"):
        if needed not in header_map:
            sys.exit(f"Column '{needed}' missing from {path.name}. Columns found: {list(header_map)}")

    def g(row: dict, name: str) -> str:
        key = header_map.get(name.lower())
        return (row.get(key) or "").strip() if key else ""

    pages = []
    for r in rows:
        addr = g(r, "Address")
        if not addr:
            continue
        pages.append(Page(
            address=addr,
            status=to_int(g(r, "Status Code")),
            status_text=g(r, "Status"),
            content_type=g(r, "Content Type"),
            indexability=g(r, "Indexability"),
            meta_robots=g(r, "Meta Robots 1"),
            x_robots=g(r, "X-Robots-Tag 1"),
            canonical=g(r, "Canonical Link Element 1"),
            canonical2=g(r, "Canonical Link Element 2"),
            depth=to_int(g(r, "Crawl Depth"), default=99),
            inlinks=to_int(g(r, "Inlinks")),
            redirect_url=g(r, "Redirect URL"),
        ))
    return pages


def load_inlink_sources(crawl_dir: Path, per_url: int = 3) -> dict[str, list[str]]:
    """Map broken destination URL -> a few linking pages (from the bulk inlinks exports)."""
    sources: dict[str, list[str]] = defaultdict(list)
    for f in crawl_dir.rglob("*.csv"):
        if "inlinks" not in f.name.lower():
            continue
        for r in read_csv(f):
            src, dst = r.get("Source", ""), r.get("Destination", "")
            if src and dst and len(sources[norm_url(dst)]) < per_url:
                sources[norm_url(dst)].append(src)
    return sources


# --------------------------------------------------------------------------- #
# "Important page" definition
# --------------------------------------------------------------------------- #
class Importance:
    """
    A page is important if ANY of these is true:
      - its URL/path matches a line in the important-pages file
      - crawl depth <= max_depth
      - inlinks >= min_inlinks (if min_inlinks > 0)

    File syntax (one per line, '#' comments):
      https://example.com/pricing     exact URL
      /blog/*                         path prefix
      /about                          exact path
      re:^https://example\\.com/p/\\d+   regex on full URL
    """

    def __init__(self, file: str | None, max_depth: int, min_inlinks: int):
        self.exact_urls, self.exact_paths, self.prefixes, self.regexes = set(), set(), [], []
        self.max_depth, self.min_inlinks = max_depth, min_inlinks
        if file and Path(file).exists():
            for line in Path(file).read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("re:"):
                    self.regexes.append(re.compile(line[3:]))
                elif line.startswith("http"):
                    self.exact_urls.add(norm_url(line))
                elif line.endswith("*"):
                    self.prefixes.append(line[:-1])
                else:
                    self.exact_paths.add(line.rstrip("/") or "/")

    def __call__(self, p: Page) -> bool:
        path = urlsplit(p.address).path or "/"
        path_n = path.rstrip("/") or "/"
        return (
            norm_url(p.address) in self.exact_urls
            or path_n in self.exact_paths
            or any(path.startswith(x) for x in self.prefixes)
            or any(r.search(p.address) for r in self.regexes)
            or p.depth <= self.max_depth
            or (self.min_inlinks > 0 and p.inlinks >= self.min_inlinks)
        )


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def analyse(pages: list[Page], is_important, link_sources, all_canonical_mismatches=False) -> list[dict]:
    issues: list[dict] = []

    def add(cat, sev, p_or_url, detail, important=False, status="", inlinks=""):
        url = p_or_url.address if isinstance(p_or_url, Page) else p_or_url
        issues.append({
            "severity": sev, "category": cat, "url": url, "detail": detail,
            "important": "yes" if important else "no", "status_code": status,
            "inlinks": inlinks, "example_sources": " | ".join(link_sources.get(norm_url(url), [])),
        })

    by_url = {norm_url(p.address): p for p in pages}

    # 1. 4xx / 5xx / no response -------------------------------------------
    for p in pages:
        imp = is_important(p)
        if 500 <= p.status <= 599:
            add("http_5xx", "critical", p, f"HTTP {p.status} {p.status_text}", imp, p.status, p.inlinks)
        elif 400 <= p.status <= 499:
            add("http_4xx", "critical" if imp else "high", p, f"HTTP {p.status} {p.status_text}", imp, p.status, p.inlinks)
        elif p.status == 0 and "blocked" not in p.status_text.lower():
            add("no_response", "high", p, p.status_text or "No response", imp, 0, p.inlinks)

    # 2. Redirect chains / loops --------------------------------------------
    redirects = {
        norm_url(p.address): norm_url(p.redirect_url)
        for p in pages if 300 <= p.status < 400 and p.redirect_url
    }
    redirect_targets = set(redirects.values())
    seen_loops: set[frozenset] = set()
    for start in redirects:
        hops, cur, seen, loop = 0, start, {start}, False
        while cur in redirects and hops < 25:
            nxt = redirects[cur]
            hops += 1
            if nxt in seen:
                loop = True
                break
            seen.add(nxt)
            cur = nxt
        page = by_url.get(start)
        imp = is_important(page) if page else False
        if loop:
            key = frozenset(seen)
            if key not in seen_loops:
                seen_loops.add(key)
                add("redirect_loop", "critical", start, f"Loop after {hops} hops", imp, page.status if page else "", page.inlinks if page else "")
        elif hops > 1 and start not in redirect_targets:  # report from chain start only
            final = by_url.get(cur)
            final_status = final.status if final else "unknown"
            broken_end = isinstance(final_status, int) and final_status >= 400
            add("redirect_chain", "critical" if broken_end else "high", start,
                f"{hops} hops -> {cur} (final status: {final_status})", imp,
                page.status if page else "", page.inlinks if page else "")

    # 3 & 4. Robots directives and canonicals (200 HTML pages only) ---------
    for p in pages:
        if p.status != 200 or not p.is_html:
            continue
        imp = is_important(p)
        d = p.directives

        if imp and "noindex" in d:
            add("noindex", "critical", p, f"Directive: meta='{p.meta_robots}' x-robots='{p.x_robots}'", imp, p.status, p.inlinks)
        if imp and "nofollow" in d:
            add("nofollow", "high", p, f"Directive: meta='{p.meta_robots}' x-robots='{p.x_robots}'", imp, p.status, p.inlinks)

        canon = p.canonical
        if not canon:
            if "noindex" not in d:
                add("canonical_missing", "high" if imp else "medium", p, "No canonical link element", imp, p.status, p.inlinks)
            continue

        if p.canonical2 and norm_url(p.canonical2) != norm_url(canon):
            add("canonical_multiple", "high", p, f"{canon} vs {p.canonical2}", imp, p.status, p.inlinks)

        if norm_url(canon) != norm_url(p.address):
            if imp or all_canonical_mismatches:
                add("canonical_mismatch", "high" if imp else "medium", p, f"canonical -> {canon}", imp, p.status, p.inlinks)
            target = by_url.get(norm_url(canon))
            if target is not None:
                if target.status >= 300:
                    add("canonical_bad_target", "critical" if imp else "high", p,
                        f"canonical {canon} returns {target.status}", imp, p.status, p.inlinks)
                elif "noindex" in target.directives:
                    add("canonical_bad_target", "critical" if imp else "high", p,
                        f"canonical {canon} is noindex", imp, p.status, p.inlinks)

    issues.sort(key=lambda i: (SEV_ORDER[i["severity"]], i["category"], -to_int(str(i["inlinks"]))))
    return issues


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def write_outputs(out_dir: Path, issues: list[dict], pages: list[Page], site: str) -> dict:
    fields = ["severity", "category", "url", "detail", "important", "status_code", "inlinks", "example_sources"]
    with open(out_dir / "issues.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(issues)
    summary = {
        "site": site,
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pages_crawled": len(pages),
        "total_issues": len(issues),
        "by_severity": dict(Counter(i["severity"] for i in issues)),
        "by_category": dict(Counter(i["category"] for i in issues)),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def build_report(summary: dict, issues: list[dict], run_url: str, top_n: int) -> str:
    sev = summary["by_severity"]
    lines = [
        f"*Technical SEO crawl: {summary['site']}*",
        f"{summary['pages_crawled']} URLs crawled | "
        f"{sev.get('critical', 0)} critical, {sev.get('high', 0)} high, {sev.get('medium', 0)} medium",
    ]
    if not issues:
        lines.append("\nNo issues found.")
    for cat, label in CATEGORIES.items():
        items = [i for i in issues if i["category"] == cat]
        if not items:
            continue
        lines.append(f"\n*{label}: {len(items)}*")
        for i in items[:top_n]:
            lines.append(f"- {i['url']}  ({i['detail']})")
        if len(items) > top_n:
            lines.append(f"  ...and {len(items) - top_n} more in issues.csv")
    if run_url:
        lines.append(f"\nFull export: {run_url}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #
def post_slack(webhook: str, text: str) -> None:
    text = html.escape(text, quote=False)  # Slack requires &, <, > escaped
    if len(text) > 3500:
        text = text[:3450] + "\n...truncated, see issues.csv"
    payload = json.dumps({"text": text, "blocks": [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    ]}).encode()
    req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        print("Slack:", resp.status)


def send_email(subject: str, body: str, attachment: Path | None) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user, pwd = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASS", "")
    sender = os.environ.get("EMAIL_FROM", user)
    recipients = [a.strip() for a in os.environ["EMAIL_TO"].split(",") if a.strip()]

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(recipients)
    msg.set_content(body.replace("*", ""))
    if attachment and attachment.exists():
        msg.add_attachment(attachment.read_bytes(), maintype="text", subtype="csv", filename=attachment.name)

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            if user:
                s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            if user:
                s.login(user, pwd)
            s.send_message(msg)
    print("Email sent to", recipients)


def notify(subject: str, text: str, attachment: Path | None = None) -> None:
    if os.environ.get("SLACK_WEBHOOK_URL"):
        try:
            post_slack(os.environ["SLACK_WEBHOOK_URL"], text)
        except Exception as exc:  # never let a notification failure hide the audit
            print("Slack notification failed:", exc, file=sys.stderr)
    if os.environ.get("SMTP_HOST") and os.environ.get("EMAIL_TO"):
        try:
            send_email(subject, text, attachment)
        except Exception as exc:
            print("Email notification failed:", exc, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("SITE_URL"), help="Start URL to crawl")
    ap.add_argument("--output-dir", default="crawl-output")
    ap.add_argument("--sf-bin", default="screamingfrogseospider")
    ap.add_argument("--config", help="Screaming Frog .seospiderconfig file saved from the GUI")
    ap.add_argument("--xvfb", action="store_true", help="Wrap the crawl in xvfb-run (Linux servers/CI)")
    ap.add_argument("--crawl-timeout", type=int, default=4 * 3600, help="Seconds before the crawl is killed")
    ap.add_argument("--skip-crawl", action="store_true", help="Only analyse an existing export")
    ap.add_argument("--crawl-dir", help="Existing export folder (with --skip-crawl)")
    ap.add_argument("--important-pages", default="important_pages.txt")
    ap.add_argument("--important-depth", type=int, default=1, help="Pages at or above this crawl depth count as important")
    ap.add_argument("--important-min-inlinks", type=int, default=0)
    ap.add_argument("--all-canonical-mismatches", action="store_true", help="Flag canonical mismatches on ALL pages, not just important ones")
    ap.add_argument("--top-n", type=int, default=5, help="Examples per category in the message")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--fail-on-critical", action="store_true", help="Exit 1 if any critical issue is found")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    if not args.url and not args.skip_crawl:
        sys.exit("Provide --url or SITE_URL")
    site = urlsplit(args.url).netloc if args.url else "(existing export)"
    run_url = os.environ.get("RUN_URL", "")

    if args.skip_crawl:
        out_dir = Path(args.crawl_dir or args.output_dir)
    else:
        out_dir = Path(args.output_dir) / datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            run_crawl(args, out_dir)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            if not args.no_notify:
                notify(f"[SEO Crawl] FAILED: {site}", f"*Technical SEO crawl FAILED for {site}*\n{exc}\n{run_url}")
            raise

    internal = next(out_dir.rglob("internal_all.csv"), None)
    if internal is None:
        sys.exit(f"internal_all.csv not found under {out_dir}. Check --export-tabs names against your SF version.")

    pages = load_pages(internal)
    importance = Importance(args.important_pages, args.important_depth, args.important_min_inlinks)
    issues = analyse(pages, importance, load_inlink_sources(out_dir), args.all_canonical_mismatches)
    summary = write_outputs(out_dir, issues, pages, site)

    report = build_report(summary, issues, run_url, args.top_n)
    print(report)

    if not args.no_notify:
        sev = summary["by_severity"]
        subject = f"[SEO Crawl] {site}: {sev.get('critical', 0)} critical, {sev.get('high', 0)} high ({datetime.now():%Y-%m-%d})"
        notify(subject, report, out_dir / "issues.csv")

    if args.fail_on_critical and summary["by_severity"].get("critical", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
