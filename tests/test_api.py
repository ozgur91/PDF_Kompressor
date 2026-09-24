import base64
import io

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import ImageChops, ImageStat

from app.core.config import get_settings
from app.services.fonts import collect_fonts

URL = "/api/v1/pdf/compress"


def post(client, data: bytes, url: str = URL, **form):
    return client.post(url, files={"file": ("Bericht Ä.pdf", data, "application/pdf")}, data=form)


def fonts_of(data: bytes):
    with pikepdf.open(io.BytesIO(data)) as pdf:
        return [(str(r.font.BaseFont), r.embedded, list(r.font.get("/Widths", []))) for r in collect_fonts(pdf)]


def render(data: bytes):
    doc = pdfium.PdfDocument(data)
    img = doc[0].render(scale=1.5, crop=(50, 600, 250, 20)).to_pil().convert("L")
    doc.close()
    return img


@pytest.mark.parametrize("level,dpi", [("low", 72), ("medium", 150), ("high", 300)])
def test_image_is_downsampled(client, image_pdf, level, dpi):
    r = post(client, image_pdf, level=level)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["x-result"] == "compressed"
    assert int(r.headers["x-result-size"]) < len(image_pdf)
    assert r.headers["x-images-downsampled"] == "1"

    analysis = post(client, r.content, url="/api/v1/pdf/analyze").json()
    assert analysis["images"][0]["effective_dpi"] == pytest.approx(dpi, rel=0.02)
    assert all(f["status"] == "already_embedded" for f in analysis["fonts"])


def test_standard_fonts_get_embedded(client, text_pdf):
    assert not any(embedded for _, embedded, _ in fonts_of(text_pdf))
    r = post(client, text_pdf)
    assert r.status_code == 200
    assert r.headers["x-result"] in ("compressed", "fonts_only")
    assert all(embedded for _, embedded, _ in fonts_of(r.content))
    # Text sieht nach dem Einbetten (nahezu) gleich aus
    diff = ImageChops.difference(render(text_pdf), render(r.content))
    assert ImageStat.Stat(diff).mean[0] < 8


def test_unknown_font_uses_fallback_and_keeps_widths(client, unknown_font_pdf):
    before = fonts_of(unknown_font_pdf)
    assert not any(embedded for _, embedded, _ in before)

    r = post(client, unknown_font_pdf, url=URL + "/base64", fallback_font="serif")
    assert r.status_code == 200
    body = r.json()
    fonts = body["report"]["fonts"]
    assert {f["status"] for f in fonts} == {"fallback"}
    assert all(f["used_font"].startswith("LiberationSerif") for f in fonts)

    after = fonts_of(base64.b64decode(body["content_base64"]))
    assert all(embedded for _, embedded, _ in after)
    assert [w for *_, w in after] == [w for *_, w in before]


def test_embed_fonts_can_be_disabled(client, text_pdf):
    r = post(client, text_pdf, embed_fonts="false")
    assert r.status_code == 200
    content = r.content if r.headers["x-result"] != "original" else text_pdf
    assert not any(embedded for _, embedded, _ in fonts_of(content))


def test_already_optimal_returns_original(client, image_pdf):
    first = post(client, image_pdf, level="medium").content
    second = post(client, first, level="medium")
    assert second.headers["x-result"] == "original"
    assert second.content == first


def test_base64_matches_file_endpoint(client, image_pdf):
    file_resp = post(client, image_pdf, level="low")
    b64_resp = post(client, image_pdf, url=URL + "/base64", level="low")
    body = b64_resp.json()
    assert base64.b64decode(body["content_base64"]) == file_resp.content
    assert body["filename"] == "Bericht Ä_compressed.pdf"
    assert body["report"]["result"] == file_resp.headers["x-result"]
    assert "filename*=UTF-8''Bericht%20%C3%84_compressed.pdf" in file_resp.headers["content-disposition"]


def test_invalid_pdf(client):
    assert post(client, b"%PDF-1.4 kaputt").status_code == 400
    assert post(client, b"kein pdf").status_code == 400


def test_encrypted_pdf(client, encrypted_pdf):
    assert post(client, encrypted_pdf).status_code == 422


def test_too_large(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    assert post(client, b"%PDF-" + b"0" * (2 * 1024 * 1024)).status_code == 413


def test_invalid_level(client, text_pdf):
    assert post(client, text_pdf, level="ultra").status_code == 422


def test_frontend_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert client.get("/api/v1/config").json()["max_upload_mb"] == get_settings().max_upload_mb


def test_static_files(client):
    assert client.get("/static/index.html").status_code == 200
    # Logo wird nicht mitgeliefert -> 404, das Frontend blendet es dann aus
    assert client.get("/static/logo.svg").status_code in (200, 404)
