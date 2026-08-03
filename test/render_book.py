"""
Book-style flowing text renderer for .aks files.

Reads a plain-text file, wraps words to a given page width, and renders
each page as a PNG.  Reports OOV (out-of-vocabulary) statistics so you can
see at a glance how well the font covers real running text.

Pages are rendered in parallel using ProcessPoolExecutor; each worker
process owns its own AksReader so there is no shared mutable state.

Usage:
    cd akshara-generator
    uv run python test/render_book.py \
        ../fonts/22/noto_kannada_regular_22.aks \
        /path/to/book.txt \
        --out-dir /tmp/aks_book \
        --page-width 480 \
        --page-height 640 \
        --pages 10
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageDraw, ImageFont

# Reuse AksReader and segment from the sibling render_png module.
sys.path.insert(0, str(Path(__file__).parent))
from render_png import AksReader, segment  # noqa: E402

# ── Layout constants ──────────────────────────────────────────────────────────
_MARGIN      = 16   # pixels on each side / top / bottom
_LINE_GAP    = 6    # extra pixels between lines
_PARA_GAP    = 10   # extra pixels between paragraphs (on top of _LINE_GAP)
_BG          = 255  # white background
_INK         = 0    # black ink


# ── Word-level measurement and rendering ─────────────────────────────────────

def _key_advance(reader: AksReader, key_entry: object) -> int:
    """Return pixel advance for a KeyEntry by summing comp entry advances."""
    sz = reader.size
    du = lambda v: round(v * sz.size_px / sz.upem) if sz.upem else 0
    return sum(du(ce.hb_advance) for ce in reader.read_comp_entries(key_entry))


def _measure_word(reader: AksReader, word: str) -> tuple[int, list]:
    """
    Return (advance_px, draws) for a single whitespace-delimited word.

    draws = list of (rel_x, KeyEntry) ready to blit.  rel_x is relative
    to the start of the word (pen_x = 0 at word start).
    """
    rules = reader.rules
    h     = reader.size
    clusters = segment(word, rules)

    pen_x = 0
    draws: list[tuple[int, object]] = []

    for cluster in clusters:
        entry = reader.lookup(cluster)
        if entry is None:
            # OOV: fall back to individual codepoints.
            for cp in cluster:
                e = reader.lookup((cp,))
                if e is not None:
                    draws.append((pen_x, e))
                    pen_x += _key_advance(reader, e)
                else:
                    pen_x += h.glyph_height // 2   # blank advance for truly unknown
        else:
            draws.append((pen_x, entry))
            pen_x += _key_advance(reader, entry)

    return pen_x, draws


def _space_advance(reader: AksReader) -> int:
    """Pixel width of a space character (U+0020), or font-height/2 fallback."""
    e = reader.lookup((0x0020,))
    return _key_advance(reader, e) if e is not None else reader.size.glyph_height // 2


def _count_oov(reader: AksReader, text: str) -> int:
    """Count clusters in text that are not found in the .aks key table."""
    rules    = reader.rules
    clusters = segment(text, rules)
    oov      = 0
    for cluster in clusters:
        if reader.lookup(cluster) is None:
            # Check if OOV fallback (individual codepoints) also fails.
            for cp in cluster:
                if reader.lookup((cp,)) is None:
                    oov += 1
    return oov


# ── Line wrapping ─────────────────────────────────────────────────────────────

def wrap_paragraph(
    reader: AksReader,
    para: str,
    max_width: int,
    space_adv: int,
) -> list[list[tuple[str, int, list]]]:
    """
    Word-wrap a paragraph into lines.

    Returns list of lines; each line is a list of (word_str, advance, draws).
    """
    words = para.split()
    if not words:
        return []

    lines: list[list[tuple[str, int, list]]] = []
    current_line: list[tuple[str, int, list]] = []
    current_w = 0

    for word in words:
        adv, draws = _measure_word(reader, word)
        # Width this word would add to the current line.
        needed = adv if not current_line else space_adv + adv
        if current_line and current_w + needed > max_width:
            lines.append(current_line)
            current_line = [(word, adv, draws)]
            current_w    = adv
        else:
            current_line.append((word, adv, draws))
            current_w += needed

    if current_line:
        lines.append(current_line)

    return lines


# ── Glyph blit onto canvas ────────────────────────────────────────────────────

def _blit_draws(
    canvas: Image.Image,
    reader: AksReader,
    draws: list[tuple[int, object]],
    origin_x: int,
    origin_y: int,
) -> None:
    """Paste glyph bitmaps from *draws* onto *canvas* at (origin_x, origin_y)."""
    sz = reader.size
    du = lambda v: round(v * sz.size_px / sz.upem) if sz.upem else 0
    for rel_x, key_entry in draws:
        comp_entries = reader.read_comp_entries(key_entry)
        local_pen = 0
        for ce in comp_entries:
            gm = reader.read_glyph_metrics(ce.glyph_idx)
            dest_x = origin_x + rel_x + local_pen + du(ce.hb_x_off) + gm.bearing_x
            dest_y = origin_y + sz.baseline - gm.top_from_base - du(ce.hb_y_off)
            glyph_img = reader.glyph_image(ce.glyph_idx, gm)
            if glyph_img is not None:
                mask = glyph_img.point(lambda p: 255 - p)
                canvas.paste(glyph_img, (max(0, dest_x), dest_y), mask)
            local_pen += du(ce.hb_advance)


# ── Worker process state and tasks ────────────────────────────────────────────

# Each worker process loads its own AksReader into this global via _worker_init.
_w_reader: AksReader | None = None


def _worker_init(
    aks_path: str, size_px: int | None, weight: int, bpp: int | None
) -> None:
    global _w_reader
    _w_reader = AksReader(aks_path, size_px=size_px, weight=weight, bpp=bpp)


class _PageTask(NamedTuple):
    page_lines: list   # list[(line_words, is_para_start)]
    page_w: int
    page_h: int
    scale: int
    space_adv: int
    line_h: int
    page_num: int
    out_path: str


def _render_page_worker(task: _PageTask) -> tuple[int, int, int, int]:
    """Render one page and save it to disk. Returns (words, clusters, oov, lines)."""
    reader = _w_reader
    assert reader is not None, "worker not initialised"
    h = reader.size

    page_img = Image.new("L", (task.page_w * task.scale, task.page_h * task.scale), _BG)
    pen_y = _MARGIN
    total_words = total_clusters = total_oov = total_lines = 0

    for line_words, is_para_start in task.page_lines:
        if is_para_start and pen_y > _MARGIN:
            pen_y += _PARA_GAP

        pen_x = _MARGIN
        for wi, (word, adv, draws) in enumerate(line_words):
            if wi > 0:
                pen_x += task.space_adv
            if task.scale == 1:
                _blit_draws(page_img, reader, draws, pen_x, pen_y)
            else:
                tmp = Image.new("L", (max(adv, 1) + h.glyph_height, h.glyph_height), _BG)
                _blit_draws(tmp, reader, draws, 0, 0)
                big = tmp.resize(
                    (tmp.width * task.scale, tmp.height * task.scale),
                    resample=Image.NEAREST,
                )
                page_img.paste(big, (pen_x * task.scale, pen_y * task.scale))
            pen_x += adv
            total_words += 1
            total_clusters += len(draws)

        line_text = " ".join(w for w, _, _ in line_words)
        total_oov += _count_oov(reader, line_text)
        pen_y += task.line_h
        total_lines += 1

    draw = ImageDraw.Draw(page_img)
    draw.rectangle(
        [1, 1, task.page_w * task.scale - 2, task.page_h * task.scale - 2],
        outline=200,
    )
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 10 * task.scale
        )
    except OSError:
        font = ImageFont.load_default()
    draw.text(
        (task.page_w * task.scale // 2 - 20, (task.page_h - _MARGIN // 2) * task.scale),
        f"- {task.page_num + 1} -",
        fill=160,
        font=font,
    )
    page_img.save(task.out_path)
    return total_words, total_clusters, total_oov, total_lines


# ── Page rendering ────────────────────────────────────────────────────────────

def render_pages(
    reader: AksReader,
    aks_path: str,
    paragraphs: list[str],
    page_w: int,
    page_h: int,
    max_pages: int,
    out_dir: Path,
    scale: int = 2,
) -> dict:
    """
    Render up to *max_pages* pages of flowing text in parallel.

    Returns a stats dict with total_lines, total_words, total_oov,
    total_clusters, pages_written.
    """
    h         = reader.size
    line_h    = h.glyph_height + _LINE_GAP
    space_adv = _space_advance(reader)
    body_w    = page_w - 2 * _MARGIN

    stats = dict(total_lines=0, total_words=0, total_oov=0,
                 total_clusters=0, pages_written=0)

    # Pre-wrap all paragraphs into lines (sequential, in-memory lookups, fast).
    all_lines: list[tuple[list, bool]] = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        wrapped = wrap_paragraph(reader, para, body_w, space_adv)
        for i, line in enumerate(wrapped):
            all_lines.append((line, i == 0))

    if not all_lines:
        print("No text to render.")
        return stats

    out_dir.mkdir(parents=True, exist_ok=True)

    # Assign lines to pages, building one _PageTask per page.
    tasks: list[_PageTask] = []
    line_idx = 0
    while line_idx < len(all_lines) and len(tasks) < max_pages:
        page_lines: list[tuple[list, bool]] = []
        pen_y = _MARGIN
        while pen_y + line_h <= page_h - _MARGIN and line_idx < len(all_lines):
            line_words, is_para_start = all_lines[line_idx]
            if is_para_start and pen_y > _MARGIN:
                pen_y += _PARA_GAP
                if pen_y + line_h > page_h - _MARGIN:
                    break
            page_lines.append((line_words, is_para_start))
            pen_y += line_h
            line_idx += 1
        if page_lines:
            page_num = len(tasks)
            tasks.append(_PageTask(
                page_lines=page_lines,
                page_w=page_w, page_h=page_h, scale=scale,
                space_adv=space_adv, line_h=line_h,
                page_num=page_num,
                out_path=str(out_dir / f"page_{page_num + 1:04d}.png"),
            ))

    if not tasks:
        return stats

    n_workers = min(len(tasks), os.cpu_count() or 1)
    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_worker_init,
        initargs=(aks_path, h.size_px, h.weight, h.bpp),
    ) as pool:
        for words, clusters, oov, lines in pool.map(_render_page_worker, tasks):
            stats["total_words"]    += words
            stats["total_clusters"] += clusters
            stats["total_oov"]      += oov
            stats["total_lines"]    += lines
            stats["pages_written"]  += 1

    return stats


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render Kannada book text from a .aks file to paginated PNGs"
    )
    parser.add_argument("aks",      help="Path to .aks file")
    parser.add_argument("text",     help="Path to plain-text book file (UTF-8)")
    parser.add_argument("--out-dir",    default="/tmp/aks_book",
                        help="Output directory for page PNGs (default: /tmp/aks_book)")
    parser.add_argument("--page-width",  type=int, default=480,
                        help="Page width in pixels at 1× scale (default: 480)")
    parser.add_argument("--page-height", type=int, default=640,
                        help="Page height in pixels at 1× scale (default: 640)")
    parser.add_argument("--pages",  type=int, default=10,
                        help="Max pages to render (default: 10)")
    parser.add_argument("--scale",  type=int, default=2,
                        help="Pixel scale factor (default: 2 for readability)")
    parser.add_argument("--skip-lines", type=int, default=0,
                        help="Skip this many lines from the start of the file (default: 0)")
    parser.add_argument("--size",   type=int, default=None,
                        help="Font size in px to select from the .aks file (default: first entry)")
    parser.add_argument("--weight", type=int, default=0,
                        help="Font weight: 0=Regular, 1=Bold (default: 0)")
    parser.add_argument("--bpp",    type=int, default=None, choices=[1, 2],
                        help="Bits per pixel to select (default: any)")
    args = parser.parse_args()

    reader = AksReader(args.aks, size_px=args.size, weight=args.weight, bpp=args.bpp)
    h = reader.size
    print(f"Font: {args.aks}")
    print(f"  size={h.size_px}px  weight={'Bold' if h.weight else 'Regular'}  "
          f"glyph_height={h.glyph_height}px  bpp={h.bpp}  "
          f"clusters={reader.header.cluster_count:,}")

    text_path = Path(args.text)
    all_lines = text_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if args.skip_lines:
        all_lines = all_lines[args.skip_lines:]

    # Group into paragraphs: blank line = paragraph break.
    paragraphs: list[str] = []
    buf: list[str] = []
    for line in all_lines:
        stripped = line.strip()
        if stripped:
            buf.append(stripped)
        else:
            if buf:
                paragraphs.append(" ".join(buf))
                buf = []
    if buf:
        paragraphs.append(" ".join(buf))

    print(f"Text: {text_path.name}: {len(all_lines):,} lines → {len(paragraphs):,} paragraphs")
    print(f"Rendering up to {args.pages} page(s) at {args.page_width}×{args.page_height}px "
          f"(scale {args.scale}×, {os.cpu_count()} CPU(s)) …")

    stats = render_pages(
        reader,
        args.aks,
        paragraphs,
        page_w    = args.page_width,
        page_h    = args.page_height,
        max_pages = args.pages,
        out_dir   = Path(args.out_dir),
        scale     = args.scale,
    )

    print(f"\nDone: {stats['pages_written']} page(s) → {args.out_dir}/")
    print(f"  Lines rendered : {stats['total_lines']:,}")
    print(f"  Words rendered : {stats['total_words']:,}")
    print(f"  Clusters blitted: {stats['total_clusters']:,}")
    if stats["total_clusters"]:
        oov_pct = 100 * stats["total_oov"] / max(stats["total_clusters"], 1)
        print(f"  OOV glyphs     : {stats['total_oov']:,}  ({oov_pct:.2f}%)")
    else:
        print("  OOV glyphs     : 0")


if __name__ == "__main__":
    main()
