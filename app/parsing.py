"""Intermediate PDF layout -> bibliography blocks -> structured citation fields.

This layer never queries a registry and never decides whether a paper exists.
"""
import re
from collections import Counter

REF_HEADING = re.compile(r"^(?:\d+[.\s]+)?(?:references|bibliography|works cited|참고\s*문헌)\s*[:.]?$", re.I)
SECTION = re.compile(r"^(?:(?i:appendix|appendices|supplementary)\b|[A-Z](?:\.\d+)*\s+[A-Z])")
YEAR = re.compile(r"\b((?:19|20)\d{2})([a-z]?)(?:\s*[)）])?\.\s*")
TRUNCATED = re.compile(r"\bet\s+al\.|\band\s+\d+\s+others?\b|…|\.\s*\.\s*\.", re.I)


def pdf_lines(reader) -> list[dict]:
    lines = []
    for page_index, page in enumerate(reader.pages):
        groups = {}
        width = float(page.mediabox.width)
        def visitor(text, cm, tm, font, size):
            if not text.strip():
                return
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            column = int(x > width / 2)
            key = (column, round(y, 1))
            if key not in groups:
                groups[key] = {"page": page_index + 1, "column": column, "x": x, "y": y, "size": size, "width": round(width), "parts": []}
            groups[key]["parts"].append(text)
            groups[key]["x"] = min(groups[key]["x"], x)
        page.extract_text(visitor_text=visitor)
        for row in sorted(groups.values(), key=lambda x: (x["column"], -x["y"])):
            row["text"] = re.sub(r"\s+", " ", "".join(row.pop("parts"))).strip()
            lines.append(row)
    return lines


def layout_references(lines: list[dict]) -> dict | None:
    headings = [i for i, line in enumerate(lines) if REF_HEADING.fullmatch(line["text"])]
    if not headings:
        return None
    start = headings[0]
    following = [line for line in lines[start + 1:] if not line["text"].isdigit()]
    if not following:
        return None
    body_size = Counter(round(line["size"], 1) for line in following[:80]).most_common(1)[0][0]
    region, end = [], len(lines)
    for index, line in enumerate(lines[start + 1:], start + 1):
        if SECTION.match(line["text"]) and line["size"] > body_size * 1.08:
            end = index
            break
        if not line["text"].isdigit() and not REF_HEADING.fullmatch(line["text"]):
            region.append(line)
    if not region:
        return None
    anchors = {}
    for line in region:
        key = (line["width"], line["column"])
        anchors[key] = min(anchors.get(key, line["x"]), line["x"])
    hanging = [line for line in region if 4 <= line["x"] - anchors[(line["width"], line["column"])] <= 25]
    starts = [line for line in region if abs(line["x"] - anchors[(line["width"], line["column"])]) <= 2]
    # Fall back when layout evidence does not show repeated hanging paragraphs.
    if len(starts) < 2 or len(hanging) < max(2, len(region) * .2):
        return None
    blocks = []
    for line in region:
        key = (line["width"], line["column"])
        if abs(line["x"] - anchors[key]) <= 2:
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    entries = []
    for block in blocks:
        raw = " ".join(row["text"] for row in block)
        number = re.match(r"^(?:\[(\d+)\]|(\d+)[.)])\s+", raw)
        label = (number.group(1) or number.group(2)) if number else str(len(entries) + 1)
        raw = raw[number.end():] if number else raw
        entries.append({"label": label, "raw": raw, "extraction_method": "pdf_hanging_indent",
                        "source_pages": sorted({row["page"] for row in block}),
                        "source_anchor": {k: block[0][k] for k in ("page", "column", "x", "y")}})
    numbered = all(re.match(r"^(?:\[\d+\]|\d+[.)])\s+", block[0]["text"]) for block in blocks)
    return {"entries": entries, "numbered": numbered,
            "body": "\n".join(row["text"] for row in lines[:start] + lines[end:]),
            "summary": {"method": "pdf_hanging_indent", "reference_pages": sorted({r["page"] for r in region}),
                        "stopped_at": lines[end]["text"] if end < len(lines) else None,
                        "count": len(entries)}}


def structured_reference(raw: str) -> dict:
    """Parse author/year/title only when a recognized header is present."""
    match = YEAR.search(raw)
    if not match or match.start() > 2200:
        return {"parse_status": "incomplete", "parse_warnings": ["저자·발행연도 경계를 구조화하지 못했습니다."]}
    authors_text = raw[:match.start()].strip().rstrip(". (（")
    # Require an author-like header, not 'Interspeech 2022, pages ...'.
    if not authors_text or re.search(r"\b(?:pages?|proceedings|conference|interspeech|vol(?:ume)?)\b", authors_text, re.I):
        return {"parse_status": "incomplete", "parse_warnings": ["항목 시작이 저자 정보인지 확인해 주세요."]}
    tail = raw[match.end():].strip()
    title = re.split(r"\.(?:\s+|(?=Preprint\b|arXiv\b|IEEE\b|In[A-Z]))", tail, maxsplit=1)[0].strip().strip('“”"')
    truncated = bool(TRUNCATED.search(authors_text))
    visible = TRUNCATED.sub("", authors_text).strip(" ,;")
    if ";" in visible:
        names = [re.sub(r"^and\s+", "", n.strip()) for n in visible.split(";") if n.strip()]
        style = "surname_initials"
        first_surname = names[0].split(",", 1)[0]
    elif re.match(r"^[^,]+,\s*(?:[A-Z]\s*\.\s*)+(?=,|$|and\s)", visible):
        # Leave comma-separated initials to the conservative legacy matcher.
        names, style = [], "initials_unsegmented"
        first_surname = visible.split(",", 1)[0]
    else:
        names = [n.strip().rstrip(".") for n in re.split(r",\s*(?:and\s+)?|\s+and\s+", visible) if n.strip()]
        style = "full_names"
        first_surname = names[0].split()[-1] if names else ""
    return {"parse_status": "structured" if title and authors_text else "incomplete",
            "author_text": authors_text, "parsed_authors": names, "author_style": style,
            "authors_truncated": truncated, "first_surname": first_surname,
            "year": match.group(1), "year_suffix": match.group(2), "title": title,
            "parse_warnings": [] if title else ["제목을 구조화하지 못했습니다."]}
