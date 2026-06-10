"""
Desktop PNG renderer for .aks v3 files.

Mimics the MCU pipeline in Python:
  UTF-8 string → segmenter (rule table from .aks) → binary search lookup
  → composition table → per-glyph blit → PIL image

Run:
    cd aks-generator
    uv run python test/render_png.py <path.aks> [output.png] --words test/test-words/tamil.txt
    uv run pytest test/render_png.py -v          # headless assertions only (Kannada)
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ── Struct formats (must match packer.py exactly) ────────────────────────────

# Header v3: magic(4) version(1) script_id(1) size_count(1) _reserved(1)
#            cluster_count(4) rule_offset(4) lookup_offset(4) comp_offset(4)
#            sizes_offset(4)
_HDR_FMT  = "<IBBBBIIIII"
# Rule table: 7×uint32 + uint8 + 3 padding bytes
_RULE_FMT = "<IIIIIIIBxxx"
# Cluster key entry v3: uint16[6] codepoints + uint32 comp_off
_KEY_FMT  = "<6HI"
# Composition block header: glyph_count(1) + pad(1)
_COMP_HDR_FMT  = "<BB"
# Composition entry: glyph_idx(2) hb_x_off(2,signed) hb_y_off(2,signed) hb_advance(2)
_COMP_ENTRY_FMT = "<HhhH"
# Size directory entry: size_px(1) weight(1) bpp(1) glyph_height(1) baseline(1)
#                       _reserved(1) upem(2) glyph_count(2) _reserved2(2)
#                       metrics_offset(4) offsets_offset(4) bitmaps_offset(4)
_SIZE_FMT  = "<BBBBBxHHxxIII"
# Per-glyph metrics: width(1) height(1) bearing_x(1,signed) top_from_base(1,signed)
_GLYPH_METRICS_FMT = "<BBbb"

_HDR_SIZE           = struct.calcsize(_HDR_FMT)           # 28
_RULE_SIZE          = struct.calcsize(_RULE_FMT)          # 32
_KEY_SIZE           = struct.calcsize(_KEY_FMT)           # 16
_COMP_HDR_SIZE      = struct.calcsize(_COMP_HDR_FMT)      # 2
_COMP_ENTRY_SIZE    = struct.calcsize(_COMP_ENTRY_FMT)    # 8
_SIZE_SIZE          = struct.calcsize(_SIZE_FMT)          # 24
_GLYPH_METRICS_SIZE = struct.calcsize(_GLYPH_METRICS_FMT) # 4

_AKS_MAGIC   = 0x414B5348  # "AKSH"
_AKS_VERSION = 3

# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AksHeader:
    version: int
    script_id: int
    size_count: int
    cluster_count: int
    rule_offset: int
    lookup_offset: int
    comp_offset: int
    sizes_offset: int


@dataclass(frozen=True)
class RuleTable:
    consonant_start: int
    consonant_end: int
    virama: int
    vowel_sign_start: int
    vowel_sign_end: int
    modifier_start: int
    modifier_end: int
    max_conjunct_depth: int


@dataclass(frozen=True)
class SizeEntry:
    size_px: int
    weight: int
    bpp: int
    glyph_height: int    # full box height in pixels (ascender + |descender|)
    baseline: int        # rows from box top to baseline (= ascender)
    upem: int
    glyph_count: int
    metrics_offset: int
    offsets_offset: int
    bitmaps_offset: int


@dataclass(frozen=True)
class KeyEntry:
    cp: tuple[int, ...]  # zero-padded to 6 elements
    comp_off: int        # byte offset into composition table


@dataclass(frozen=True)
class CompEntry:
    glyph_idx: int
    hb_x_off: int        # design units (positive = right)
    hb_y_off: int        # design units (positive = up)
    hb_advance: int      # design units


@dataclass(frozen=True)
class GlyphMetrics:
    width: int           # content bitmap width in pixels
    height: int          # content bitmap height in pixels
    bearing_x: int       # ft.bitmap_left (signed)
    top_from_base: int   # ft.bitmap_top (signed; positive = above baseline)


# ── .aks v3 reader ────────────────────────────────────────────────────────────

class AksReader:
    """
    Loads a .aks v3 file, mirroring the MCU akshara_init() and akshara_select_size().

    Key table is fully loaded into memory for binary search.
    Composition entries and glyph data are read on demand.
    """

    def __init__(self, path: str | Path,
                 size_px: int | None = None, weight: int = 0) -> None:
        self._data = Path(path).read_bytes()
        self._hdr, self._rules = self._parse_header()
        self._keys: list[KeyEntry] = self._load_key_table()
        self._size: SizeEntry = self._select_size(size_px, weight)

    def _parse_header(self) -> tuple[AksHeader, RuleTable]:
        d = self._data
        if len(d) < _HDR_SIZE:
            raise ValueError("file too small for header")

        magic, version, script_id, size_count, _res, cluster_count, \
            rule_offset, lookup_offset, comp_offset, sizes_offset = \
            struct.unpack_from(_HDR_FMT, d, 0)

        if magic != _AKS_MAGIC:
            raise ValueError(f"bad magic: 0x{magic:08X}")
        if version != _AKS_VERSION:
            raise ValueError(
                f"unsupported format version {version} (expected {_AKS_VERSION})"
            )

        cs, ce, virama, vs_s, vs_e, mod_s, mod_e, depth = \
            struct.unpack_from(_RULE_FMT, d, rule_offset)

        hdr = AksHeader(
            version=version, script_id=script_id, size_count=size_count,
            cluster_count=cluster_count, rule_offset=rule_offset,
            lookup_offset=lookup_offset, comp_offset=comp_offset,
            sizes_offset=sizes_offset,
        )
        rules = RuleTable(
            consonant_start=cs, consonant_end=ce, virama=virama,
            vowel_sign_start=vs_s, vowel_sign_end=vs_e,
            modifier_start=mod_s, modifier_end=mod_e,
            max_conjunct_depth=depth,
        )
        return hdr, rules

    def _load_size_entry_at(self, idx: int) -> SizeEntry:
        off = self._hdr.sizes_offset + idx * _SIZE_SIZE
        sz, wt, bpp, gh, bl, upem, gcount, mo, oo, bo = \
            struct.unpack_from(_SIZE_FMT, self._data, off)
        return SizeEntry(size_px=sz, weight=wt, bpp=bpp, glyph_height=gh,
                         baseline=bl, upem=upem, glyph_count=gcount,
                         metrics_offset=mo, offsets_offset=oo, bitmaps_offset=bo)

    def _select_size(self, size_px: int | None, weight: int) -> SizeEntry:
        if size_px is None:
            return self._load_size_entry_at(0)
        for i in range(self._hdr.size_count):
            e = self._load_size_entry_at(i)
            if e.size_px == size_px and e.weight == weight:
                return e
        return self._load_size_entry_at(0)

    def _load_key_table(self) -> list[KeyEntry]:
        d = self._data
        off = self._hdr.lookup_offset
        entries: list[KeyEntry] = []
        for _ in range(self._hdr.cluster_count):
            cp0, cp1, cp2, cp3, cp4, cp5, comp_off = \
                struct.unpack_from(_KEY_FMT, d, off)
            off += _KEY_SIZE
            entries.append(KeyEntry(
                cp=(cp0, cp1, cp2, cp3, cp4, cp5),
                comp_off=comp_off,
            ))
        return entries

    def lookup(self, cluster: tuple[int, ...]) -> KeyEntry | None:
        """Binary search for a cluster. Returns None on OOV miss."""
        key = tuple(list(cluster) + [0] * (6 - len(cluster)))[:6]
        lo, hi = 0, len(self._keys) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            e = self._keys[mid]
            if e.cp == key:
                return e
            if e.cp < key:
                lo = mid + 1
            else:
                hi = mid - 1
        return None

    def read_comp_entries(self, key_entry: KeyEntry) -> list[CompEntry]:
        """Read all glyph composition entries for a cluster."""
        d = self._data
        abs_off = self._hdr.comp_offset + key_entry.comp_off
        glyph_count, _ = struct.unpack_from(_COMP_HDR_FMT, d, abs_off)
        entries: list[CompEntry] = []
        off = abs_off + _COMP_HDR_SIZE
        for _ in range(glyph_count):
            gidx, hb_x, hb_y, hb_adv = struct.unpack_from(_COMP_ENTRY_FMT, d, off)
            entries.append(CompEntry(glyph_idx=gidx, hb_x_off=hb_x,
                                     hb_y_off=hb_y, hb_advance=hb_adv))
            off += _COMP_ENTRY_SIZE
        return entries

    def read_glyph_metrics(self, glyph_idx: int) -> GlyphMetrics:
        off = self._size.metrics_offset + glyph_idx * _GLYPH_METRICS_SIZE
        w, h, bx, tfb = struct.unpack_from(_GLYPH_METRICS_FMT, self._data, off)
        return GlyphMetrics(width=w, height=h, bearing_x=bx, top_from_base=tfb)

    def read_glyph_bitmap(self, glyph_idx: int, gm: GlyphMetrics) -> bytes | None:
        """Return raw content-only bitmap bytes, or None for non-printing glyphs."""
        sz = self._size
        if gm.width == 0 or gm.height == 0:
            return None
        off = sz.offsets_offset + glyph_idx * 4
        bmap_rel, = struct.unpack_from("<I", self._data, off)
        stride = math.ceil(gm.width / (8 / sz.bpp))
        nbytes = stride * gm.height
        abs_off = sz.bitmaps_offset + bmap_rel
        return self._data[abs_off: abs_off + nbytes]

    def glyph_pixels(self, glyph_idx: int, gm: GlyphMetrics) -> list[list[int]] | None:
        """Decode a glyph bitmap to an 8-bit greyscale 2D list (rows × cols).

        Returns None for non-printing glyphs.  0 = black ink, 255 = white background.
        """
        bmp = self.read_glyph_bitmap(glyph_idx, gm)
        if bmp is None:
            return None
        bpp = self._size.bpp
        stride = math.ceil(gm.width / (8 / bpp))
        pixels: list[list[int]] = []
        for row in range(gm.height):
            row_buf = bmp[row * stride: (row + 1) * stride]
            row_pixels: list[int] = []
            for col in range(gm.width):
                if bpp == 1:
                    bit = (row_buf[col // 8] >> (7 - col % 8)) & 1
                    row_pixels.append(0 if bit else 255)
                else:
                    byte = row_buf[col // 4]
                    level = (byte >> (6 - 2 * (col % 4))) & 0x03
                    row_pixels.append(255 - level * 85)
            pixels.append(row_pixels)
        return pixels

    @property
    def header(self) -> AksHeader:
        return self._hdr

    @property
    def rules(self) -> RuleTable:
        return self._rules

    @property
    def size(self) -> SizeEntry:
        return self._size


# ── Reference segmenter (mirrors MCU segmenter.c spec) ───────────────────────

def segment(text: str, rules: RuleTable) -> list[tuple[int, ...]]:
    """
    Segment a UTF-8 string into akshara clusters.

    Grammar (from spec):
        cluster = consonant (virama consonant)* vowel_sign? modifier?
                | any_other_codepoint   ← single-codepoint cluster

    Greedy left-to-right parse; virama is checked before the coarse
    vowel_sign range because the range includes virama.
    """
    cps = [ord(c) for c in text]
    clusters: list[tuple[int, ...]] = []
    i = 0

    def is_consonant(cp: int) -> bool:
        return rules.consonant_start <= cp <= rules.consonant_end

    def is_vowel_sign(cp: int) -> bool:
        return (rules.vowel_sign_start <= cp <= rules.vowel_sign_end
                and cp != rules.virama)

    def is_modifier(cp: int) -> bool:
        return rules.modifier_start <= cp <= rules.modifier_end

    while i < len(cps):
        cp = cps[i]

        if is_consonant(cp):
            current = [cp]
            i += 1

            depth = 0
            while (depth < rules.max_conjunct_depth
                   and i < len(cps) and cps[i] == rules.virama
                   and i + 1 < len(cps) and is_consonant(cps[i + 1])):
                current += [cps[i], cps[i + 1]]
                i += 2
                depth += 1

            if i < len(cps) and cps[i] == rules.virama:
                current.append(cps[i])
                i += 1
                clusters.append(tuple(current))
                continue

            if i < len(cps) and is_vowel_sign(cps[i]):
                current.append(cps[i])
                i += 1

            if i < len(cps) and is_modifier(cps[i]):
                current.append(cps[i])
                i += 1

            clusters.append(tuple(current))

        else:
            clusters.append((cp,))
            i += 1

    return clusters


# ── Renderer ──────────────────────────────────────────────────────────────────

_LINE_PAD  = 6
_LABEL_COL = 36


def render_string(
    reader: AksReader,
    text: str,
) -> tuple[Image.Image, int, int]:
    """
    Render one string to a PIL greyscale image.

    Mirrors the MCU blit.c render loop:
      - blit is called once per glyph (not per cluster)
      - positions use HarfBuzz design units scaled to pixels at render time
      - OOV fallback tries consonant+sign pairs before individual codepoints

    Returns (image, rendered_width_px, oov_count).
    """
    sz = reader.size
    rules = reader.rules
    clusters = segment(text, rules)

    def du(val: int) -> int:
        """Scale design units to pixels (mirrors MCU du_to_px)."""
        return round(val * sz.size_px / sz.upem) if sz.upem else 0

    # (blit_x, blit_y, glyph_idx, gm) — collected in one pass, drawn after
    glyph_draws: list[tuple[int, int, int, GlyphMetrics]] = []
    oov_count = 0
    pen_x = 0

    def emit_comp(key_entry: KeyEntry, start_x: int) -> int:
        """Emit all glyphs for one cluster; return pixel advance."""
        comp_entries = reader.read_comp_entries(key_entry)
        local_pen = 0
        for ce in comp_entries:
            gm = reader.read_glyph_metrics(ce.glyph_idx)
            # Mirror blit.c render_comp() position calculation:
            #   blit_x = x + pen_x + hb_x_px + bearing_x
            #   blit_y = y + baseline - top_from_base - hb_y_px
            blit_x = start_x + local_pen + du(ce.hb_x_off) + gm.bearing_x
            blit_y = sz.baseline - gm.top_from_base - du(ce.hb_y_off)
            if gm.width > 0 and gm.height > 0:
                glyph_draws.append((blit_x, blit_y, ce.glyph_idx, gm))
            local_pen += du(ce.hb_advance)
        return local_pen

    def emit_oov(cluster: tuple[int, ...], start_x: int) -> int:
        """OOV fallback: try consonant+sign pairs, then individual codepoints."""
        nonlocal oov_count
        x = start_x
        cps = list(cluster)
        i = 0
        while i < len(cps):
            cp = cps[i]
            is_cons = rules.consonant_start <= cp <= rules.consonant_end
            next_cp = cps[i + 1] if i + 1 < len(cps) else None
            next_is_sign = next_cp is not None and (
                (rules.vowel_sign_start <= next_cp <= rules.vowel_sign_end
                 and next_cp != rules.virama)
                or (rules.modifier_start <= next_cp <= rules.modifier_end)
            )
            if is_cons and next_is_sign:
                e = reader.lookup((cp, next_cp))
                if e is not None:
                    x += emit_comp(e, x)
                    i += 2
                    continue
            e = reader.lookup((cp,))
            if e is not None:
                x += emit_comp(e, x)
            else:
                oov_count += 1
                x += sz.glyph_height // 2
            i += 1
        return x - start_x

    for cluster in clusters:
        e = reader.lookup(cluster)
        if e is not None:
            pen_x += emit_comp(e, pen_x)
        else:
            pen_x += emit_oov(cluster, pen_x)

    # Build image and paste each glyph
    img_w = max(pen_x, 1)
    img_h = sz.glyph_height
    img = Image.new("L", (img_w, img_h), 255)

    for blit_x, blit_y, glyph_idx, gm in glyph_draws:
        px_grid = reader.glyph_pixels(glyph_idx, gm)
        if px_grid is None:
            continue
        glyph_img = Image.new("L", (gm.width, gm.height), 255)
        for row_idx, row in enumerate(px_grid):
            for col_idx, val in enumerate(row):
                glyph_img.putpixel((col_idx, row_idx), val)
        paste_x = max(0, blit_x)
        paste_y = max(0, blit_y)
        if paste_x < img_w and paste_y < img_h:
            img.paste(glyph_img, (paste_x, paste_y))

    return img, pen_x, oov_count


def load_words_file(path: Path) -> list[tuple[str, str]]:
    """Load (label, text) pairs from a tab-separated words file."""
    pairs: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            pairs.append((parts[0].strip(), parts[1].strip()))
        else:
            pairs.append((parts[0].strip(), parts[0].strip()))
    return pairs


def render_grid(
    reader: AksReader,
    strings: list[tuple[str, str]],
    output: Path,
    scale: int = 1,
) -> None:
    """Render a grid of strings, one per row: index | script text | label."""
    sz = reader.size
    row_h = sz.glyph_height + _LINE_PAD * 2

    max_render_w = 0
    rendered_rows: list[tuple[Image.Image, int, int, str]] = []
    for label, text in strings:
        img, w, oov = render_string(reader, text)
        max_render_w = max(max_render_w, w)
        rendered_rows.append((img, w, oov, label))

    label_font_size = max(9, sz.glyph_height // 3)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            label_font_size,
        )
    except OSError:
        font = ImageFont.load_default()

    render_col_w = max_render_w + _LINE_PAD * 2
    label_col_w  = 200
    total_w = _LABEL_COL + render_col_w + label_col_w
    total_h = row_h * len(strings) + _LINE_PAD

    canvas = Image.new("L", (total_w * scale, total_h * scale), 255)
    draw   = ImageDraw.Draw(canvas)

    for row_idx, (img, w, oov, label) in enumerate(rendered_rows):
        y = row_idx * row_h + _LINE_PAD

        if row_idx > 0:
            draw.line(
                [(0, row_idx * row_h * scale),
                 (total_w * scale, row_idx * row_h * scale)],
                fill=220,
            )

        draw.text(
            (2 * scale, (y + sz.glyph_height // 4) * scale),
            str(row_idx + 1),
            fill=140,
            font=font,
        )

        if scale == 1:
            canvas.paste(img, (_LABEL_COL, y))
        else:
            big = img.resize((img.width * scale, img.height * scale),
                             resample=Image.NEAREST)
            canvas.paste(big, (_LABEL_COL * scale, y * scale))

        oov_note = f" ({oov} OOV)" if oov else ""
        draw.text(
            ((_LABEL_COL + render_col_w + 4) * scale,
             (y + sz.glyph_height // 4) * scale),
            label + oov_note,
            fill=80,
            font=font,
        )

    canvas.save(output)
    print(f"Saved → {output}  ({len(strings)} strings, {total_w}×{total_h}px)")


# ── Test strings ──────────────────────────────────────────────────────────────

TEST_STRINGS: list[tuple[str, str]] = [
    ("Kannada (script name)",  "ಕನ್ನಡ"),
    ("India",                  "ಭಾರತ"),
    ("Hello/Namaskara",        "ನಮಸ್ಕಾರ"),
    ("Thank you",              "ಧನ್ಯವಾದ"),
    ("Bengaluru",              "ಬೆಂಗಳೂರು"),
    ("Mysore",                 "ಮೈಸೂರು"),
    ("Hubli",                  "ಹುಬ್ಬಳ್ಳಿ"),
    ("Ramayana",               "ರಾಮಾಯಣ"),
    ("Mahabharata",            "ಮಹಾಭಾರತ"),
    ("Sanskrit",               "ಸಂಸ್ಕೃತ"),
    ("Love",                   "ಪ್ರೀತಿ"),
    ("Science",                "ವಿಜ್ಞಾನ"),
    ("Technology",             "ತಂತ್ರಜ್ಞಾನ"),
    ("Akshara (letter)",       "ಅಕ್ಷರ"),
    ("Literature",             "ಸಾಹಿತ್ಯ"),
    ("Music",                  "ಸಂಗೀತ"),
    ("Water",                  "ನೀರು"),
    ("Sun",                    "ಸೂರ್ಯ"),
    ("Moon",                   "ಚಂದ್ರ"),
    ("Earth",                  "ಭೂಮಿ"),
    ("Sky",                    "ಆಕಾಶ"),
    ("Forest",                 "ಕಾಡು"),
    ("River",                  "ನದಿ"),
    ("Gold",                   "ಚಿನ್ನ"),
    ("Silver",                 "ಬೆಳ್ಳಿ"),
    ("Mixed: num + script",    "೧೨೩ ಕನ್ನಡ"),
    ("Mixed: num + script",    "123 ಕನ್ನಡ"),
    ("Punctuation",            "ಕನ್ನಡ, ಭಾರತ."),
]


# ── pytest assertions (run without PIL output) ────────────────────────────────

import pytest  # noqa: E402

_AKS_PATH = Path("/tmp/noto_kannada_regular_22.aks")


@pytest.fixture(scope="module")
def reader() -> AksReader:
    if not _AKS_PATH.exists():
        pytest.skip(f".aks file not found: {_AKS_PATH}")
    return AksReader(_AKS_PATH)


class TestSegmenter:
    def test_bare_consonant(self, reader: AksReader) -> None:
        clusters = segment("ಕ", reader.rules)
        assert clusters == [(0x0C95,)]

    def test_consonant_vowel_sign(self, reader: AksReader) -> None:
        clusters = segment("ಕಾ", reader.rules)
        assert clusters == [(0x0C95, 0x0CBE)]

    def test_conjunct(self, reader: AksReader) -> None:
        clusters = segment("ಕ್ತ", reader.rules)
        assert clusters == [(0x0C95, 0x0CCD, 0x0CA4)]

    def test_conjunct_with_vowel(self, reader: AksReader) -> None:
        clusters = segment("ಕ್ತಾ", reader.rules)
        assert clusters == [(0x0C95, 0x0CCD, 0x0CA4, 0x0CBE)]

    def test_modifier(self, reader: AksReader) -> None:
        clusters = segment("ಕಂ", reader.rules)
        assert clusters == [(0x0C95, 0x0C82)]

    def test_halant_form(self, reader: AksReader) -> None:
        clusters = segment("ಕ್", reader.rules)
        assert clusters == [(0x0C95, 0x0CCD)]

    def test_independent_vowel(self, reader: AksReader) -> None:
        clusters = segment("ಅ", reader.rules)
        assert clusters == [(0x0C85,)]

    def test_multi_cluster_word(self, reader: AksReader) -> None:
        clusters = segment("ಕನ್ನಡ", reader.rules)
        assert len(clusters) == 3
        assert clusters[0] == (0x0C95,)
        assert clusters[1] == (0x0CA8, 0x0CCD, 0x0CA8)
        assert clusters[2] == (0x0CA1,)

    def test_ascii_passthrough(self, reader: AksReader) -> None:
        clusters = segment("abc", reader.rules)
        assert clusters == [(0x61,), (0x62,), (0x63,)]


class TestLookup:
    def test_known_cluster_found(self, reader: AksReader) -> None:
        entry = reader.lookup((0x0C95,))  # ಕ bare consonant
        assert entry is not None
        assert entry.comp_off < 1024 * 1024  # sanity bound

    def test_oov_returns_none(self, reader: AksReader) -> None:
        entry = reader.lookup((0x0041,))  # Latin A — not in .aks
        assert entry is None

    def test_glyph_has_positive_dimensions(self, reader: AksReader) -> None:
        entry = reader.lookup((0x0C95,))
        assert entry is not None
        comps = reader.read_comp_entries(entry)
        assert len(comps) >= 1
        gm = reader.read_glyph_metrics(comps[0].glyph_idx)
        assert gm.width > 0
        assert gm.height > 0

    def test_glyph_bitmap_correct_size(self, reader: AksReader) -> None:
        entry = reader.lookup((0x0C95,))
        assert entry is not None
        comps = reader.read_comp_entries(entry)
        gm = reader.read_glyph_metrics(comps[0].glyph_idx)
        pixels = reader.glyph_pixels(comps[0].glyph_idx, gm)
        assert pixels is not None
        assert len(pixels) == gm.height
        assert all(len(row) == gm.width for row in pixels)

    def test_glyph_has_ink(self, reader: AksReader) -> None:
        entry = reader.lookup((0x0C95,))
        assert entry is not None
        comps = reader.read_comp_entries(entry)
        gm = reader.read_glyph_metrics(comps[0].glyph_idx)
        pixels = reader.glyph_pixels(comps[0].glyph_idx, gm)
        assert pixels is not None
        assert any(px < 128 for row in pixels for px in row)

    def test_cluster_advance_positive(self, reader: AksReader) -> None:
        entry = reader.lookup((0x0C95,))
        assert entry is not None
        comps = reader.read_comp_entries(entry)
        sz = reader.size
        total_du = sum(c.hb_advance for c in comps)
        adv_px = round(total_du * sz.size_px / sz.upem) if sz.upem else 0
        assert adv_px > 0


class TestRenderStrings:
    @pytest.mark.parametrize("label,text", TEST_STRINGS)
    def test_renders_without_error(self, reader: AksReader,
                                   label: str, text: str) -> None:
        img, width, oov = render_string(reader, text)
        assert img is not None
        assert width > 0, f"{label!r}: zero-width render"

    def test_word_width_increases_with_length(self, reader: AksReader) -> None:
        _, short_w, _ = render_string(reader, "ಕ")
        _, long_w, _  = render_string(reader, "ಕನ್ನಡ")
        assert long_w > short_w

    def test_known_strings_zero_oov(self, reader: AksReader) -> None:
        zero_oov = [
            "ಕನ್ನಡ",
            "ಭಾರತ",
            "ಬೆಂಗಳೂರು",
            "ನೀರು",
            "ಕಾಡು",
        ]
        for text in zero_oov:
            _, _, oov = render_string(reader, text)
            assert oov == 0, f"{text!r} had {oov} OOV clusters"

    def test_measure_consistent(self, reader: AksReader) -> None:
        """render_string width must be consistent across two calls."""
        _, w1, _ = render_string(reader, "ಕನ್ನಡ")
        _, w2, _ = render_string(reader, "ಕನ್ನಡ")
        assert w1 == w2


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render Indic script strings from a .aks v3 file to PNG"
    )
    parser.add_argument("aks", help="Path to .aks file")
    parser.add_argument(
        "output", nargs="?",
        default="/tmp/akshara_render.png",
        help="Output PNG path (default: /tmp/akshara_render.png)",
    )
    parser.add_argument(
        "--words", metavar="FILE",
        help="Tab-separated words file (label<TAB>text); defaults to built-in Kannada strings",
    )
    parser.add_argument(
        "--size", type=int, default=None, metavar="PX",
        help="Pixel size to render (selects from multi-size .aks; default: first entry)",
    )
    parser.add_argument(
        "--weight", type=int, default=0, choices=[0, 1],
        help="Font weight: 0=Regular (default), 1=Bold",
    )
    parser.add_argument(
        "--scale", type=int, default=2, metavar="N",
        help="Pixel scale factor for readability (default: 2)",
    )
    args = parser.parse_args()

    reader = AksReader(args.aks, size_px=args.size, weight=args.weight)
    h  = reader.header
    sz = reader.size
    print(f"Loaded: {args.aks}")
    print(f"  version={h.version}  script_id=0x{h.script_id:02X}  "
          f"clusters={h.cluster_count}  sizes={h.size_count}")
    print(f"  active: {sz.size_px}px  weight={sz.weight}  bpp={sz.bpp}  "
          f"glyph_height={sz.glyph_height}  baseline={sz.baseline}  "
          f"glyphs={sz.glyph_count}")

    strings = load_words_file(Path(args.words)) if args.words else TEST_STRINGS
    render_grid(reader, strings, Path(args.output), scale=args.scale)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] not in ("-v", "--co", "-p"):
        main()
    else:
        print("Usage: python test/render_png.py <path.aks> [output.png] [--size PX] [--scale N]")
        print("       cd aks-generator && uv run pytest test/render_png.py -v")
