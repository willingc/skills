#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
gather_mail.py

Exports mailboxes from Apple Mail to .mbox files, organised by account.
Supports Gmail, Fastmail, and Apple/iCloud accounts. Recent macOS versions
store each account as a UUID-named folder under Mail/V*; the script matches
those to providers using ~/Library/Accounts/Accounts*.sqlite when needed.

Run with: uv run gather_mail.py … (see repository AGENTS.md)

Usage:
    uv run gather_mail.py --output ~/Documents/EmailArchives
    uv run gather_mail.py --output ~/Documents/EmailArchives --accounts gmail fastmail
    uv run gather_mail.py --output ~/Documents/EmailArchives --dry-run

    Optional: if a Fastmail-hosted custom domain is not detected, set a comma-separated
    list of domains, e.g. GATHER_MBOX_EXTRA_FASTMAIL_DOMAINS=willingconsulting.com
"""

import argparse
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Account fingerprints
# Apple Mail stores mail under ~/Library/Mail/V10/ (or V9, V8 depending on
# macOS version).  Each account is usually a folder whose name is either:
#   - A UUID (common on recent macOS), or
#   - A legacy string such as IMAP-user@gmail.com@imap.gmail.com/
# UUID folder names do not contain the provider; we resolve them via
# ~/Library/Accounts/Accounts*.sqlite (ZACCOUNT.ZIDENTIFIER → hostnames).
# ---------------------------------------------------------------------------

UUID_DIR = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)

ACCOUNT_PATTERNS = {
    "gmail":    ["gmail.com"],
    "fastmail": ["fastmail.com", "fastmail.fm", "messagingengine.com"],
    "apple":    ["icloud.com", "me.com", "mac.com", "iCloud"],
}

# Optional: comma-separated email domains hosted on Fastmail (custom domains).
# Normally unnecessary — Mail still stores imap.fastmail.com / messagingengine.com
# in the account record — but use this if an account is not classified as fastmail.
_EXTRA_FASTMAIL_DOMAINS_ENV = "GATHER_MBOX_EXTRA_FASTMAIL_DOMAINS"


def _extra_fastmail_domains() -> list[str]:
    raw = os.environ.get(_EXTRA_FASTMAIL_DOMAINS_ENV, "")
    return [x.strip().lower() for x in raw.split(",") if x.strip()]

# Mailboxes to skip — these are noisy and rarely worth archiving
SKIP_MAILBOXES = {"Junk", "Trash", "Deleted Messages", "Spam", "[Gmail]/Spam", "[Gmail]/Trash"}


def find_mail_root() -> Path:
    """Return the Apple Mail data directory, trying V10 down to V2."""
    base = Path.home() / "Library" / "Mail"
    for version in ["V10", "V9", "V8", "V7", "V6", "V5", "V4", "V3", "V2"]:
        candidate = base / version
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"Cannot find Apple Mail data directory under {base}. "
        "Make sure Apple Mail has been set up on this Mac."
    )


def _classify_from_text(blob: str) -> str | None:
    """Match ACCOUNT_PATTERNS against arbitrary text (folder name or plist fields)."""
    lower = blob.lower()
    for domain in _extra_fastmail_domains():
        if domain in lower:
            return "fastmail"
    for key, patterns in ACCOUNT_PATTERNS.items():
        if any(p.lower() in lower for p in patterns):
            return key
    return None


def load_mail_account_hints() -> dict[str, str]:
    """
    Map Mail account directory names (UUID or identifier) to a searchable string
    built from the Accounts database (hostnames, usernames, descriptions).
    """
    hints: dict[str, str] = {}
    accounts_dir = Path.home() / "Library" / "Accounts"
    for db_name in ("Accounts4.sqlite", "Accounts3.sqlite", "Accounts2.sqlite"):
        db_path = accounts_dir / db_name
        if not db_path.exists():
            continue
        try:
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            continue
        loaded_from_this_db = False
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ZACCOUNT'"
            )
            if not cur.fetchone():
                continue
            cur.execute("PRAGMA table_info(ZACCOUNT)")
            col_names = {row[1] for row in cur.fetchall()}
            ident_col = "ZIDENTIFIER" if "ZIDENTIFIER" in col_names else None
            if not ident_col:
                continue
            parts = [ident_col]
            for optional in (
                "ZUSERNAME",
                "ZACCOUNTDESCRIPTION",
                "ZEMAILADDRESS",
                "ZFULLNAME",
                "ZHOSTNAME",
            ):
                if optional in col_names:
                    parts.append(optional)
            cur.execute(f"SELECT {', '.join(parts)} FROM ZACCOUNT")
            for row in cur.fetchall():
                ident = row[0]
                if ident is None:
                    continue
                ident_str = str(ident).strip()
                if not ident_str:
                    continue
                blob = " ".join(str(x) for x in row if x)
                hints[ident_str] = blob
            loaded_from_this_db = True
        except sqlite3.Error:
            pass
        finally:
            conn.close()
        if loaded_from_this_db:
            break
    return hints


def _hint_blob_for_dir(name: str, account_hints: dict[str, str]) -> str | None:
    if name in account_hints:
        return account_hints[name]
    nl = name.lower()
    for k, v in account_hints.items():
        if k.lower() == nl:
            return v
    return None


def classify_account(
    account_dir: Path, account_hints: dict[str, str] | None = None
) -> str | None:
    """Return the account key ('gmail', 'fastmail', 'apple') or None."""
    name = account_dir.name
    if account_hints:
        blob = _hint_blob_for_dir(name, account_hints)
        if blob is not None:
            hit = _classify_from_text(blob)
            if hit is not None:
                return hit
    hit = _classify_from_text(name)
    if hit is not None:
        return hit
    # Last resort: scan small plist files for provider hostnames
    if UUID_DIR.match(name):
        try:
            for p in account_dir.rglob("*.plist"):
                try:
                    if p.stat().st_size > 2_000_000:
                        continue
                    data = p.read_text(errors="ignore")
                except OSError:
                    continue
                hit = _classify_from_text(data)
                if hit is not None:
                    return hit
        except OSError:
            pass
    return None


def find_mbox_dirs(account_dir: Path) -> list[Path]:
    """
    Recursively find all .mbox directories inside an account folder.
    Apple Mail stores each mailbox as a directory named <Mailbox>.mbox.
    """
    return sorted(account_dir.rglob("*.mbox"))


def mbox_display_name(mbox_path: Path, account_dir: Path) -> str:
    """Return a human-readable name relative to the account root."""
    try:
        rel = mbox_path.relative_to(account_dir)
    except ValueError:
        rel = mbox_path
    # Strip .mbox suffix from each component
    parts = [p.replace(".mbox", "") for p in rel.parts]
    return " / ".join(parts)


def should_skip(mbox_path: Path) -> bool:
    """Return True if this mailbox should be excluded."""
    name = mbox_path.stem  # filename without .mbox
    return name in SKIP_MAILBOXES


def copy_mbox(src: Path, dest: Path, dry_run: bool) -> bool:
    """
    Copy an Apple Mail .mbox directory to dest as a self-contained .mbox folder.
    Returns True on success.
    """
    if dry_run:
        print(f"    [dry-run] Would copy → {dest}")
        return True

    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        shutil.rmtree(dest)

    try:
        shutil.copytree(src, dest)
        return True
    except Exception as exc:
        print(f"     ERROR copying {src}: {exc}", file=sys.stderr)
        return False


def export_accounts(
    output_root: Path,
    requested_accounts: list[str],
    dry_run: bool,
    skip_junk_trash: bool,
) -> dict:
    """Main export loop. Returns a summary dict."""
    mail_root = find_mail_root()
    print(f"Apple Mail data: {mail_root}\n")

    account_hints = load_mail_account_hints()
    if account_hints:
        print(f"Loaded {len(account_hints)} account id(s) from ~/Library/Accounts/*.sqlite\n")

    date_stamp = datetime.now().strftime("%Y-%m")
    summary = {"exported": 0, "skipped": 0, "errors": 0, "accounts": {}}

    account_dirs = [d for d in mail_root.iterdir() if d.is_dir()]

    for account_dir in sorted(account_dirs):
        account_key = classify_account(account_dir, account_hints)
        if account_key is None:
            continue
        if account_key not in requested_accounts:
            continue

        print(f"{'='*60}")
        print(f"Account : {account_dir.name}")
        print(f"Type    : {account_key}")

        mbox_dirs = find_mbox_dirs(account_dir)
        if not mbox_dirs:
            print("  (no mailboxes found)\n")
            continue

        account_summary = {"exported": 0, "skipped": 0, "errors": 0}
        # One Mail account per top-level folder; include folder name so UUIDs
        # and multiple accounts of the same type do not overwrite each other.
        dest_account_root = output_root / account_key / account_dir.name / date_stamp

        for mbox in mbox_dirs:
            display = mbox_display_name(mbox, account_dir)

            if skip_junk_trash and should_skip(mbox):
                print(f"  SKIP  {display}")
                account_summary["skipped"] += 1
                summary["skipped"] += 1
                continue

            # Build destination path mirroring source structure
            rel = mbox.relative_to(account_dir)
            dest = dest_account_root / rel

            print(f"  →  {display}")
            if not dry_run:
                print(f"       {dest}")

            ok = copy_mbox(mbox, dest, dry_run)
            if ok:
                account_summary["exported"] += 1
                summary["exported"] += 1
            else:
                account_summary["errors"] += 1
                summary["errors"] += 1

        label = f"{account_key} — {account_dir.name}"
        summary["accounts"][label] = account_summary
        print()

    return summary


def print_summary(summary: dict, dry_run: bool):
    print("=" * 60)
    print("SUMMARY" + (" (dry run — nothing was copied)" if dry_run else ""))
    print("=" * 60)
    for account, stats in summary["accounts"].items():
        print(f"  {account}  exported={stats['exported']}  "
              f"skipped={stats['skipped']}  errors={stats['errors']}")
    print("-" * 60)
    print(f"  TOTAL  exported={summary['exported']}  "
          f"skipped={summary['skipped']}  errors={summary['errors']}")


def main():
    parser = argparse.ArgumentParser(
        description="Export Apple Mail mailboxes to .mbox files."
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Destination folder, e.g. ~/Documents/EmailArchives"
    )
    parser.add_argument(
        "--accounts", "-a",
        nargs="+",
        choices=["gmail", "fastmail", "apple"],
        default=["gmail", "fastmail", "apple"],
        help="Accounts to export (default: all three)"
    )
    parser.add_argument(
        "--include-junk-trash",
        action="store_true",
        default=False,
        help="Also export Junk and Trash mailboxes (skipped by default)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be exported without copying anything"
    )

    args = parser.parse_args()

    output_root = Path(args.output).expanduser().resolve()
    print(f"Output root : {output_root}")
    print(f"Accounts    : {', '.join(args.accounts)}")
    print(f"Dry run     : {args.dry_run}\n")

    if not args.dry_run:
        output_root.mkdir(parents=True, exist_ok=True)

    try:
        summary = export_accounts(
            output_root=output_root,
            requested_accounts=args.accounts,
            dry_run=args.dry_run,
            skip_junk_trash=not args.include_junk_trash,
        )
    except FileNotFoundError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print_summary(summary, args.dry_run)

    if summary["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
