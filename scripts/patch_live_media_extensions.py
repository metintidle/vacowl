#!/usr/bin/env python3
"""
Patch image extension URLs in a live WordPress database (no SQL dump).

This is serialized-safe for PHP meta_value by delegating patching to:
  tools/serialize_patch.php

It updates:
  - wp_postmeta.meta_value (serialized-safe)
  - wp_posts post_content / post_excerpt / post_content_filtered / guid (serialized-safe)
  - wp_options option_value (plain or serialized)
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
PHP_WORKER = REPO_ROOT / "tools" / "serialize_patch.php"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Patch live WordPress DB image extensions to WebP safely."
    )
    p.add_argument("--mysql-host", required=True)
    p.add_argument("--mysql-port", type=int, default=3306)
    p.add_argument("--mysql-user", required=True)
    p.add_argument(
        "--mysql-password",
        default=os.environ.get("MYSQL_PWD", ""),
        help="If empty, falls back to env MYSQL_PWD.",
    )
    p.add_argument("--mysql-db", required=True, help="Database name (e.g. vcawol)")
    p.add_argument("--table-prefix", default="wp_", help="WordPress table prefix")
    p.add_argument(
        "--php-bin",
        default="php",
        help="php binary (for tools/serialize_patch.php)",
    )
    p.add_argument(
        "--mysql-ssl",
        action="store_true",
        help="Use SSL for pymysql connect (commonly needed when server presents self-signed cert).",
    )
    p.add_argument(
        "--mysql-ssl-verify-server-cert",
        type=int,
        choices=[0, 1],
        default=1,
        help="0 disables server cert verification for SSL.",
    )

    p.add_argument(
        "--replace",
        action="append",
        default=[],
        metavar="FROM:TO",
        help="Replacement pair (repeatable). Applied in order; put longer substrings first.",
    )
    p.add_argument(
        "--replacements-json",
        type=Path,
        help="JSON file [[from,to],...] instead of --replace",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not commit or update rows; only report how many would change.",
    )
    p.add_argument(
        "--commit",
        action="store_true",
        help="For safety, require --commit to actually write changes.",
    )
    return p.parse_args()


def build_pairs(args: argparse.Namespace) -> list[list[str]]:
    if args.replacements_json:
        raw = json.loads(args.replacements_json.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise SystemExit("--replacements-json must be JSON array of [from,to]")
        out: list[list[str]] = []
        for i, item in enumerate(raw):
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise SystemExit(f"replacements-json[{i}] must be [from,to]")
            a, b = item
            if not isinstance(a, str) or not isinstance(b, str):
                raise SystemExit(f"replacements-json[{i}] must be strings")
            out.append([a, b])
        pairs = out
    else:
        pairs: list[list[str]] = []
        for spec in args.replace:
            if ":" not in spec:
                raise SystemExit(f"--replace must be FROM:TO, got {spec!r}")
            left, _, right = spec.partition(":")
            pairs.append([left, right])
        if not pairs:
            raise SystemExit("Provide at least one --replace or --replacements-json")

    # Prevent the common double-extension bug:
    # - if you replace ".jpg" -> ".webp", then a value like "foo.jpg.webp"
    #   would become "foo.webp.webp" unless we also handle " .jpg.webp " cases.
    expanded: list[list[str]] = []
    seen: set[tuple[str, str]] = set()
    for from_s, to_s in pairs:
        expanded.append([from_s, to_s])
        seen.add((from_s, to_s))

        if to_s.lower() == ".webp":
            combo = f"{from_s}{to_s}"  # e.g. ".jpg.webp"
            if (combo, to_s) not in seen:
                expanded.append([combo, to_s])
                seen.add((combo, to_s))
            webp_webp = f"{to_s}{to_s}"  # e.g. ".webp.webp"
            if (webp_webp, to_s) not in seen:
                expanded.append([webp_webp, to_s])
                seen.add((webp_webp, to_s))

    # Longest-first makes nested replacements deterministic (e.g. ".jpg.webp" before ".jpg").
    expanded.sort(key=lambda p: len(p[0]), reverse=True)
    return expanded


def needs_patch(value: str | None, needles: Sequence[str]) -> bool:
    if not value:
        return False
    return any(n in value for n in needles)


class PhpSerializeWorker:
    """Stream patching via tools/serialize_patch.php."""

    def __init__(self, php_bin: str, replacements_path: Path) -> None:
        if not PHP_WORKER.is_file():
            raise FileNotFoundError(f"Missing PHP worker: {PHP_WORKER}")
        self._proc = subprocess.Popen(
            [php_bin, str(PHP_WORKER), str(replacements_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert self._proc.stdin
        assert self._proc.stdout

    def patch(self, value: str | None) -> str | None:
        if value is None:
            return None
        line_in = json.dumps({"value": value}, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line_in)
        self._proc.stdin.flush()
        line_out = self._proc.stdout.readline()
        if not line_out:
            err = self._proc.stderr.read() if self._proc.stderr else ""
            raise RuntimeError(f"PHP worker exited without output: {err!r}")
        row = json.loads(line_out)
        if "error" in row:
            raise RuntimeError(f"PHP worker: {row['error']}")
        return row["value"]

    def close(self) -> None:
        if self._proc.stdin:
            self._proc.stdin.close()
        self._proc.wait(timeout=120)


def main() -> None:
    args = parse_args()
    pairs = build_pairs(args)
    needles = [p[0] for p in pairs if p[0]]

    try:
        import pymysql
    except ImportError as e:
        raise SystemExit("Install pymysql: pip install -r requirements.txt") from e

    # Write replacement pairs for the PHP worker to read.
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        encoding="utf-8",
        delete=False,
    ) as tmp:
        json.dump(pairs, tmp, ensure_ascii=False)
        repl_path = Path(tmp.name)

    worker: PhpSerializeWorker | None = None
    conn = None
    try:
        worker = PhpSerializeWorker(args.php_bin, repl_path)

        conn_kwargs = dict(
            host=args.mysql_host,
            port=args.mysql_port,
            user=args.mysql_user,
            password=args.mysql_password,
            database=args.mysql_db,
            charset="utf8mb4",
            autocommit=False,
        )
        if args.mysql_ssl:
            if args.mysql_ssl_verify_server_cert == 0:
                conn_kwargs["ssl"] = {"cert_reqs": ssl.CERT_NONE}
            else:
                conn_kwargs["ssl"] = {}

        conn = pymysql.connect(**conn_kwargs)
        prefix = args.table_prefix

        with conn.cursor() as cur:
            # wp_postmeta.meta_value
            cur.execute(
                f"SELECT meta_id, meta_value FROM `{prefix}postmeta`"
            )
            rows_postmeta = cur.fetchall()
            postmeta_to_update = 0
            updates_postmeta: list[tuple[str, int]] = []
            for meta_id, mv in rows_postmeta:
                if not needs_patch(mv, needles):
                    continue
                new_v = worker.patch(mv)
                if new_v != mv:
                    postmeta_to_update += 1
                    if args.commit and not args.dry_run:
                        updates_postmeta.append((new_v, meta_id))

            if updates_postmeta:
                for new_v, meta_id in updates_postmeta:
                    cur.execute(
                        f"UPDATE `{prefix}postmeta` SET meta_value=%s WHERE meta_id=%s",
                        (new_v, meta_id),
                    )

            # wp_posts content-like columns
            cur.execute(
                f"SELECT ID, post_content, post_excerpt, post_content_filtered, guid "
                f"FROM `{prefix}posts`"
            )
            rows_posts = cur.fetchall()
            posts_to_update = 0
            updates_posts: list[tuple[dict[str, str], int]] = []
            for row in rows_posts:
                pid = row[0]
                fields = {
                    "post_content": row[1],
                    "post_excerpt": row[2],
                    "post_content_filtered": row[3],
                    "guid": row[4],
                }
                updates: dict[str, str] = {}
                for col, val in fields.items():
                    if not needs_patch(val, needles):
                        continue
                    new_val = worker.patch(val)
                    if new_val != val:
                        updates[col] = new_val
                if updates:
                    posts_to_update += 1
                    if args.commit and not args.dry_run:
                        updates_posts.append((updates, pid))

            if updates_posts:
                for updates, pid in updates_posts:
                    sets = ", ".join(f"`{c}`=%s" for c in updates)
                    vals = list(updates.values()) + [pid]
                    cur.execute(
                        f"UPDATE `{prefix}posts` SET {sets} WHERE ID=%s",
                        vals,
                    )

            # wp_options option_value
            cur.execute(
                f"SELECT option_id, option_value FROM `{prefix}options`"
            )
            rows_options = cur.fetchall()
            options_to_update = 0
            updates_options: list[tuple[str, int]] = []
            for oid, ov in rows_options:
                if not needs_patch(ov, needles):
                    continue
                new_v = worker.patch(ov)
                if new_v != ov:
                    options_to_update += 1
                    if args.commit and not args.dry_run:
                        updates_options.append((new_v, oid))

            if updates_options:
                for new_v, oid in updates_options:
                    cur.execute(
                        f"UPDATE `{prefix}options` SET option_value=%s WHERE option_id=%s",
                        (new_v, oid),
                    )

        if args.commit and not args.dry_run:
            conn.commit()
        else:
            # Avoid accidental commits.
            conn.rollback()

        print(
            f"Would update rows: postmeta={postmeta_to_update}, posts={posts_to_update}, options={options_to_update} "
            f"(commit={args.commit}, dry_run={args.dry_run})"
        )
    finally:
        if worker:
            worker.close()
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        repl_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

