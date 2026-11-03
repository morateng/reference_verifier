"""Real uploaded PDF layout replay plus independent end-to-end PDF controls."""
import io
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from app.extraction import extract_pdf, extract_text
from app.main import app
from app.parsing import layout_references, structured_reference
from app.verification import assess, metadata, name_matches

FIXTURES = Path(__file__).parent / "fixtures"


def uploaded_layout():
    lines = json.loads((FIXTURES / "acl_reference_layout.json").read_text())
    layout = layout_references(lines)
    return extract_text(["References\n" + "\n".join(r["text"] for r in lines)], layout=layout)


def test_uploaded_pdf_bibliography_is_51_entries_on_pages_10_to_12():
    result = uploaded_layout()
    assert result["parsing"]["reference_pages"] == [10, 11, 12]
    assert result["parsing"]["stopped_at"] == "A Additional Details on Human"
    assert len(result["references"]) == result["structured_count"] == 51
    assert result["references"][0]["parsed_authors"][0] == "Philip Anastassiou"
    assert result["references"][-1]["parsed_authors"][0] == "Han Zhu"
    assert result["references"][8]["year"] == "2025"
    assert result["references"][8]["year_suffix"] == "c"
    assert all("Apache-2.0" not in r["raw"] for r in result["references"])


def test_previous_html_metadata_replay_now_matches_two_actual_pdf_entries():
    # Metadata was recorded in the user's earlier HTML. This is an offline replay,
    # not a new live lookup and not proof that all 51 references are correct.
    works = json.loads((FIXTURES / "saved_html_metadata.json").read_text())["works"]
    refs = uploaded_layout()["references"]
    for work, reference in zip(works, [refs[31], refs[8]]):
        result = assess(reference, metadata(work), via_doi=False)
        assert result["status"] == "matched", result
        assert not result["issues"]
        assert all(check["status"] == "match" for check in result["checks"])


def test_full_names_and_others_are_a_valid_abbreviation():
    raw = "Philip Anastassiou, Jiawei Chen, Jitong Chen, and 27 others. 2024. A family of speech generation models. Preprint."
    ref = {"raw": raw, **structured_reference(raw)}
    work = {"title": ["A family of speech generation models"], "author": [
        {"given": "Philip", "family": "Anastassiou"}, {"given": "Jiawei", "family": "Chen"},
        {"given": "Jitong", "family": "Chen"}, {"given": "Another", "family": "Author"}], "published": {"date-parts": [[2024]]}}
    result = assess(ref, metadata(work), True)
    assert ref["authors_truncated"]
    assert result["status"] == "matched"
    assert result["checks"][-1]["status"] == "unverified"


def test_repeated_surnames_are_compared_in_sequence_and_wrong_order_is_flagged():
    work = {"title": ["A test title"], "author": [{"given": "Alice", "family": "Chen"},
        {"given": "Bob", "family": "Wang"}, {"given": "Carol", "family": "Chen"}], "published": {"date-parts": [[2024]]}}
    for authors, expected in [("Alice Chen, Bob Wang, and Carol Chen", "matched"), ("Alice Chen, Carol Chen, and Bob Wang", "review")]:
        raw = authors + ". 2024. A test title. Journal."
        assert assess({"raw": raw, **structured_reference(raw)}, metadata(work), True)["status"] == expected


def test_abbreviation_does_not_hide_an_explicitly_wrong_visible_author():
    work = {"title": ["A test title"], "author": [{"given": "Alice", "family": "Chen"},
        {"given": "Bob", "family": "Wang"}, {"given": "Carol", "family": "Chen"}], "published": {"date-parts": [[2024]]}}
    raw = "Alice Chen, Wrong Person, and 1 others. 2024. A test title. Journal."
    result = assess({"raw": raw, **structured_reference(raw)}, metadata(work), True)
    assert result["status"] == "review"
    assert any("Wrong Person" in issue for issue in result["issues"])


def test_wrong_title_and_year_remain_flagged():
    work = {"title": ["An entirely different study"], "author": [{"given": "Alice", "family": "Chen"}], "published": {"date-parts": [[2024]]}}
    raw = "Alice Chen. 2020. A test title. Journal."
    result = assess({"raw": raw, **structured_reference(raw)}, metadata(work), True)
    assert result["status"] == "review"
    assert sum(c["status"] == "review" for c in result["checks"]) == 2


def test_initials_given_names_and_compound_surnames():
    assert name_matches("Smith, A. M.", "Smith", "Alice Mary")
    assert name_matches("Alice M. Smith", "Smith", "Alice Mary")
    assert name_matches("J. van der Meer", "van der Meer", "John")
    assert not name_matches("Smith, B. M.", "Smith", "Alice Mary")


def test_initials_in_second_full_name_do_not_trigger_surname_first_style():
    parsed = structured_reference("Yuxuan Wang, R.J. Skerry-Ryan, and Daisy Stanton. 2017. Tacotron: Towards End-to-End Speech Synthesis. Conference.")
    assert parsed["author_style"] == "full_names"
    assert parsed["parsed_authors"] == ["Yuxuan Wang", "R.J. Skerry-Ryan", "Daisy Stanton"]


def synthetic_pdf():
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream)
    pdf.setFont("Helvetica", 10)
    pdf.drawString(45, 730, "An introduction with evidence (Smith et al., 2024). This is a study.")
    pdf.showPage()
    pdf.setFont("Helvetica", 12)
    pdf.drawString(45, 750, "References")
    pdf.setFont("Helvetica", 10)
    for x, y, text in [(45, 725, "Alice Smith, Bob Jones, and Carol Chen."), (55, 711, "2024. An example title. Journal."),
                       (45, 680, "Dana White. 2022. A second title."), (55, 666, "Journal of examples."),
                       (330, 750, "Evan Brown. 2021. A third title."), (340, 736, "Another journal.")]:
        pdf.drawString(x, y, text)
    pdf.showPage()
    pdf.setFont("Helvetica", 12)
    pdf.drawString(45, 750, "A Additional Experimental Details")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(45, 725, "Whisper (Radford et al., 2023) Apache-2.0 is a software license row.")
    pdf.save()
    return stream.getvalue()


def test_geometry_extraction_from_real_pdf_bytes_excludes_appendix():
    result = extract_pdf(synthetic_pdf())
    assert result["parsing"]["method"] == "pdf_hanging_indent"
    assert len(result["references"]) == 3
    assert result["references"][0]["source_pages"] == [2]
    assert result["references"][0]["contexts"]
    assert all("Apache" not in r["raw"] for r in result["references"])


def test_parsing_preview_does_not_contact_external_services():
    with TestClient(app) as client:
        original = app.state.client
        def no_network(request):
            raise AssertionError("Parsing preview must not query metadata providers")
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(no_network))
        response = client.post("/api/parse", files={"file": ("test.pdf", synthetic_pdf(), "application/pdf")})
        assert response.status_code == 200
        assert response.json()["structured_count"] == 3
        app.state.client = original
