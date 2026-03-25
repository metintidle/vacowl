#!/usr/bin/env python3
"""
WordPress uploads tooling (see project plan). Single CLI: paths, logging, dry-run,
--videos (ffmpeg: H.264/AAC, long-edge cap, ≤20 MB), --images (Pillow + cwebp), --report-largest, --unused, SQL patch stub.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None  # type: ignore[misc, assignment]
    ImageOps = None  # type: ignore[misc, assignment]

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None  # type: ignore[misc, assignment]

# Matches wp-content/uploads/... in plain URLs and in JSON-style escaped slashes (\/).
_RE_UPLOADS_PREFIX = re.compile(
    r"wp-content(?:\\/|/)+uploads(?:\\/|/)((?:(?:\\/)|/|[A-Za-z0-9_.\-]|%[0-9A-Fa-f]{2})+)",
    re.IGNORECASE,
)

LOG = logging.getLogger("media_optimize")

DEFAULT_PRIORITY_SUBDIRS: tuple[str, ...] = ("2022/10", "2022/11")
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".m4v"})
# Plan §1: H.264/AAC MP4, long edge ≤1200px, target ≤20 MB (CRF steps, then downscale).
VIDEO_MAX_LONG_EDGE_DEFAULT = 1200
VIDEO_TARGET_MAX_BYTES_DEFAULT = 20 * 1024 * 1024
VIDEO_CRF_INITIAL = 24
VIDEO_CRF_STEP = 2
VIDEO_CRF_CEILING = 36
VIDEO_DOWNSCALE_STEPS: tuple[int, ...] = (960, 720, 540)

IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
)
SKIP_DIR_NAMES = frozenset({"unused", ".image-originals"})
SKIP_AS_NON_RASTER = frozenset({".pdf", ".svg"}) | VIDEO_EXTENSIONS
DEFAULT_MAX_LONG_EDGE = 1920
DEFAULT_CWEBP_QUALITY = 85


def configure_logging(
    level: int,
    *,
    log_file: Path | None,
) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    err = logging.StreamHandler(sys.stderr)
    err.setFormatter(fmt)
    root.addHandler(err)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


def resolve_log_level(*, quiet: bool, verbose: int, log_level: str | None) -> int:
    if log_level:
        return getattr(logging, log_level.upper(), logging.INFO)
    if quiet:
        return logging.WARNING
    if verbose >= 2:
        return logging.DEBUG
    if verbose == 1:
        return logging.DEBUG
    return logging.INFO


def _normalize_subdir(s: str) -> str:
    s = s.strip().replace("\\", "/").strip("/")
    while "//" in s:
        s = s.replace("//", "/")
    return s


def parse_priority_subdirs(raw: str | None) -> tuple[str, ...]:
    if not raw or not str(raw).strip():
        return DEFAULT_PRIORITY_SUBDIRS
    parts = [_normalize_subdir(p) for p in raw.split(",") if _normalize_subdir(p)]
    return tuple(parts) if parts else DEFAULT_PRIORITY_SUBDIRS


def canonical_upload_relpath(fragment: str) -> str | None:
    """Turn a raw capture after 'uploads/' into a normalized relative path."""
    if not fragment or fragment.startswith(".."):
        return None
    s = fragment.replace("\\/", "/").replace("\\\\", "\\")
    for sep in ("?", "#"):
        if sep in s:
            s = s.split(sep, 1)[0]
    s = s.strip("/")
    if not s or ".." in Path(s).parts:
        return None
    norm = Path(os.path.normpath(s)).as_posix()
    if norm.startswith("../") or norm == "..":
        return None
    return norm


def referenced_upload_paths_from_sql(sql_text: str) -> set[str]:
    """Collect every uploads-relative path mentioned anywhere in the dump text."""
    refs: set[str] = set()
    for m in _RE_UPLOADS_PREFIX.finditer(sql_text):
        rel = canonical_upload_relpath(m.group(1))
        if rel:
            refs.add(rel)
    return refs


def load_sql(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _upload_walk_skip_names(
    uploads_root: Path, image_backup_root: Path | None = None
) -> frozenset[str]:
    """Directory name segments to prune under uploads (quarantine, image backup tree, etc.)."""
    names = set(SKIP_DIR_NAMES)
    if image_backup_root is not None:
        try:
            rel = image_backup_root.resolve().relative_to(uploads_root.resolve())
            if rel.parts:
                names.add(rel.parts[0])
        except ValueError:
            pass
    return frozenset(names)


def iter_upload_files(uploads_root: Path):
    """Yield (absolute_path, relative_posix_path) for each file under uploads_root."""
    uploads_root = uploads_root.resolve()
    skip = _upload_walk_skip_names(uploads_root, None)
    for dirpath, dirnames, filenames in os.walk(
        uploads_root, topdown=True, followlinks=False
    ):
        dirnames[:] = [d for d in dirnames if d not in skip]

        rel_dir = Path(dirpath).relative_to(uploads_root)
        if any(part in skip for part in rel_dir.parts):
            dirnames[:] = []
            continue

        for name in filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                continue
            rel = (rel_dir / name).as_posix()
            if any(rel == s or rel.startswith(s + "/") for s in skip):
                continue
            yield p, rel


def move_unused_uploads(
    uploads_root: Path,
    referenced: set[str],
    *,
    dry_run: bool,
    verbose: bool,
) -> tuple[int, int, int]:
    """
    Move files whose relative path is not in ``referenced`` to
    ``uploads_root / 'unused' / relpath``.

    Returns (files_on_disk, orphans_found, moves_done).
    """
    uploads_root = uploads_root.resolve()
    unused_root = uploads_root / "unused"
    on_disk = 0
    orphans = 0
    moved = 0

    for abs_path, rel in iter_upload_files(uploads_root):
        on_disk += 1
        if rel in referenced:
            continue
        orphans += 1
        if verbose:
            LOG.debug("orphan: %s", rel)
        dest = unused_root / rel
        if dry_run:
            if verbose:
                LOG.info("[dry-run] would move: %s -> %s", rel, dest.relative_to(uploads_root))
            moved += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            LOG.warning("skip (destination exists): %s -> %s", rel, dest)
            continue
        shutil.move(str(abs_path), str(dest))
        moved += 1

    return on_disk, orphans, moved


def is_under_priority_prefix(rel_posix: str, prefixes: tuple[str, ...]) -> bool:
    rel = rel_posix.replace("\\", "/")
    for p in prefixes:
        pn = _normalize_subdir(p)
        if not pn:
            continue
        if rel == pn or rel.startswith(pn + "/"):
            return True
    return False


def format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit, div in (("KiB", 1024), ("MiB", 1024**2), ("GiB", 1024**3)):
        x = n / div
        if x < 1024 or unit == "GiB":
            return f"{x:.2f} {unit}"
    return f"{n} B"


def _image_priority_sort_key(
    rel_posix: str, prefixes: tuple[str, ...]
) -> tuple[int, int, str]:
    rel = rel_posix.replace("\\", "/")
    for i, p in enumerate(prefixes):
        pn = _normalize_subdir(p)
        if pn and (rel == pn or rel.startswith(pn + "/")):
            return (0, i, rel)
    return (1, 0, rel)


def collect_image_paths(
    uploads_root: Path,
    priority: tuple[str, ...],
    skip_names: frozenset[str],
) -> list[Path]:
    """Raster images under uploads; priority prefixes first (plan §3)."""
    uploads_root = uploads_root.resolve()
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(
        uploads_root, topdown=True, followlinks=False
    ):
        dirnames[:] = [d for d in dirnames if d not in skip_names]
        rel_dir = Path(dirpath).relative_to(uploads_root)
        if any(part in skip_names for part in rel_dir.parts):
            dirnames[:] = []
            continue
        for name in filenames:
            ext = Path(name).suffix.lower()
            if ext in SKIP_AS_NON_RASTER:
                continue
            if ext not in IMAGE_EXTENSIONS:
                continue
            out.append(Path(dirpath) / name)

    def sort_key(p: Path) -> tuple[int, int, str]:
        rel = p.resolve().relative_to(uploads_root).as_posix()
        return _image_priority_sort_key(rel, priority)

    out.sort(key=sort_key)
    return out


def _has_transparency(img: Image.Image) -> bool:
    if img.mode in ("RGBA", "LA"):
        return True
    if img.mode != "P":
        return False
    return "transparency" in img.info or img.info.get("transparency") is not None


def _prepare_for_webp(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    if img.mode == "CMYK":
        img = img.convert("RGB")
    elif img.mode == "P":
        img = img.convert("RGBA")
    elif img.mode not in ("RGB", "RGBA", "L", "LA"):
        img = img.convert("RGBA" if _has_transparency(img) else "RGB")
    return img


def _resize_max_long_edge(img: Image.Image, max_long_edge: int) -> Image.Image:
    w, h = img.size
    long_edge = max(w, h)
    if long_edge <= max_long_edge:
        return img
    scale = max_long_edge / float(long_edge)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    return img.resize((nw, nh), Image.Resampling.LANCZOS)


def _run_cwebp(
    src_png: Path,
    out_webp: Path,
    quality: int,
    lossless_alpha: bool,
    has_alpha: bool,
) -> None:
    out_webp.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["cwebp", "-quiet"]
    if lossless_alpha and has_alpha:
        cmd.append("-lossless")
    else:
        cmd.extend(["-q", str(quality)])
        if has_alpha:
            cmd.extend(["-alpha_q", "100"])
    cmd.extend([str(src_png), "-o", str(out_webp)])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
        raise RuntimeError(f"cwebp failed: {err}")


def process_one_image(
    path: Path,
    uploads_root: Path,
    backup_root: Path,
    max_long_edge: int,
    cwebp_quality: int,
    lossless_alpha: bool,
    force: bool,
    webp_reencode_min_bytes: int | None,
    dry_run: bool,
) -> tuple[bool, str]:
    """
    Pillow resize (max long edge), encode WebP via cwebp; move original into
    ``backup_root`` mirroring the path under uploads.
    """
    uploads_root = uploads_root.resolve()
    path = path.resolve()
    ext = path.suffix.lower()
    rel_under_uploads = path.relative_to(uploads_root)

    if ext == ".webp":
        if not force:
            if webp_reencode_min_bytes is None:
                return True, "skip .webp (use --force or --webp-reencode-min-bytes)"
            try:
                if path.stat().st_size < webp_reencode_min_bytes:
                    return True, "skip .webp (under size threshold)"
            except OSError as e:
                return False, str(e)

    out_webp = (
        uploads_root / rel_under_uploads
        if ext == ".webp"
        else uploads_root / rel_under_uploads.with_suffix(".webp")
    )

    if Image is None or ImageOps is None:
        return False, "Pillow not installed"

    try:
        with Image.open(path) as im:
            im.load()
            work = _prepare_for_webp(im)
            work = _resize_max_long_edge(work, max_long_edge)
    except OSError as e:
        return False, f"open/resize: {e}"

    if work.mode == "LA":
        work = work.convert("RGBA")
    has_alpha = work.mode in ("RGBA", "LA")

    backup_target = backup_root / rel_under_uploads

    if dry_run:
        return (
            True,
            f"dry-run: backup {backup_target.as_posix()} -> "
            f"{out_webp.relative_to(uploads_root).as_posix()}",
        )

    backup_target.parent.mkdir(parents=True, exist_ok=True)
    if backup_target.exists():
        backup_target.unlink()
    shutil.move(str(path), str(backup_target))

    fd, tmp_name = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    tmp_png_path = Path(tmp_name)
    try:
        work.save(tmp_png_path, format="PNG", compress_level=6)
        out_webp.parent.mkdir(parents=True, exist_ok=True)
        if out_webp.exists():
            out_webp.unlink()
        _run_cwebp(
            tmp_png_path,
            out_webp,
            cwebp_quality,
            lossless_alpha,
            has_alpha,
        )
    except Exception as e:
        try:
            shutil.move(str(backup_target), str(path))
        except OSError:
            pass
        return False, str(e)
    finally:
        tmp_png_path.unlink(missing_ok=True)

    return True, (
        f"ok -> {out_webp.relative_to(uploads_root).as_posix()} "
        f"(original in backup: {backup_target.relative_to(backup_root).as_posix()})"
    )


def cmd_images(args: argparse.Namespace, priority: tuple[str, ...]) -> int:
    """Plan §3: max long edge (default 1920), cwebp, originals under configurable backup root."""
    if not shutil.which("cwebp"):
        LOG.error("cwebp not found on PATH (install WebP tools).")
        return 1
    if Image is None:
        LOG.error("Pillow required: pip install -r requirements.txt")
        return 1

    uploads = args.uploads.resolve()
    backup_root = Path(args.image_backup_root)
    if not backup_root.is_absolute():
        backup_root = (uploads / backup_root).resolve()
    else:
        backup_root = backup_root.resolve()

    skip_names = _upload_walk_skip_names(uploads, backup_root)
    paths = collect_image_paths(uploads, priority, skip_names)
    if args.image_limit is not None:
        paths = paths[: max(0, args.image_limit)]

    LOG.info(
        "Images: %d file(s); backup %s; max long edge %d; cwebp -q %d; lossless_alpha=%s",
        len(paths),
        backup_root,
        args.image_max_long_edge,
        args.cwebp_quality,
        args.lossless_alpha,
    )

    if args.dry_run:
        cap = 100
        for p in paths[:cap]:
            ok, msg = process_one_image(
                p,
                uploads_root=uploads,
                backup_root=backup_root,
                max_long_edge=args.image_max_long_edge,
                cwebp_quality=args.cwebp_quality,
                lossless_alpha=args.lossless_alpha,
                force=bool(args.force),
                webp_reencode_min_bytes=args.webp_reencode_min_bytes,
                dry_run=True,
            )
            if ok:
                LOG.info("%s: %s", p, msg)
            else:
                LOG.error("%s: %s", p, msg)
        if len(paths) > cap:
            LOG.info("[dry-run] ... %d more file(s) not simulated line-by-line", len(paths) - cap)
        return 0

    errors = 0
    it: Iterable[Path] = paths
    if tqdm is not None and sys.stderr.isatty():
        it = tqdm(paths, desc="images", unit="file")

    for p in it:
        ok, msg = process_one_image(
            p,
            uploads_root=uploads,
            backup_root=backup_root,
            max_long_edge=args.image_max_long_edge,
            cwebp_quality=args.cwebp_quality,
            lossless_alpha=args.lossless_alpha,
            force=bool(args.force),
            webp_reencode_min_bytes=args.webp_reencode_min_bytes,
            dry_run=False,
        )
        if not ok:
            errors += 1
            LOG.error("%s: %s", p, msg)
        elif args.verbose > 0:
            LOG.info("%s: %s", p, msg)

    LOG.info("Images: done (%d file(s); %d error(s))", len(paths), errors)
    return 1 if errors else 0


def _run_cmd(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def ffprobe_json(path: Path) -> dict | None:
    r = _run_cmd(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    if r.returncode != 0:
        LOG.warning("ffprobe failed for %s: %s", path, (r.stderr or r.stdout)[:500])
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def scale_filter_video_long_edge(max_long: int) -> str:
    # libx264 needs even width/height; first scale caps long edge, then round down to even.
    return (
        f"scale='if(gt(iw,ih),min({max_long},iw),-2)':"
        f"'if(gt(iw,ih),-2,min({max_long},ih))':force_original_aspect_ratio=decrease,"
        f"scale=trunc(iw/2)*2:trunc(ih/2)*2"
    )


def ffmpeg_transcode_video(
    src: Path,
    dst: Path,
    *,
    max_long_edge: int,
    crf: int,
    audio_bitrate_k: int = 128,
) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    vf = scale_filter_video_long_edge(max_long_edge)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        str(crf),
        "-c:a",
        "aac",
        "-b:a",
        f"{audio_bitrate_k}k",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    r = _run_cmd(cmd)
    if r.returncode != 0:
        LOG.error("ffmpeg failed for %s -> %s: %s", src, dst, (r.stderr or r.stdout)[:800])
        return False
    return True


def backup_video_original(rel: str, src: Path, backup_root: Path) -> Path:
    dest = backup_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def output_video_path_for_source(src: Path) -> Path:
    if src.suffix.lower() in {".mov", ".m4v", ".webm"}:
        return src.with_suffix(".mp4")
    return src


def transcode_one_video(
    src: Path,
    rel: str,
    *,
    backup_root: Path,
    min_bytes_to_process: int,
    force: bool,
    max_long_edge: int,
    target_max_bytes: int,
) -> tuple[bool, str]:
    size = src.stat().st_size
    if not force and size <= min_bytes_to_process:
        return True, "skip (under size threshold)"

    if not ffprobe_json(src):
        return False, "ffprobe failed"

    out_path = output_video_path_for_source(src)
    backup_video_original(rel, src, backup_root)

    dimension_candidates: list[int] = [max_long_edge]
    for d in VIDEO_DOWNSCALE_STEPS:
        if d < max_long_edge:
            dimension_candidates.append(d)

    for current_edge in dimension_candidates:
        crf = VIDEO_CRF_INITIAL
        while crf <= VIDEO_CRF_CEILING:
            fd, tmp_name = tempfile.mkstemp(suffix=".mp4", dir=str(out_path.parent))
            os.close(fd)
            tmp_path = Path(tmp_name)
            try:
                ok = ffmpeg_transcode_video(src, tmp_path, max_long_edge=current_edge, crf=crf)
                if not ok:
                    return False, "ffmpeg error"
                out_sz = tmp_path.stat().st_size
                if out_sz <= target_max_bytes:
                    if src.exists():
                        src.unlink()
                    tmp_path.replace(out_path)
                    return True, f"ok dim={current_edge} crf={crf} size={format_bytes(out_sz)}"
            finally:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
            crf += VIDEO_CRF_STEP
        LOG.warning(
            "Still above %s at dim=%s up to CRF=%s (%s) — trying smaller dimension",
            format_bytes(target_max_bytes),
            current_edge,
            VIDEO_CRF_CEILING,
            rel,
        )

    return False, f"could not reach {format_bytes(target_max_bytes)} (CRF/dimension floor)"


def iter_videos_prioritized(
    uploads_root: Path, priority_prefixes: tuple[str, ...]
) -> list[tuple[Path, str, int]]:
    rows: list[tuple[Path, str, int]] = []
    for p, rel in iter_upload_files(uploads_root):
        if Path(rel).suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        rows.append((p, rel, sz))
    pri = [r for r in rows if is_under_priority_prefix(r[1], priority_prefixes)]
    oth = [r for r in rows if not is_under_priority_prefix(r[1], priority_prefixes)]
    pri.sort(key=lambda x: x[2], reverse=True)
    oth.sort(key=lambda x: x[2], reverse=True)
    return pri + oth


def cmd_videos(args: argparse.Namespace, priority: tuple[str, ...]) -> int:
    """
    FFmpeg: H.264 + AAC in MP4, long edge capped (default 1200px), target ≤20 MB
    via CRF escalation then smaller dimensions (960 / 720 / 540).
    """
    uploads_root = args.uploads.resolve()
    backup_root = Path(args.video_backup_dir).resolve() if args.video_backup_dir else (
        uploads_root / "unused" / ".video_backup"
    )
    min_bytes = int(args.min_video_size_mb * 1024 * 1024)
    target_bytes = int(args.max_video_mb * 1024 * 1024)
    max_edge = int(args.video_long_edge)

    queue = iter_videos_prioritized(uploads_root, priority)
    if args.video_prefix:
        vp = _normalize_subdir(str(args.video_prefix))
        if vp:
            queue = [r for r in queue if r[1] == vp or r[1].startswith(vp + "/")]
            LOG.info("Filtered to --video-prefix %s: %d file(s)", vp, len(queue))
    LOG.info(
        "Video queue: %d file(s); priority %s; backup root %s; max long edge %d; target %s",
        len(queue),
        list(priority),
        backup_root,
        max_edge,
        format_bytes(target_bytes),
    )

    if args.dry_run:
        for i, (path, rel, sz) in enumerate(queue[:20], 1):
            LOG.info("[dry-run] %d. %s (%s)", i, rel, format_bytes(sz))
        if len(queue) > 20:
            LOG.info("[dry-run] ... and %d more", len(queue) - 20)
        return 0

    if not queue:
        return 0

    ok_n = skip_n = fail_n = 0
    force = bool(args.force)
    for i, (path, rel, _sz) in enumerate(queue, 1):
        LOG.info("[%d/%d] %s", i, len(queue), rel)
        ok, msg = transcode_one_video(
            path,
            rel,
            backup_root=backup_root,
            min_bytes_to_process=min_bytes,
            force=force,
            max_long_edge=max_edge,
            target_max_bytes=target_bytes,
        )
        if ok:
            if msg.startswith("skip"):
                skip_n += 1
            else:
                ok_n += 1
            LOG.info("  %s", msg)
        else:
            fail_n += 1
            LOG.error("  %s", msg)

    LOG.info("Videos: transcoded=%d skipped=%d failed=%d", ok_n, skip_n, fail_n)
    return 0 if fail_n == 0 else 2


def cmd_report_largest(
    uploads_root: Path,
    priority: tuple[str, ...],
    top_n: int,
    *,
    dry_run: bool,
) -> None:
    """Pre-run audit: largest raster/video files; * marks paths under priority dirs."""
    uploads_root = uploads_root.resolve()
    rows: list[tuple[int, str, bool]] = []
    for dirpath, dirnames, filenames in os.walk(uploads_root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        rel_dir = Path(dirpath).relative_to(uploads_root)
        if any(part in SKIP_DIR_NAMES for part in rel_dir.parts):
            dirnames[:] = []
            continue
        for name in filenames:
            p = Path(dirpath) / name
            if not p.is_file() or p.is_symlink():
                continue
            suf = p.suffix.lower()
            if suf not in VIDEO_EXTENSIONS and suf not in IMAGE_EXTENSIONS:
                continue
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            rel = (rel_dir / name).as_posix()
            pri = is_under_priority_prefix(rel, priority)
            rows.append((sz, rel, pri))

    total = sum(r[0] for r in rows)
    pri_bytes = sum(r[0] for r in rows if r[2])
    LOG.info(
        "Uploads: %s | media files: %d | total %s | priority subtrees: %s (%s)",
        uploads_root,
        len(rows),
        format_bytes(total),
        list(priority),
        format_bytes(pri_bytes),
    )
    if dry_run:
        LOG.info("[dry-run] report only; no files modified.")

    ranked = sorted(rows, key=lambda x: x[0], reverse=True)[: max(top_n, 1)]
    print("\n=== Largest media files (top %d) — * = under priority dir ===\n" % top_n)
    print(f"{'PRI':^3}  {'SIZE':>12}  RELATIVE PATH")
    print("-" * 72)
    for sz, rel, pri in ranked:
        mark = "*" if pri else " "
        print(f"{mark:^3}  {format_bytes(sz):>12}  {rel}")
    print()


def cmd_unused(args: argparse.Namespace) -> int:
    sql_path: Path = args.sql.resolve()
    uploads_root: Path = args.uploads.resolve()

    LOG.info("Indexing upload paths from SQL: %s", sql_path)
    refs = referenced_upload_paths_from_sql(load_sql(sql_path))
    LOG.info("Referenced upload paths (unique): %d", len(refs))

    dry_run = args.dry_run
    if dry_run:
        LOG.info("Dry-run: no files will be moved.")

    on_disk, orphans, done = move_unused_uploads(
        uploads_root,
        refs,
        dry_run=dry_run,
        verbose=args.verbose > 0,
    )
    LOG.info(
        "Files under uploads (excl. unused/): %d | orphans: %d | %s: %d",
        on_disk,
        orphans,
        "Would move" if dry_run else "Moved",
        done,
    )
    return 0


def cmd_sql_patch_stub(args: argparse.Namespace) -> None:
    prefix = "[dry-run] " if args.dry_run else ""
    LOG.info(
        "%s--sql-patch not implemented yet (sql=%s)",
        prefix,
        args.sql.resolve() if args.sql else None,
    )


def build_parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(
        description="WordPress media / uploads maintenance (vcawl project).",
    )
    p.add_argument(
        "--uploads",
        type=Path,
        required=True,
        help="Path to wp-content/uploads (contains year/month folders, etc.).",
    )
    p.add_argument(
        "--sql",
        type=Path,
        default=repo_root / "vcawolor_wp1.sql",
        help=f"Path to WordPress SQL dump (default: {repo_root / 'vcawolor_wp1.sql'}).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not move, transcode, or write outputs; log intended actions where supported.",
    )
    p.add_argument(
        "--log-file",
        type=Path,
        metavar="FILE",
        help="Append logs to this file as well as stderr.",
    )
    p.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=None,
        help="Override log level (default: INFO, or DEBUG with -v).",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="More verbose logging (-v or -vv for DEBUG when --log-level is unset).",
    )
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Warnings and errors only on stderr.",
    )
    p.add_argument(
        "--priority-subdirs",
        default=",".join(DEFAULT_PRIORITY_SUBDIRS),
        metavar="LIST",
        help=(
            "Comma-separated paths under uploads to treat as priority "
            f"(default: {','.join(DEFAULT_PRIORITY_SUBDIRS)})."
        ),
    )
    p.add_argument(
        "--report-largest",
        action="store_true",
        help="Print a pre-run report of largest image/video files under uploads.",
    )
    p.add_argument(
        "--report-top",
        type=int,
        default=50,
        metavar="N",
        help="With --report-largest, list top N files (default: 50).",
    )
    p.add_argument(
        "--video-prefix",
        default=None,
        metavar="SUBDIR",
        help=(
            "With --videos: only process files under this path relative to --uploads "
            "(e.g. 2022/10)."
        ),
    )
    p.add_argument(
        "--videos",
        action="store_true",
        help=(
            "Compress videos: H.264/AAC MP4, max long edge 1200px (configurable), "
            "target ≤20 MB via CRF then downscale; priority dirs first."
        ),
    )
    p.add_argument(
        "--images",
        action="store_true",
        help="Resize rasters (max long edge) + WebP via cwebp; originals -> --image-backup-root.",
    )
    p.add_argument("--all", action="store_true", help="Same as --videos --images.")
    p.add_argument(
        "--unused",
        action="store_true",
        help="Move files not referenced in the SQL dump to uploads/unused/.",
    )
    p.add_argument(
        "--sql-patch",
        action="store_true",
        help="Rewrite SQL for WebP / path changes using PHP serialize helper (planned).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Videos: transcode regardless of --min-video-size-mb. Images: re-encode .webp too.",
    )
    p.add_argument(
        "--video-backup-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Copy originals here before replace (default: <uploads>/unused/.video_backup). "
            "Mirrors relative paths under uploads."
        ),
    )
    p.add_argument(
        "--min-video-size-mb",
        type=float,
        default=VIDEO_TARGET_MAX_BYTES_DEFAULT / (1024 * 1024),
        metavar="MB",
        help="Only transcode when source is larger than this (default: 20).",
    )
    p.add_argument(
        "--max-video-mb",
        type=float,
        default=VIDEO_TARGET_MAX_BYTES_DEFAULT / (1024 * 1024),
        metavar="MB",
        help="Target maximum output file size (default: 20).",
    )
    p.add_argument(
        "--video-long-edge",
        type=int,
        default=VIDEO_MAX_LONG_EDGE_DEFAULT,
        metavar="PX",
        help="Maximum video long edge in pixels before downscale steps (default: 1200).",
    )
    p.add_argument(
        "--image-backup-root",
        type=Path,
        default=Path(".image-originals"),
        help=(
            "With --images/--all: directory tree mirroring uploads for originals before WebP output "
            "(relative paths resolve under --uploads; default: .image-originals)."
        ),
    )
    p.add_argument(
        "--image-max-long-edge",
        type=int,
        default=DEFAULT_MAX_LONG_EDGE,
        metavar="PX",
        help=f"With --images: max long edge in pixels after resize (default {DEFAULT_MAX_LONG_EDGE}).",
    )
    p.add_argument(
        "--cwebp-quality",
        type=int,
        default=DEFAULT_CWEBP_QUALITY,
        metavar="Q",
        help=f"With --images: cwebp -q for lossy RGB (default {DEFAULT_CWEBP_QUALITY}).",
    )
    p.add_argument(
        "--lossless-alpha",
        action="store_true",
        help="With --images: cwebp -lossless when the image has transparency.",
    )
    p.add_argument(
        "--webp-reencode-min-bytes",
        type=int,
        default=None,
        metavar="N",
        help="With --images: re-encode .webp when size >= N bytes (without --force).",
    )
    p.add_argument(
        "--image-limit",
        type=int,
        default=None,
        metavar="N",
        help="With --images: process at most N files (debug).",
    )
    return p


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    uploads = args.uploads.resolve()
    if not uploads.is_dir():
        parser.error(f"--uploads is not a directory: {uploads}")

    if args.unused or args.sql_patch:
        sql_path = args.sql.resolve()
        if not sql_path.is_file():
            parser.error(f"--sql is not a file: {sql_path}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    level = resolve_log_level(quiet=args.quiet, verbose=args.verbose, log_level=args.log_level)
    configure_logging(level, log_file=args.log_file)

    validate_args(parser, args)
    priority = parse_priority_subdirs(args.priority_subdirs)

    do_videos = args.all or args.videos
    do_images = args.all or args.images
    any_action = (
        args.report_largest or do_videos or do_images or args.unused or args.sql_patch
    )
    if not any_action:
        parser.print_help()
        print(
            "\nNo action selected. Use --report-largest, --videos, --images, --all, "
            "--unused, and/or --sql-patch.",
            file=sys.stderr,
        )
        return 2

    exit_code = 0

    if args.report_largest:
        cmd_report_largest(
            args.uploads,
            priority,
            max(1, args.report_top),
            dry_run=args.dry_run,
        )
    if do_videos:
        exit_code = max(exit_code, cmd_videos(args, priority))
    if do_images:
        exit_code = max(exit_code, cmd_images(args, priority))
    if args.unused:
        cmd_unused(args)
    if args.sql_patch:
        cmd_sql_patch_stub(args)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
