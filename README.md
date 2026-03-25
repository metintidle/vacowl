# vcawl — WordPress uploads tooling
# Database Connection string
Host: 152.69.175.15
Database Name: vcawol
USerName: itt-admin
Password: GCdGb!fNmk!!3dH


## Setup

From the project root (`vcawl/`):

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**Optional system tools** (needed for full `media_optimize` behaviour):

- **ffmpeg** and **ffprobe** — video compression (`--videos` / `--all`)
- **cwebp** (WebP utilities) — image WebP output (`--images` / `--all`)
- **mysql**, **mysqldump**, **php** — for `sql_serialize_patch.py` (see below)

## Running the scripts

### Where to run from

1. **`cd`** to the **project root** (`vcawl/`, the directory that contains `scripts/`, `vcawolor_wp1.sql`, and `requirements.txt`).
2. **Optional:** `source .venv/bin/activate` (see [Setup](#setup)).
3. Invoke the CLI with **`python3`** (or `python` inside the venv).

Relative paths such as `./uploads` or `vcawolor_wp1.sql` are interpreted from this directory. The default **`--sql`** path is **`vcawolor_wp1.sql` in the project root** (resolved from the script’s location, so it stays correct even if you use an absolute path for `--uploads`).

```bash
python3 scripts/media_optimize.py --help
python3 -m scripts.media_optimize --help
```

### `scripts/media_optimize.py`

Single CLI for uploads: largest-file report, video compression, image → WebP, moving unreferenced files, and a placeholder for SQL patching.

**How to run**

1. Use the **project root** steps above (`cd`, optional venv, `pip install -r requirements.txt` from [Setup](#setup) when you need **`--images`** / **`--all`**).
2. Put **`ffmpeg`**, **`ffprobe`**, and **`cwebp`** on your `PATH` when using **`--videos`**, **`--images`**, or **`--all`** (not required for **`--report-largest`** or **`--unused`** alone).
3. Run with **`--uploads`** plus **at least one action** from the table below. If you omit every action, the script prints **`--help`**, a short hint to stderr, and exits **`2`**.

```bash
# Safe audit only (no file changes)
python3 scripts/media_optimize.py --uploads ./uploads --report-largest

# Same via module (also from project root)
python3 -m scripts.media_optimize --uploads ./uploads --report-largest
```

**Required**

- **`--uploads`** — absolute or relative path to `wp-content/uploads` (the folder that contains `2021/`, `2022/`, plugin subfolders, etc.).

**Pick at least one action** (otherwise behaviour is as in step 4 above):

| Flag | What it does |
|------|----------------|
| `--report-largest` | Print largest image/video files; use `--report-top N` (default 50). |
| `--videos` | Transcode videos (ffmpeg): H.264/AAC MP4, long edge cap (default 1200px), target size ≤ `--max-video-mb` (default 20 MiB); see **Video compression** below. |
| `--images` | Resize rasters (default long edge 1920px) + `cwebp`; see **Replacing originals** and **Image options** below. |
| `--all` | Same as `--videos` and `--images`. |
| `--unused` | Move files on disk that are not referenced in the SQL dump to `uploads/unused/...`. **Requires a valid `--sql` file.** |
| `--sql-patch` | Stub only (logs); use `sql_serialize_patch.py` for real SQL rewrites. |

**`--sql`**

- **Default:** `vcawolor_wp1.sql` next to the project root (same folder as `scripts/`; path is resolved from the script so it stays correct if you pass an absolute `--uploads`).
- The file must **exist** when you use **`--unused`** or **`--sql-patch`**. For **`--videos`**, **`--images`**, **`--report-largest`**, or **`--all`** alone, the default SQL file is not read and may be absent.

**Common options**

- **`--dry-run`** — No moves, no transcodes, no new WebP files (for `--videos`, lists the queue only; `--report-largest` still prints its table; `--unused` only reports what would move).
- **`--log-file FILE`** — Append logs to a file as well as stderr (all actions, including `--unused`).
- **`--log-level`** — `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` (overrides the default below).
- **`-v` / `--verbose`** — Richer logging; without `--log-level`, one or more `-v` sets the root logger to **DEBUG**. With **`--unused`**, **`-v` / `--verbose` also prints every orphan path** to stderr while scanning.
- **`-q` / `--quiet`** — Warnings and errors only on stderr.
- **`--priority-subdirs`** — Comma-separated paths under `--uploads` handled **first** (default `2022/10,2022/11`). **Videos** and **images** use the same order: everything under those subtrees, **largest file first**, then the rest of uploads, again **largest first**. With **`--report-largest`**, lines under a priority subtree are marked with `*` in the table. **`--unused` does not use this flag** (it walks the whole tree except `unused/`).

**Video options** (with `--videos` or `--all`)

- **`--video-backup-dir DIR`** — Copy each original here before replace (default: `<uploads>/unused/.video_backup`, same relative paths as under uploads).
- **`--min-video-size-mb`** — Only transcode when the source is larger than this (default `20`). Use **`--force`** to transcode every video regardless.
- **`--max-video-mb`** — Target maximum output size (default `20`).
- **`--video-long-edge`** — Max long edge in pixels before extra downscale steps (default `1200`).
- **`--force`** — Videos: ignore `--min-video-size-mb`. Images: also re-encode existing `.webp` (see `--help`).

**Image options** (with `--images` or `--all`)

- **`--image-backup-root`** — Tree under `--uploads` where originals are moved before WebP (default `.image-originals`).
- **`--image-max-long-edge`** — After resize (default `1920`).
- **`--cwebp-quality`**, **`--lossless-alpha`**, **`--webp-reencode-min-bytes`**, **`--image-limit`** — See `python3 scripts/media_optimize.py --help`.

**Exit codes**

- **`0`** — Success (including “all skipped” for video if nothing matched thresholds).
- **`1`** — **`--images`** / **`--all`** image pass reported errors (e.g. a file failed), or **cwebp** / **Pillow** missing when images ran.
- **`2`** — No action selected, **`argparse`** validation error, or **at least one video transcode failed** (when `--videos` / `--all` ran the video pass).

### Video compression (`--videos`)

1. Put **`ffmpeg`** and **`ffprobe`** on your `PATH` (e.g. Homebrew `ffmpeg`).
2. From the project root, point **`--uploads`** at your real `wp-content/uploads` tree.

```bash
# See queue and settings (no encoding)
python3 scripts/media_optimize.py \
  --uploads ./uploads \
  --videos \
  --dry-run

# Run compression; log to a file
python3 scripts/media_optimize.py \
  --uploads ./uploads \
  --videos \
  --log-file media_optimize_run.log

# Stricter cap, custom backup location
python3 scripts/media_optimize.py \
  --uploads ./uploads \
  --videos \
  --max-video-mb 15 \
  --video-long-edge 1080 \
  --video-backup-dir /path/to/video-originals-backup
```

After **`.mov` / `.webm` / `.m4v` → `.mp4`**, update the database or dump (e.g. `sql_serialize_patch.py` with `--replace .mov:.mp4`) so URLs match.

**Replacing originals (images and videos)**

- **Images:** The script **moves** the original file (e.g. `photo.jpg`) into the backup tree, then writes **`photo.webp` in the same folder** under uploads. The uploads tree ends up with WebP in place of the old name’s role; the original bytes live only under `--image-backup-root` until you delete them.
- **Videos:** Each source is **copied** to the video backup dir, then the working file is replaced by the encoded **MP4** (same basename for `.mp4`; `.mov` / `.webm` / `.m4v` become a sibling `.mp4` and the non-MP4 source is removed after success).

**Examples**

```bash
# Help (lists every flag)
python3 scripts/media_optimize.py --help

# Largest files (safe; no file changes; --dry-run does not change the report)
python3 scripts/media_optimize.py \
  --uploads ./uploads \
  --report-largest

# Preview video + image work (see also "Video compression" for video-only examples)
python3 scripts/media_optimize.py \
  --uploads ./uploads \
  --all --dry-run

# Videos only (quick check)
python3 scripts/media_optimize.py --uploads ./uploads --videos --dry-run

# Log to a file (paths still relative to project root if you use ./uploads)
python3 scripts/media_optimize.py \
  --uploads ./uploads \
  --report-largest \
  --log-file media_optimize_run.log

# Move unreferenced files (default --sql = project root vcawolor_wp1.sql)
python3 scripts/media_optimize.py \
  --uploads ./uploads \
  --unused \
  --dry-run \
  --verbose

# Same, with a log file on disk
python3 scripts/media_optimize.py \
  --uploads ./uploads \
  --unused \
  --dry-run \
  --verbose \
  --log-file media_optimize_run.log

# Actually move orphans (omit --dry-run)
python3 scripts/media_optimize.py \
  --uploads ./uploads \
  --sql vcawolor_wp1.sql \
  --unused
```

Install Python deps first (`pip install -r requirements.txt`) so **`--images`** has Pillow; put **ffmpeg**, **ffprobe**, and **cwebp** on your `PATH` for **`--videos`** / **`--images`**.

### `scripts/sql_upload_index.py`

Lists how many distinct upload paths appear in a SQL dump; optional samples.

```bash
python3 scripts/sql_upload_index.py vcawolor_wp1.sql --count
python3 scripts/sql_upload_index.py vcawolor_wp1.sql --sample 20
# or: python3 -m scripts.sql_upload_index vcawolor_wp1.sql --count
```

### `scripts/sql_serialize_patch.py`

Imports a SQL dump into a **temporary** MySQL database, patches `wp_postmeta`, `wp_posts`, and `wp_options` text columns through **`tools/serialize_patch.php`** (so PHP-serialized `meta_value` stays valid), then runs **`mysqldump`** to write the output file. The default temp database (`vcawolor_wp_patch_tmp`) is **dropped** after a successful run unless you pass **`--keep-temp-db`**.

**Prerequisites**

- **MySQL or MariaDB** running locally (or reachable via `--mysql-host` / `--mysql-port`), with a user that can `CREATE DATABASE`, `DROP DATABASE`, and import the full dump.
- **`mysql`**, **`mysqldump`**, and **`php`** on your `PATH` (or override with `--mysql-bin`, `--mysqldump-bin`, `--php-bin`).
- Virtualenv with dependencies: `pip install -r requirements.txt` (provides **pymysql**).

**Basic run** (from project root; use your real MySQL user and password):

```bash
source .venv/bin/activate   # if you use the venv from Setup
python3 scripts/sql_serialize_patch.py \
  --sql vcawolor_wp1.sql \
  --out vcawolor_wp1.patched.sql \
  --replace .jpg:.webp --replace .jpeg:.webp \
  --mysql-user root --mysql-password ''
```

For a non-empty password, use e.g. `--mysql-password 'yourpassword'`. Quotes around the password avoid shell parsing issues.

**Preview how many rows would be patched** (imports the dump, prints counts, then drops the temp DB unless you add `--keep-temp-db`):

```bash
python3 scripts/sql_serialize_patch.py \
  --sql vcawolor_wp1.sql \
  --out /tmp/unused.sql \
  --replace .jpg:.webp \
  --mysql-user root --mysql-password '' \
  --dry-run
```

(`--out` is still required by the CLI; it is not written in `--dry-run` mode. With `--skip-import`, `--dry-run` does not drop the temp database.)

**Many replacements** via a JSON file `[[ "from", "to" ], ...]`:

```bash
python3 scripts/sql_serialize_patch.py \
  --sql vcawolor_wp1.sql \
  --out vcawolor_wp1.patched.sql \
  --replacements-json replacements.json \
  --mysql-user root --mysql-password ''
```

**Re-patch without re-importing** (database already loaded as `--temp-database`):

```bash
python3 scripts/sql_serialize_patch.py \
  --sql vcawolor_wp1.sql \
  --out vcawolor_wp1.patched.sql \
  --replace .mov:.mp4 \
  --mysql-user root --mysql-password '' \
  --skip-import
```

More flags: `--table-prefix`, `--temp-database`, `--keep-temp-db` — see the docstring at the top of `scripts/sql_serialize_patch.py` or run `python3 scripts/sql_serialize_patch.py --help`.
