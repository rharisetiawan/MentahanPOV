"""Doctor Idol Season IV (unofficial fancam): from raw recordings to reels.

Workflow for a batch of finalists (details: doctor_idol/README.md):

    python scripts/doctor_idol.py scan --write     # new raw videos -> finalists.toml stubs
    python scripts/doctor_idol.py suggest --all    # loudest 30 s windows per recording
      (fill name/city/song/segments in doctor_idol/finalists.toml)
    python scripts/doctor_idol.py render --all     # reels.mp4 + caption.txt per finalist
    python scripts/doctor_idol.py status           # what's done, what's missing

Runs locally (needs ffmpeg/ffprobe on PATH); it isn't part of the bot.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Windows consoles default to cp1252; names and emoji in captions need UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from doctor_idol import catalog as cat  # noqa: E402
from doctor_idol.captions import build_captions  # noqa: E402


def _pick(catalog: cat.Catalog, slugs: list[str], all_: bool) -> list[cat.Finalist]:
    if all_:
        return list(catalog.finalists)
    if not slugs:
        raise SystemExit("sebutkan slug finalis, atau pakai --all")
    return [catalog.get(s) for s in slugs]


def cmd_status(catalog: cat.Catalog, args) -> int:
    root = cat.media_root()
    print(f"Media: {root}")
    print(f"{'slug':28} {'raw':4} {'clip':>6} {'reel':5} {'status':9} kurang")
    for f in catalog.finalists:
        raw = cat.raw_video(f.slug, root)
        reel = cat.reels_dir(f.slug, root) / "reels.mp4"
        clip = f"{f.clip_length:.0f}s" if f.segments else "-"
        missing = ", ".join(f.missing()) or "-"
        print(f"{f.slug:28} {'ya' if raw else 'TIDAK':4} {clip:>6} {'ya' if reel.exists() else '-':5} "
              f"{f.status:9} {missing}")
    extra = cat.unlisted_videos(catalog, root)
    if extra:
        print(f"\n{len(extra)} video di raw/ belum ada di finalists.toml — jalankan: scan --write")
    return 0


def cmd_scan(catalog: cat.Catalog, args) -> int:
    root = cat.media_root()
    new = cat.unlisted_videos(catalog, root)
    if not new:
        print(f"Tidak ada video baru di {cat.raw_dir(root)}")
        return 0
    stubs = []
    known = {f.slug for f in catalog.finalists}
    for path in new:
        slug = cat.slugify(path.stem)
        if slug in known:
            print(f"! {path.name}: slug '{slug}' sudah dipakai, ganti nama filenya dulu")
            continue
        known.add(slug)
        target = path.with_name(slug + path.suffix.lower())
        if args.write and target != path:
            path.rename(target)
            print(f"rename {path.name} -> {target.name}")
        stubs.append(cat.stub_entry(slug))
        print(f"+ {slug}")
    if args.write and stubs:
        with cat.CATALOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write("".join(stubs))
        print(f"\n{len(stubs)} entri baru ditambahkan ke {cat.CATALOG_PATH}")
    elif stubs:
        print("\n(belum ditulis; tambah --write untuk menambahkan ke finalists.toml)")
    return 0


def cmd_suggest(catalog: cat.Catalog, args) -> int:
    from doctor_idol.highlights import best_windows, measure

    length = args.length or catalog.settings.target_clip_s
    for f in _pick(catalog, args.slugs, args.all):
        raw = cat.raw_video(f.slug)
        if not raw:
            print(f"{f.slug}: video mentah tidak ada")
            continue
        loud = measure(raw)
        windows = best_windows(loud, length=length, count=args.count)
        print(f"{f.slug} ({len(loud)} s):")
        for w in windows:
            mm, ss = divmod(int(w.start), 60)
            print(f"  segments = [{w.as_toml()}]   # mulai {mm}:{ss:02d}, {w.loudness} LUFS")
    return 0


def _write_caption(f: cat.Finalist, catalog: cat.Catalog) -> Path:
    out = cat.reels_dir(f.slug) / "caption.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_captions(f, catalog.settings), encoding="utf-8")
    return out


def cmd_captions(catalog: cat.Catalog, args) -> int:
    for f in _pick(catalog, args.slugs, args.all):
        print(_write_caption(f, catalog))
    return 0


def cmd_render(catalog: cat.Catalog, args) -> int:
    from doctor_idol.cover import make_cover
    from doctor_idol.render import render

    failed = 0
    for f in _pick(catalog, args.slugs, args.all):
        raw = cat.raw_video(f.slug)
        reel = cat.reels_dir(f.slug) / "reels.mp4"
        if not raw:
            print(f"- {f.slug}: lewati, video mentah tidak ada")
            continue
        if not f.segments:
            print(f"- {f.slug}: lewati, segments belum diisi (jalankan suggest)")
            continue
        if reel.exists() and not args.force and reel.stat().st_mtime > max(
            raw.stat().st_mtime, cat.CATALOG_PATH.stat().st_mtime
        ):
            print(f"= {f.slug}: sudah ada dan terbaru (pakai --force untuk render ulang)")
            continue
        try:
            out = render(f, catalog.settings, source=raw, out_dir=reel.parent)
        except (RuntimeError, ValueError) as exc:
            failed += 1
            print(f"x {f.slug}: {exc}")
            continue
        _write_caption(f, catalog)
        make_cover(out, f, catalog.settings)
        gaps = f.missing()
        note = f"  (masih kurang: {', '.join(gaps)})" if gaps else ""
        print(f"✓ {f.slug}: {out}{note}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="ringkasan semua finalis")
    p = sub.add_parser("scan", help="cari video baru di raw/")
    p.add_argument("--write", action="store_true", help="rename ke slug + tambah ke finalists.toml")
    for name, helptext in (("suggest", "usulkan potongan tersemarak"),
                           ("render", "buat reels.mp4 + cover.jpg + caption.txt"),
                           ("captions", "tulis ulang caption.txt saja")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("slugs", nargs="*")
        p.add_argument("--all", action="store_true")
        if name == "suggest":
            p.add_argument("--length", type=float, help="panjang potongan (detik)")
            p.add_argument("--count", type=int, default=3)
        if name == "render":
            p.add_argument("--force", action="store_true", help="render ulang walau sudah ada")
    args = parser.parse_args(argv)
    try:
        catalog = cat.load_catalog()
    except cat.CatalogError as exc:
        print(f"finalists.toml: {exc}", file=sys.stderr)
        return 2
    handler = {"status": cmd_status, "scan": cmd_scan, "suggest": cmd_suggest,
               "render": cmd_render, "captions": cmd_captions}[args.cmd]
    try:
        return handler(catalog, args)
    except cat.CatalogError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
