#!/usr/bin/env python3
"""
Import a WordPress SQL dump into a temporary MySQL database, patch serialized and
plain text columns via tools/serialize_patch.php, then dump the result.

Typical use after converting uploads to WebP / MP4:

  python scripts/sql_serialize_patch.py \\
    --sql vcawolor_wp1.sql \\
    --out vcawolor_wp1.patched.sql \\
    --replace .jpg:.webp --replace .jpeg:.webp \\
    --mysql-user root --mysql-password ''

  After transcoding 2022/10 videos (.mov → .mp4), apply tools/replacements_video_2022_10_mov_mp4.json
  via --replacements-json (pairs are longest-first). Wordfence path rows for those files are updated
  automatically when patch_database runs.

  For the same video URL patch without MySQL, use scripts/patch_sql_file_video_mov_mp4.py on vcawolor_wp1.sql.

Requires: mysql and mysqldump clients, php, pymysql (see requirements.txt).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
PHP_WORKER = REPO_ROOT / "tools" / "serialize_patch.php"

# Relative paths (as in Wordfence / wffilemods) for uploads/2022/10 videos transcoded .mov → .mp4.
_WF_VIDEO_2022_10_MOV: tuple[str, ...] = (
    "wp-content/uploads/2022/10/Video-24-10-2022-4-05-10-pm.mov",
    "wp-content/uploads/2022/10/Video-24-10-2022-4-05-10-pm-1.mov",
    "wp-content/uploads/2022/10/Video-24-10-2022-11-03-59-am.mov",
    "wp-content/uploads/2022/10/Video-24-10-2022-11-03-59-am-1.mov",
    "wp-content/uploads/2022/10/Video-24-10-2022-11-22-57-am.mov",
    "wp-content/uploads/2022/10/Video-31-10-2022-11-44-47-am.mov",
    "wp-content/uploads/2022/10/Video-31-10-2022-11-46-39-am.mov",
)
_WF_KNOWN_HOME_PREFIX = "/home/vcawolor/public_html/"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MySQL round-trip SQL patch with PHP serialize safety.")
    p.add_argument("--sql", required=True, type=Path, help="Path to input .sql dump")
    p.add_argument("--out", required=True, type=Path, help="Path for patched mysqldump output")
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
    p.add_argument("--mysql-bin", default="mysql", help="mysql client binary")
    p.add_argument("--mysqldump-bin", default="mysqldump", help="mysqldump binary")
    p.add_argument("--php-bin", default="php", help="php binary (for serialize_patch.php)")
    p.add_argument("--mysql-host", default="127.0.0.1")
    p.add_argument("--mysql-port", type=int, default=3306)
    p.add_argument("--mysql-user", required=True)
    p.add_argument("--mysql-password", default="", help="Empty string for socket/no-password setups")
    p.add_argument(
        "--mysql-ssl",
        action="store_true",
        help="Pass --ssl to mysql/mysqldump client (useful when server requires TLS).",
    )
    p.add_argument(
        "--mysql-ssl-verify-server-cert",
        type=int,
        choices=[0, 1],
        default=1,
        help=(
            "Pass --ssl-verify-server-cert=<0|1> to mysql/mysqldump client. "
            "Use 0 when connecting to servers with self-signed cert chains."
        ),
    )
    p.add_argument(
        "--temp-database",
        default="vcawolor_wp_patch_tmp",
        help="Database name to create, import into, and drop after dump (unless --keep-temp-db)",
    )
    p.add_argument("--table-prefix", default="wp_", help="WordPress table prefix")
    p.add_argument("--keep-temp-db", action="store_true", help="Do not DROP DATABASE after success")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report how many cells would be sent to the PHP worker (no import/update/dump)",
    )
    p.add_argument(
        "--skip-import",
        action="store_true",
        help="Assume --temp-database already contains the imported dump; skip mysql < file",
    )
    return p.parse_args()


def build_pairs(args: argparse.Namespace) -> list[list[str]]:
    if args.replacements_json:
        raw = json.loads(args.replacements_json.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise SystemExit("replacements-json must be a JSON array")
        out: list[list[str]] = []
        for i, item in enumerate(raw):
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise SystemExit(f"replacements-json[{i}] must be [from, to]")
            a, b = item
            if not isinstance(a, str) or not isinstance(b, str):
                raise SystemExit(f"replacements-json[{i}] must be string pair")
            out.append([a, b])
        pairs = out
    else:
        pairs = []
        for spec in args.replace:
            if ":" not in spec:
                raise SystemExit(f"--replace must be FROM:TO, got {spec!r}")
            left, _, right = spec.partition(":")
            pairs.append([left, right])
        if not pairs:
            raise SystemExit("Provide at least one --replace or --replacements-json")

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

    expanded.sort(key=lambda p: len(p[0]), reverse=True)
    return expanded


def mysql_base_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        args.mysql_bin,
        f"-h{args.mysql_host}",
        f"-P{args.mysql_port}",
        f"-u{args.mysql_user}",
        f"--password={args.mysql_password}",
    ]
    if getattr(args, "mysql_ssl", False):
        cmd.append("--ssl")
        cmd.append(f"--ssl-verify-server-cert={int(args.mysql_ssl_verify_server_cert)}")
    return cmd


def mysqldump_base_cmd(args: argparse.Namespace) -> list[str]:
    dump_bin_name = Path(args.mysqldump_bin).name
    cmd = [
        args.mysqldump_bin,
        f"-h{args.mysql_host}",
        f"-P{args.mysql_port}",
        f"-u{args.mysql_user}",
        f"--password={args.mysql_password}",
        "--single-transaction",
        "--skip-lock-tables",
        "--skip-routines",
        "--triggers",
    ]
    if dump_bin_name not in {"mariadb-dump"}:
        cmd.append("--set-gtid-purged=OFF")
    if getattr(args, "mysql_ssl", False):
        cmd.append("--ssl")
        cmd.append(f"--ssl-verify-server-cert={int(args.mysql_ssl_verify_server_cert)}")
    return cmd


def run_mysql_sql(args: argparse.Namespace, sql: str) -> None:
    cmd = mysql_base_cmd(args) + ["-e", sql]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def mysql_import_dump(args: argparse.Namespace, db: str, sql_path: Path) -> None:
    cmd = mysql_base_cmd(args) + [db]
    with sql_path.open("rb") as f:
        subprocess.run(cmd, stdin=f, check=True, capture_output=True)


def mysqldump_db(args: argparse.Namespace, db: str, out_path: Path) -> None:
    cmd = mysqldump_base_cmd(args) + [db]
    with out_path.open("wb") as out:
        subprocess.run(cmd, stdout=out, check=True, stderr=subprocess.PIPE)


def needs_patch(value: str | None, needles: Sequence[str]) -> bool:
    if not value:
        return False
    return any(n in value for n in needles)


class PhpSerializeWorker:
    """One line in, one line out JSON protocol matching serialize_patch.php."""

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


def dry_run_counts(pairs: list[list[str]], args: argparse.Namespace) -> None:
    try:
        import pymysql
    except ImportError as e:
        raise SystemExit("Install pymysql: pip install -r requirements.txt") from e
    needles = [p[0] for p in pairs if p[0]]
    conn = pymysql.connect(
        host=args.mysql_host,
        port=args.mysql_port,
        user=args.mysql_user,
        password=args.mysql_password,
        database=args.temp_database,
        charset="utf8mb4",
    )
    prefix = args.table_prefix
    try:
        with conn.cursor() as cur:
            total = 0
            cur.execute(
                f"SELECT COUNT(*) FROM `{prefix}postmeta` "
                f"WHERE meta_value IS NOT NULL AND meta_value != ''"
            )
            n_meta = cur.fetchone()[0]
            cur.execute(f"SELECT meta_id, meta_value FROM `{prefix}postmeta`")
            for mid, mv in cur:
                if needs_patch(mv, needles):
                    total += 1
            print(f"postmeta rows with any replacement substring: {total} / {n_meta}")

            cur.execute(
                f"SELECT COUNT(*) FROM `{prefix}posts` WHERE post_content IS NOT NULL "
                f"OR post_excerpt IS NOT NULL OR post_content_filtered IS NOT NULL OR guid IS NOT NULL"
            )
            n_posts = cur.fetchone()[0]
            cur.execute(
                f"SELECT ID, post_content, post_excerpt, post_content_filtered, guid FROM `{prefix}posts`"
            )
            hit = 0
            for _pid, *cols in cur:
                if any(needs_patch(c, needles) for c in cols if c):
                    hit += 1
            print(f"posts rows touching any text column with substring: {hit} / {n_posts}")

            cur.execute(
                f"SELECT COUNT(*) FROM `{prefix}options` WHERE option_value IS NOT NULL "
                f"AND option_value != ''"
            )
            n_opt = cur.fetchone()[0]
            cur.execute(f"SELECT option_id, option_value FROM `{prefix}options`")
            oh = 0
            for _oid, ov in cur:
                if needs_patch(ov, needles):
                    oh += 1
            print(f"options rows with any replacement substring: {oh} / {n_opt}")
    finally:
        conn.close()


def patch_wordfence_video_mov_to_mp4(cur, table_prefix: str) -> None:
    """Update Wordfence file tables (plain paths, not serialized)."""
    for old_rel in _WF_VIDEO_2022_10_MOV:
        new_rel = old_rel.replace(".mov", ".mp4")
        old_abs = _WF_KNOWN_HOME_PREFIX + old_rel
        new_abs = _WF_KNOWN_HOME_PREFIX + new_rel
        cur.execute(
            f"UPDATE `{table_prefix}wfknownfilelist` SET `path`=%s, `wordpress_path`=%s "
            f"WHERE `path`=%s",
            (new_abs, new_rel, old_abs),
        )
        md5_hex = hashlib.md5(new_rel.encode("utf-8")).hexdigest()
        cur.execute(
            f"UPDATE `{table_prefix}wffilemods` SET `filename`=%s, `real_path`=%s, "
            f"`filenameMD5`=UNHEX(%s) WHERE `filename`=%s",
            (new_rel, new_abs, md5_hex, old_rel),
        )


def patch_database(pairs: list[list[str]], args: argparse.Namespace) -> None:
    try:
        import pymysql
    except ImportError as e:
        raise SystemExit("Install pymysql: pip install -r requirements.txt") from e

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        encoding="utf-8",
        delete=False,
    ) as tmp:
        json.dump(pairs, tmp, ensure_ascii=False)
        repl_path = Path(tmp.name)

    worker: PhpSerializeWorker | None = None
    try:
        worker = PhpSerializeWorker(args.php_bin, repl_path)
        needles = [p[0] for p in pairs if p[0]]
        conn = pymysql.connect(
            host=args.mysql_host,
            port=args.mysql_port,
            user=args.mysql_user,
            password=args.mysql_password,
            database=args.temp_database,
            charset="utf8mb4",
        )
        prefix = args.table_prefix
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT meta_id, meta_value FROM `{prefix}postmeta`")
                for meta_id, mv in cur:
                    if not needs_patch(mv, needles):
                        continue
                    new_v = worker.patch(mv)
                    if new_v != mv:
                        cur.execute(
                            f"UPDATE `{prefix}postmeta` SET meta_value=%s WHERE meta_id=%s",
                            (new_v, meta_id),
                        )
                conn.commit()

                cur.execute(
                    f"SELECT ID, post_content, post_excerpt, post_content_filtered, guid "
                    f"FROM `{prefix}posts`"
                )
                for row in cur:
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
                        sets = ", ".join(f"`{c}`=%s" for c in updates)
                        vals = list(updates.values()) + [pid]
                        cur.execute(f"UPDATE `{prefix}posts` SET {sets} WHERE ID=%s", vals)
                conn.commit()

                cur.execute(f"SELECT option_id, option_value FROM `{prefix}options`")
                for oid, ov in cur:
                    if not needs_patch(ov, needles):
                        continue
                    new_v = worker.patch(ov)
                    if new_v != ov:
                        cur.execute(
                            f"UPDATE `{prefix}options` SET option_value=%s WHERE option_id=%s",
                            (new_v, oid),
                        )
                conn.commit()

                patch_wordfence_video_mov_to_mp4(cur, prefix)
                conn.commit()
        finally:
            conn.close()
    finally:
        if worker:
            worker.close()
        repl_path.unlink(missing_ok=True)


def main() -> None:
    args = parse_args()
    pairs = build_pairs(args)
    sql_path = args.sql.resolve()
    if not sql_path.is_file():
        raise SystemExit(f"SQL file not found: {sql_path}")

    db = args.temp_database
    if not args.skip_import:
        run_mysql_sql(args, f"DROP DATABASE IF EXISTS `{db}`")
        run_mysql_sql(
            args,
            f"CREATE DATABASE `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
        )
        try:
            mysql_import_dump(args, db, sql_path)
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode("utf-8", "replace")
            raise SystemExit(f"mysql import failed: {stderr}") from e

    if args.dry_run:
        dry_run_counts(pairs, args)
        if not args.keep_temp_db and not args.skip_import:
            run_mysql_sql(args, f"DROP DATABASE IF EXISTS `{db}`")
        return

    patch_database(pairs, args)

    out_path = args.out.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mysqldump_db(args, db, out_path)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", "replace")
        raise SystemExit(f"mysqldump failed: {stderr}") from e

    if not args.keep_temp_db:
        run_mysql_sql(args, f"DROP DATABASE IF EXISTS `{db}`")

    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
