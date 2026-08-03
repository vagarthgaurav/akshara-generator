"""
Akshara v3 packer.

Writes a .aks v3 binary from shaped cluster data and rasterized glyph bitmaps.

File layout:
  [ Header v3          ]  28 bytes
  [ Rule Table         ]  32 bytes
  [ Cluster Key Table  ]  cluster_count × 16 bytes
  [ Composition Table  ]  variable (per-cluster: 2-byte header + N × 8 bytes)
  [ Size Directory     ]  size_count × 24 bytes
  [ Per-size sections  ]  glyph metrics + glyph bitmap offsets + bitmap store

Key optimisations vs v2:
  1. Glyph store: each unique glyph bitmap stored once; clusters reference glyph indices.
  2. Multi-size: key table and composition table are shared; only bitmaps differ per size.
  3. uint16_t codepoints: halves key entry size (32 B → 16 B per entry).
  4. Bold sharing: same glyph_idx mapping used for Regular and Bold; only bitmaps differ.

Usage (CLI):
    # Single size, regular only:
    python -m packer --font NotoSansKannada-Regular.ttf --script kannada
                     --size 22 --bpp 1 --output out.aks

    # Multi-size:
    python -m packer --font NotoSansKannada-Regular.ttf --script kannada
                     --sizes 16,22,24 --bpp 1 --output out.aks

    # Multi-size + Bold:
    python -m packer --font-regular NotoSansKannada-Regular.ttf
                     --font-bold    NotoSansKannada-Bold.ttf
                     --script kannada --sizes 16,22,24 --bpp 1 --output out.aks
"""

from __future__ import annotations

import argparse
import importlib
import struct
from dataclasses import dataclass
from pathlib import Path

import freetype

from cluster_enum import Cluster, ScriptConfig, from_module
from rasterizer import RasterizedGlyph, rasterize_glyph_set
from shaper import ShapedClusterDU, shape_all_du

# ── Struct formats (all little-endian, packed) ────────────────────────────────

# Header v3: magic(4) version(1) script_id(1) size_count(1) _reserved(1)
#            cluster_count(4) rule_offset(4) lookup_offset(4) comp_offset(4)
#            sizes_offset(4)
_HDR_FMT  = "<IBBBBIIIII"
_HDR_SIZE = struct.calcsize(_HDR_FMT)   # 28 bytes

_AKS_MAGIC   = 0x414B5348   # "AKSH"
_AKS_VERSION = 3

# Rule table: 7 × uint32 + uint8 + 3 padding bytes
_RULE_FMT  = "<IIIIIIIBxxx"
_RULE_SIZE = struct.calcsize(_RULE_FMT)  # 32 bytes

# Cluster key entry: uint16[6] + uint32
_KEY_FMT  = "<6HI"
_KEY_SIZE = struct.calcsize(_KEY_FMT)   # 16 bytes

# Composition block header: uint8 glyph_count + uint8 pad
_COMP_HDR_FMT  = "<BB"
_COMP_HDR_SIZE = struct.calcsize(_COMP_HDR_FMT)  # 2 bytes

# Composition entry per glyph: uint16 glyph_idx + int16 hb_x_off + int16 hb_y_off
#                              + uint16 hb_advance
_COMP_ENTRY_FMT  = "<HhhH"
_COMP_ENTRY_SIZE = struct.calcsize(_COMP_ENTRY_FMT)  # 8 bytes

# Size directory entry: BBBBBx HH xx III
_SIZE_FMT  = "<BBBBBxHHxxIII"
_SIZE_SIZE = struct.calcsize(_SIZE_FMT)  # 24 bytes

# Per-glyph metrics: uint8 width + uint8 height + int8 bearing_x + int8 top_from_base
_GLYPH_METRICS_FMT  = "<BBbb"
_GLYPH_METRICS_SIZE = struct.calcsize(_GLYPH_METRICS_FMT)  # 4 bytes

assert _HDR_SIZE           == 28, f"header size {_HDR_SIZE}"
assert _RULE_SIZE          == 32, f"rule size {_RULE_SIZE}"
assert _KEY_SIZE           == 16, f"key size {_KEY_SIZE}"
assert _COMP_HDR_SIZE      ==  2, f"comp hdr size {_COMP_HDR_SIZE}"
assert _COMP_ENTRY_SIZE    ==  8, f"comp entry size {_COMP_ENTRY_SIZE}"
assert _SIZE_SIZE          == 24, f"size entry size {_SIZE_SIZE}"
assert _GLYPH_METRICS_SIZE ==  4, f"glyph metrics size {_GLYPH_METRICS_SIZE}"


@dataclass(frozen=True)
class FontBox:
    """Global glyph box from FreeType face metrics at a given pixel size."""
    glyph_height: int  # ascender + |descender| in pixels
    baseline: int      # rows from box top to baseline (= ascender)


def get_font_box(font_path: str | Path, size: int) -> FontBox:
    face = freetype.Face(str(font_path))
    face.set_pixel_sizes(0, size)
    m = face.size
    ascender  = m.ascender >> 6
    descender = -(m.descender >> 6)
    return FontBox(glyph_height=ascender + descender, baseline=ascender)


def _cluster_sort_key(cluster: Cluster) -> tuple[int, ...]:
    """Lexicographic uint16[6] sort key (zero-pads to 6 elements)."""
    padded = list(cluster) + [0] * (6 - len(cluster))
    return tuple(padded[:6])


def build_glyph_index(
    shaped_pairs: list[tuple[Cluster, ShapedClusterDU]],
) -> dict[int, int]:
    """
    Deduplicate glyph IDs across all clusters.
    Returns glyph_id → glyph_idx mapping (stable insertion-order index).
    """
    seen: dict[int, int] = {}
    for _, glyphs in shaped_pairs:
        for g in glyphs:
            if g.glyph_id not in seen:
                seen[g.glyph_id] = len(seen)
    return seen


def build_composition_table(
    sorted_pairs: list[tuple[Cluster, ShapedClusterDU]],
    glyph_index: dict[int, int],
) -> tuple[list[bytes], list[int]]:
    """
    Build the composition table and return (comp_blocks, comp_offsets).

    comp_blocks : list of raw bytes, one per cluster (in the same order as
                  sorted_pairs), to be concatenated into the composition table.
    comp_offsets: byte offset of each cluster's block from start of comp table.
    """
    comp_blocks: list[bytes] = []
    comp_offsets: list[int] = []
    running_offset = 0

    for _, glyphs in sorted_pairs:
        comp_offsets.append(running_offset)

        block = bytearray()
        block += struct.pack(_COMP_HDR_FMT, len(glyphs), 0)

        for g in glyphs:
            glyph_idx = glyph_index[g.glyph_id]
            # Clamp design-unit offsets to int16 range.
            hb_x = max(-32768, min(32767, g.x_offset))
            hb_y = max(-32768, min(32767, g.y_offset))
            # Clamp advance to uint16 range (should never overflow for real fonts).
            hb_adv = max(0, min(65535, g.x_advance))
            block += struct.pack(_COMP_ENTRY_FMT, glyph_idx, hb_x, hb_y, hb_adv)

        comp_blocks.append(bytes(block))
        running_offset += len(block)

    return comp_blocks, comp_offsets


def build_size_section(
    font_path: str | Path,
    size: int,
    bpp: int,
    glyph_ids_ordered: list[int],  # ordered by glyph_idx
) -> tuple[bytes, bytes, bytes, FontBox]:
    """
    Rasterize all glyphs and build the three per-size binary sections:
      (metrics_blob, offsets_blob, bitmap_blob), font_box

    metrics_blob : aks_glyph_metrics_t array (4 bytes × glyph_count)
    offsets_blob : uint32_t array (4 bytes × glyph_count), offsets into bitmap_blob
    bitmap_blob  : packed raw glyph bitmaps, content-only
    """
    box        = get_font_box(font_path, size)
    rasterized = rasterize_glyph_set(font_path, size, bpp, glyph_ids_ordered)

    metrics_data = bytearray()
    offsets_data = bytearray()
    bitmap_data  = bytearray()

    for gid in glyph_ids_ordered:
        rg: RasterizedGlyph = rasterized[gid]

        # Clamp metrics to int8/uint8 range.
        bx  = max(-128, min(127, rg.bearing_x))
        tfb = max(-128, min(127, rg.top_from_base))
        w   = min(255, rg.width)
        h   = min(255, rg.height)

        metrics_data += struct.pack(_GLYPH_METRICS_FMT, w, h, bx, tfb)

        bmap_off = len(bitmap_data)
        offsets_data += struct.pack("<I", bmap_off)
        bitmap_data  += rg.bitmap

    return bytes(metrics_data), bytes(offsets_data), bytes(bitmap_data), box


def pack(
    font_regular: str | Path,
    cfg: ScriptConfig,
    sizes: list[int],
    bpp: int,
    output: str | Path,
    font_bold: str | Path | None = None,
) -> int:
    """
    Write a .aks v3 binary.  Returns the number of clusters written.

    font_regular : path to the Regular weight font.
    cfg          : script config (from cluster_enum.from_module).
    sizes        : list of pixel sizes to include (e.g. [16, 22, 24]).
    bpp          : bits per pixel (1 or 2).
    font_bold    : path to the Bold weight font; omit for Regular-only files.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    # Shape all clusters once (size-independent, design units).
    shaped_pairs, upem = shape_all_du(font_regular, cfg)

    # Sort by codepoint sequence for binary search.
    sorted_pairs = sorted(shaped_pairs, key=lambda pair: _cluster_sort_key(pair[0]))

    # Build the glyph deduplication index.
    glyph_index = build_glyph_index(sorted_pairs)  # glyph_id → glyph_idx
    glyph_ids_ordered = sorted(glyph_index, key=lambda gid: glyph_index[gid])
    glyph_count = len(glyph_ids_ordered)

    # For Bold: verify same glyph set (shaping must be weight-independent).
    if font_bold is not None:
        bold_pairs, _ = shape_all_du(font_bold, cfg)
        bold_glyph_ids = {g.glyph_id for _, gs in bold_pairs for g in gs}
        regular_glyph_ids = set(glyph_index.keys())
        extra = bold_glyph_ids - regular_glyph_ids
        missing = regular_glyph_ids - bold_glyph_ids
        if extra or missing:
            raise ValueError(
                f"Bold and Regular have different glyph sets.\n"
                f"  Bold-only glyph IDs: {sorted(extra)[:10]}\n"
                f"  Regular-only glyph IDs: {sorted(missing)[:10]}"
            )

    # Build the composition table.
    comp_blocks, comp_offsets = build_composition_table(sorted_pairs, glyph_index)

    # Build the key table.
    cluster_count = len(sorted_pairs)
    key_entries: list[bytes] = []
    for i, (cluster, _) in enumerate(sorted_pairs):
        cp = list(cluster) + [0] * (6 - len(cluster))
        key_entries.append(struct.pack(_KEY_FMT,
                                       cp[0], cp[1], cp[2], cp[3], cp[4], cp[5],
                                       comp_offsets[i]))

    # Build the rule table.
    rule_table = struct.pack(
        _RULE_FMT,
        cfg.consonant_range[0], cfg.consonant_range[1],
        cfg.virama,
        cfg.vowel_sign_range[0], cfg.vowel_sign_range[1],
        cfg.modifier_range[0], cfg.modifier_range[1],
        cfg.max_conjunct_depth,
    )

    # Build the per-size sections.
    variants: list[tuple[int, int]] = []  # (size_px, weight)
    for sz in sizes:
        variants.append((sz, 0))  # Regular
    if font_bold is not None:
        for sz in sizes:
            variants.append((sz, 1))  # Bold

    size_sections: list[tuple[bytes, bytes, bytes, FontBox]] = []
    for sz, weight in variants:
        fp = font_regular if weight == 0 else font_bold
        metrics, offsets_blob, bitmaps, box = build_size_section(
            fp, sz, bpp, glyph_ids_ordered,
        )
        size_sections.append((metrics, offsets_blob, bitmaps, box))

    # Compute section offsets.
    rule_offset   = _HDR_SIZE
    lookup_offset = rule_offset + _RULE_SIZE
    comp_offset   = lookup_offset + cluster_count * _KEY_SIZE
    comp_table_size = sum(len(b) for b in comp_blocks)
    sizes_offset  = comp_offset + comp_table_size
    size_count    = len(variants)

    # Compute per-size section offsets (come after the size directory).
    size_dir_size = size_count * _SIZE_SIZE
    current_off = sizes_offset + size_dir_size

    size_entry_blobs: list[bytes] = []
    for i, ((sz, weight), (metrics, offsets_blob, bitmaps, box)) in enumerate(
        zip(variants, size_sections)
    ):
        metrics_offset  = current_off
        offsets_offset  = metrics_offset + len(metrics)
        bitmaps_offset  = offsets_offset + len(offsets_blob)
        current_off     = bitmaps_offset + len(bitmaps)

        size_entry_blobs.append(struct.pack(
            _SIZE_FMT,
            sz,            # size_px
            weight,        # weight
            bpp,           # bpp
            box.glyph_height,
            box.baseline,
            upem,          # upem (uint16_t)
            glyph_count,   # glyph_count (uint16_t)
            metrics_offset,
            offsets_offset,
            bitmaps_offset,
        ))

    # Pack the header.
    header = struct.pack(
        _HDR_FMT,
        _AKS_MAGIC,
        _AKS_VERSION,
        cfg.script_id,
        size_count,
        0,              # _reserved
        cluster_count,
        rule_offset,
        lookup_offset,
        comp_offset,
        sizes_offset,
    )

    # Write the file.
    with output.open("wb") as f:
        f.write(header)
        f.write(rule_table)
        for entry in key_entries:
            f.write(entry)
        for block in comp_blocks:
            f.write(block)
        for blob in size_entry_blobs:
            f.write(blob)
        for metrics, offsets_blob, bitmaps, _ in size_sections:
            f.write(metrics)
            f.write(offsets_blob)
            f.write(bitmaps)

    return cluster_count


def validate(path: str | Path) -> None:
    """
    Re-parse a .aks v3 file and assert structural invariants.
    Raises ValueError with a descriptive message on any inconsistency.
    """
    path = Path(path)
    data = path.read_bytes()
    file_size = len(data)

    if file_size < _HDR_SIZE:
        raise ValueError(f"file too small for header: {file_size} bytes")

    magic, version, _, size_count, _, cluster_count, \
        rule_offset, lookup_offset, comp_offset, sizes_offset = \
        struct.unpack_from(_HDR_FMT, data, 0)

    if magic != _AKS_MAGIC:
        raise ValueError(f"bad magic: 0x{magic:08X}")
    if version != _AKS_VERSION:
        raise ValueError(f"unsupported version: {version}")
    if size_count == 0:
        raise ValueError("size_count == 0")

    expected_rule   = _HDR_SIZE
    expected_lookup = expected_rule + _RULE_SIZE
    expected_comp   = expected_lookup + cluster_count * _KEY_SIZE

    if rule_offset != expected_rule:
        raise ValueError(f"rule_offset {rule_offset} != expected {expected_rule}")
    if lookup_offset != expected_lookup:
        raise ValueError(f"lookup_offset {lookup_offset} != expected {expected_lookup}")
    if comp_offset != expected_comp:
        raise ValueError(f"comp_offset {comp_offset} != expected {expected_comp}")

    # Validate first, middle, and last key entries.
    check_indices = {0, cluster_count // 2, cluster_count - 1} if cluster_count else set()
    for idx in sorted(check_indices):
        off = lookup_offset + idx * _KEY_SIZE
        cp0, cp1, cp2, cp3, cp4, cp5, coff = struct.unpack_from(_KEY_FMT, data, off)
        abs_comp = comp_offset + coff
        if abs_comp >= file_size:
            raise ValueError(f"key[{idx}]: comp_off {coff} points past end of file")
        glyph_count = data[abs_comp]
        if glyph_count == 0:
            # Zero-glyph cluster (e.g. some whitespace): comp_off still valid.
            pass

    # Validate each size entry.
    for i in range(size_count):
        se_off = sizes_offset + i * _SIZE_SIZE
        if se_off + _SIZE_SIZE > file_size:
            raise ValueError(f"size entry {i} truncated")
        sz, wt, bpp, gh, bl, upem, glyph_count, mo, oo, bo = \
            struct.unpack_from(_SIZE_FMT, data, se_off)
        if bpp not in (1, 2):
            raise ValueError(f"size entry {i}: invalid bpp {bpp}")
        if bl >= gh:
            raise ValueError(f"size entry {i}: baseline {bl} >= glyph_height {gh}")
        metrics_end = mo + glyph_count * _GLYPH_METRICS_SIZE
        offsets_end = oo + glyph_count * 4
        if metrics_end > file_size or offsets_end > file_size or bo > file_size:
            raise ValueError(f"size entry {i}: section offsets truncated")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pack Akshara v3 clusters into a .aks binary"
    )
    parser.add_argument(
        "--font", "--font-regular",
        dest="font_regular", required=True,
        help="Path to Regular weight TTF/OTF font file",
    )
    parser.add_argument(
        "--font-bold",
        dest="font_bold", default=None,
        help="Path to Bold weight TTF/OTF font (enables Bold section in output)",
    )
    parser.add_argument(
        "--script", required=True,
        choices=["kannada", "tamil", "devanagari", "malayalam", "telugu", "bengali", "gujarati"],
    )
    parser.add_argument(
        "--size", type=int, default=22,
        help="Single pixel size (shorthand for --sizes)",
    )
    parser.add_argument(
        "--sizes", type=str, default=None,
        help="Comma-separated pixel sizes to include (e.g. 16,22,24); overrides --size",
    )
    parser.add_argument("--bpp", type=int, default=1, choices=[1, 2], help="Bits per pixel")
    parser.add_argument("--output", required=True, help="Output .aks file path")
    args = parser.parse_args()

    sizes = [int(s.strip()) for s in args.sizes.split(",")] if args.sizes else [args.size]

    mod = importlib.import_module(f"scripts.{args.script}")
    cfg = from_module(mod)

    print("Shaping clusters…")
    # Shape once just to report count; full shaping done inside pack().
    from shaper import shape_all_du as _shape_du
    from cluster_enum import enumerate_clusters
    clusters = enumerate_clusters(cfg)
    print(f"  {len(clusters)} clusters enumerated")

    print(f"Packing v3 .aks for {args.script}, sizes={sizes}, bpp={args.bpp}…")
    if args.font_bold:
        print(f"  Regular: {args.font_regular}")
        print(f"  Bold:    {args.font_bold}")

    count = pack(
        font_regular=args.font_regular,
        cfg=cfg,
        sizes=sizes,
        bpp=args.bpp,
        output=args.output,
        font_bold=args.font_bold,
    )

    file_size = Path(args.output).stat().st_size
    size_label = ",".join(str(s) for s in sizes)
    print(f"  {count} clusters → {args.output} ({file_size:,} bytes, sizes={size_label})")

    print("Validating…")
    validate(args.output)
    print("  OK")


if __name__ == "__main__":
    main()
