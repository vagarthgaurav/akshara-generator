# akshara-generator

Host-side (Python) build tool that generates `.aks` font files: compact,
pre-shaped, pre-rasterized bitmap fonts for rendering Indic scripts on
memory-constrained e-reader firmware.

This repo is a companion tool for the main [Akshara](../Akshara) repo
(and the sibling [Akshara-arduino](../Akshara-arduino) firmware repo).
Akshara's MCU firmware can't afford to run a full shaping engine
(HarfBuzz) or rasterizer (FreeType) on-device, so this tool does that
work offline, on a desktop machine, instead. It enumerates every valid
orthographic syllable (akshara) for a script, shapes each one with
HarfBuzz, rasterizes the resulting glyphs with FreeType, and packs the
result into a single binary `.aks` file that the firmware can map and
read directly, doing only a cheap binary search and blit at runtime.

Supported scripts: Kannada, Tamil, Devanagari, Telugu, Malayalam, Bengali,
Gujarati.

The Justfile defaults to Noto Sans (`fonts/original/NotoSans<Script>-
Regular.ttf` in the Akshara repo) for convenience, but the pipeline
works with any TTF/OTF that has proper OpenType shaping rules for the
target script. Point `font=` (and optionally `font_bold=`) at whatever
font file you want to package instead.

## How it fits together

```text
cluster_enum.py -> shaper.py -> rasterizer.py -> packer.py -> <script>.aks
 (enumerate all     (HarfBuzz     (FreeType        (write v3
  valid clusters)    shaping)      rasterization)   binary format)
```

- **`cluster_enum.py`**: Per-script rules (in `scripts/<script>.py`)
  describe independent vowels, consonants, virama, vowel signs, modifiers,
  and max conjunct depth. This module enumerates every valid codepoint
  cluster (akshara) the script can produce.
- **`shaper.py`**: Feeds each cluster through HarfBuzz to get glyph IDs
  and positions, either in font design units (v3, size-independent) or
  scaled to pixels (legacy v2 path).
- **`rasterizer.py`**: Rasterizes each unique glyph with FreeType, either
  as hinted 1bpp monochrome bitmaps or anti-aliased 2bpp grayscale.
- **`packer.py`**: Packs cluster keys, glyph composition data, and glyph
  bitmaps for one or more sizes (and optionally a Bold weight) into a
  single `.aks` v3 binary. Glyph bitmaps are deduplicated and shared
  across sizes where possible.
- **`aks2h.py`**: Converts a packed `.aks` file into a C header
  (`static const uint8_t[]`) for baking directly into firmware flash, for
  targets that can't read the file from a filesystem.

`test/render_png.py` and `test/render_book.py` are a desktop-only
reimplementation of the firmware's read path (segment, lookup, composite,
blit), used to visually validate `.aks` files without needing real
hardware.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/) and
[`just`](https://github.com/casey/just).

```bash
just install
```

This repo expects to sit alongside its sibling checkouts:

```text
Akshara Project/
|-- akshara-generator/   (this repo)
|-- Akshara/             (fonts/original, fonts/generated)
`-- Akshara-arduino/     (src/fonts, firmware headers)
```

Override these paths in the `Justfile` (`akshara_repo`, `arduino_repo`)
if your layout differs.

## `just` commands

All recipes are run from this repo's root: `just <recipe>`. Most recipes
take a required `script=` variable (one of `kannada`, `tamil`,
`devanagari`, `telugu`, `malayalam`, `bengali`, `gujarati`); some take
further variables, passed the same way, e.g.
`just script=tamil size=22 render-book`.

### Pipeline

- **`pack`**: Generate `.aks` files for one script, one per size (16, 18,
  20, 22, 24 px). Output goes to
  `<akshara_repo>/fonts/generated/<script>/noto_<script>_regular_<N>px.aks`.

  ```bash
  just script=tamil pack
  just script=kannada \
      font_bold=../Akshara/fonts/original/NotoSansKannada-Bold.ttf pack
  ```

- **`pack-all`**: Runs `pack` for every supported script.

- **`render out="out.png"`**: Render a test string to a PNG using a
  packed `.aks` file, to sanity-check the output. Uses
  `test/test-words/<script>.txt` as the input word list.

  ```bash
  just script=tamil render
  ```

- **`build-and-render out="out.png"`**: Runs `pack` then `render` in one
  step, for a quick visual check after changing script rules.

  ```bash
  just script=tamil build-and-render
  ```

- **`aks2h array="AKSHARA_FONT" out=""`**: Convert one `.aks` file to a C
  header. Defaults to writing into the sibling `Akshara-arduino`
  checkout; pass `out=` to write elsewhere.

  ```bash
  just script=tamil aks2h
  just script=kannada aks2h array=NOTO_KANNADA out=/tmp/font.h
  ```

- **`gen-headers`**: Generate `.h` headers for every script and size
  into the sibling `Akshara-arduino` repo (`src/fonts/<script>/`).

  ```bash
  just gen-headers
  ```

- **`render-book out-dir="/tmp/aks_book" pages="5"`**: Render a
  plain-text file as paginated PNGs with word wrap, reporting
  out-of-vocabulary (OOV) stats so you can see how well a font covers
  real running text. Pages render in parallel.

  ```bash
  just script=kannada text=/path/to/book.txt render-book
  just script=kannada text=/path/to/book.txt size=22 pages=50 \
      out-dir=/tmp/pages render-book
  just script=kannada text=/path/to/book.txt size=22 weight=1 \
      render-book   # Bold
  just script=kannada text=/path/to/book.txt size=22 render_bpp=2 \
      render-book   # 2bpp
  ```

### Testing

- **`test`**: Run the full test suite (`uv run pytest test/ -v`).
- **`test-clusters`**: Run only cluster enumeration tests.
- **`count-clusters`**: Dry run: count how many clusters would be
  generated for a script, without shaping or rasterizing.

  ```bash
  just script=tamil count-clusters
  ```

### Utilities

- **`install`**: Install Python dependencies (`uv sync`).
- **`vars`**: Print the resolved values of `font`, `script`, `bpp`, and
  `aks` for the current variable settings, useful for debugging recipe
  substitution.
- **`default`**: Show all available recipes (`just --list`); this runs
  when you type `just` with no arguments.

## Key variables

Set these as `key=value` before the recipe name, e.g.
`just script=tamil bpp=2 pack`:

- **`script`** (required): one of the seven supported scripts.
- **`font`** (default: `fonts/original/NotoSans<Script>-Regular.ttf` in
  the Akshara repo): path to the source TTF/OTF. Any font with correct
  OpenType shaping rules for the script works, not just Noto Sans.
- **`font_bold`** (default: empty): path to a Bold weight font; enables
  a Bold section in `pack`.
- **`bpp`** (default: `1`): bits per pixel for `pack` (1 = monochrome,
  2 = grayscale).
- **`px`** (default: `22`): which pre-generated size to use for
  `render` / `aks2h` / `render-book`.
- **`text`** (default: empty): input text file for `render-book`.
- **`size`** (default: empty, falls back to `px`): font size in px for
  `render-book`.
- **`weight`** (default: `0`): `render-book` weight, `0` = Regular,
  `1` = Bold.
- **`render_bpp`** (default: empty, matches any): filter `render-book`
  to a specific bpp (`1` or `2`).

## The `.aks` v3 format

A single binary file laying out, in order: a 28-byte header, a 32-byte
rule table (used by the firmware's segmenter to split UTF-8 text into
clusters), a cluster key table (codepoint sequence to cluster ID), a
composition table (cluster ID to glyph IDs and shaping offsets), a size
directory, and one glyph-metrics/bitmap section per generated size.
Glyph bitmaps are stored once and shared across sizes and between
Regular/Bold where possible. See the struct formats at the top of
`packer.py` for the exact byte layout.
