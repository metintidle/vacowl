"""
Extract WordPress upload file references from a mysqldump-style .sql file.

Scans the entire dump text for ``wp-content/uploads/`` and normalizes each hit
to a canonical relative path (under the uploads root), e.g. ``2022/05/photo.jpg``.

Used for orphan-file detection and later SQL patching; does not parse SQL
structure row-by-row (regex over dump text matches serialized JSON, post
content, GUIDs, options, etc.).
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Iterable

# Path segment after "wp-content/uploads/" until an SQL-/string-friendly terminator.
# Stops before quotes, whitespace, parens, commas, angle brackets, backslash-newline, etc.
_AFTER_UPLOADS = re.compile(
    r"(?i)wp-content/uploads/([^\\'\"\s\),;<>]+)",
    re.DOTALL,
)

# Relative paths stored without the uploads prefix (e.g. _wp_attached_file, serialized "file").
# Third segment must look like a file name (contains a dot) to limit false positives.
_YEAR_MONTH_FILE = re.compile(
    r"(?<![0-9])(20[0-9]{2})/(0[1-9]|1[0-2])/([^\\'\"\s\),;<>]+)",
    re.IGNORECASE | re.DOTALL,
)


def _looks_like_upload_filename(rest: str) -> bool:
    rest = rest.strip().rstrip("/")
    if not rest:
        return False
    leaf = rest.split("/")[-1]
    return "." in leaf


def normalize_uploads_relative_path(raw: str) -> str | None:
    """
    Turn the substring immediately after ``wp-content/uploads/`` into a canonical path.

    - Strips URL query and fragment.
    - URL-decodes ``%xx`` (so disk paths match decoded names).
    - Normalizes slashes and removes redundant ``.`` / ``..`` where safe.
    Returns None if the result is empty or escapes the uploads tree.
    """
    if not raw:
        return None
    s = raw.strip().strip("/")
    if not s:
        return None
    # Query string / fragment (rare in dump but possible)
    for sep in ("?", "#"):
        if sep in s:
            s = s.split(sep, 1)[0]
    try:
        s = urllib.parse.unquote(s)
    except Exception:
        pass
    # Normalize to forward slashes only
    s = s.replace("\\", "/")
    parts: list[str] = []
    for p in s.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(p)
    out = "/".join(parts)
    return out if out else None


def extract_raw_fragments_from_text(text: str) -> Iterable[str]:
    """Yield raw path fragments (still after uploads/) for each regex match."""
    for m in _AFTER_UPLOADS.finditer(text):
        yield m.group(1)


def extract_year_month_paths_from_text(text: str) -> Iterable[str]:
    """Yield ``YYYY/MM/rest`` paths from postmeta-style values (no wp-content prefix)."""
    for m in _YEAR_MONTH_FILE.finditer(text):
        y, mo, rest = m.group(1), m.group(2), m.group(3)
        combined = f"{y}/{mo}/{rest}"
        if _looks_like_upload_filename(rest):
            yield combined


def index_referenced_upload_paths(text: str) -> set[str]:
    """Collect canonical relative upload paths from full dump text."""
    out: set[str] = set()
    for frag in extract_raw_fragments_from_text(text):
        norm = normalize_uploads_relative_path(frag)
        if norm is not None:
            out.add(norm)
    for ym in extract_year_month_paths_from_text(text):
        norm = normalize_uploads_relative_path(ym)
        if norm is not None:
            out.add(norm)
    return out


def load_referenced_upload_paths_from_sql(
    sql_path: str | Path,
    *,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> set[str]:
    """
    Read a .sql file and return the set of referenced paths under ``uploads/``.

    The file is read fully into memory (typical project dumps are tens of MB).
    """
    path = Path(sql_path)
    text = path.read_text(encoding=encoding, errors=errors)
    return index_referenced_upload_paths(text)


def main() -> None:
    import argparse
    import sys

    p = argparse.ArgumentParser(description="List canonical upload paths referenced in a WordPress SQL dump.")
    p.add_argument("sql", type=Path, help="Path to .sql dump")
    p.add_argument("--count", action="store_true", help="Print only the number of distinct paths")
    p.add_argument("--sample", type=int, default=0, metavar="N", help="Print first N paths (sorted)")
    args = p.parse_args()
    if not args.sql.is_file():
        print(f"Not a file: {args.sql}", file=sys.stderr)
        sys.exit(1)
    refs = load_referenced_upload_paths_from_sql(args.sql)
    if args.count:
        print(len(refs))
    if args.sample > 0:
        for line in sorted(refs)[: args.sample]:
            print(line)
    if not args.count and not args.sample:
        print(len(refs), "distinct canonical upload paths (use --count / --sample N)")


if __name__ == "__main__":
    main()
