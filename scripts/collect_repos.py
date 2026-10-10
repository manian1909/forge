"""Collect well-liked GitHub projects and portfolio sites as markdown notes for later reference.

Uses GitHub's search API (allowed, rate-limited) rather than scraping pages. For each repo it saves the README plus the
facts that matter: stars, license, live-site link, topics. Resumable: repos already saved are skipped.

    python scripts/collect_repos.py --out workspace/kb/github --topics portfolio-website,portfolio --min-stars 300
    python scripts/collect_repos.py --query "dashboard ui in:name,description" --max 100

Set GITHUB_TOKEN for a higher rate limit (search: 30 requests/min instead of 10). Check each repo's license before
copying its code; the notes are for inspiration and study.
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path

import httpx

API = "https://api.github.com"


class Client:
    def __init__(self, delay):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "forge-repo-collector"}
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.Client(headers=headers, timeout=30, follow_redirects=True)
        self.delay = delay if token is None else min(delay, 2.5)

    def get(self, url, **params):
        for _ in range(4):
            time.sleep(self.delay)
            r = self.http.get(url, params=params)
            if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
                wait = max(5, int(r.headers.get("x-ratelimit-reset", time.time() + 60)) - int(time.time()) + 1)
                print(f"rate limited, waiting {wait}s", flush=True)
                time.sleep(min(wait, 900))
                continue
            return r
        return r


def slug(full_name):
    return re.sub(r"[^A-Za-z0-9._-]+", "__", full_name)


def search(client, q, pages):
    for page in range(1, pages + 1):
        r = client.get(f"{API}/search/repositories", q=q, sort="stars", order="desc", per_page=100, page=page)
        if r.status_code != 200:
            print(f"search stopped: {r.status_code} {r.text[:120]}", flush=True)
            return
        items = r.json().get("items", [])
        yield from items
        if len(items) < 100:
            return


def readme_text(client, repo):
    r = client.get(f"{API}/repos/{repo['full_name']}/readme", ref=repo.get("default_branch"))
    if r.status_code != 200:
        return ""
    data = r.json()
    raw = client.http.get(data["download_url"]).text if data.get("download_url") else ""
    return raw[:40000]


def note(repo, readme):
    lic = (repo.get("license") or {}).get("spdx_id") or "none stated"
    topics = ", ".join(repo.get("topics") or [])
    live = repo.get("homepage") or ""
    head = [
        "---",
        f"repo: {repo['html_url']}",
        f"stars: {repo['stargazers_count']}",
        f"license: {lic}",
        f"live_site: {live}",
        f"language: {repo.get('language') or ''}",
        f"topics: [{topics}]",
        f"updated: {repo['pushed_at'][:10]}",
        "---",
        "",
        f"# {repo['full_name']}",
        "",
        f"{repo.get('description') or ''}",
        "",
        f"- GitHub: {repo['html_url']}",
    ]
    if live:
        head.append(f"- Live site: {live}")
    head += [f"- Stars: {repo['stargazers_count']:,}   License: {lic}", "", "## README", "", readme or "(no README)"]
    return "\n".join(head) + "\n"


def write_index(out):
    rows = []
    for f in out.glob("*.md"):
        if f.name == "index.md":
            continue
        head = f.read_text(encoding="utf-8", errors="replace").split("---")[1] if f.read_text(encoding="utf-8", errors="replace").startswith("---") else ""
        meta = dict(line.split(": ", 1) for line in head.strip().splitlines() if ": " in line)
        rows.append((int(meta.get("stars", 0) or 0), f.stem, meta))
    rows.sort(reverse=True)
    lines = ["# Collected repositories", "", "| Stars | Repo | License | Live site |", "|---:|---|---|---|"]
    for stars, name, meta in rows:
        lines.append(f"| {stars:,} | [{name}]({name}.md) | {meta.get('license', '')} | {meta.get('live_site', '')} |")
    (out / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="workspace/kb/github")
    p.add_argument("--topics", default="portfolio-website,portfolio-template,personal-website",
                   help="comma-separated GitHub topics to search")
    p.add_argument("--query", help="a raw GitHub search query instead of topics")
    p.add_argument("--min-stars", type=int, default=300)
    p.add_argument("--max", type=int, default=200, help="maximum new repos to save this run")
    p.add_argument("--pages", type=int, default=3, help="search result pages (100 each) per query")
    p.add_argument("--need-live-site", action="store_true", help="only repos that list a live website")
    p.add_argument("--delay", type=float, default=6.5, help="seconds between API calls without a token")
    a = p.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    client = Client(a.delay)
    queries = [a.query] if a.query else [f"topic:{t.strip()} stars:>={a.min_stars} fork:false archived:false"
                                          for t in a.topics.split(",") if t.strip()]
    saved = 0
    for q in queries:
        print(f"search: {q}", flush=True)
        for repo in search(client, q, a.pages):
            if saved >= a.max:
                break
            path = out / f"{slug(repo['full_name'])}.md"
            if path.exists() or repo["stargazers_count"] < a.min_stars:
                continue
            if a.need_live_site and not repo.get("homepage"):
                continue
            path.write_text(note(repo, readme_text(client, repo)), encoding="utf-8")
            saved += 1
            print(f"[{saved}] {repo['full_name']}  {repo['stargazers_count']:,} stars", flush=True)
    write_index(out)
    print(f"saved {saved} new notes in {out} (see index.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
