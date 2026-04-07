#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Use the same Python that weasyprint is installed under
WEASY_PYTHON="$(head -1 "$(which weasyprint)" | sed 's/^#!//')"
INPUT_DIR="${1:-$SCRIPT_DIR/resumes}"
OUTPUT_DIR="${2:-$SCRIPT_DIR/_output}"

mkdir -p "$OUTPUT_DIR"

build_one() {
  local input="$1"
  local basename
  basename="$(basename "$input" .md)"

  echo "  $basename"

  pandoc "$input" \
    --lua-filter="$SCRIPT_DIR/filter.lua" \
    --template="$SCRIPT_DIR/template.html" \
    --css="$SCRIPT_DIR/style.css" \
    -o "$OUTPUT_DIR/$basename.html"

  $WEASY_PYTHON "$SCRIPT_DIR/fit.py" "$OUTPUT_DIR/$basename.html" "$OUTPUT_DIR/$basename.pdf"

  pandoc "$input" \
    --lua-filter="$SCRIPT_DIR/filter.lua" \
    --reference-doc="$SCRIPT_DIR/reference.docx" \
    -o "$OUTPUT_DIR/$basename.docx"
}

count=0
for md in "$INPUT_DIR"/*.md; do
  [ -f "$md" ] || continue
  build_one "$md"
  count=$((count + 1))
done

if [ "$count" -eq 0 ]; then
  echo "No .md files found in $INPUT_DIR" >&2
  exit 1
fi

echo "Built $count resume(s) → $OUTPUT_DIR/"
