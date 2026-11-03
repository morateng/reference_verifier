"""Expose evidence and failure causes without changing matching thresholds."""
from collections import Counter

VERSION = "0.3.0-reference-boundaries"


def evidence(result: dict) -> dict:
    has_record = bool(result.get("record"))
    direct_doi = has_record and result.get("matched_by") == "doi"
    direct_arxiv = has_record and result.get("matched_by") == "arxiv_id"
    fields = {c["field"]: c["status"] for c in result.get("checks", [])}
    identity = has_record and result.get("status") != "candidate" and all(
        fields.get(f) == "match" for f in ("제목", "첫 저자", "발행연도")
    )
    return {
        "registry": "doi_registered" if direct_doi else "arxiv_registered" if direct_arxiv else "search_candidate" if has_record else "lookup_failed" if result["status"] == "error" else "not_resolved",
        "identity": "major_fields_match" if identity else "not_established",
        "bibliography": "differences_detected" if any(v == "review" for v in fields.values()) else "checked_fields_match" if fields and all(v == "match" for v in fields.values()) else "partially_checked" if fields else "not_checked",
        "content": result.get("content_check", {}).get("status", "unverified"),
    }


def summarize(results: list[dict], total: int) -> dict:
    dimensions = [evidence(r) for r in results]
    fields = Counter(c["field"] for r in results for c in r.get("checks", []) if c["status"] == "review")
    errors = Counter(str(r.get("lookup_error", {}).get("http_status") or r.get("lookup_error", {}).get("kind", "unknown")) for r in results if r["status"] == "error")
    return {
        "total": total, "processed": len(results),
        "doi_registered": sum(d["registry"] == "doi_registered" for d in dimensions),
        "arxiv_registered": sum(d["registry"] == "arxiv_registered" for d in dimensions),
        "major_fields_match": sum(d["identity"] == "major_fields_match" for d in dimensions),
        "unresolved": sum(r["status"] == "unverified" for r in results),
        "lookup_failed": sum(r["status"] == "error" for r in results),
        "ambiguous_candidates": sum(r["status"] == "candidate" for r in results),
        "abstract_reviewed": sum(d["content"] == "abstract_only" for d in dimensions),
        "field_warnings": dict(fields), "lookup_errors": dict(errors),
        "extraction_flags": sum(bool(r.get("extraction_flags")) for r in results),
        "note": "DOI 등록 확인·동일 논문 대조·서지 표기·내용 검증은 서로 다른 판단입니다.",
    }
