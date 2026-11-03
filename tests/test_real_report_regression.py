"""Regression cases derived from bibliography excerpts in the uploaded HTML.

These tests validate extraction/matching behavior, not live database availability.
"""
import asyncio
from pathlib import Path

import httpx

from app.extraction import extract_text
from app.providers import ProviderGate, parse_arxiv
from app.verification import verify_reference, normalize
from app.reporting import evidence

FIXTURE = Path(__file__).parent / "fixtures" / "wrapped_references.txt"


def test_uploaded_bibliography_author_lists_stay_with_their_own_titles():
    result = extract_text([FIXTURE.read_text()])
    refs = result["references"]
    assert len(refs) == 7
    assert [r["first_surname"] for r in refs] == ["Anastassiou", "Ben-Artzy", "Cao", "Chen", "Chen", "Chen", "Du"]
    assert refs[4]["raw"].startswith("Chen, G.; Chai, S.")
    assert "Gigaspeech:" in refs[4]["raw"]
    assert "Ben-Artzy" not in refs[4]["raw"]
    assert refs[6]["raw"].startswith("Du, C.; Guo,")
    assert refs[5]["year_suffix"] == "b"
    assert not any(r["extraction_flags"] for r in refs)
    assert not any("분리하지 못했습니다" in w for w in result["warnings"])


def test_year_suffix_connects_to_the_correct_body_citation():
    result = extract_text(["Chen et al. 2024b developed a system.\n" + FIXTURE.read_text()])
    refs = result["references"]
    assert refs[5]["contexts"]
    assert not refs[3]["contexts"]  # 2024a must not consume a 2024b citation.


def test_comma_separated_apa_authors_are_not_cut_at_last_author():
    result = extract_text(["References\nSmith, A., Jones, B. (2021). A title.\nBrown, C., White, D. (2022). Another title."])
    assert len(result["references"]) == 2
    assert result["references"][0]["raw"].startswith("Smith, A., Jones")
    assert result["references"][1]["raw"].startswith("Brown, C., White")


def test_pdf_wrap_and_detached_diacritics_normalize_consistently():
    assert normalize("consol- idate") == normalize("consolidate")
    assert normalize("H ¨ullermeier") == normalize("Hüllermeier")
    assert normalize("Markovi ´c") == normalize("Marković")


ATOM = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>http://arxiv.org/abs/2301.02111v1</id>
<title>Neural Codec Language Models are Zero-Shot Text to Speech Synthesizers</title>
<published>2023-01-05T00:00:00Z</published>
<author><name>Chengyi Wang</name></author><author><name>Sanyuan Chen</name></author>
<summary>A preprint abstract for the test.</summary></entry></feed>'''


async def test_arxiv_preprint_not_replaced_by_later_journal_paper():
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=ATOM)
    raw = "Wang, C.; Chen, S.; et al. 2023. Neural codec language models are zero-shot text to speech synthesizers. arXiv:2301.02111."
    reference = extract_text(["References\n" + raw])["references"][0]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await verify_reference(reference, client)
    assert result["status"] == "matched"
    assert result["record"]["years"] == ["2023"]
    assert result["record"]["source"] == "arXiv"
    assert result["record"]["source_url"] == "https://arxiv.org/abs/2301.02111v1"
    assert result["matched_by"] == "arxiv_id"
    assert evidence(result)["registry"] == "arxiv_registered"
    assert len(requests) == 1


def test_arxiv_different_identifier_or_version_is_not_accepted():
    assert parse_arxiv(ATOM, "2406.02430") is None
    assert parse_arxiv(ATOM, "2301.02111v2") is None


async def test_retry_after_is_shared_and_requests_serialized():
    elapsed = [0.0]
    delays, calls = [], []
    async def sleep(delay):
        delays.append(delay)
        elapsed[0] += delay
    def handler(request):
        calls.append(elapsed[0])
        return httpx.Response(429, headers={"Retry-After": "5"}) if len(calls) == 1 else httpx.Response(200)
    gate = ProviderGate(1.1, sleep=sleep, clock=lambda: elapsed[0])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await asyncio.gather(gate.get(client, "https://api.crossref.org/works"), gate.get(client, "https://api.crossref.org/works"))
    assert [r.status_code for r in results] == [200, 200]
    assert calls[1] >= 5
    assert calls[2] - calls[1] >= 1.1 - 1e-9
    assert delays[0] == 5


async def test_long_rate_limit_does_not_block_all_queued_jobs():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "3600"})
    async def no_sleep(delay):
        raise AssertionError("Long server cooldown should return rather than block the task")
    gate = ProviderGate(1.1, sleep=no_sleep, clock=lambda: 1.)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await gate.get(client, "https://api.crossref.org/works")).status_code == 429
        assert (await gate.get(client, "https://api.crossref.org/works")).status_code == 429
    assert len(calls) == 1
