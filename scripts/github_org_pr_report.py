#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""
Generate a markdown report of pull requests opened in the past week for a GitHub organization.

Run: uv run scripts/github_org_pr_report.py ORG [--days N] [--output FILE]

Requires GITHUB_TOKEN in the environment for sufficient API rate limits (recommended).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class PullRequestRow:
    repo_full_name: str
    title: str
    html_url: str
    author_login: str
    created_at: datetime


def _parse_iso_dt(value: str) -> datetime:
    # GitHub returns e.g. 2025-03-15T12:00:00Z
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _repo_full_name_from_repository_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return path or "unknown/repo"


def fetch_org_prs_opened_since(
    *,
    org: str,
    since_day: datetime,
    token: str | None,
) -> tuple[list[PullRequestRow], int]:
    """Return PR rows and total_count from GitHub (may be capped at 1000 items)."""
    since_str = since_day.strftime("%Y-%m-%d")
    q = f"org:{org} is:pr created:>={since_str}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    rows: list[PullRequestRow] = []
    total_count = 0
    page = 1
    per_page = 100

    with httpx.Client(timeout=60.0) as client:
        while True:
            r = client.get(
                f"{GITHUB_API}/search/issues",
                params={"q": q, "per_page": per_page, "page": page, "sort": "created", "order": "desc"},
                headers=headers,
            )
            if r.status_code == 401:
                print(
                    "Error: GitHub returned 401 Unauthorized. Check GITHUB_TOKEN.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if r.status_code == 403:
                detail = r.json().get("message", r.text)
                print(f"Error: GitHub returned 403 Forbidden: {detail}", file=sys.stderr)
                sys.exit(1)
            r.raise_for_status()
            data = r.json()
            if page == 1:
                total_count = int(data.get("total_count", 0))

            items = data.get("items") or []
            if not items:
                break

            for it in items:
                repo_url = it.get("repository_url") or ""
                repo_full = _repo_full_name_from_repository_url(repo_url)
                user = it.get("user") or {}
                author = user.get("login") or "unknown"
                created = _parse_iso_dt(it["created_at"])
                rows.append(
                    PullRequestRow(
                        repo_full_name=repo_full,
                        title=it.get("title") or "(no title)",
                        html_url=it.get("html_url") or "",
                        author_login=author,
                        created_at=created,
                    )
                )

            if len(items) < per_page:
                break
            page += 1
            if len(rows) >= 1000:
                break

    return rows, total_count


def build_markdown(
    *,
    org: str,
    days: int,
    since_day: datetime,
    rows: list[PullRequestRow],
    total_count: int,
    truncated: bool,
) -> str:
    now = datetime.now(UTC)
    lines: list[str] = [
        f"# New PRs in `{org}` (past {days} days)",
        "",
        f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"Listing PRs with `created` on or after **{since_day.strftime('%Y-%m-%d')}** (UTC date).",
        "",
    ]
    if truncated:
        lines.append(
            "> **Note:** GitHub Search returns at most 1000 results. "
            f"The API reports **{total_count}** matching PRs; this report may be incomplete."
        )
        lines.append("")
    elif total_count == 0:
        lines.append("No matching pull requests found.")
        lines.append("")
        return "\n".join(lines)

    by_repo: dict[str, list[PullRequestRow]] = defaultdict(list)
    for row in rows:
        by_repo[row.repo_full_name].append(row)

    for repo in sorted(by_repo.keys()):
        prs = sorted(by_repo[repo], key=lambda r: r.created_at, reverse=True)
        lines.append(f"## {repo}")
        lines.append("")
        for pr in prs:
            created = pr.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
            title_esc = pr.title.replace("\n", " ").strip()
            if pr.html_url:
                lines.append(f"- [{title_esc}]({pr.html_url}) — @{pr.author_login} — opened {created}")
            else:
                lines.append(f"- {title_esc} — @{pr.author_login} — opened {created}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Markdown report of new GitHub org PRs opened in the recent past."
    )
    parser.add_argument("org", help="GitHub organization name")
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Look back this many days from now (default: 7)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Write markdown to this file instead of stdout",
    )
    args = parser.parse_args()

    if args.days < 1:
        print("Error: --days must be at least 1.", file=sys.stderr)
        sys.exit(1)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print(
            "Warning: GITHUB_TOKEN not set. Search rate limits are low; export GITHUB_TOKEN for normal use.",
            file=sys.stderr,
        )

    now = datetime.now(UTC)
    since_day = (now - timedelta(days=args.days)).date()
    since_dt = datetime(since_day.year, since_day.month, since_day.day, tzinfo=UTC)

    rows, total_count = fetch_org_prs_opened_since(org=args.org, since_day=since_dt, token=token)
    truncated = total_count > len(rows)

    md = build_markdown(
        org=args.org,
        days=args.days,
        since_day=since_dt,
        rows=rows,
        total_count=total_count,
        truncated=truncated,
    )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(md)
    else:
        sys.stdout.write(md)


if __name__ == "__main__":
    main()
