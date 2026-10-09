#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-platform plain-text journal CLI (Python 3.8+).

Entries are stored as: JOURNAL_ROOT/YYYY-MM-DD/TITLE.txt
The default root is ~/.journal on Windows, Linux, and macOS. Override it with
~/.journalrc, JOURNAL_HOME, --location, or the JOURNAL_HOME environment variable.
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

APP_NAME = "journal"
VERSION = "2.0"
DEFAULT_JOURNAL = "~/.journal"
DEFAULT_CONFIG = "~/.journalrc"
DATE_FORMAT = "%Y-%m-%d"
INVALID_TITLE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
TIME_HEADER = re.compile(r"(?m)^###\s*🕒\s*Time:\s*\d{2}[:-_.]\d{2}[:-_.]\d{2}\s*\n*")


class JournalError(Exception):
    """Expected user-facing journal error."""


def expand_path(value: str) -> pathlib.Path:
    return pathlib.Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def default_config_path() -> pathlib.Path:
    return expand_path(os.environ.get("JOURNAL_CONFIG", DEFAULT_CONFIG))


def load_config(config_path: pathlib.Path) -> pathlib.Path:
    """Load a tiny INI config without making configparser a requirement for use."""
    if config_path.is_file():
        try:
            import configparser
            parser = configparser.ConfigParser()
            parser.read(str(config_path), encoding="utf-8")
            location = parser.get("journal", "location", fallback=DEFAULT_JOURNAL)
            return expand_path(location)
        except Exception as exc:
            print("journal: warning: could not read config {}: {}".format(config_path, exc), file=sys.stderr)
    return expand_path(os.environ.get("JOURNAL_HOME", DEFAULT_JOURNAL))


def resolve_root(args: argparse.Namespace) -> pathlib.Path:
    if getattr(args, "location", None):
        return expand_path(args.location)
    config = expand_path(getattr(args, "config", None) or str(default_config_path()))
    return load_config(config)


def parse_date(value: str, today: Optional[dt.date] = None) -> dt.date:
    today = today or dt.date.today()
    text = value.strip().lower()
    if text in ("today", "now"):
        return today
    if text == "yesterday":
        return today - dt.timedelta(days=1)
    if text == "tomorrow":
        return today + dt.timedelta(days=1)
    if text in ("last-week", "lastweek"):
        return today - dt.timedelta(days=7)
    if text in ("last-month", "lastmonth"):
        month = today.month - 1 or 12
        year = today.year - (1 if today.month == 1 else 0)
        import calendar
        return dt.date(year, month, min(today.day, calendar.monthrange(year, month)[1]))
    match = re.fullmatch(r"(\d+)d", text)
    if match:
        return today - dt.timedelta(days=int(match.group(1)))
    try:
        return dt.datetime.strptime(text, DATE_FORMAT).date()
    except ValueError:
        raise JournalError("invalid date {!r}; use YYYY-MM-DD, today, yesterday, tomorrow, last-week, last-month, or Nd".format(value))


def safe_title(title: str) -> str:
    """Return a safe, portable filename stem; reject path-like or empty titles."""
    title = title.strip()
    if not title or title in (".", ".."):
        raise JournalError("entry title cannot be empty or '.'/'..'")
    if "/" in title or "\\" in title or ".." in title:
        raise JournalError("title cannot contain path separators or '..'")
    title = INVALID_TITLE_CHARS.sub("_", title).strip(" .")
    title = re.sub(r"\s+", " ", title)
    if not title:
        raise JournalError("title contains no usable filename characters")
    # Windows reserves these device names even when the program runs elsewhere.
    if title.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?i)(COM|LPT)[1-9]", title.split(".")[0]):
        title = "_" + title
    return title[:160]


def date_dir(root: pathlib.Path, day: dt.date) -> pathlib.Path:
    return root / day.strftime(DATE_FORMAT)


def safe_entry_path(root: pathlib.Path, day: dt.date, filename: str) -> pathlib.Path:
    """Resolve a filename inside the expected date folder and reject traversal."""
    if filename.endswith(".txt"):
        filename = filename[:-4]
    stem = safe_title(filename)
    folder = date_dir(root, day).resolve()
    path = (folder / (stem + ".txt")).resolve()
    if path.parent != folder:
        raise JournalError("entry path escapes its date directory")
    return path


def atomic_write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=str(path.parent),
                                         prefix="." + path.name + ".", suffix=".tmp", delete=False) as handle:
            temp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, str(path))
    except OSError as exc:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise JournalError("could not write {}: {}".format(path, exc))


def normalize_markdown(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?i)<br\s*/?>", "  \n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def render_markdown(text: str, plain: bool = False) -> None:
    normalized = normalize_markdown(text)
    glow = None if plain else (shutil.which("glow") or shutil.which("glow.exe"))
    style = os.environ.get("JOURNAL_GLOW_STYLE") or os.environ.get("GLOW_STYLE")
    if glow:
        cmd = [glow]
        if style:
            cmd += ["-s", style]
        try:
            subprocess.run(cmd + ["-"], input=normalized, text=True, encoding="utf-8", check=True)
            return
        except (OSError, subprocess.SubprocessError) as exc:
            print("journal: warning: Glow failed ({}); using plain text".format(exc), file=sys.stderr)
    print(normalized)


def clean_legacy_headers(content: str) -> str:
    first = TIME_HEADER.search(content)
    if not first:
        return content.strip()
    head = content[:first.end()]
    body = TIME_HEADER.sub("", content[first.end():])
    return (head.rstrip() + "\n\n" + body.strip()).strip()


def parse_tags(content: str) -> List[str]:
    """Read optional tags from a simple '# Tags: work, personal' line."""
    match = re.search(r"(?im)^#\s*Tags:\s*(.*?)\s*$", content)
    if not match:
        return []
    return [x.strip().lstrip("#").strip() for x in match.group(1).split(",") if x.strip()]


def list_entry_files(root: pathlib.Path, start: Optional[dt.date] = None,
                     end: Optional[dt.date] = None) -> List[Tuple[dt.date, pathlib.Path]]:
    found: List[Tuple[dt.date, pathlib.Path]] = []
    if not root.is_dir():
        return found
    try:
        dirs = list(root.iterdir())
    except OSError as exc:
        raise JournalError("cannot list journal directory {}: {}".format(root, exc))
    for folder in dirs:
        if not folder.is_dir():
            continue
        try:
            day = dt.datetime.strptime(folder.name, DATE_FORMAT).date()
        except ValueError:
            continue
        if start and day < start:
            continue
        if end and day > end:
            continue
        try:
            for path in folder.glob("*.txt"):
                if path.is_file():
                    found.append((day, path))
        except OSError as exc:
            print("journal: warning: cannot read {}: {}".format(folder, exc), file=sys.stderr)
    return sorted(found, key=lambda item: (item[0], _entry_time(item[1]), item[1].name.lower()))


def _entry_time(path: pathlib.Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as f:
            for _ in range(5):
                line = f.readline()
                if not line:
                    break
                m = re.search(r"Time:\s*(\d{2}[:-_.]\d{2}[:-_.]\d{2})", line)
                if m:
                    return m.group(1).replace("-", ":").replace("_", ":").replace(".", ":")
    except OSError:
        pass
    return "00:00:00"


def read_entry(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise JournalError("cannot read {}: {}".format(path, exc))


def resolve_title(root: pathlib.Path, title: str, day: Optional[dt.date] = None) -> Tuple[dt.date, pathlib.Path]:
    title = title.strip()
    if day:
        path = safe_entry_path(root, day, title)
        if path.is_file():
            return day, path
        raise JournalError("entry {!r} not found on {}".format(title, day))
    wanted = title.lower()
    matches = [(d, p) for d, p in list_entry_files(root) if p.stem.lower() == wanted or p.name.lower() == wanted]
    if not matches:
        # Helpful partial-title suggestions.
        partial = [(d, p) for d, p in list_entry_files(root) if wanted in p.stem.lower()]
        if partial:
            options = ", ".join("{} ({})".format(p.stem, d) for d, p in partial[:12])
            raise JournalError("no exact title match for {!r}. Partial matches: {}".format(title, options))
        raise JournalError("no entry found with title {!r}".format(title))
    if len(matches) > 1:
        options = "\n".join("  {}  {}".format(d, p.stem) for d, p in matches)
        raise JournalError("title {!r} exists on multiple dates; specify a date:\n{}".format(title, options))
    return matches[0]


def prompt_multiline() -> str:
    print("Enter/paste your journal entry. Finish with a line containing :wq.")
    print("You can also send EOF (Ctrl+D on Unix, Ctrl+Z then Enter on Windows).")
    lines: List[str] = []
    try:
        while True:
            line = input()
            if line.strip() == ":wq":
                break
            lines.append(line)
    except EOFError:
        pass
    except KeyboardInterrupt:
        raise JournalError("cancelled")
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def create_entry(root: pathlib.Path, title: str, body: str, day: Optional[dt.date] = None,
                 tags: Optional[Sequence[str]] = None) -> pathlib.Path:
    day = day or dt.date.today()
    title = safe_title(title)
    folder = date_dir(root, day)
    folder.mkdir(parents=True, exist_ok=True)
    path = safe_entry_path(root, day, title)
    counter = 1
    while path.exists():
        path = safe_entry_path(root, day, "{} ({})".format(title, counter))
        counter += 1
    now = dt.datetime.now()
    tag_line = "# Tags: {}\n\n".format(", ".join(dict.fromkeys(t.strip() for t in (tags or []) if t.strip()))) if tags else ""
    text = "### 🕒 Time: {}\n\n{}{}\n".format(now.strftime("%H:%M:%S"), tag_line, body.strip())
    atomic_write(path, text)
    return path


def command_add(args: argparse.Namespace, root: pathlib.Path) -> None:
    body = " ".join(args.text).strip() if args.text else ""
    if not body:
        body = prompt_multiline()
    if not body:
        raise JournalError("entry body is empty; nothing saved")
    title = args.title
    if not title:
        default = dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        title = input("Title [{}]: ".format(default)).strip() or default
    path = create_entry(root, title, body, parse_date(args.date) if args.date else None, args.tag)
    print("journal: saved {}".format(path.relative_to(root)))


def command_list(args: argparse.Namespace, root: pathlib.Path) -> None:
    start = end = None
    if args.date:
        start = end = parse_date(args.date)
    elif args.month:
        try:
            year_s, month_s = args.month.split("-", 1)
            year, month = int(year_s), int(month_s)
            import calendar
            start = dt.date(year, month, 1)
            end = dt.date(year, month, calendar.monthrange(year, month)[1])
        except (ValueError, TypeError):
            raise JournalError("--month must be YYYY-MM")
    elif args.week:
        end = parse_date(args.week)
        start = end - dt.timedelta(days=6)
    files = list_entry_files(root, start, end)
    if not files:
        print("journal: no entries found")
        return
    for day, path in files:
        print("{}  {}  [{}]".format(day, path.stem, _entry_time(path)))


def command_show(args: argparse.Namespace, root: pathlib.Path) -> None:
    day = parse_date(args.date) if args.date else None
    day, path = resolve_title(root, args.title, day)
    render_markdown("# 📒 {} | {}\n\n{}".format(day, path.stem, clean_legacy_headers(read_entry(path))), args.plain)


def command_search(args: argparse.Namespace, root: pathlib.Path) -> None:
    needle = args.query if args.case_sensitive else args.query.casefold()
    start = parse_date(args.since) if args.since else None
    end = parse_date(args.until) if args.until else None
    hits = 0
    for day, path in list_entry_files(root, start, end):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            print("journal: warning: cannot read {}: {}".format(path, exc), file=sys.stderr)
            continue
        matches = []
        for number, line in enumerate(lines, 1):
            haystack = line if args.case_sensitive else line.casefold()
            if needle in haystack:
                matches.append((number, line.strip()))
        if matches:
            hits += len(matches)
            print("{}  {}".format(day, path.stem))
            for number, line in matches[:args.max_matches]:
                print("  {:>4}: {}".format(number, line))
            if args.full:
                render_markdown(read_entry(path), args.plain)
    if not hits:
        print("journal: no matches for {!r}".format(args.query))
    else:
        print("\n{} matching line(s)".format(hits))


def command_edit(args: argparse.Namespace, root: pathlib.Path) -> None:
    day, path = resolve_title(root, args.title, parse_date(args.date) if args.date else None)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        editor = "notepad" if os.name == "nt" else ("nano" if shutil.which("nano") else "vi")
    # Honor editor arguments such as "code --wait" without invoking a shell.
    import shlex
    command = shlex.split(editor, posix=(os.name != "nt")) + [str(path)]
    try:
        result = subprocess.run(command, check=False)
    except OSError as exc:
        raise JournalError("could not launch editor {!r}: {}".format(editor, exc))
    if result.returncode:
        raise JournalError("editor exited with status {}".format(result.returncode))
    print("journal: edited {} ({})".format(path.stem, day))


def command_append(args: argparse.Namespace, root: pathlib.Path) -> None:
    day, path = resolve_title(root, args.title, parse_date(args.date) if args.date else None)
    body = " ".join(args.text).strip() if args.text else prompt_multiline()
    if not body:
        raise JournalError("append text is empty")
    old = read_entry(path)
    atomic_write(path, old.rstrip() + "\n\n" + body + "\n")
    print("journal: appended to {} ({})".format(path.stem, day))


def confirm_or_fail(prompt: str, yes: bool) -> None:
    if yes:
        return
    try:
        answer = input(prompt + " [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raise JournalError("cancelled")
    if answer not in ("y", "yes"):
        raise JournalError("cancelled; no changes made")


def command_delete(args: argparse.Namespace, root: pathlib.Path) -> None:
    day, path = resolve_title(root, args.title, parse_date(args.date) if args.date else None)
    confirm_or_fail("Delete {!r} from {}? This cannot be undone.".format(path.stem, day), args.yes)
    try:
        path.unlink()
        folder = path.parent
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError as exc:
        raise JournalError("could not delete {}: {}".format(path, exc))
    print("journal: deleted {} ({})".format(path.stem, day))


def command_rename(args: argparse.Namespace, root: pathlib.Path) -> None:
    day, old = resolve_title(root, args.title, parse_date(args.date) if args.date else None)
    new_stem = safe_title(args.new_title)
    new = safe_entry_path(root, day, new_stem)
    if new.exists():
        raise JournalError("an entry named {!r} already exists on {}".format(new_stem, day))
    try:
        old.rename(new)
    except OSError as exc:
        raise JournalError("could not rename entry: {}".format(exc))
    print("journal: renamed {!r} to {!r}".format(old.stem, new.stem))


def command_tags(args: argparse.Namespace, root: pathlib.Path) -> None:
    found: Dict[str, List[str]] = {}
    for day, path in list_entry_files(root):
        try:
            tags = parse_tags(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        for tag in tags:
            found.setdefault(tag, []).append("{} {}".format(day, path.stem))
    if args.tag:
        key = args.tag.casefold()
        matches = [(tag, entries) for tag, entries in found.items() if tag.casefold() == key]
        if not matches:
            print("journal: no entries tagged {!r}".format(args.tag))
            return
        for tag, entries in matches:
            print("{} ({} entries)".format(tag, len(entries)))
            for entry in entries:
                print("  " + entry)
    else:
        if not found:
            print("journal: no tags found")
            return
        for tag in sorted(found, key=str.casefold):
            print("{} ({})".format(tag, len(found[tag])))


def command_export(args: argparse.Namespace, root: pathlib.Path) -> None:
    start = parse_date(args.since) if args.since else None
    end = parse_date(args.until) if args.until else None
    entries = list_entry_files(root, start, end)
    if not entries:
        raise JournalError("no entries found in the requested range")
    chunks = ["# Journal Export\n"]
    for day, path in entries:
        chunks.append("## {} | {}\n\n{}".format(day, path.stem, clean_legacy_headers(read_entry(path))))
    output = expand_path(args.output)
    atomic_write(output, "\n\n---\n\n".join(chunks).rstrip() + "\n")
    print("journal: exported {} entries to {}".format(len(entries), output))


def command_backup(args: argparse.Namespace, root: pathlib.Path) -> None:
    if not root.is_dir():
        raise JournalError("journal directory does not exist: {}".format(root))
    output = expand_path(args.output) if args.output else expand_path("journal-backup-{}.zip".format(dt.datetime.now().strftime("%Y%m%d-%H%M%S")))
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(str(output), "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in root.rglob("*"):
                if path.is_file() and path.resolve() != output.resolve():
                    archive.write(str(path), str(path.relative_to(root)))
    except OSError as exc:
        raise JournalError("backup failed: {}".format(exc))
    print("journal: backup created at {}".format(output))


def command_restore(args: argparse.Namespace, root: pathlib.Path) -> None:
    archive_path = expand_path(args.archive)
    if not archive_path.is_file():
        raise JournalError("archive not found: {}".format(archive_path))
    try:
        with zipfile.ZipFile(str(archive_path), "r") as archive:
            members = archive.infolist()
            for member in members:
                target = (root / member.filename).resolve()
                if target != root.resolve() and root.resolve() not in target.parents:
                    raise JournalError("archive contains unsafe path: {}".format(member.filename))
            files = [m for m in members if not m.is_dir()]
            if not files:
                raise JournalError("archive contains no files")
            confirm_or_fail("Restore {} file(s) into {}? Existing files will not be overwritten.".format(len(files), root), args.yes)
            root.mkdir(parents=True, exist_ok=True)
            restored = 0
            for member in files:
                target = (root / member.filename).resolve()
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member, "r") as source, target.open("xb") as dest:
                    shutil.copyfileobj(source, dest)
                restored += 1
    except zipfile.BadZipFile:
        raise JournalError("invalid ZIP archive")
    except OSError as exc:
        raise JournalError("restore failed: {}".format(exc))
    print("journal: restored {} file(s); existing files were preserved".format(restored))


def command_random(args: argparse.Namespace, root: pathlib.Path) -> None:
    entries = list_entry_files(root)
    if not entries:
        raise JournalError("journal is empty")
    day, path = random.choice(entries)
    render_markdown("# 📒 {} | {}\n\n{}".format(day, path.stem, clean_legacy_headers(read_entry(path))), args.plain)


def command_init(args: argparse.Namespace, root: pathlib.Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    print("journal directory: {}".format(root))
    print("config file: {}".format(expand_path(args.config)))
    print("To set a custom location, put this in your config file:\n\n[journal]\nlocation = {}".format(root))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="journal", description="Cross-platform plain-text journal CLI")
    parser.add_argument("--version", action="version", version="journal {}".format(VERSION))
    parser.add_argument("-c", "--config", default=str(default_config_path()), help="INI config path (default: ~/.journalrc)")
    parser.add_argument("-l", "--location", help="override journal directory (default: ~/.journal)")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("add", help="create a journal entry")
    p.add_argument("title", nargs="?", help="entry title")
    p.add_argument("--text", nargs="+", help="entry text; omit to enter multiline text")
    p.add_argument("--date", help="entry date (default: today)")
    p.add_argument("--tag", action="append", default=[], help="tag; may be repeated")

    p = sub.add_parser("list", help="list entries")
    p.add_argument("--date", help="only entries for this date")
    p.add_argument("--week", nargs="?", const="today", help="show seven days ending on DATE (default: today)")
    p.add_argument("--month", help="show entries in YYYY-MM")

    p = sub.add_parser("show", help="show an entry by title, optionally specifying its date")
    p.add_argument("title")
    p.add_argument("date", nargs="?", help="date for this title")
    p.add_argument("--plain", action="store_true", help="do not use Glow")

    p = sub.add_parser("search", help="search entry contents")
    p.add_argument("query")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--case-sensitive", action="store_true")
    p.add_argument("--full", action="store_true", help="also print complete matching entries")
    p.add_argument("--max-matches", type=int, default=20, help="maximum matching lines shown per entry")
    p.add_argument("--plain", action="store_true")

    p = sub.add_parser("edit", help="edit an existing entry using VISUAL/EDITOR")
    p.add_argument("title")
    p.add_argument("date", nargs="?")

    p = sub.add_parser("append", help="append text to an existing entry")
    p.add_argument("title")
    p.add_argument("text", nargs="*")
    p.add_argument("--date")

    p = sub.add_parser("rename", help="rename an entry")
    p.add_argument("title")
    p.add_argument("new_title")
    p.add_argument("--date")

    p = sub.add_parser("delete", help="delete an entry")
    p.add_argument("title")
    p.add_argument("date", nargs="?")
    p.add_argument("-y", "--yes", action="store_true", help="skip confirmation")

    p = sub.add_parser("tags", help="list tags or entries carrying a tag")
    p.add_argument("tag", nargs="?")

    p = sub.add_parser("export", help="export a date range to Markdown")
    p.add_argument("output", help="output Markdown file")
    p.add_argument("--since")
    p.add_argument("--until")

    p = sub.add_parser("backup", help="create a ZIP backup")
    p.add_argument("output", nargs="?", help="output ZIP path (default: timestamped ZIP in current directory)")

    p = sub.add_parser("restore", help="restore a ZIP backup without overwriting existing files")
    p.add_argument("archive")
    p.add_argument("-y", "--yes", action="store_true", help="skip confirmation")

    p = sub.add_parser("random", help="show a random journal entry")
    p.add_argument("--plain", action="store_true")

    sub.add_parser("init", help="create the journal directory and show configuration")

    # Backward-compatible legacy flags.
    parser.add_argument("-j", "--journal", help="legacy: entry title")
    parser.add_argument("-s", "--since", help="legacy: show entries since DATE")
    parser.add_argument("-v", "--view", nargs="*", help="legacy: list entries, list a date, or show DATE TITLE")
    parser.add_argument("-d", "--delete", dest="delete_legacy", nargs="*", help="legacy: list/delete entries by date and title")
    parser.add_argument("entry", nargs="*", help=argparse.SUPPRESS)
    return parser


def legacy_main(args: argparse.Namespace, root: pathlib.Path) -> bool:
    dp = parse_date
    if args.delete_legacy is not None:
        values = args.delete_legacy
        if len(values) == 1:
            day = dp(values[0])
            files = list_entry_files(root, day, day)
            if not files:
                print("journal: no entries found on {}".format(day))
            else:
                for _, path in files:
                    print(path.name)
            return True
        if len(values) >= 2:
            day = dp(values[0])
            path = safe_entry_path(root, day, values[1])
            if not path.is_file():
                raise JournalError("entry not found: {}".format(path.name))
            confirm_or_fail("Delete {} from {}?".format(path.name, day), False)
            path.unlink()
            print("journal: deleted {}".format(path.name))
            return True
        raise JournalError("legacy --delete requires a date, optionally followed by a title")
    if args.view is not None:
        if not args.view or args.view == ["all"]:
            for day, path in list_entry_files(root):
                print("{}  {}".format(day, path.name))
            return True
        if len(args.view) == 1:
            value = args.view[0]
            try:
                day = dp(value)
                for _, path in list_entry_files(root, day, day):
                    print(path.name)
                return True
            except JournalError:
                day, path = resolve_title(root, value)
                render_markdown("# 📒 {} | {}\n\n{}".format(day, path.stem, clean_legacy_headers(read_entry(path))))
                return True
        day = dp(args.view[0])
        _, path = resolve_title(root, args.view[1], day)
        render_markdown("# 📒 {} | {}\n\n{}".format(day, path.stem, clean_legacy_headers(read_entry(path))))
        return True
    if args.since:
        start = dp(args.since)
        entries = list_entry_files(root, start, dt.date.today())
        if not entries:
            print("journal: no entries found")
        else:
            chunks = ["# Journal entries since {}".format(start)]
            for day, path in entries:
                chunks.append("## {} | {}\n\n{}".format(day, path.stem, clean_legacy_headers(read_entry(path))))
            render_markdown("\n\n---\n\n".join(chunks))
        return True
    if args.journal or args.entry:
        title = args.journal or dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        body = " ".join(args.entry).strip() or prompt_multiline()
        if not body:
            raise JournalError("entry body is empty")
        path = create_entry(root, title, body)
        print("journal: saved {}".format(path.relative_to(root)))
        return True
    return False


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        root = resolve_root(args)
        if args.command:
            handlers = {
                "add": command_add, "list": command_list, "show": command_show,
                "search": command_search, "edit": command_edit, "append": command_append,
                "rename": command_rename, "delete": command_delete, "tags": command_tags,
                "export": command_export, "backup": command_backup, "restore": command_restore,
                "random": command_random, "init": command_init,
            }
            handlers[args.command](args, root)
            return 0
        if legacy_main(args, root):
            return 0
        parser.print_help()
        return 0
    except JournalError as exc:
        print("journal: error: {}".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\njournal: cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
