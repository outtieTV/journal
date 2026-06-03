#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
A simple CLI journal tool (Python 3.8+).

Features
--------
* Store daily entries in ``~/.journal`` (one text file per day).
* Configurable location via ``~/.journalrc``.
* View specific sub-journals by title, partial titles, or list all available sub-journals.
* Supports safe multi-line pasting in terminal environments.
* Seamless rendering of Markdown via Glow integration.
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
# Defaults & Global Regex Pattern
# ----------------------------------------------------------------------
DEFAULT_JOURNAL = "~/.journal"
DEFAULT_JOURNAL_RC = "~/.journalrc"

# Look for files matching YYYY.MM.DD.txt
DATE_PATTERN = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})\.txt$")


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
# Core journal functionality
# ----------------------------------------------------------------------


def expand_path(p: str) -> pathlib.Path:
    """Expand ``~`` and return a fully resolved ``Path`` object."""
    return pathlib.Path(os.path.expanduser(p)).resolve()


def load_config(config_path: pathlib.Path) -> pathlib.Path:
    """Read configuration from file (if present) or return the default journal path."""
    if not config_path.is_file():
        return expand_path(DEFAULT_JOURNAL)

    try:
        cfg = configparser.ConfigParser(
            {
                "journal": {"default": "__journal"},
                "__journal": {"location": DEFAULT_JOURNAL},
            }
        )
        cfg.read(config_path, encoding="utf-8")

        # Resolve which section holds the journal location
        journal_section = cfg.get("journal", "default", fallback="__journal")
        location = cfg.get(journal_section, "location", fallback=DEFAULT_JOURNAL)
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


def journal_file_path(journal_dir: pathlib.Path, date: datetime.date) -> pathlib.Path:
    """Determine file name ``YYYY.MM.DD.txt`` inside ``journal_dir``."""
    return journal_dir / f"{date:%Y.%m.%d}.txt"


def normalize_markdown(text: str) -> str:
    """Normalize and format markdown text to ensure reliable rendering across parsers."""
    # Normalize Windows line endings to Unix style
    text = text.replace("\r\n", "\n")
    
    # Convert HTML linebreaks (<br>, <br/>, <br />) into proper Markdown newlines
    text = re.sub(r'(?i)<br\s*/?>', '  \n', text)
    
    # Collapse excessive blank lines (more than 2 consecutive newlines) to exactly 2
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text


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
            # Safely pipe normalized content through stdin directly to Glow
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


def write_entry(journal_dir: pathlib.Path, entries: List[str]) -> None:
    """Safely and atomically append a timestamped entry to today's journal file."""
    ensure_dir(journal_dir)
    now = datetime.datetime.now()
    header = f"### 🕒 {now:%a %H:%M:%S %Y-%m-%d}\n\n"
    
    raw_content = "\n".join(entries)
    entry_line = raw_content + "\n\n"

    path = journal_file_path(journal_dir, now.date())
    temp_path = path.with_suffix(".tmp")
    
    try:
        # Read existing file content first to ensure safe atomic updates
        existing_content = ""
        if path.is_file():
            existing_content = path.read_text(encoding="utf-8")
        
        # Write consolidated content to a temporary file, then swap atomically
        temp_path.write_text(existing_content + header + entry_line, encoding="utf-8")
        temp_path.replace(path)
    except OSError as exc:
        print(f"journal: error: failed to write entry to file {path}: {exc}", file=sys.stderr)
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        sys.exit(1)


def get_all_subjournals(base_dir: pathlib.Path) -> List[str]:
    """Return a sorted list of all unique sub-journal folder names (titles)."""
    if not base_dir.is_dir():
        return []
    
    try:
        return sorted([item.name for item in base_dir.iterdir() if item.is_dir()])
    except OSError as exc:
        print(f"journal: error: failed to list sub-journals in {base_dir}: {exc}", file=sys.stderr)
        return []


def read_full_journal(subjournal_dir: pathlib.Path) -> str:
    """Read and aggregate all daily text files inside a specific sub-journal folder with markdown headers."""
    if not subjournal_dir.is_dir():
        return ""
    
    output_blocks = []
    try:
        for file in sorted(subjournal_dir.iterdir()):
            if DATE_PATTERN.match(file.name) and file.is_file():
                # Reformat YYYY.MM.DD to YYYY-MM-DD
                date_str = file.name.replace(".txt", "").replace(".", "-")
                try:
                    content = file.read_text(encoding="utf-8").strip()
                    if content:
                        block = f"## 📅 Date: {date_str}\n\n{content}"
                        output_blocks.append(block)
                except OSError as exc:
                    print(f"journal: warning: failed to read file {file.name}: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"journal: error: failed to read directory {subjournal_dir}: {exc}", file=sys.stderr)

    # Use a visual separator between aggregated files that remains completely Glow-friendly
    return "\n\n---\n\n".join(output_blocks)


def show_since(journal_dir: pathlib.Path, since: datetime.date) -> None:
    """Print all entries from ``since`` up to today, sorted globally in chronological order."""
    today = datetime.date.today()
    collected_entries: List[Tuple[datetime.date, str, str]] = []

    # Traverse all folders to collect matching entries
    for root_dir, _, files in os.walk(journal_dir):
        root_path = pathlib.Path(root_dir)
        sub_journal = root_path.name if root_path != journal_dir else "Root Journal"
        for filename in files:
            match = DATE_PATTERN.match(filename)
            if match:
                file_date = datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                if since <= file_date <= today:
                    try:
                        path = root_path / filename
                        content = path.read_text(encoding="utf-8").strip()
                        if content:
                            collected_entries.append((file_date, sub_journal, content))
                    except OSError:
                        pass

    if not collected_entries:
        print("journal: no matching entries found.")
        return

    # Sort globally by date first, then chronologically by sub-journal name
    collected_entries.sort(key=lambda x: (x[0], x[1]))

    full_output = [f"# 📚 Journal Entries Since {since}"]
    for entry_date, sub_journal, content in collected_entries:
        header = f"## 📅 {entry_date:%Y-%m-%d} | {sub_journal}\n"
        full_output.append(f"{header}\n{content}")

    print_markdown("\n\n---\n\n".join(full_output))


def prompt_for_entry() -> List[str]:
    """Prompt the user for their journal entry directly with multi-line paste/input capability."""
    print("----------------------------------------------------------------------")
    print("Enter/Paste your journal entry. When finished:")
    if sys.platform == "win32":
        print("  - Type ':wq' or 'DONE' on a new line and press Enter")
        print("  - OR press Ctrl+Z then Enter to submit.")
    else:
        print("  - Type ':wq' or 'DONE' on a new line and press Enter")
        print("  - OR press Ctrl+D to submit.")
    print("----------------------------------------------------------------------")
    
    lines: List[str] = []
    while True:
        try:
            line = input()
            # Intercept common editor/saving exit indicators
            if line.strip() in (":wq", "DONE", "done"):
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
    
    # Optional search query or empty list
    parser.add_argument("-v", "--view", metavar="TITLE", nargs="?", const="all",
                        help="View journals. If empty, lists all titles. If partial string, lists matching titles. If full title, prints entries.")

    # Free‑form entry text
    parser.add_argument("entry", nargs="*", help="Text to add as a new journal entry")

    return parser


def main() -> None:
    init_terminal()

    parser = build_parser()
    args = parser.parse_args()

    # Determine the base journal directory
    config_path = expand_path(args.config)
    base_journal_dir = load_config(config_path)

    if args.location:
        base_journal_dir = expand_path(args.location)

    # Handle Sub-Journal Name logic (only prompt if we aren't viewing historical logs)
    is_viewing = args.view is not None or args.since is not None
    journal_name = args.journal

    if not journal_name and not is_viewing:
        # Generate default timestamp: YYYY-mm-dayThour-minute-second
        default_name = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        try:
            journal_name = input(f"Enter sub-journal name [{default_name}]: ").strip()
            if not journal_name:
                journal_name = default_name
        except (KeyboardInterrupt, EOFError):
            print("\nOperation cancelled.", file=sys.stderr)
            sys.exit(1)

    # Target directory for writes
    active_journal_dir = base_journal_dir / journal_name if journal_name else base_journal_dir

    dp = DateParser()

    # ------------------------------------------------------------------
    # View logic with dynamic filtering/printing
    # ------------------------------------------------------------------
    if args.view:
        all_titles = get_all_subjournals(base_journal_dir)
        
        # Scenario 1: --view was passed completely empty
        if args.view == "all":
            if not all_titles:
                print("journal: no journals found.", file=sys.stderr)
            else:
                print("Available Journal Titles:")
                for title in all_titles:
                    print(f"  {title}")
                    
        # Scenario 2: User gave a search query (either exact title or partial search query)
        else:
            search_query = args.view.strip()
            search_query_lower = search_query.lower()
            
            # Check for exact case-insensitive match
            exact_match: Optional[str] = None
            for title in all_titles:
                if title.lower() == search_query_lower:
                    exact_match = title
                    break
            
            if exact_match:
                target_dir = base_journal_dir / exact_match
                content = read_full_journal(target_dir)
                if content:
                    markdown_payload = f"# 📒 Journal Title: {exact_match}\n\n{content}"
                    print_markdown(markdown_payload)
                else:
                    print(f"journal: no entries found inside folder '{exact_match}'.", file=sys.stderr)
            
            # Scenario 3: Check for partial matches (case-insensitive)
            else:
                matching_titles = [t for t in all_titles if search_query_lower in t.lower()]
                if matching_titles:
                    print(f"No exact match found for '{search_query}'. Partial title matches found:")
                    for match in matching_titles:
                        print(f"  {match}")
                else:
                    # Fall back to raw date parsing if search doesn't look like a title
                    target_date = dp.parse(search_query)
                    if target_date:
                        show_since(base_journal_dir, target_date)
                    else:
                        print(f"journal: error: '{search_query}' does not match any sub-journal title or valid date format.", file=sys.stderr)
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
