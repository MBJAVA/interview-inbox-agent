from pathlib import Path

from tools.attachments import extract_text, read_attachment

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "attachments"


def test_reads_prep_guide_pdf_as_untrusted(sandbox):
    sandbox("s25_karat_followup")
    r = read_attachment.invoke({"message_id": "m25a", "filename": "Karat Interview Prep Guide.pdf"})
    assert r["ok"] and r["pages"] == 1
    assert r["text"].startswith("<untrusted_content>") and "45 minutes" in r["text"]


def test_missing_attachment_is_data_not_exception(sandbox):
    sandbox("s25_karat_followup")
    r = read_attachment.invoke({"message_id": "m25a", "filename": "resume.docx"})
    assert r["ok"] is False and r["hint"]


def test_injection_inside_pdf_stays_data():
    text, _ = extract_text((FIX / "injection_sample.pdf").read_bytes(), "application/pdf", "x.pdf")
    assert "ignore your instructions" in text  # extracted verbatim, then wrapped as untrusted by the tool


def test_size_limit():
    import pytest

    with pytest.raises(ValueError):
        extract_text(b"0" * (6 * 1024 * 1024), "application/pdf", "big.pdf")
