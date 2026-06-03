#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
A simple CLI journal tool (Python 3 only).

Features
--------
* Store daily entries in ``~/.journal`` (one text file per day).
* Configurable location via ``~/.journalrc``.
* View a specific date, list entries since a given date, or view all logged dates.
"""

import argparse
import configparser
import datetime
import os
import pathlib
import re
import sys
from typing import List, Optional

# ----------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------
DEFAULT_JOURNAL = "~/.journal"
DEFAULT_JOURNAL_RC = "~/.journalrc"

# Look for files matching YYYY.MM.DD.txt
DATE_PATTERN = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})\.txt$")

# ----------------------------------------------------------------------
# Helper classes
# ----------------------------------------------------------------------


class DateParser:
    """Parse informal date strings (e.g. ``2023-04-01`` or ``yesterday``)."""

    @staticmethod
    def _parse_iso(date_str: str) -> Optional[datetime.date]:
        """Try ISO‑format parsing (YYYY‑MM‑DD)."""
        try:
            return datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return None

    @staticmethod
    def _parse_word(date_str: str) -> Optional[datetime.date]:
        """Very small subset: ``today`` or ``yesterday``."""
        today = datetime.date.today()
        if date_str.lower() in ("today", "now"):
            return today
        if date_str.lower() == "yesterday":
            return today - datetime.timedelta(days=1)
        return None

    def parse(self, s: str) -> Optional[datetime.date]:
        """Return a ``datetime.date`` or ``None`` if parsing fails."""
        for fn in (self._parse_iso, self._parse_word):
            d = fn(s)
            if d:
                return d
        return None


# ----------------------------------------------------------------------
# Core journal functionality
# ----------------------------------------------------------------------


def expand_path(p: str) -> pathlib.Path:
    """Expand ``~`` and return a ``Path`` object."""
    return pathlib.Path(os.path.expanduser(p)).resolve()


def load_config(config_path: pathlib.Path) -> pathlib.Path:
    """Read ``~/.journalrc`` (if present) and return the selected journal path."""
    if not config_path.is_file():
        # No config – fall back to the default location
        return expand_path(DEFAULT_JOURNAL)

    cfg = configparser.ConfigParser(
        {
            "journal": {"default": "__journal"},
            "__journal": {"location": DEFAULT_JOURNAL},
        }
    )
    cfg.read(config_path)

    # Resolve which section holds the journal location
    journal_section = cfg.get("journal", "default")
    location = cfg.get(journal_section, "location")
    return expand_path(location)


def ensure_dir(p: pathlib.Path) -> None:
    """Create directory ``p`` if it does not exist."""
    if not p.is_dir():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            print(f"journal: error: cannot create directory {p!s}: {exc}", file=sys.stderr)
            sys.exit(1)


def journal_file_path(journal_dir: pathlib.Path, date: datetime.date) -> pathlib.Path:
    """File name ``YYYY.MM.DD.txt`` inside ``journal_dir``."""
    return journal_dir / f"{date:%Y.%m.%d}.txt"


def write_entry(journal_dir: pathlib.Path, entries: List[str]) -> None:
    """Append a timestamped entry to today’s journal file."""
    ensure_dir(journal_dir)
    now = datetime.datetime.now()
    header = now.strftime("%a %H:%M:%S %Y-%m-%d") + "\n"
    entry_line = "- " + " ".join(entries) + "\n\n"

    path = journal_file_path(journal_dir, now.date())
    with path.open("a", encoding="utf-8") as fp:
        fp.write(header + entry_line)


def read_entries_for_day(journal_dir: pathlib.Path, day: datetime.date) -> List[str]:
    """Find and return text content from all matching date files across all sub-folders."""
    if not journal_dir.is_dir():
        return []

    target_filename = f"{day:%Y.%m.%d}.txt"
    contents = []

    # Walk through root and all sub-folders to find matching filenames
    for root, _, files in os.walk(journal_dir):
        if target_filename in files:
            path = pathlib.Path(root) / target_filename
            try:
                contents.append(path.read_text(encoding="utf-8"))
            except Exception:
                pass
                
    return contents


def list_all_dates(journal_dir: pathlib.Path) -> None:
    """Find and print all unique dates across all sub-directories, ordered chronologically."""
    if not journal_dir.is_dir():
        print("journal: no entries found (directory does not exist).", file=sys.stderr)
        return

    found_dates = set()

    # Recursively find all files matching YYYY.MM.DD.txt
    for root, _, files in os.walk(journal_dir):
        for filename in files:
            match = DATE_PATTERN.match(filename)
            if match:
                found_dates.add(f"{match.group(1)}-{match.group(2)}-{match.group(3)}")

    if not found_dates:
        print("journal: no entries found.")
    else:
        print("Journal entries available for the following dates:")
        for date_str in sorted(found_dates):
            print(f"  {date_str}")


def iter_dates(start: datetime.date, end: datetime.date):
    """Yield each date from ``start`` to ``end`` inclusive."""
    for n in range((end - start).days + 1):
        yield start + datetime.timedelta(days=n)


def show_since(journal_dir: pathlib.Path, since: datetime.date) -> None:
    """Print all entries from ``since`` up to today, searching recursively."""
    today = datetime.date.today()
    for d in iter_dates(since, today):
        txt_list = read_entries_for_day(journal_dir, d)
        for txt in txt_list:
            print(txt)


def prompt_for_entry() -> List[str]:
    """Prompt the user for their journal entry directly in the console."""
    try:
        user_input = input("Enter your journal entry: ").strip()
        if not user_input:
            return []
        return [user_input]
    except (KeyboardInterrupt, EOFError):
        print("\nOperation cancelled.", file=sys.stderr)
        sys.exit(1)


# ----------------------------------------------------------------------
# Argument handling
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A CLI tool to help with keeping a work/personal journal"
    )
    parser.add_argument("--version", action="version", version="journal 1.0")

    # Config / location
    parser.add_argument("-c", "--config", metavar="FILE", default=DEFAULT_JOURNAL_RC,
                        help="Path to config file (default: %(default)s)")
    parser.add_argument("-j", "--journal", metavar="NAME",
                        help="Sub‑journal name (creates a sub‑folder)")
    parser.add_argument("-l", "--location", metavar="DIR",
                        help="Override journal storage location")

    # Viewing options
    parser.add_argument("-s", "--since", metavar="DATE",
                        help="Show all entries since DATE (YYYY‑MM‑DD or today/yesterday)")
    
    # Made 'nargs="?"' and 'const="all"' so --view acts as a flag OR takes an optional date
    parser.add_argument("-v", "--view", metavar="DATE", nargs="?", const="all",
                        help="Show entries for a specific DATE (YYYY‑MM‑DD). If omitted, lists all logged dates.")

    # Free‑form entry text
    parser.add_argument("entry", nargs="*", help="Text to add as a new journal entry")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Determine the base journal directory
    config_path = expand_path(args.config)
    base_journal_dir = load_config(config_path)

    if args.location:
        base_journal_dir = expand_path(args.location)

    # Handle Sub-Journal Name logic (only prompt if we aren't viewing historical data)
    is_viewing = args.view is not None or args.since is not None
    journal_name = args.journal

    if not journal_name and not is_viewing:
        # Generate the default timestamp name: YYYY-mm-dayThour-minute-second
        default_name = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        try:
            journal_name = input(f"Enter sub-journal name [{default_name}]: ").strip()
            if not journal_name:
                journal_name = default_name
        except (KeyboardInterrupt, EOFError):
            print("\nOperation cancelled.", file=sys.stderr)
            sys.exit(1)

    # Target directory for writes uses the specific sub-journal if specified
    active_journal_dir = base_journal_dir / journal_name if journal_name else base_journal_dir

    dp = DateParser()

    # ------------------------------------------------------------------
    # View entries or list available dates
    # ------------------------------------------------------------------
    if args.view:
        if args.view == "all":
            # User passed --view without a specific date; search across the base journal dir
            list_all_dates(base_journal_dir)
        else:
            # User passed --view YYYY-MM-DD
            target = dp.parse(args.view)
            if not target:
                print("journal: error: unknown date format", file=sys.stderr)
                sys.exit(1)

            # Look through base directory recursively for entries matching this day
            txt_list = read_entries_for_day(base_journal_dir, target)
            if txt_list:
                for txt in txt_list:
                    print(txt)
            else:
                print(f"journal: error: entry not found on date {target}", file=sys.stderr)
                sys.exit(1)

    # ------------------------------------------------------------------
    # Show entries since a date
    # ------------------------------------------------------------------
    elif args.since:
        start = dp.parse(args.since)
        if not start:
            print("journal: error: unknown date format", file=sys.stderr)
            sys.exit(1)
        show_since(base_journal_dir, start)

    # ------------------------------------------------------------------
    # Add a new entry
    # ------------------------------------------------------------------
    else:
        # If no entry arguments were supplied via CLI, prompt the user
        if not args.entry:
            entries = prompt_for_entry()
            if not entries:
                print("journal: error: missing entry", file=sys.stderr)
                sys.exit(1)
        else:
            entries = args.entry

        write_entry(active_journal_dir, entries)


if __name__ == "__main__":
    main()
