#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
A simple CLI journal tool (Python 3.8+).

Storage Format:
~/.journal/YYYY-MM-DD/title.txt
"""

import argparse
import configparser
import datetime
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Generator, List, Optional, Tuple

# ----------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------
DEFAULT_JOURNAL = "~/.journal"
DEFAULT_JOURNAL_RC = "~/.journalrc"


# ----------------------------------------------------------------------
# Terminal & Console Formatting Setup
# ----------------------------------------------------------------------
def init_terminal() -> None:
    """Ensure UTF-8 encoding and ANSI escape sequence compatibility."""
    if sys.platform == "win32":
        # Enable ANSI colors on Windows Terminal / Legacy CMD
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h_out = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(h_out, ctypes.byref(mode)):
                kernel32.SetConsoleMode(h_out, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            pass

    # Ensure stdout/stderr streams default strictly to UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


# ----------------------------------------------------------------------
# Helper classes & Date Parsers
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
        normalized = date_str.lower().strip()
        if normalized in ("today", "now"):
            return today
        if normalized == "yesterday":
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
# Core Path/Config Helpers
# ----------------------------------------------------------------------
def expand_path(p: str) -> pathlib.Path:
    """Expand ``~`` and return a fully resolved ``Path`` object."""
    return pathlib.Path(os.path.expanduser(p)).resolve()


def load_config(config_path: pathlib.Path) -> pathlib.Path:
    """Read configuration from file (if present) or return the default journal path."""
    if not config_path.is_file():
        return expand_path(DEFAULT_JOURNAL)

    try:
        cfg = configparser.ConfigParser()
        cfg.read(config_path, encoding="utf-8")
        location = cfg.get("journal", "location", fallback=DEFAULT_JOURNAL)
        return expand_path(location)
    except Exception as exc:
        print(f"journal: warning: failed to parse config {config_path}: {exc}. Using default.", file=sys.stderr)
        return expand_path(DEFAULT_JOURNAL)


def ensure_dir(p: pathlib.Path) -> None:
    """Create directory ``p`` if it does not exist."""
    if not p.is_dir():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"journal: error: cannot create directory {p!s}: {exc}", file=sys.stderr)
            sys.exit(1)


def get_date_dir(base: pathlib.Path, date: datetime.date) -> pathlib.Path:
    """Determine the date-based sub-folder layout: base_dir/YYYY-MM-DD."""
    return base / f"{date:%Y-%m-%d}"


# ----------------------------------------------------------------------
# Content Normalization
# ----------------------------------------------------------------------
def normalize_markdown(text: str) -> str:
    """Normalize and format markdown text to ensure reliable rendering across parsers."""
    # Normalize Windows line endings to Unix style
    text = text.replace("\r\n", "\n")
    
    # Convert HTML linebreaks (<br>, <br/>, <br />) into proper Markdown newlines
    text = re.sub(r'(?i)<br\s*/?>', '  \n', text)
    
    # Collapse excessive blank lines (more than 2 consecutive newlines) to exactly 2
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text


def clean_duplicate_time_headers(content: str) -> str:
    """Clean duplicate or legacy timestamp header flags from the markdown content body."""
    pattern = r'(?m)^###\s*🕒\s*Time:\s*\d{2}[:-_.]\d{2}[:-_.]\d{2}\s*\n*'
    first_match = re.search(pattern, content)
    if first_match:
        start, end = first_match.span()
        # Preserve only the very first timestamp, strip all subsequent duplicates
        cleaned_body = re.sub(pattern, '', content[end:])
        return (content[:end] + "\n" + cleaned_body).strip()
    return content.strip()


# ----------------------------------------------------------------------
# Markdown Rendering
# ----------------------------------------------------------------------
def get_glow_command() -> Optional[List[str]]:
    """Resolve glow path dynamically and append optional configuration arguments."""
    glow_bin = shutil.which("glow") or shutil.which("glow.exe")
    if not glow_bin:
        return None

    cmd = [glow_bin]
    # Check for customizable user styling environment variables
    style = os.getenv("JOURNAL_GLOW_STYLE") or os.getenv("GLOW_STYLE")
    if style:
        cmd.extend(["-s", style])
    return cmd


def print_markdown(text: str) -> None:
    """Render markdown using `glow` if available, otherwise fallback to plain text."""
    normalized_text = normalize_markdown(text)
    glow_cmd = get_glow_command()

    if glow_cmd:
        try:
            # Safely pipe normalized content through stdin directly to Glow with encoding forced to UTF-8
            subprocess.run(
                glow_cmd + ["-"],
                input=normalized_text,
                text=True,
                encoding="utf-8",
                check=True
            )
            return
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"journal: warning: Glow rendering failed ({exc}). Falling back to plain text.", file=sys.stderr)

    # Clean plain-text fallback
    print(normalized_text)


# ----------------------------------------------------------------------
# WRITE (NEW STRUCTURE)
# ----------------------------------------------------------------------
def write_entry(base_dir: pathlib.Path, entries: List[str], title: str) -> None:
    """Safely and atomically append a timestamped entry to the YYYY-MM-DD/title.txt location."""
    ensure_dir(base_dir)

    now = datetime.datetime.now()
    date_dir = get_date_dir(base_dir, now.date())
    ensure_dir(date_dir)

    file_path = date_dir / f"{title}.txt"
    temp_path = file_path.with_suffix(".tmp")

    # Only write the Time header if the file does not already exist
    header = ""
    if not file_path.exists():
        header = f"### 🕒 Time: {now:%H:%M:%S}\n\n"

    content = "\n".join(entries).strip() + "\n\n"

    try:
        # Read existing file content first to ensure safe atomic updates
        existing_content = ""
        if file_path.exists():
            existing_content = file_path.read_text(encoding="utf-8")
        
        # Write consolidated content to a temporary file, then swap atomically
        temp_path.write_text(existing_content + header + content, encoding="utf-8")
        temp_path.replace(file_path)
    except OSError as exc:
        print(f"journal: error: failed to write entry to file {file_path}: {exc}", file=sys.stderr)
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        sys.exit(1)


# ----------------------------------------------------------------------
# LIST ENTRIES IN DATE
# ----------------------------------------------------------------------
def list_titles_for_date(base_dir: pathlib.Path, date: datetime.date) -> None:
    """Find and print only the titles of entries on a specific date, without showing their full contents."""
    date_dir = get_date_dir(base_dir, date)

    if not date_dir.exists():
        print(f"journal: no entries found for {date}.", file=sys.stderr)
        return

    try:
        files = sorted(date_dir.glob("*.txt"))
    except OSError as exc:
        print(f"journal: error: failed to read entries on date {date}: {exc}", file=sys.stderr)
        return

    if not files:
        print(f"journal: no entries found for {date}.", file=sys.stderr)
        return

    print(f"Journal entries for {date}:")
    for f in files:
        # Extract time header if present to show time next to titles
        time_str = "Legacy Entry"
        try:
            first_few_lines = f.read_text(encoding="utf-8")[:200]
            time_match = re.search(r"Time:\s*(\d{2}[:-_.]\d{2}[:-_.]\d{2})", first_few_lines)
            if time_match:
                time_str = time_match.group(1)
        except Exception:
            pass
        print(f"  • {f.stem} (Time: {time_str})")


# ----------------------------------------------------------------------
# READ ENTRY
# ----------------------------------------------------------------------
def read_journal_content(base_dir: pathlib.Path, title: str) -> str:
    """Scan all date directories and read the content of a target journal entry."""
    if not base_dir.is_dir():
        return ""

    try:
        # Loop through date folders (e.g., YYYY-MM-DD)
        for date_dir in sorted(base_dir.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue

            candidate = date_dir / f"{title}.txt"
            if candidate.is_file():
                try:
                    content = candidate.read_text(encoding="utf-8").strip()
                    if content:
                        content = clean_duplicate_time_headers(content)
                        return f"## 📅 Date: {date_dir.name}\n\n{content}"
                except OSError as exc:
                    print(f"journal: warning: failed to read file {candidate.name}: {exc}", file=sys.stderr)
    except OSError as exc:
         print(f"journal: error: failed to scan base directory {base_dir}: {exc}", file=sys.stderr)

    return ""


# ----------------------------------------------------------------------
# LIST ALL TITLES & DATE DIRECTORIES
# ----------------------------------------------------------------------
def get_all_titles(base_dir: pathlib.Path) -> List[str]:
    """Return all unique journal titles found inside our date directories."""
    titles = set()
    if not base_dir.exists():
        return []

    try:
        for date_dir in base_dir.iterdir():
            if not date_dir.is_dir():
                continue
            for f in date_dir.glob("*.txt"):
                titles.add(f.stem)
    except OSError:
        pass

    return sorted(list(titles))


def list_all(base_dir: pathlib.Path) -> None:
    """Show available journals nested cleanly under their parent YYYY-MM-DD date folders."""
    if not base_dir.exists():
        print("journal: empty")
        return

    try:
        dirs = [d for d in base_dir.iterdir() if d.is_dir()]
    except OSError as exc:
        print(f"journal: error: failed to list directories in {base_dir}: {exc}", file=sys.stderr)
        return

    if not dirs:
        print("journal: empty")
        return

    print("Available Journals:")
    for date_dir in sorted(dirs):
        try:
            files = sorted(date_dir.glob("*.txt"))
            if files:
                print(f"\n{date_dir.name}")
                for f in files:
                    print(f"  • {f.stem}")
        except OSError:
            pass


# ----------------------------------------------------------------------
# SINCE
# ----------------------------------------------------------------------
def show_since(base_dir: pathlib.Path, since: datetime.date) -> None:
    """Print all entries from ``since`` up to today, sorted globally in chronological order."""
    today = datetime.date.today()
    collected_entries: List[Tuple[datetime.date, str, str]] = []

    if not base_dir.is_dir():
        print("journal: empty")
        return

    try:
        for date_dir in base_dir.iterdir():
            if not date_dir.is_dir():
                continue

            try:
                dir_date = datetime.datetime.strptime(date_dir.name, "%Y-%m-%d").date()
            except ValueError:
                continue

            if since <= dir_date <= today:
                try:
                    for f in sorted(date_dir.glob("*.txt")):
                        content = f.read_text(encoding="utf-8").strip()
                        if content:
                            content = clean_duplicate_time_headers(content)
                            collected_entries.append((dir_date, f.stem, content))
                except OSError:
                    pass
    except OSError as exc:
        print(f"journal: error: failed to read entries since {since}: {exc}", file=sys.stderr)
        return

    if not collected_entries:
        print("journal: no matching entries found.")
        return

    # Sort globally by date first, then by title name
    collected_entries.sort(key=lambda x: (x[0], x[1]))

    full_output = [f"# 📚 Journal Entries Since {since}"]
    for entry_date, title, content in collected_entries:
        header = f"## 📅 {entry_date:%Y-%m-%d} | {title}\n"
        full_output.append(f"{header}\n{content}")

    print_markdown("\n\n---\n\n".join(full_output))


# ----------------------------------------------------------------------
# Multi-line Prompt helper
# ----------------------------------------------------------------------
def prompt_for_entry() -> List[str]:
    """Prompt the user for their journal entry directly with multi-line paste/input capability."""
    print("----------------------------------------------------------------------")
    print("Enter/Paste your journal entry. When finished:")
    if sys.platform == "win32":
        print("  - Type ':wq' on a new line and press Enter")
        print("  - OR press Ctrl+Z then Enter to submit.")
    else:
        print("  - Type ':wq' on a new line and press Enter")
        print("  - OR press Ctrl+D to submit.")
    print("----------------------------------------------------------------------")
    
    lines: List[str] = []
    while True:
        try:
            line = input()
            # Intercept saving exit indicator
            if line.strip() == ":wq":
                break
            lines.append(line)
        except EOFError:
            break
        except KeyboardInterrupt:
            print("\nOperation cancelled.", file=sys.stderr)
            sys.exit(1)
            
    # Cleanup trailing empty entries
    while lines and not lines[-1].strip():
        lines.pop()
        
    return lines


# ----------------------------------------------------------------------
# ARG PARSER
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="A CLI tool to help with keeping a work/personal journal"
    )
    p.add_argument("--version", action="version", version="journal 1.0")
    p.add_argument("-c", "--config", default=DEFAULT_JOURNAL_RC,
                   help="Path to config file")
    p.add_argument("-l", "--location",
                   help="Override journal storage location")
    p.add_argument("-j", "--journal", help="Entry title")
    p.add_argument("-s", "--since",
                   help="Show all entries since DATE (YYYY‑MM‑DD or today/yesterday)")
    p.add_argument("-v", "--view", nargs="?", const="all",
                   help="View journals. If empty, lists all folders/files. If partial, lists matching titles. If full, prints entries.")
    p.add_argument("entry", nargs="*", help="Direct text entry")
    return p


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
def main() -> None:
    init_terminal()

    args = build_parser().parse_args()

    base = load_config(expand_path(args.config))
    if args.location:
        base = expand_path(args.location)

    dp = DateParser()

    # ------------------------------------------------------------------
    # VIEW MODE
    # ------------------------------------------------------------------
    if args.view is not None:
        if args.view == "all":
            list_all(base)
            return

        target = args.view.strip()
        target_lower = target.lower()

        # Try viewing as a pure YYYY-MM-DD date lookup first
        parsed_date = dp.parse(target)
        if parsed_date:
            list_titles_for_date(base, parsed_date)
            return

        # Try to locate exact title (case-insensitive check)
        all_titles = get_all_titles(base)
        exact_match: Optional[str] = None
        for title in all_titles:
            if title.lower() == target_lower:
                exact_match = title
                break

        if exact_match:
            content = read_journal_content(base, exact_match)
            if content:
                markdown_payload = f"# 📒 Journal Title: {exact_match}\n\n{content}"
                print_markdown(markdown_payload)
            else:
                print(f"journal: no entries found with exact title '{exact_match}'.", file=sys.stderr)
        else:
            # Fall back to show partial keyword title matches
            matching_titles = [t for t in all_titles if target_lower in t.lower()]
            if matching_titles:
                print(f"No exact match found for '{target}'. Partial matches available:")
                for match in matching_titles:
                    print(f"  {match}")
            else:
                print(f"journal: error: '{target}' does not match any sub-journal title or valid date format.", file=sys.stderr)
                sys.exit(1)
        return

    # ------------------------------------------------------------------
    # SINCE
    # ------------------------------------------------------------------
    if args.since:
        start_date = dp.parse(args.since)
        if not start_date:
            print("journal: error: unknown date format", file=sys.stderr)
            sys.exit(1)
        show_since(base, start_date)
        return

    # ------------------------------------------------------------------
    # WRITE
    # ------------------------------------------------------------------
    if not args.journal:
        # Prompt dynamically for the journal name with a fallback default timestamp string
        default_name = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        try:
            args.journal = input(f"Title [{default_name}]: ").strip()
            if not args.journal:
                args.journal = default_name
        except (KeyboardInterrupt, EOFError):
            print("\nOperation cancelled.", file=sys.stderr)
            sys.exit(1)

    if not args.entry:
        lines = prompt_for_entry()
        if not lines:
            print("journal: error: missing entry", file=sys.stderr)
            sys.exit(1)
    else:
        lines = args.entry

    write_entry(base, lines, args.journal)


if __name__ == "__main__":
    main()
