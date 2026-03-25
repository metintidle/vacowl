#!/usr/bin/env python3
"""
Apply tools/replacements_video_2022_10_mov_mp4.json directly to a mysqldump file.

No MySQL server: ordered str.replace on the raw SQL text, then fix Wordfence
wp_wffilemods filenameMD5 (leading 0x… hex) when that MD5 appears exactly once.

Why the older flow used MySQL: sql_serialize_patch.py walks live rows and runs
PHP unserialize/serialize so any changed string length inside serialized meta is
safe. For this video patch, .mov→.mp4 keeps the same byte length on paths/URLs,
and enclosure rows are plain meta_value (not serialized), so file-level replace
is sufficient for vcawolor_wp1.sql.

If you later add replacements that change length inside PHP-serialized meta_value,
use sql_serialize_patch.py (MySQL + PHP) instead.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON = REPO_ROOT / "tools" / "replacements_video_2022_10_mov_mp4.json"

# Same relative paths as sql_serialize_patch._WF_VIDEO_2022_10_MOV
_WF_REL_MOV: tuple[str, ...] = (
    "wp-content/uploads/2022/10/Video-24-10-2022-4-05-10-pm.mov",
    "wp-content/uploads/2022/10/Video-24-10-2022-4-05-10-pm-1.mov",
    "wp-content/uploads/2022/10/Video-24-10-2022-11-03-59-am.mov",
    "wp-content/uploads/2022/10/Video-24-10-2022-11-03-59-am-1.mov",
    "wp-content/uploads/2022/10/Video-24-10-2022-11-22-57-am.mov",
    "wp-content/uploads/2022/10/Video-31-10-2022-11-44-47-am.mov",
    "wp-content/uploads/2022/10/Video-31-10-2022-11-46-39-am.mov",
)


def load_pairs(path: Path) -> list[tuple[str, str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise SystemExit(f"{path}: pair {i} must be [from, to]")
        a, b = item
        if not isinstance(a, str) or not isinstance(b, str):
            raise SystemExit(f"{path}: pair {i} must be strings")
        out.append((a, b))
    return out


def patch_wffilemods_md5(data: str, *, verbose: bool) -> str:
    """Rewrite leading filenameMD5 when old path was wp_wffilemods-scanned."""
    for old_rel in _WF_REL_MOV:
        new_rel = old_rel.replace(".mov", ".mp4")
        old_h = "0x" + hashlib.md5(old_rel.encode()).hexdigest()
        new_h = "0x" + hashlib.md5(new_rel.encode()).hexdigest()
        n = data.count(old_h)
        if n == 0:
            continue
        if n != 1:
            print(
                f"warning: {old_h} appears {n} times; skipping MD5 rewrite "
                f"(manual check for {old_rel})",
                file=sys.stderr,
            )
            continue
        data = data.replace(old_h, new_h, 1)
        if verbose:
            print(f"filenameMD5: {old_h} -> {new_h}", file=sys.stderr)
    return data


def main() -> None:
    p = argparse.ArgumentParser(description="Patch .sql dump for 2022/10 video .mov→.mp4.")
    p.add_argument("--sql", type=Path, required=True, help="Input mysqldump")
    p.add_argument("--out", type=Path, required=True, help="Output mysqldump")
    p.add_argument(
        "--replacements-json",
        type=Path,
        default=DEFAULT_JSON,
        help=f"JSON array of [from,to] (default: {DEFAULT_JSON})",
    )
    p.add_argument(
        "--skip-wffilemods-md5",
        action="store_true",
        help="Do not rewrite wp_wffilemods filenameMD5 prefixes",
    )
    p.add_argument("-n", "--dry-run", action="store_true", help="Print counts only")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    sql_path = args.sql.resolve()
    if not sql_path.is_file():
        raise SystemExit(f"Not a file: {sql_path}")

    pairs = load_pairs(args.replacements_json.resolve())

    data = sql_path.read_text(encoding="utf-8", errors="replace")
    before = {frm: data.count(frm) for frm, _ in pairs if frm}

    if args.dry_run:
        print("Would apply", len(pairs), "replacement pairs")
        for frm, to in pairs:
            c = before.get(frm, 0)
            if c:
                print(f"  {c} x {frm[:72]}{'…' if len(frm) > 72 else ''}")
        return

    for frm, to in pairs:
        if not frm:
            continue
        data = data.replace(frm, to)

    if not args.skip_wffilemods_md5:
        data = patch_wffilemods_md5(data, verbose=args.verbose)

    out_path = args.out.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(data, encoding="utf-8", newline="\n")
    print(f"Wrote {out_path} ({len(data):,} bytes)")


if __name__ == "__main__":
    main()
