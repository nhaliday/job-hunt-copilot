"""Render variants of a resume Markdown file via Jinja2.

Usage: render_variants.py <source.md> <variants.toml> <output_dir>

Reads variants.toml and, for each variant, renders source.md through Jinja2
with the variant's vars and writes <output_dir>/<variant_name>.md. Prints
variant names to stdout, one per line, in declaration order.
"""

import sys
import tomllib
from pathlib import Path

from jinja2 import Environment, StrictUndefined


def main() -> int:
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <source.md> <variants.toml> <output_dir>",
              file=sys.stderr)
        return 2

    src = Path(sys.argv[1])
    variants_path = Path(sys.argv[2])
    out_dir = Path(sys.argv[3])
    out_dir.mkdir(parents=True, exist_ok=True)

    config = tomllib.loads(variants_path.read_text())
    env = Environment(
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    template = env.from_string(src.read_text())

    for v in config["variants"]:
        name = v["name"]
        vars_ = v.get("vars", {})
        (out_dir / f"{name}.md").write_text(template.render(**vars_))
        print(name)

    return 0


if __name__ == "__main__":
    sys.exit(main())
