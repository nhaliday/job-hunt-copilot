#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Use the project's uv-managed Python (has weasyprint + pdfplumber)
WEASY_PYTHON="$SCRIPT_DIR/.venv/bin/python"
INPUT_DIR="${1:-$SCRIPT_DIR/resumes}"
OUTPUT_DIR="${2:-$SCRIPT_DIR/_output}"

mkdir -p "$OUTPUT_DIR"

# Shared dependencies: if any of these change, all PDFs must rebuild
DEPS=(
  "$SCRIPT_DIR/filter.lua"
  "$SCRIPT_DIR/template.html"
  "$SCRIPT_DIR/style.css"
  "$SCRIPT_DIR/fit.py"
  "$SCRIPT_DIR/verify_lines.py"
  "$SCRIPT_DIR/render_variants.py"
)

# Return 0 (true) if pdf exists and is newer than all given source paths and shared deps
is_up_to_date() {
  local pdf="$1"; shift
  [ -f "$pdf" ] || return 1
  for dep in "$@" "${DEPS[@]}"; do
    [ "$pdf" -nt "$dep" ] || return 1
  done
}

build_one() {
  local input="$1"
  local out_name="$2"

  echo "  $out_name"

  pandoc "$input" \
    --lua-filter="$SCRIPT_DIR/filter.lua" \
    --template="$SCRIPT_DIR/template.html" \
    --css="$SCRIPT_DIR/style.css" \
    -o "$OUTPUT_DIR/$out_name.html"

  $WEASY_PYTHON "$SCRIPT_DIR/fit.py" "$OUTPUT_DIR/$out_name.html" "$OUTPUT_DIR/$out_name.pdf"

  smoke_test "$OUTPUT_DIR/$out_name.pdf" "$input"
  $WEASY_PYTHON "$SCRIPT_DIR/verify_lines.py" "$OUTPUT_DIR/$out_name.pdf" "$input"
}

smoke_test() {
  local pdf="$1"
  local md="$2"
  local text warn=0

  text="$(pdftotext "$pdf" -)"

  # Section headers should be extractable
  for section in "SKILLS" "EMPLOYMENT HISTORY" "EDUCATION"; do
    if ! echo "$text" | grep -q "$section"; then
      echo "    WARN: section '$section' not found in text extraction" >&2
      warn=1
    fi
  done

  # Name from YAML frontmatter should appear
  local name
  name="$(sed -n 's/^name: *//p' "$md" | head -1)"
  if [ -n "$name" ] && ! echo "$text" | grep -qi "$name"; then
    echo "    WARN: name '$name' not found in text extraction" >&2
    warn=1
  fi

  # Contact info should appear
  local email
  email="$(sed -n 's/^email: *//p' "$md" | head -1)"
  if [ -n "$email" ] && ! echo "$text" | grep -q "$email"; then
    echo "    WARN: email '$email' not found in text extraction" >&2
    warn=1
  fi

  # Entry headers should extract with title and date on the same line (layout mode)
  local layout_text
  layout_text="$(pdftotext -layout "$pdf" -)"
  if ! echo "$layout_text" | grep -qE '^.{10,}(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [0-9]{4}'; then
    echo "    WARN: entry title and date not on same line in layout extraction" >&2
    warn=1
  fi

  # Bullet markers should be present
  if ! echo "$text" | grep -q '♦'; then
    echo "    WARN: bullet marker ♦ not found in text extraction" >&2
    warn=1
  fi

  # Bullet text should be inline (not just bare markers)
  if echo "$text" | grep -qE '^♦[[:space:]]*$'; then
    echo "    WARN: bullet markers detached from text (ATS may misparse)" >&2
    warn=1
  fi

  if [ "$warn" -eq 0 ]; then
    echo "    ATS smoke test passed"
  fi
}

TMPDIR_BUILD="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_BUILD"' EXIT
LOCK="$TMPDIR_BUILD/lock"

# Acquire/release a mutex for sequential stdout output
lock()   { while ! mkdir "$LOCK" 2>/dev/null; do sleep 0.05; done; }
unlock() { rmdir "$LOCK"; }

# Wrapper: build, then print output atomically under lock
build_worker() {
  local md="$1"
  local out_name="$2"
  local log="$TMPDIR_BUILD/$out_name.log"
  local rc=0
  build_one "$md" "$out_name" >"$log" 2>&1 || rc=$?
  lock
  cat "$log"
  unlock
  return "$rc"
}

pids=()
total=0
skipped=0
started=0

schedule() {
  # schedule <output_name> <input_md> <source_md_for_mtime> [extra_dep ...]
  local out_name="$1" input_md="$2"; shift 2
  total=$((total + 1))
  if is_up_to_date "$OUTPUT_DIR/$out_name.pdf" "$@"; then
    echo "  $out_name (up to date)"
    skipped=$((skipped + 1))
  else
    build_worker "$input_md" "$out_name" &
    pids+=("$!")
    started=$((started + 1))
  fi
}

for md in "$INPUT_DIR"/*.md; do
  [ -f "$md" ] || continue
  basename="$(basename "$md" .md)"
  variants_file="$INPUT_DIR/$basename.variants.toml"

  if [ -f "$variants_file" ]; then
    # Render all variants of this resume into TMPDIR_BUILD (synchronous, fast)
    while IFS= read -r vname; do
      schedule "$vname" "$TMPDIR_BUILD/$vname.md" "$md" "$variants_file"
    done < <($WEASY_PYTHON "$SCRIPT_DIR/render_variants.py" "$md" "$variants_file" "$TMPDIR_BUILD")
  else
    schedule "$basename" "$md" "$md"
  fi
done

if [ "$total" -eq 0 ]; then
  echo "No .md files found in $INPUT_DIR" >&2
  exit 1
fi

# Wait for all background jobs
failed=0
for pid in ${pids[@]+"${pids[@]}"}; do
  wait "$pid" || failed=$((failed + 1))
done

built=$((started - failed))
echo "Built $built, skipped $skipped → $OUTPUT_DIR/"
[ "$failed" -eq 0 ]
