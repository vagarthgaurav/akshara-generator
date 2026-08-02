# akshara-generator recipes — the host-side (Python) .aks build pipeline
# Run from this repo's root: just <recipe>
# Requires: just, uv

# Sibling checkouts this repo reads/writes across.
akshara_repo := "../Akshara"
arduino_repo := "../Akshara-arduino"
fonts_dir    := akshara_repo / "fonts"

# script is required — always pass it before the recipe name:
#   just script=tamil pack
# font defaults to fonts/original/NotoSans<Script>-Regular.ttf; override if needed.
script      := ""
font        := if script == "kannada"    { fonts_dir / "original/NotoSansKannada-Regular.ttf" } \
          else if script == "tamil"      { fonts_dir / "original/NotoSansTamil-Regular.ttf" } \
          else if script == "devanagari" { fonts_dir / "original/NotoSansDevanagari-Regular.ttf" } \
          else if script == "malayalam"  { fonts_dir / "original/NotoSansMalayalam-Regular.ttf" } \
          else if script == "telugu"     { fonts_dir / "original/NotoSansTelugu-Regular.ttf" } \
          else if script == "bengali"    { fonts_dir / "original/NotoSansBengali-Regular.ttf" } \
          else if script == "gujarati"   { fonts_dir / "original/NotoSansGujarati-Regular.ttf" } \
          else                           { "" }
bpp         := "1"
font_bold   := ""        # path to Bold weight font; enables Bold section in output
# px selects which pre-generated size to use for render/aks2h/render-book (default 22)
px          := "22"
aks         := fonts_dir / "generated" / script / ("noto_" + script + "_regular_" + px + "px.aks")
text        := ""
size        := ""        # font size in px for render-book (e.g. just size=22 render-book)
weight      := "0"       # font weight for render-book: 0=Regular 1=Bold
render_bpp  := ""        # bpp for render-book: 1 or 2 (default: any matching size+weight)

# Show available recipes
default:
    @just --list

# ── Pipeline ──────────────────────────────────────────────────────────────────

# Generate clusters, shape, rasterize, and pack .aks files — one per size.
# Output goes to <akshara_repo>/fonts/generated/<script>/noto_<script>_regular_<N>px.aks
# Usage: just script=tamil pack
#        just script=kannada font_bold=../Akshara/fonts/original/NotoSansKannada-Bold.ttf pack
pack:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p {{fonts_dir}}/generated/{{script}}
    for sz in 16 18 20 22 24; do
        uv run python packer.py \
            --font {{font}} \
            {{if font_bold != "" { "--font-bold " + font_bold } else { "" }}} \
            --script {{script}} \
            --sizes $sz \
            --bpp {{bpp}} \
            --output {{fonts_dir}}/generated/{{script}}/noto_{{script}}_regular_${sz}px.aks
    done

# Generate .aks files for all supported scripts (all sizes)
pack-all:
    #!/usr/bin/env bash
    set -euo pipefail
    for s in kannada tamil devanagari malayalam telugu bengali gujarati; do
        just script=$s pack
    done

# Render a string to PNG using a .aks file (validates the packed output)
# Usage: just script=tamil render
render out="out.png":
    uv run python test/render_png.py \
        {{aks}} {{out}} \
        --words test/test-words/{{script}}.txt

# Pack then immediately render — useful for a quick visual check after rule changes
# Usage: just script=tamil build-and-render
build-and-render out="out.png": pack
    just script={{script}} render out="{{out}}"

# Convert a single .aks file to a C header for baking into firmware flash.
# Defaults to writing into the sibling Akshara-arduino checkout; pass out= to override.
# Usage: just script=tamil aks2h
#        just script=kannada aks2h array=NOTO_KANNADA out=/tmp/font.h
aks2h array="AKSHARA_FONT" out="":
    #!/usr/bin/env bash
    set -euo pipefail
    dest="{{out}}"
    if [ -z "$dest" ]; then
        dest="{{arduino_repo}}/src/fonts/{{script}}/noto_{{script}}_regular_{{px}}px.h"
    fi
    mkdir -p "$(dirname "$dest")"
    uv run python aks2h.py {{aks}} {{array}} > "$dest"
    echo "  wrote $dest"

# Generate .h headers for all scripts x sizes into the sibling Akshara-arduino repo.
gen-headers:
    #!/usr/bin/env bash
    set -euo pipefail
    out={{arduino_repo}}/src/fonts
    mkdir -p "$out"
    for s in kannada tamil devanagari malayalam telugu bengali gujarati; do
        mkdir -p "${out}/${s}"
        for sz in 16 18 20 22 24; do
            aks={{fonts_dir}}/generated/${s}/noto_${s}_regular_${sz}px.aks
            array=$(echo "NOTO_${s}_REGULAR_${sz}PX" | tr '[:lower:]' '[:upper:]')
            header=${out}/${s}/noto_${s}_regular_${sz}px.h
            uv run python aks2h.py ${aks} ${array} > ${header}
            echo "  wrote ${header}"
        done
    done

# Render a plain-text book file as paginated PNGs (flowing text with word wrap)
# Usage: just script=kannada text=/path/to/book.txt render-book
#        just script=kannada text=/path/to/book.txt size=22 pages=50 out-dir=/tmp/pages render-book
#        just script=kannada text=/path/to/book.txt size=22 weight=1 render-book  # Bold
#        just script=kannada text=/path/to/book.txt size=22 render_bpp=2 render-book  # 2bpp
render-book out-dir="/tmp/aks_book" pages="5":
    uv run python test/render_book.py \
        {{aks}} {{text}} \
        --out-dir {{out-dir}} \
        --pages {{pages}} \
        {{ if size != "" { "--size " + size } else { "" } }} \
        --weight {{weight}} \
        {{ if render_bpp != "" { "--bpp " + render_bpp } else { "" } }}

# ── Testing ───────────────────────────────────────────────────────────────────

# Run all akshara-generator tests
test:
    uv run pytest test/ -v

# Run only cluster enumeration tests
test-clusters:
    uv run pytest test/test_clusters.py -v

# Count clusters that would be generated (dry run, no rasterization)
# Usage: just script=tamil count-clusters
count-clusters:
    uv run python cluster_enum.py --script {{script}} --count

# ── Utilities ─────────────────────────────────────────────────────────────────

# Install Python dependencies
install:
    uv sync

# Print the resolved variable values (debug recipe substitution)
vars:
    @echo "font:   {{font}}"
    @echo "script: {{script}}"
    @echo "bpp:    {{bpp}}"
    @echo "aks:    {{aks}}"
