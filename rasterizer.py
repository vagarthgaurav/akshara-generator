"""
Akshara rasterizer stage.

Two modes:
  - Per-glyph mode (v3): rasterize each unique glyph_id individually.
    Use rasterize_glyph() or rasterize_glyph_set().
  - Cluster composite mode (legacy): composite all glyphs of a cluster into
    one bitmap.  Use rasterize_all() — still used by the desktop renderer.

For 1bpp: FT_LOAD_TARGET_MONO gives hinted monochrome bitmaps directly.
For 2bpp: anti-aliased grey rendering, quantized to 4 levels.

Usage:
    python -m rasterizer --font NotoSansKannada-Regular.ttf --script kannada --size 24 --bpp 1
    python -m rasterizer --font NotoSansKannada-Regular.ttf --script kannada --size 24 --bpp 1 --count
"""

from __future__ import annotations

import argparse
import importlib
import math
from dataclasses import dataclass
from pathlib import Path

import freetype

from cluster_enum import Cluster, ScriptConfig, from_module
from shaper import ShapedCluster, shape_all

# FreeType load flags.
_FT_LOAD_RENDER = freetype.FT_LOAD_RENDER
_FT_LOAD_MONO   = freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_MONO

# FreeType pixel mode constants.
_FT_PIXEL_MODE_MONO = freetype.FT_PIXEL_MODES["FT_PIXEL_MODE_MONO"]
_FT_PIXEL_MODE_GRAY = freetype.FT_PIXEL_MODES["FT_PIXEL_MODE_GRAY"]


@dataclass
class RasterizedGlyph:
    """A single glyph rasterized at a specific size.  Content-only (not padded)."""
    bitmap: bytes       # packed 1bpp or 2bpp, rows byte-aligned, MSB-first
    width: int          # bitmap width in pixels (0 for non-printing glyphs)
    height: int         # bitmap height in pixels (content rows only)
    bearing_x: int      # ft.bitmap_left  (signed: pen-to-bitmap-left)
    top_from_base: int  # ft.bitmap_top   (signed: rows above baseline)


@dataclass
class RasterizedCluster:
    """A composite bitmap for one cluster (legacy, cluster-composite mode)."""
    bitmap: bytes
    width: int
    height: int
    bearing_x: int
    bearing_y: int  # rows from content top to baseline (positive = baseline below top)
    advance: int    # total horizontal advance in pixels


class Rasterizer:
    """FreeType rasterizer for one font face at a fixed pixel size and bpp."""

    def __init__(self, font_path: str | Path, size: int, bpp: int) -> None:
        if bpp not in (1, 2):
            raise ValueError(f"bpp must be 1 or 2, got {bpp}")
        self._face = freetype.Face(str(font_path))
        self._face.set_pixel_sizes(0, size)
        self._bpp = bpp
        self._load_flags = _FT_LOAD_MONO if bpp == 1 else _FT_LOAD_RENDER

    def _render_glyph_raw(
        self, glyph_id: int,
    ) -> tuple[list[list[int]], int, int]:
        """
        Render one glyph to an 8-bit 2D list (rows × cols, values 0–255).
        Returns (pixels, bitmap_left, bitmap_top).
        Returns empty pixels list if the glyph has no ink (e.g. space).
        """
        self._face.load_glyph(glyph_id, self._load_flags)
        ft = self._face.glyph
        bm = ft.bitmap
        rows, cols = bm.rows, bm.width

        if rows == 0 or cols == 0:
            return [], ft.bitmap_left, ft.bitmap_top

        buf = bytes(bm.buffer)
        pixels: list[list[int]] = [[0] * cols for _ in range(rows)]
        pitch = abs(bm.pitch)

        if bm.pixel_mode == _FT_PIXEL_MODE_MONO:
            for row in range(rows):
                base = row * pitch
                for col in range(cols):
                    if buf[base + col // 8] & (0x80 >> (col % 8)):
                        pixels[row][col] = 255
        elif bm.pixel_mode == _FT_PIXEL_MODE_GRAY:
            for row in range(rows):
                base = row * pitch
                for col in range(cols):
                    pixels[row][col] = buf[base + col]

        return pixels, ft.bitmap_left, ft.bitmap_top

    def rasterize_single(self, glyph_id: int) -> RasterizedGlyph:
        """
        Rasterize one glyph independently.  Returns content-only bitmap + metrics.
        Glyphs with no ink (e.g. space) get width=0, height=0, empty bitmap.
        """
        pixels, bx, bt = self._render_glyph_raw(glyph_id)

        if not pixels:
            return RasterizedGlyph(
                bitmap=b"",
                width=0,
                height=0,
                bearing_x=bx,
                top_from_base=bt,
            )

        h = len(pixels)
        w = len(pixels[0])
        bitmap = _pack_canvas(pixels, w, h, self._bpp)
        return RasterizedGlyph(
            bitmap=bitmap,
            width=w,
            height=h,
            bearing_x=bx,
            top_from_base=bt,
        )

    def rasterize(
        self, cluster: Cluster, shaped: ShapedCluster,
    ) -> RasterizedCluster | None:
        """
        Composite all glyphs in a shaped cluster into one packed bitmap.
        Used by the legacy cluster-composite path (desktop renderer, v2 packer).
        Returns None if all glyphs are invisible.
        """
        if not shaped:
            return None

        rendered: list[tuple[list[list[int]], int, int]] = [
            self._render_glyph_raw(g.glyph_id) for g in shaped
        ]

        pen_x = 0
        min_x: float = float("inf")
        min_y: float = float("inf")
        max_x: float = float("-inf")
        max_y: float = float("-inf")

        for (pixels, bx, by), glyph in zip(rendered, shaped):
            if not pixels:
                pen_x += glyph.x_advance
                continue
            rows, cols = len(pixels), len(pixels[0])
            gx = pen_x + glyph.x_offset + bx
            gy = -(glyph.y_offset + by)
            min_x = min(min_x, gx)
            min_y = min(min_y, gy)
            max_x = max(max_x, gx + cols)
            max_y = max(max_y, gy + rows)
            pen_x += glyph.x_advance

        if min_x == float("inf"):
            return None

        canvas_w = int(max_x - min_x)
        canvas_h = int(max_y - min_y)
        if canvas_w <= 0 or canvas_h <= 0:
            return None

        canvas = [[0] * canvas_w for _ in range(canvas_h)]
        pen_x = 0
        for (pixels, bx, by), glyph in zip(rendered, shaped):
            if not pixels:
                pen_x += glyph.x_advance
                continue
            gx = int(pen_x + glyph.x_offset + bx - min_x)
            gy = int(-(glyph.y_offset + by) - min_y)
            for ri, row in enumerate(pixels):
                cy = gy + ri
                if cy < 0 or cy >= canvas_h:
                    continue
                for ci, val in enumerate(row):
                    cx = gx + ci
                    if cx < 0 or cx >= canvas_w:
                        continue
                    if val > canvas[cy][cx]:
                        canvas[cy][cx] = val
            pen_x += glyph.x_advance

        total_advance = sum(g.x_advance for g in shaped)
        bitmap = _pack_canvas(canvas, canvas_w, canvas_h, self._bpp)
        bearing_y = int(-min_y)
        return RasterizedCluster(
            bitmap=bitmap,
            width=canvas_w,
            height=canvas_h,
            bearing_x=int(min_x),
            bearing_y=bearing_y,
            advance=total_advance,
        )


def _pack_canvas(canvas: list[list[int]], w: int, h: int, bpp: int) -> bytes:
    """Pack an 8-bit greyscale canvas to 1bpp or 2bpp, rows byte-aligned, MSB-first."""
    result = bytearray()
    if bpp == 1:
        pitch = math.ceil(w / 8)
        for row in canvas:
            row_bytes = bytearray(pitch)
            for col, val in enumerate(row):
                if val >= 128:
                    row_bytes[col // 8] |= 0x80 >> (col % 8)
            result.extend(row_bytes)
    else:  # bpp == 2: 4 pixels per byte, MSB-first; 0=white 3=black
        pitch = math.ceil(w / 4)
        for row in canvas:
            row_bytes = bytearray(pitch)
            for col, val in enumerate(row):
                level = val >> 6
                shift = 6 - 2 * (col % 4)
                row_bytes[col // 4] |= level << shift
            result.extend(row_bytes)
    return bytes(result)


def rasterize_glyph_set(
    font_path: str | Path,
    size: int,
    bpp: int,
    glyph_ids: list[int],
) -> dict[int, RasterizedGlyph]:
    """
    Rasterize a set of unique glyph IDs at a given size and bpp.
    Returns a dict mapping glyph_id → RasterizedGlyph.
    Used by the v3 packer to build the per-size glyph store.
    """
    rasterizer = Rasterizer(font_path, size, bpp)
    return {gid: rasterizer.rasterize_single(gid) for gid in glyph_ids}


def rasterize_all(
    font_path: str | Path,
    cfg: ScriptConfig,
    size: int,
    bpp: int,
    shaped: list[tuple[Cluster, ShapedCluster]] | None = None,
) -> list[tuple[Cluster, RasterizedCluster]]:
    """
    Rasterize every shaped cluster into composite bitmaps (legacy/v2 path).
    Clusters producing no visible bitmap are silently dropped.
    """
    if shaped is None:
        shaped = shape_all(font_path, cfg, size)

    rasterizer = Rasterizer(font_path, size, bpp)
    results: list[tuple[Cluster, RasterizedCluster]] = []
    for cluster, shaped_cluster in shaped:
        rast = rasterizer.rasterize(cluster, shaped_cluster)
        if rast is not None:
            results.append((cluster, rast))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Rasterize Akshara clusters via FreeType")
    parser.add_argument("--font", required=True, help="Path to TTF/OTF font file")
    parser.add_argument(
        "--script", required=True,
        choices=["kannada", "tamil", "devanagari", "malayalam", "telugu", "bengali", "gujarati"],
    )
    parser.add_argument("--size", type=int, default=24, help="Pixel size")
    parser.add_argument("--bpp", type=int, default=1, choices=[1, 2], help="Bits per pixel")
    parser.add_argument(
        "--count", action="store_true",
        help="Print rasterized cluster count only and exit",
    )
    args = parser.parse_args()

    mod = importlib.import_module(f"scripts.{args.script}")
    cfg = from_module(mod)
    results = rasterize_all(args.font, cfg, args.size, args.bpp)

    if args.count:
        print(f"{len(results)} rasterized clusters")
        return

    total_bytes = sum(len(r.bitmap) for _, r in results)
    print(f"{len(results)} clusters, {total_bytes} bitmap bytes total")
    print(f"avg bitmap: {total_bytes / len(results):.1f} bytes" if results else "")

    for cluster, rast in results[:20]:
        cps = " ".join(f"U+{cp:04X}" for cp in cluster)
        print(
            f"{cps}  {rast.width}×{rast.height}px  "
            f"bx={rast.bearing_x}  adv={rast.advance}  "
            f"{len(rast.bitmap)}B"
        )
    if len(results) > 20:
        print(f"  … {len(results) - 20} more")


if __name__ == "__main__":
    main()
