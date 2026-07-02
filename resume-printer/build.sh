#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Use the project's uv-managed Python (has weasyprint + pdfplumber)
WEASY_PYTHON="$SCRIPT_DIR/.venv/bin/python"
OUTPUT_ROOT="${1:-$SCRIPT_DIR/_output}"

mkdir -p "$OUTPUT_ROOT"

# Per-call globals set by build_dir, read by build_one/schedule/post_build
DOC_TYPE=
TEMPLATE=
CSS=
FILTER=
OUT_SUBDIR=
DEPS=()

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

  local pandoc_args=(
    "$input"
    --template="$TEMPLATE"
    --css="$CSS"
    -o "$OUT_SUBDIR/$out_name.html"
  )
  [ -n "$FILTER" ] && pandoc_args+=(--lua-filter="$FILTER")
  pandoc "${pandoc_args[@]}"

  $WEASY_PYTHON "$SCRIPT_DIR/fit.py" "$OUT_SUBDIR/$out_name.html" "$OUT_SUBDIR/$out_name.pdf"
}

# Run all post-build checks. Page count is checked for both types (warn-only).
# Resumes additionally get an ATS smoke test and h2-separator verification.
post_build() {
  local pdf="$1" md="$2"
  $WEASY_PYTHON "$SCRIPT_DIR/verify_pages.py" "$pdf"
  case "$DOC_TYPE" in
    resume)
      smoke_test "$pdf" "$md"
      $WEASY_PYTHON "$SCRIPT_DIR/verify_lines.py" "$pdf" "$md"
      ;;
    letter)
      ;;
  esac
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

# Wrapper: optionally build, then run checks, then print output atomically under lock
build_worker() {
  local md="$1"
  local out_name="$2"
  local needs_build="$3"
  local log="$TMPDIR_BUILD/$DOC_TYPE-$out_name.log"
  local rc=0
  {
    if [ "$needs_build" = 1 ]; then
      echo "  $DOC_TYPE/$out_name"
      build_one "$md" "$out_name"
    else
      echo "  $DOC_TYPE/$out_name (cached)"
    fi
    post_build "$OUT_SUBDIR/$out_name.pdf" "$md"
  } >"$log" 2>&1 || rc=$?
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
  local needs_build=1
  total=$((total + 1))
  if is_up_to_date "$OUT_SUBDIR/$out_name.pdf" "$@"; then
    needs_build=0
    skipped=$((skipped + 1))
  else
    started=$((started + 1))
  fi
  build_worker "$input_md" "$out_name" "$needs_build" &
  pids+=("$!")
}

# Build a directory of source .md files into _output/<doc_type>s/.
# Args: doc_type input_dir template css filter [extra_deps...]
# Pass empty string for filter to skip --lua-filter.
build_dir() {
  DOC_TYPE="$1"
  local input_dir="$2"
  TEMPLATE="$3"
  CSS="$4"
  FILTER="$5"
  shift 5
  DEPS=("$SCRIPT_DIR/fit.py" "$TEMPLATE" "$CSS" "$@")
  [ -n "$FILTER" ] && DEPS+=("$FILTER")
  OUT_SUBDIR="$OUTPUT_ROOT/${DOC_TYPE}s"
  mkdir -p "$OUT_SUBDIR"

  for md in "$input_dir"/*.md; do
    [ -f "$md" ] || continue
    local basename
    basename="$(basename "$md" .md)"
    local variants_file="$input_dir/$basename.variants.toml"

    if [ -f "$variants_file" ]; then
      while IFS= read -r vname; do
        schedule "$vname" "$OUT_SUBDIR/$vname.md" "$md" "$variants_file"
      done < <($WEASY_PYTHON "$SCRIPT_DIR/render_variants.py" "$md" "$variants_file" "$OUT_SUBDIR")
    else
      schedule "$basename" "$md" "$md"
    fi
  done
}

build_dir resume "$SCRIPT_DIR/resumes" \
  "$SCRIPT_DIR/template.html" \
  "$SCRIPT_DIR/style.css" \
  "$SCRIPT_DIR/filter.lua" \
  "$SCRIPT_DIR/render_variants.py"

build_dir letter "$SCRIPT_DIR/letters" \
  "$SCRIPT_DIR/template-letter.html" \
  "$SCRIPT_DIR/letter.css" \
  ""

if [ "$total" -eq 0 ]; then
  echo "No .md files found in $SCRIPT_DIR/resumes or $SCRIPT_DIR/letters" >&2
  exit 1
fi

# Wait for all background jobs
failed=0
for pid in ${pids[@]+"${pids[@]}"}; do
  wait "$pid" || failed=$((failed + 1))
done

built=$((started - failed))
echo "Built $built, skipped $skipped → $OUTPUT_ROOT/"
[ "$failed" -eq 0 ]
