#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
gather_mail.py

Exports mailboxes from Apple Mail to .mbox files, organised by account.
Supports Gmail, Fastmail, and Apple/iCloud accounts.

Run with: uv run gather_mail.py … (see repository AGENTS.md)

Usage:
    uv run gather_mail.py --output ~/Documents/EmailArchives
    uv run gather_mail.py --output ~/Documents/EmailArchives --accounts gmail fastmail
    uv run gather_mail.py --output ~/Documents/EmailArchives --dry-run
"""

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Account fingerprints
# Apple Mail stores mail under ~/Library/Mail/V10/ (or V9, V8 depending on
# macOS version).  Each account sits in a directory whose name encodes the
# account type, e.g.:
#   IMAP-user@gmail.com@imap.gmail.com/
#   IMAP-user@fastmail.com@imap.fastmail.com/
#   EWS-…/ or iCloud-…/ for Apple Mail accounts
# ---------------------------------------------------------------------------

ACCOUNT_PATTERNS = {
    "gmail":    ["gmail.com"],
    "fastmail": ["fastmail.com", "fastmail.fm", "messagingengine.com"],
    "apple":    ["icloud.com", "me.com", "mac.com", "iCloud"],
}

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


def classify_account(account_dir: Path) -> str | None:
    """Return the account key ('gmail', 'fastmail', 'apple') or None."""
    name = account_dir.name.lower()
    for key, patterns in ACCOUNT_PATTERNS.items():
        if any(p.lower() in name for p in patterns):
            return key
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

    date_stamp = datetime.now().strftime("%Y-%m")
    summary = {"exported": 0, "skipped": 0, "errors": 0, "accounts": {}}

    account_dirs = [d for d in mail_root.iterdir() if d.is_dir()]

    for account_dir in sorted(account_dirs):
        account_key = classify_account(account_dir)
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
        dest_account_root = output_root / account_key / date_stamp

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

        summary["accounts"][account_key] = account_summary
        print()

    return summary


def print_summary(summary: dict, dry_run: bool):
    print("=" * 60)
    print("SUMMARY" + (" (dry run — nothing was copied)" if dry_run else ""))
    print("=" * 60)
    for account, stats in summary["accounts"].items():
        print(f"  {account:12s}  exported={stats['exported']}  "
              f"skipped={stats['skipped']}  errors={stats['errors']}")
    print("-" * 60)
    print(f"  {'TOTAL':12s}  exported={summary['exported']}  "
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
