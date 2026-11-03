import httpx
from fastapi.testclient import TestClient

from app.extraction import extract_text
from app.main import app
from app.reporting import VERSION, evidence, summarize


def example(status="review", **kwargs):
    return {"status": status, "record": {"doi": "10.1234/example"}, "matched_by": "doi",
            "checks": [{"field": "제목", "status": "review"}, {"field": "첫 저자", "status": "match"}, {"field": "발행연도", "status": "match"}],
            "content_check": {"status": "unverified"}, **kwargs}


def test_wrong_citation_does_not_hide_registered_doi():
    result = evidence(example())
    assert result["registry"] == "doi_registered"
    assert result["identity"] == "not_established"
    assert result["bibliography"] == "differences_detected"
    assert result["content"] == "unverified"


def test_search_candidate_does_not_become_confirmed_identity():
    checks = [{"field": f, "status": "match"} for f in ("제목", "첫 저자", "발행연도")]
    result = evidence(example("candidate", matched_by="bibliographic_search", checks=checks))
    assert result["registry"] == "search_candidate"
    assert result["identity"] == "not_established"


def test_secondary_author_warning_keeps_identity_and_bibliography_separate():
    checks = [{"field": f, "status": "match"} for f in ("제목", "첫 저자", "발행연도")]
    checks.append({"field": "전체 저자", "status": "review"})
    result = evidence(example(checks=checks))
    assert result["identity"] == "major_fields_match"
    assert result["bibliography"] == "differences_detected"


def test_summary_separates_api_failures_unresolved_and_field_warnings():
    results = [example(), example("error", record=None, checks=[], lookup_error={"http_status": 403}),
               example("unverified", record=None, checks=[])]
    result = summarize(results, 10)
    assert result["processed"] == 3 and result["total"] == 10
    assert result["lookup_failed"] == 1 and result["unresolved"] == 1
    assert result["field_warnings"] == {"제목": 1}
    assert result["lookup_errors"] == {"403": 1}
    assert result["abstract_reviewed"] == 0


def test_merged_reference_warning_preserves_original_data():
    raw = "Smith (2021). A. doi:10.1234/one Jones (2020). B. doi:10.1234/two"
    result = extract_text(["Body [1].\nReferences\n[1] " + raw])
    assert result["references"][0]["raw"] == raw
    assert result["references"][0]["extraction_flags"]


def test_duplicate_doi_url_is_not_merging_evidence():
    result = extract_text(["Body [1].\nReferences\n[1] Smith (2021). A. doi:10.1234/one https://doi.org/10.1234/one"])
    assert not result["references"][0]["extraction_flags"]


def test_connection_probe_reports_each_provider_independently():
    def handler(request):
        if request.url.host == "api.crossref.org":
            return httpx.Response(403)
        return httpx.Response(200, json={"data": []})
    with TestClient(app) as client:
        original = app.state.client
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        data = client.get("/api/diagnostics").json()
        assert data["version"] == VERSION
        assert data["sources"][0]["ok"] is False
        assert data["sources"][0]["http_status"] == 403
        assert data["sources"][1]["ok"] is True
        app.state.client = original
