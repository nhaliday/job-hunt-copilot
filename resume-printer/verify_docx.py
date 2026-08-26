"""Verify a generated resume DOCX structurally matches its source Markdown.

Counterpart of verify_lines.py for the DOCX output: checks that the name,
email, section headings, separator rules, and bullets all survived the
HTML -> DOCX transformation. Exits nonzero on mismatch (fails the build).
"""

import re
import sys
import zipfile

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def md_facts(md_path):
    text = open(md_path).read()
    fm = re.search(r"\A---\n(.*?)\n---", text, re.S)
    fm_text = fm.group(1) if fm else ""

    def field(name):
        m = re.search(rf"^{name}: *(.+)$", fm_text, re.M)
        return m.group(1).strip() if m else ""

    headings = re.findall(r"^## +(.+?)\s*$", text, re.M)
    bullets = len(re.findall(r"^- ", text, re.M))
    return field("name"), field("email"), headings, bullets


def docx_facts(docx_path):
    from xml.etree import ElementTree

    with zipfile.ZipFile(docx_path) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
        try:
            numbering = z.read("word/numbering.xml").decode()
        except KeyError:
            numbering = ""

    def para_text(p):
        return "".join(t.text or "" for t in p.iter(f"{W}t"))

    full_text = []
    headings = []
    rules = 0
    bullets = 0
    for p in root.iter(f"{W}p"):
        text = para_text(p)
        full_text.append(text)
        ppr = p.find(f"{W}pPr")
        if ppr is not None:
            if ppr.find(f"{W}pBdr") is not None:
                rules += 1
                if ppr.find(f"{W}pBdr/{W}bottom") is not None:
                    headings.append(text)
            if ppr.find(f"{W}numPr") is not None:
                bullets += 1
    # the DOCX marker is ◆ (U+25C6) — EB Garamond lacks the PDF's ♦ (U+2666)
    if not bullets:  # literal-marker fallback when numbering is unavailable
        bullets = sum(1 for t in full_text if t.startswith("◆"))
    marker_ok = "◆" in numbering or any(t.startswith("◆") for t in full_text)
    return " ".join(full_text), headings, rules, bullets, marker_ok


def verify(docx_path, md_path):
    name, email, md_headings, md_bullets = md_facts(md_path)
    text, headings, rules, bullets, marker_ok = docx_facts(docx_path)
    errors = []
    if name and name.lower() not in text.lower():
        errors.append(f"name {name!r} missing from document text")
    if email and email not in text:
        errors.append(f"email {email!r} missing from document text")
    if [h.lower() for h in headings] != [h.lower() for h in md_headings]:
        errors.append(f"section headings {headings} != markdown {md_headings}")
    if rules != len(md_headings) + 1:
        errors.append(f"{rules} rules for {len(md_headings)} sections + header")
    if bullets != md_bullets:
        errors.append(f"{bullets} bullets in docx vs {md_bullets} in markdown")
    if not marker_ok:
        errors.append("bullet marker ♦ not found")
    for e in errors:
        print(f"    FAIL: docx: {e}", file=sys.stderr)
    if not errors:
        print(f"    DOCX verified ({len(headings)} sections, {bullets} bullets)")
    return not errors


if __name__ == "__main__":
    sys.exit(0 if verify(sys.argv[1], sys.argv[2]) else 1)
