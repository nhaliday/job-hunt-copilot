#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Use the same Python that weasyprint is installed under
WEASY_PYTHON="$(head -1 "$(which weasyprint)" | sed 's/^#!//')"
INPUT_DIR="${1:-$SCRIPT_DIR/resumes}"
OUTPUT_DIR="${2:-$SCRIPT_DIR/_output}"

mkdir -p "$OUTPUT_DIR"

# Shared dependencies: if any of these change, all PDFs must rebuild
DEPS=(
  "$SCRIPT_DIR/filter.lua"
  "$SCRIPT_DIR/template.html"
  "$SCRIPT_DIR/style.css"
  "$SCRIPT_DIR/fit.py"
)

# Return 0 (true) if pdf exists and is newer than the .md and all shared deps
is_up_to_date() {
  local pdf="$1" md="$2"
  [ -f "$pdf" ] || return 1
  for dep in "$md" "${DEPS[@]}"; do
    [ "$pdf" -nt "$dep" ] || return 1
  done
}

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

  smoke_test "$OUTPUT_DIR/$basename.pdf" "$input"
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
  local log="$TMPDIR_BUILD/$(basename "$md" .md).log"
  local rc=0
  build_one "$md" >"$log" 2>&1 || rc=$?
  lock
  cat "$log"
  unlock
  return "$rc"
}

pids=()
total=0
skipped=0
started=0

for md in "$INPUT_DIR"/*.md; do
  [ -f "$md" ] || continue
  total=$((total + 1))
  basename="$(basename "$md" .md)"

  if is_up_to_date "$OUTPUT_DIR/$basename.pdf" "$md"; then
    echo "  $basename (up to date)"
    skipped=$((skipped + 1))
  else
    build_worker "$md" &
    pids+=("$!")
    started=$((started + 1))
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
