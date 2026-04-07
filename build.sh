#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INPUT="${1:-$SCRIPT_DIR/resume.md}"
BASENAME="$(basename "$INPUT" .md)"
OUTPUT_DIR="${2:-$SCRIPT_DIR/_output}"

mkdir -p "$OUTPUT_DIR"

echo "Building from: $INPUT"

# Generate HTML (intermediate for PDF)
pandoc "$INPUT" \
  --lua-filter="$SCRIPT_DIR/filter.lua" \
  --template="$SCRIPT_DIR/template.html" \
  --css="$SCRIPT_DIR/style.css" \
  -o "$OUTPUT_DIR/$BASENAME.html"

# Generate PDF via WeasyPrint
weasyprint "$OUTPUT_DIR/$BASENAME.html" "$OUTPUT_DIR/$BASENAME.pdf"

# Generate DOCX using sample as reference for styles
pandoc "$INPUT" \
  --lua-filter="$SCRIPT_DIR/filter.lua" \
  --reference-doc="$SCRIPT_DIR/reference.docx" \
  -o "$OUTPUT_DIR/$BASENAME.docx"

echo "Done:"
echo "  PDF:  $OUTPUT_DIR/$BASENAME.pdf"
echo "  DOCX: $OUTPUT_DIR/$BASENAME.docx"
echo "  HTML: $OUTPUT_DIR/$BASENAME.html"
