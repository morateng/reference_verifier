import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from app.extraction import extract_pdf, extract_text
from app.main import app, JOBS
from app.verification import verify_reference, check_content, metadata, rank_candidates


WORK = {"DOI": "10.1234/example", "title": ["Deep learning for scientific discovery"],
        "author": [{"given": "Alice", "family": "Smith"}, {"given": "Bob", "family": "Jones"}],
        "published": {"date-parts": [[2021]]}, "container-title": ["Science Examples"],
        "volume": "5", "page": "10-20", "abstract": "<p>The study improves image classification accuracy.</p>"}
RAW = "Smith, A., Jones, B. (2021). Deep learning for scientific discovery. Science Examples 5, 10-20. doi:10.1234/example"


def ref(raw=RAW, doi="10.1234/example"):
    return {"id": 1, "label": "1", "raw": raw, "doi": doi, "year": "2021", "contexts": ["Image classification improves [1]."]}


def pdf_bytes(text=None):
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream)
    obj = pdf.beginText(40, 800)
    for line in (text or ["A study of scientific references. Image classification improves [1].", "This paper evaluates evidence and checks whether references exist.", "References", "[1] " + RAW]).copy():
        obj.textLine(line)
    pdf.drawText(obj)
    pdf.save()
    return stream.getvalue()


def test_extract_pdf_and_context():
    result = extract_pdf(pdf_bytes())
    assert len(result["references"]) == 1
    assert result["references"][0]["doi"] == "10.1234/example"
    assert "improves [1]" in result["references"][0]["contexts"][0]


def test_multiline_and_citation_range():
    result = extract_text(["Evidence [1–2].\nReferences\n[1] Smith (2021).\nFirst title.\n[2] Jones (2022). Second title."])
    assert len(result["references"]) == 2
    assert all(r["contexts"] for r in result["references"])
    assert "First title." in result["references"][0]["raw"]


def test_author_year():
    result = extract_text(["Smith et al. (2021) proposed a method.\nReferences\nSmith, A. (2021). A method.\nJones, B. (2020). Another method."])
    assert len(result["references"]) == 2
    assert result["references"][0]["contexts"]
    assert not result["references"][1]["contexts"]


def test_no_heading_is_explicit_failure():
    with pytest.raises(ValueError, match="섹션"):
        extract_text(["This document has no bibliography."])


def test_scan_is_explicit_failure():
    with pytest.raises(ValueError, match="OCR"):
        extract_pdf(pdf_bytes([""]))


async def verify(handler, reference=None, ai=False):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await verify_reference(reference or ref(), client, ai)


async def test_doi_match():
    result = await verify(lambda request: httpx.Response(200, json={"message": WORK}))
    assert result["status"] == "matched"
    assert result["matched_by"] == "doi"
    assert "https://doi.org/10.1234/example" in result["suggested_citation"]


async def test_wrong_title_author_year():
    bad = ref("Unknown, C. (2019). An unrelated work.")
    bad["year"] = "2019"
    result = await verify(lambda r: httpx.Response(200, json={"message": WORK}), bad)
    assert result["status"] == "review"
    assert all(c["status"] == "review" for c in result["checks"])


async def test_missing_doi_falls_back_without_claiming_match():
    def handler(request):
        if "/works/" in request.url.path or "/dois/" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json={"message": {"items": [WORK]}})
    result = await verify(handler)
    assert result["status"] == "review"
    assert result["matched_by"] == "bibliographic_search"


async def test_search_no_results_not_fabricated():
    result = await verify(lambda r: httpx.Response(200, json={"message": {"items": []}}), ref(doi=None))
    assert result["status"] == "unverified"
    assert result["record"] is None


async def test_similar_candidates_remain_ambiguous():
    other = {**WORK, "DOI": "10.1234/other"}
    result = await verify(lambda r: httpx.Response(200, json={"message": {"items": [WORK, other]}}), ref(doi=None))
    assert result["status"] == "candidate"


@pytest.mark.parametrize("status", [403, 500])
async def test_network_error_not_nonexistent(status):
    result = await verify(lambda r: httpx.Response(status))
    assert result["status"] == "error"
    assert str(status) in result["issues"][0]


async def test_network_timeout():
    def handler(request):
        raise httpx.ReadTimeout("timeout", request=request)
    assert (await verify(handler))["status"] == "error"


async def test_ai_without_key(monkeypatch):
    monkeypatch.delenv("REFERENCE_AI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = await verify(lambda r: httpx.Response(200, json={"message": WORK}), ai=True)
    assert result["status"] == "matched"
    assert result["content_check"]["status"] == "unverified"


async def test_ai_invented_quote_is_rejected(monkeypatch):
    monkeypatch.setenv("REFERENCE_AI_API_KEY", "test-only-key")
    output = {"items": [{"index": 1, "verdict": "supported_by_abstract", "reason": "Matches", "evidence_quote": "This quote does not exist"}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output)}}]}))) as client:
        result = await check_content(ref(), metadata(WORK), client)
    assert result["items"][0]["verdict"] == "insufficient_evidence"


async def test_ai_missing_abstract_never_called(monkeypatch):
    monkeypatch.setenv("REFERENCE_AI_API_KEY", "test-only-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("Unexpected AI request"))) as client:
        result = await check_content(ref(), metadata({**WORK, "abstract": ""}), client)
    assert result["status"] == "unverified"


def test_upload_poll_report_end_to_end():
    JOBS.clear()
    with TestClient(app) as client:
        original = app.state.client
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"message": WORK})))
        assert client.get("/").status_code == 200
        assert client.get("/api/health").json()["status"] == "ok"
        response = client.post("/api/jobs", files={"file": ("example.pdf", pdf_bytes(), "application/pdf")})
        assert response.status_code == 202
        job_id = response.json()["id"]
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["state"] == "done"
        assert job["completed"] == job["total"] == 1
        assert job["results"][0]["status"] == "matched"
        assert client.get(f"/api/jobs/{job_id}/report").json()["id"] == job_id
        app.state.client = original


def test_invalid_upload_and_missing_result():
    with TestClient(app) as client:
        assert client.post("/api/jobs", files={"file": ("fake.pdf", b"not a pdf")}).status_code == 400
        assert client.get("/api/jobs/missing").status_code == 404


def test_result_expiration():
    from app.main import expire_jobs
    JOBS["expired"] = {"state": "done", "created_at": 0, "finished_at": 0}
    expire_jobs()
    assert "expired" not in JOBS


@pytest.mark.parametrize("authors", ["A. Smith and B. Jones", "Alice Smith, Bob Jones", "Smith A, Jones B"])
async def test_common_author_styles_are_not_false_mismatches(authors):
    item = ref(authors + ' (2021). Deep learning for scientific discovery.')
    result = await verify(lambda r: httpx.Response(200, json={"message": WORK}), item)
    assert result["status"] == "matched"


async def test_author_mentioned_only_in_title_is_not_match():
    work = {**WORK, "title": ["Smith and Jones methods for scientific discovery"]}
    item = ref("Unknown, C. (2021). Smith and Jones methods for scientific discovery.")
    result = await verify(lambda r: httpx.Response(200, json={"message": work}), item)
    assert result["status"] == "review"
    assert next(c for c in result["checks"] if c["field"] == "첫 저자")["status"] == "review"


async def test_reversed_authors_flag_order():
    item = ref("B. Jones and A. Smith (2021). Deep learning for scientific discovery.")
    result = await verify(lambda r: httpx.Response(200, json={"message": WORK}), item)
    assert result["status"] == "review"
    assert any("순서" in issue for issue in result["issues"])


async def test_diacritic_author_variants():
    work = {**WORK, "author": [{"family": "García", "given": "José"}]}
    item = ref("J. Garcia (2021). Deep learning for scientific discovery.")
    assert (await verify(lambda r: httpx.Response(200, json={"message": work}), item))["status"] == "matched"


def test_candidate_ranking_uses_author_and_year_not_just_title():
    wrong = {**WORK, "DOI": "10.1234/wrong", "author": [{"family": "Brown"}], "published": {"date-parts": [[2010]]}}
    short = {**WORK, "DOI": "10.1234/short", "title": ["Deep learning"]}
    assert rank_candidates(ref(), [wrong, short, WORK])[0][1]["DOI"] == WORK["DOI"]


def test_duplicate_search_records_not_ambiguous():
    assert len(rank_candidates(ref(), [WORK, WORK])) == 1


async def test_datacite_doi_fallback():
    def handler(request):
        if request.url.host == 'api.crossref.org':
            return httpx.Response(404)
        return httpx.Response(200, json={"data": {"attributes": {
            "doi": WORK["DOI"], "titles": [{"title": WORK["title"][0]}], "publicationYear": 2021,
            "creators": [{"givenName": "Alice", "familyName": "Smith"}, {"givenName": "Bob", "familyName": "Jones"}]
        }}})
    result = await verify(handler)
    assert result["status"] == "matched"
    assert result["record"]["source"] == "DataCite"
    assert result["matched_by"] == "doi"


async def test_datacite_denied_still_uses_crossref_search():
    def handler(request):
        if request.url.host == 'api.datacite.org':
            return httpx.Response(403)
        if '/works/' in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json={"message": {"items": [WORK]}})
    result = await verify(handler)
    assert result["record"]["title"] == WORK["title"][0]
    assert result["status"] == "review"
    assert any('추가 조회 실패' in issue for issue in result["issues"])
