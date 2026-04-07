"""Find the largest base font size that fits a resume on a target page count."""

import os
import sys

from weasyprint import HTML


def render(html_path, font_size_pt):
    """Render HTML with overridden body font-size, return WeasyPrint document."""
    with open(html_path) as f:
        html = f.read()
    override = f"<style>body{{font-size:{font_size_pt:.4f}pt}}</style>"
    html = html.replace("</head>", override + "</head>")
    return HTML(string=html, base_url=os.path.dirname(os.path.abspath(html_path))).render()


def find_max_font_size(html_path, target_pages=1, lo=6.0, hi=16.0, precision=0.01):
    """Binary search for the largest body font-size that fits in target_pages."""
    while hi - lo > precision:
        mid = (lo + hi) / 2
        pages = len(render(html_path, mid).pages)
        if pages <= target_pages:
            lo = mid
        else:
            hi = mid
    return lo


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Fit resume to page count by scaling font size")
    parser.add_argument("html", help="Input HTML file")
    parser.add_argument("pdf", help="Output PDF file")
    parser.add_argument("--pages", type=int, default=1, help="Target page count (default: 1)")
    parser.add_argument("--min-pt", type=float, default=6.0, help="Minimum font size to try")
    parser.add_argument("--max-pt", type=float, default=16.0, help="Maximum font size to try")
    args = parser.parse_args()

    optimal = find_max_font_size(args.html, args.pages, args.min_pt, args.max_pt)
    doc = render(args.html, optimal)
    doc.write_pdf(args.pdf)

    print(f"{optimal:.2f}pt", file=sys.stderr)


if __name__ == "__main__":
    main()
