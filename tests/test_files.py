import json
import os
import time

import pytest

from conftest import FakeResponse, Seq
from moodle_mcp import cache, files
from moodle_mcp.moodle import APIFunction, get_moodle_api_data

PDF_URL = "https://moodle.example.org/webservice/pluginfile.php/99/mod_resource/content/1/notes.pdf"

TYPED = (
    "Lo spazio vettoriale V delle funzioni da R in R: somma di funzioni e prodotto"
    " per uno scalare sono definiti punto per punto. Esempio 1."
)


def make_pdf(page_texts: list[str]) -> bytes:
    """A minimal valid PDF with one line of Helvetica text per page ('' = blank page)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in page_texts:
        stream = f"BT /F1 10 Tf 20 700 Td ({text}) Tj ET".encode("latin-1") if text else b""
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream")
        content_ref = len(objects)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_ref} 0 R >>"
        )
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        body = obj if isinstance(obj, bytes) else obj.encode("latin-1")
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def pdf_response(content: bytes) -> FakeResponse:
    return FakeResponse(content=content, headers={"Content-Type": "application/pdf"})


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOODLE_MCP_CACHE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def clear_versions(monkeypatch):
    monkeypatch.setattr(files, "_file_versions", {})


# --- text layer detection ------------------------------------------------


def test_typed_pdf_returns_text_with_paging_header(fake_moodle):
    fake_moodle.responses[files.normalize_file_url(PDF_URL)] = pdf_response(make_pdf([TYPED] * 5))
    result = files.read_course_file(PDF_URL, pages="1-2")

    assert list(result)[:3] == ["pages_total", "has_text_layer", "next_pages"]
    assert result["pages_total"] == 5
    assert result["has_text_layer"] is True
    assert result["next_pages"] == "3-5"
    assert "spazio vettoriale" in result["text"]


def test_handwritten_pdf_returns_no_text_and_points_to_images(fake_moodle):
    # Scanned notes: blank text layer, or a header with a few characters.
    fake_moodle.responses[files.normalize_file_url(PDF_URL)] = pdf_response(make_pdf(["Lezione 3", "", "", "Pag 4"]))
    result = files.read_course_file(PDF_URL)

    assert result["has_text_layer"] is False
    assert result["text"] == ""
    assert "view_course_file_pages" in result["hint"]
    assert "pages='1-4'" in result["hint"]


def test_slides_with_some_image_pages_still_return_text(fake_moodle):
    # Slide decks have title or diagram pages with little text; they are not handwriting.
    pages = [TYPED, "Titolo", TYPED, TYPED, "", TYPED]
    fake_moodle.responses[files.normalize_file_url(PDF_URL)] = pdf_response(make_pdf(pages))
    result = files.read_course_file(PDF_URL)

    assert result["has_text_layer"] == "partial"
    assert result["pages_without_text"] == "2,5"
    assert "spazio vettoriale" in result["text"]


def test_title_page_alone_does_not_make_a_typed_pdf_handwritten(fake_moodle):
    pages = ["Titolo", TYPED, TYPED, TYPED, TYPED]
    fake_moodle.responses[files.normalize_file_url(PDF_URL)] = pdf_response(make_pdf(pages))
    result = files.read_course_file(PDF_URL, pages="1")

    assert result["has_text_layer"] == "partial"
    assert result["pages_without_text"] == "1"
    assert "handwritten" not in (result["hint"] or "")


def test_single_page_handwritten_pdf_is_detected(fake_moodle):
    fake_moodle.responses[files.normalize_file_url(PDF_URL)] = pdf_response(make_pdf(["Pag 1"]))
    assert files.read_course_file(PDF_URL)["has_text_layer"] is False


def test_ocr_noise_counts_as_no_text():
    assert files._meaningful_chars("�" * 30) == 0
    assert files._meaningful_chars(TYPED) > files.MIN_PAGE_CHARS


def test_default_max_chars_is_20000():
    assert files.DEFAULT_MAX_CHARS == 20_000


# --- rendering ------------------------------------------------------------


def test_view_pages_defaults_to_grayscale_and_allows_eight_pages(fake_moodle):
    fake_moodle.responses[files.normalize_file_url(PDF_URL)] = pdf_response(make_pdf([TYPED] * 10))
    result = files.view_course_file_pages(PDF_URL, pages="1-10")

    images = [r for r in result if not isinstance(r, str)]
    assert len(images) == 8
    assert "pages='9-10'" in result[0]

    from PIL import Image
    import base64
    import io

    image = Image.open(io.BytesIO(base64.b64decode(images[0].data)))
    assert image.mode == "L"
    # A4 at 110 dpi.
    assert abs(image.height - 842 * 110 / 72) <= 2


def test_view_pages_respects_byte_budget(fake_moodle, monkeypatch):
    monkeypatch.setattr(files, "MAX_RENDER_BYTES", 1)
    fake_moodle.responses[files.normalize_file_url(PDF_URL)] = pdf_response(make_pdf([TYPED] * 3))
    result = files.view_course_file_pages(PDF_URL, pages="1-3")
    assert len([r for r in result if not isinstance(r, str)]) == 1
    assert "pages='2-3'" in result[0]


# --- downloads ---------------------------------------------------------------


def test_large_download_returns_metadata_instead_of_failing(fake_moodle, monkeypatch):
    monkeypatch.setattr(files, "MAX_BLOB_MB", 0)
    monkeypatch.setattr(files, "DOWNLOAD_DIR", None)
    fake_moodle.responses[files.normalize_file_url(PDF_URL)] = pdf_response(make_pdf([TYPED] * 3))
    [summary] = files.download_course_file(PDF_URL)
    summary = json.loads(summary)
    assert summary["returned_inline"] is False
    assert summary["pages_total"] == 3
    assert "read_course_file" in summary["next_step"]


# --- cache -------------------------------------------------------------------


def test_cache_disabled_with_empty_dir(monkeypatch):
    monkeypatch.setenv("MOODLE_MCP_CACHE_DIR", "")
    cache.put("x", "k", b"data")
    assert cache.get("x", "k", 3600) is None


def test_cache_roundtrip_and_ttl(cache_dir):
    cache.put("x", "k", b"data", {"mimetype": "text/plain"})
    assert cache.get("x", "k", 3600) == (b"data", {"mimetype": "text/plain"})

    entry = next(cache_dir.glob("x/*.bin"))
    old = time.time() - 7200
    os.utime(entry, (old, old))
    assert cache.get("x", "k", 3600) is None


def test_cache_evicts_oldest_over_size_limit(cache_dir, monkeypatch):
    monkeypatch.setattr(cache, "MAX_BYTES", 10)
    cache.put("x", "old", b"123456")
    entry = next(cache_dir.glob("x/*.bin"))
    os.utime(entry, (time.time() - 100, time.time() - 100))
    cache.put("x", "new", b"abcdef")
    assert cache.get("x", "old", 3600) is None
    assert cache.get("x", "new", 3600) is not None


def test_files_are_downloaded_once(fake_moodle, cache_dir):
    url = files.normalize_file_url(PDF_URL)
    fake_moodle.responses[url] = pdf_response(make_pdf([TYPED] * 4))
    files.read_course_file(PDF_URL, pages="1-2")
    files.read_course_file(PDF_URL, pages="3-4")
    assert fake_moodle.count(url) == 1


def test_new_timemodified_invalidates_cached_file(fake_moodle, cache_dir):
    url = files.normalize_file_url(PDF_URL)

    def contents(timemodified):
        return [{"name": "S", "modules": [{"id": 1, "name": "Notes", "modname": "resource",
                 "contents": [{"type": "file", "filename": "notes.pdf", "fileurl": PDF_URL + "?forcedownload=1", "timemodified": timemodified}]}]}]

    fake_moodle.responses["core_course_get_contents"] = contents(1000)
    fake_moodle.responses[url] = Seq([pdf_response(make_pdf(["old " + TYPED])), pdf_response(make_pdf(["new " + TYPED]))])

    files.list_course_files(5)
    assert files.read_course_file(PDF_URL)["text"].count("old") == 1

    # The teacher uploads a new version; once the cached course page expires,
    # the fresh one reports it.
    for p in cache_dir.glob("ws/*"):
        p.unlink()
    fake_moodle.responses["core_course_get_contents"] = contents(2000)
    files.list_course_files(5)
    assert "new" in files.read_course_file(PDF_URL)["text"]
    assert fake_moodle.count(url) == 2


def test_course_contents_are_cached(fake_moodle, cache_dir):
    fake_moodle.responses["core_course_get_contents"] = [{"name": "S", "modules": []}]
    get_moodle_api_data(APIFunction.core_course_get_contents, {"courseid": "5"})
    second = get_moodle_api_data(APIFunction.core_course_get_contents, {"courseid": "5"})
    assert second == [{"name": "S", "modules": []}]
    assert fake_moodle.count("core_course_get_contents") == 1
    # Another course is a different entry.
    get_moodle_api_data(APIFunction.core_course_get_contents, {"courseid": "6"})
    assert fake_moodle.count("core_course_get_contents") == 2
