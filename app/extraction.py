"""Conservative extraction: never equate a missing search result with fabrication."""
import io
import re
from pypdf import PdfReader

MAX_REFERENCES = 200
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s<>\"\]]+", re.I)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}[a-z]?\b")
HEADING = re.compile(r"^\s*(?:\d+[.\s]+)?(?:references|bibliography|works cited|참고\s*문헌)\s*[:.]?\s*$", re.I | re.M)
NUMBERED = re.compile(r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})[.)])\s+")


def extract_pdf(data: bytes) -> dict:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("암호화된 PDF입니다. 암호를 해제한 파일을 올려 주세요.")
        if len(reader.pages) > 300:
            raise ValueError("최대 300페이지까지 지원합니다.")
        pages = [p.extract_text(extraction_mode="plain") or "" for p in reader.pages]
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("PDF를 읽을 수 없습니다. 파일이 손상되지 않았는지 확인해 주세요.") from exc
    if sum(len(p.strip()) for p in pages) < 100:
        raise ValueError("추출할 텍스트가 부족합니다. 스캔 PDF는 먼저 OCR 처리가 필요합니다.")
    return extract_text(pages)


def extract_text(pages: list[str]) -> dict:
    text = "\n".join(pages)
    headings = list(HEADING.finditer(text))
    if not headings:
        raise ValueError("참고문헌 섹션을 찾지 못했습니다. References / Bibliography / 참고문헌 제목이 있는 텍스트 PDF를 사용해 주세요.")
    # Last heading avoids the table of contents; repeated page headings are removed below.
    heading = headings[0]
    if len(headings) > 1 and heading.start() < len(text) * .2:
        heading = headings[1]
    body = text[:heading.start()]
    tail = text[heading.end():]
    tail = re.split(r"\n\s*(?:appendix\b|appendices\b|supplementary material\b|부록\b)", tail, maxsplit=1, flags=re.I)[0]
    lines = [x.strip() for x in tail.splitlines() if x.strip() and not HEADING.fullmatch(x) and not re.fullmatch(r"\d+", x.strip())]
    numbered = sum(bool(NUMBERED.match(x)) for x in lines) >= 1
    entries: list[dict] = []
    warnings = ["자동 추출은 누락·병합이 있을 수 있습니다. 표시된 항목 수와 PDF의 참고문헌 수를 비교해 주세요."]
    current: list[str] = []
    label = ""
    preamble: list[str] = []
    for line in lines:
        match = NUMBERED.match(line) if numbered else None
        # Author-year entries commonly begin with surname, initials and a year.
        starts = bool(match) if numbered else bool(re.match(r"^[^\d\W][^:]{0,180}?\b(?:19|20)\d{2}[a-z]?\b", line) and re.search(r"[,，(（]", line[:100]))
        if starts:
            if current:
                entries.append({"label": label, "raw": " ".join(current)})
            label = (match.group(1) or match.group(2)) if match else str(len(entries) + 1)
            current = [line[match.end():] if match else line]
        elif current:
            current.append(line)
        else:
            preamble.append(line)
    if current:
        entries.append({"label": label, "raw": " ".join(current)})
    if not entries:
        raise ValueError("참고문헌 항목을 구분하지 못했습니다. 현재는 번호형 및 저자–연도형 참고문헌을 지원합니다.")
    if preamble:
        warnings.append("참고문헌 섹션의 일부 텍스트를 항목으로 분리하지 못했습니다: " + " ".join(preamble)[:250])
    if len(entries) > MAX_REFERENCES:
        raise ValueError(f"참고문헌 {len(entries)}개를 찾았습니다. 현재 한 번에 최대 {MAX_REFERENCES}개까지 지원합니다.")
    for i, entry in enumerate(entries):
        entry["id"] = i + 1
        doi = DOI_RE.search(entry["raw"])
        entry["doi"] = doi.group().rstrip(".,;") if doi else None
        distinct_dois = {d.group().rstrip(".,;)").lower() for d in DOI_RE.finditer(entry["raw"])}
        entry["extraction_flags"] = []
        if len(distinct_dois) > 1:
            entry["extraction_flags"].append("서로 다른 DOI가 여러 개 추출됐습니다. 참고문헌 항목이 합쳐졌는지 확인해 주세요.")
        if len(entry["raw"]) > 3000:
            entry["extraction_flags"].append("참고문헌 한 항목이 매우 깁니다. 항목 병합이나 페이지 머리말 포함 여부를 확인해 주세요.")
        if entry["doi"]:
            while entry["doi"].endswith(")") and entry["doi"].count(")") > entry["doi"].count("("):
                entry["doi"] = entry["doi"][:-1]
        year = YEAR_RE.search(entry["raw"])
        entry["year"] = year.group()[:4] if year else None
        entry["contexts"] = find_contexts(body, entry, numbered)
    labels = [e["label"] for e in entries]
    if len(labels) != len(set(labels)):
        warnings.append("중복된 참고문헌 번호가 있습니다. PDF 열 순서 또는 항목 분리를 확인해 주세요.")
    return {"page_count": len(pages), "references": entries, "warnings": warnings}


def find_contexts(body: str, entry: dict, numbered: bool) -> list[str]:
    spans = []
    if numbered:
        for m in re.finditer(r"\[([\d\s,;–−-]+)\]", body):
            labels: set[int] = set()
            for part in re.split(r"[,;]", m.group(1)):
                nums = re.findall(r"\d+", part)
                if len(nums) == 1:
                    labels.add(int(nums[0]))
                elif len(nums) == 2 and 0 <= int(nums[1]) - int(nums[0]) <= 200:
                    labels.update(range(int(nums[0]), int(nums[1]) + 1))
            if int(entry["label"]) in labels:
                spans.append(m.span())
    elif entry.get("year"):
        surname = re.match(r"[\wÀ-ž가-힣'-]+", entry["raw"])
        if surname:
            pattern = r"\b" + re.escape(surname.group()) + r"\b(?:\s+et\s+al\.)?[^.!?\n]{0,90}?" + re.escape(entry["year"])
            spans = [m.span() for m in re.finditer(pattern, body, re.I)]
    contexts = []
    for start, end in spans:
        left = max(body.rfind(". ", 0, start), body.rfind("\n\n", 0, start))
        right = re.search(r"[.!?](?:\s|$)", body[end:])
        snippet = body[max(left + 2, start - 500, 0): min(end + right.end() if right else end + 300, end + 500)]
        snippet = re.sub(r"\s+", " ", snippet).strip()
        if snippet and snippet not in contexts:
            contexts.append(snippet)
    return contexts
