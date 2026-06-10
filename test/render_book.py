"""
Book-style flowing text renderer for .aks files.

Reads a plain-text file, wraps words to a given page width, and renders
each page as a PNG.  Reports OOV (out-of-vocabulary) statistics so you can
see at a glance how well the font covers real running text.

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
from pathlib import Path

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

def _measure_word(reader: AksReader, word: str) -> tuple[int, list]:
    """
    Return (advance_px, draws) for a single whitespace-delimited word.

    draws = list of (rel_x, KeyEntry) ready to blit.  rel_x is relative
    to the start of the word (pen_x = 0 at word start).
    """
    rules = reader.rules
    h     = reader.header
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
                    pen_x += e.advance
                else:
                    pen_x += h.glyph_height // 2   # blank advance for truly unknown
        else:
            draws.append((pen_x, entry))
            pen_x += entry.advance

    return pen_x, draws


def _space_advance(reader: AksReader) -> int:
    """Pixel width of a space character (U+0020), or font-height/2 fallback."""
    e = reader.lookup((0x0020,))
    return e.advance if e is not None else reader.header.glyph_height // 2


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
    h = reader.header
    for rel_x, entry in draws:
        px_grid = reader.bitmap_pixels(entry)
        gw = entry.width
        gh = h.glyph_height
        glyph_img = Image.new("L", (gw, gh), _BG)
        for row_idx, row in enumerate(px_grid):
            for col_idx, val in enumerate(row):
                glyph_img.putpixel((col_idx, row_idx), val)
        dest_x = origin_x + rel_x + entry.bearing_x
        dest_y = origin_y
        canvas.paste(glyph_img, (max(0, dest_x), dest_y))


# ── Page rendering ────────────────────────────────────────────────────────────

def render_pages(
    reader: AksReader,
    paragraphs: list[str],
    page_w: int,
    page_h: int,
    max_pages: int,
    out_dir: Path,
    scale: int = 2,
) -> dict:
    """
    Render up to *max_pages* pages of flowing text.

    Returns a stats dict with total_lines, total_words, total_oov,
    total_clusters, pages_written.
    """
    h         = reader.header
    line_h    = h.glyph_height + _LINE_GAP
    space_adv = _space_advance(reader)
    body_w    = page_w - 2 * _MARGIN
    body_h    = page_h - 2 * _MARGIN

    stats = dict(total_lines=0, total_words=0, total_oov=0,
                 total_clusters=0, pages_written=0)

    # Pre-wrap all paragraphs into lines.
    all_lines: list[tuple[list, bool]] = []  # (line_words, is_para_start)
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
    line_idx  = 0
    page_num  = 0

    while line_idx < len(all_lines) and page_num < max_pages:
        page_img = Image.new("L", (page_w * scale, page_h * scale), _BG)
        pen_y    = _MARGIN

        while pen_y + line_h <= page_h - _MARGIN and line_idx < len(all_lines):
            line_words, is_para_start = all_lines[line_idx]

            # Extra gap before a new paragraph (except the very first line of page).
            if is_para_start and pen_y > _MARGIN:
                pen_y += _PARA_GAP
                if pen_y + line_h > page_h - _MARGIN:
                    break   # no room — start on next page

            pen_x = _MARGIN
            for wi, (word, adv, draws) in enumerate(line_words):
                if wi > 0:
                    pen_x += space_adv
                if scale == 1:
                    _blit_draws(page_img, reader, draws, pen_x, pen_y)
                else:
                    # Render at 1× then upscale for readability.
                    tmp = Image.new("L", (adv + h.glyph_height, h.glyph_height), _BG)
                    _blit_draws(tmp, reader, draws, 0, 0)
                    big = tmp.resize(
                        (tmp.width * scale, tmp.height * scale),
                        resample=Image.NEAREST,
                    )
                    page_img.paste(big, (pen_x * scale, pen_y * scale))
                pen_x += adv

                stats["total_words"] += 1
                stats["total_clusters"] += len(draws)

            # OOV count for the whole line text.
            line_text = " ".join(w for w, _, _ in line_words)
            stats["total_oov"] += _count_oov(reader, line_text)

            pen_y += line_h
            stats["total_lines"] += 1
            line_idx += 1

        # Draw a thin page border for visual clarity.
        draw = ImageDraw.Draw(page_img)
        draw.rectangle(
            [1, 1, page_w * scale - 2, page_h * scale - 2],
            outline=200,
        )
        # Page number footer.
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 10 * scale
            )
        except OSError:
            font = ImageFont.load_default()
        draw.text(
            (page_w * scale // 2 - 20, (page_h - _MARGIN // 2) * scale),
            f"— {page_num + 1} —",
            fill=160,
            font=font,
        )

        out_path = out_dir / f"page_{page_num + 1:04d}.png"
        page_img.save(out_path)
        page_num += 1
        stats["pages_written"] += 1

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
    args = parser.parse_args()

    reader = AksReader(args.aks)
    h = reader.header
    print(f"Font: {args.aks}")
    print(f"  glyph_height={h.glyph_height}px  bpp={h.bpp}  "
          f"clusters={h.cluster_count:,}")

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

    print(f"Text: {text_path.name} — {len(all_lines):,} lines → {len(paragraphs):,} paragraphs")
    print(f"Rendering up to {args.pages} page(s) at {args.page_width}×{args.page_height}px "
          f"(scale {args.scale}×) …")

    stats = render_pages(
        reader,
        paragraphs,
        page_w    = args.page_width,
        page_h    = args.page_height,
        max_pages = args.pages,
        out_dir   = Path(args.out_dir),
        scale     = args.scale,
    )

    print(f"\nDone — {stats['pages_written']} page(s) → {args.out_dir}/")
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
