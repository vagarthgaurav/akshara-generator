"""
Akshara shaper stage.

Feeds each cluster (codepoint sequence from cluster_enum.py) through HarfBuzz
and returns the glyph run for that cluster.

Two output modes:
  - Design-unit mode (v3): positions in font design units (size-independent).
    Use shape_all_du(), which returns GlyphInfoDU instances.
  - Pixel mode (legacy): positions already scaled to pixels at a given size.
    Use shape_all(), which returns GlyphInfo instances. Still used by the v2 path
    and the desktop renderer.

Usage:
    python -m shaper --font NotoSansKannada-Regular.ttf --script kannada --size 24
    python -m shaper --font NotoSansKannada-Regular.ttf --script kannada --size 24 --count
"""

from __future__ import annotations

import argparse
import importlib
from dataclasses import dataclass
from pathlib import Path

import uharfbuzz as hb

from cluster_enum import Cluster, ScriptConfig, enumerate_clusters, from_module

# Maps AKS_SCRIPT_* id → (4-letter OpenType script tag, BCP-47 language tag).
_SCRIPT_HB: dict[int, tuple[str, str]] = {
    0x01: ("Knda", "kn"),
    0x02: ("Taml", "ta"),
    0x03: ("Deva", "hi"),
    0x04: ("Telu", "te"),
    0x05: ("Mlym", "ml"),
    0x06: ("Beng", "bn"),
    0x07: ("Gujr", "gu"),
}


@dataclass(frozen=True)
class GlyphInfo:
    """Glyph position in pixel units (scaled to a specific size)."""
    glyph_id: int
    x_offset: int   # pixels, rounded
    y_offset: int   # pixels, rounded (positive = up, HarfBuzz convention)
    x_advance: int  # pixels, rounded


@dataclass(frozen=True)
class GlyphInfoDU:
    """Glyph position in font design units (size-independent)."""
    glyph_id: int
    x_offset: int   # design units (positive = right)
    y_offset: int   # design units (positive = up, HarfBuzz convention)
    x_advance: int  # design units


ShapedCluster    = list[GlyphInfo]
ShapedClusterDU  = list[GlyphInfoDU]


class Shaper:
    """
    HarfBuzz shaper for one font face.

    When size is provided, positions are scaled to pixels (legacy mode).
    Call shape_du() to get design-unit positions (v3 mode).

    HarfBuzz operates in design units; pixel conversion is: round(du * size / upem).
    The font scale is set to upem so raw position values come back as design units.
    """

    def __init__(self, font_path: str | Path, cfg: ScriptConfig,
                 size: int | None = None) -> None:
        script_tag, lang_tag = _SCRIPT_HB.get(cfg.script_id, ("Latn", "en"))

        blob = hb.Blob.from_file_path(str(font_path))
        face = hb.Face(blob)
        self._font = hb.Font(face)
        self._upem: int = face.upem
        self._px_scale: float = (size / self._upem) if size is not None else 1.0

        # Scale = upem so positions come back in design units.
        self._font.scale = (self._upem, self._upem)

        self._script_tag = script_tag
        self._lang_tag = lang_tag

    @property
    def upem(self) -> int:
        return self._upem

    def _shape_raw(self, cluster: Cluster) -> list[tuple[int, int, int, int]]:
        """
        Shape one cluster.  Returns list of (glyph_id, x_off_du, y_off_du, x_adv_du).
        All positions are in design units.  Returns [] if HarfBuzz yields no glyphs.
        """
        buf = hb.Buffer()
        buf.add_codepoints(list(cluster))
        buf.guess_segment_properties()
        buf.language = self._lang_tag

        hb.shape(self._font, buf)

        result: list[tuple[int, int, int, int]] = []
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
            result.append((
                info.codepoint,     # after shaping, .codepoint holds glyph_id
                pos.x_offset,
                pos.y_offset,
                pos.x_advance,
            ))
        return result

    def shape(self, cluster: Cluster) -> ShapedCluster:
        """Shape one cluster; return glyph run in pixel units (legacy mode)."""
        s = self._px_scale
        return [
            GlyphInfo(
                glyph_id=gid,
                x_offset=round(xo * s),
                y_offset=round(yo * s),
                x_advance=round(xa * s),
            )
            for gid, xo, yo, xa in self._shape_raw(cluster)
        ]

    def shape_du(self, cluster: Cluster) -> ShapedClusterDU:
        """Shape one cluster; return glyph run in design units (v3 mode)."""
        return [
            GlyphInfoDU(glyph_id=gid, x_offset=xo, y_offset=yo, x_advance=xa)
            for gid, xo, yo, xa in self._shape_raw(cluster)
        ]


def shape_all(
    font_path: str | Path,
    cfg: ScriptConfig,
    size: int,
    clusters: list[Cluster] | None = None,
) -> list[tuple[Cluster, ShapedCluster]]:
    """
    Shape every cluster for a script; positions in pixels.
    Clusters that produce no HarfBuzz output are silently dropped.
    """
    if clusters is None:
        clusters = enumerate_clusters(cfg)

    shaper = Shaper(font_path, cfg, size)
    results: list[tuple[Cluster, ShapedCluster]] = []
    for cluster in clusters:
        shaped = shaper.shape(cluster)
        if shaped:
            results.append((cluster, shaped))
    return results


def shape_all_du(
    font_path: str | Path,
    cfg: ScriptConfig,
    clusters: list[Cluster] | None = None,
) -> tuple[list[tuple[Cluster, ShapedClusterDU]], int]:
    """
    Shape every cluster; positions in design units (size-independent).
    Returns (shaped_pairs, upem).
    Clusters that produce no output are silently dropped.
    """
    if clusters is None:
        clusters = enumerate_clusters(cfg)

    shaper = Shaper(font_path, cfg)
    results: list[tuple[Cluster, ShapedClusterDU]] = []
    for cluster in clusters:
        shaped = shaper.shape_du(cluster)
        if shaped:
            results.append((cluster, shaped))
    return results, shaper.upem


def main() -> None:
    parser = argparse.ArgumentParser(description="Shape Akshara clusters via HarfBuzz")
    parser.add_argument("--font", required=True, help="Path to TTF/OTF font file")
    parser.add_argument(
        "--script", required=True,
        choices=["kannada", "tamil", "devanagari", "malayalam", "telugu", "bengali", "gujarati"],
    )
    parser.add_argument("--size", type=int, default=24, help="Pixel size")
    parser.add_argument(
        "--count", action="store_true",
        help="Print shaped cluster count only and exit",
    )
    args = parser.parse_args()

    mod = importlib.import_module(f"scripts.{args.script}")
    cfg = from_module(mod)
    results = shape_all(args.font, cfg, args.size)

    if args.count:
        print(f"{len(results)} shaped clusters")
        return

    for cluster, glyphs in results:
        cps = " ".join(f"U+{cp:04X}" for cp in cluster)
        glyph_str = " ".join(
            f"[gid={g.glyph_id} dx={g.x_offset} dy={g.y_offset} adv={g.x_advance}]"
            for g in glyphs
        )
        print(f"{cps}\t→\t{glyph_str}")


if __name__ == "__main__":
    main()
