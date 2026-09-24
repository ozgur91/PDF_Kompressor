import base64
import io

import pikepdf
import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.core import integrity
from app.core.config import get_settings
from app.core.security import SecurityMiddleware, safe_filename_stem
from app.services import sanitizer

URL = "/api/v1/pdf/compress"


def post(client, data: bytes, url: str = URL, filename: str = "test.pdf", **form):
    return client.post(url, files={"file": (filename, data, "application/pdf")}, data=form)


def active_items(data: bytes) -> list[str]:
    with pikepdf.open(io.BytesIO(data)) as pdf:
        return sanitizer.find_active_content(pdf)


# ------------------------------------------------------------ aktive Inhalte


def test_active_content_is_removed(client, active_pdf):
    assert len(active_items(active_pdf)) >= 7

    r = post(client, active_pdf, url=URL + "/base64")
    assert r.status_code == 200
    body = r.json()
    report = body["report"]
    assert report["result"] in ("compressed", "sanitized")
    removed = " | ".join(report["active_content_removed"])
    for needle in ("JavaScript", "Launch", "eingebettete Dateien", "FileAttachment", "Lesezeichen", "automatische Aktionen"):
        assert needle in removed, needle

    out = base64.b64decode(body["content_base64"])
    assert active_items(out) == []
    with pikepdf.open(io.BytesIO(out)) as pdf:
        assert "/OpenAction" not in pdf.Root
        assert len(pdf.attachments) == 0
        annots = pdf.pages[0].Annots
        # der harmlose URI-Link bleibt, die Launch-Aktion ist weg, die Dateianlage entfernt
        uris = [str(a.A.URI) for a in annots if "/A" in a and a.A.get("/S") == pikepdf.Name.URI]
        assert uris == ["https://www.aok.de/"]
        assert all(a.get("/Subtype") != pikepdf.Name.FileAttachment for a in annots)
    assert int(post(client, active_pdf).headers["x-active-content-removed"]) >= 7


def test_active_content_kept_on_request(client, active_pdf):
    r = post(client, active_pdf, url=URL + "/base64", remove_active_content="false")
    assert r.status_code == 200
    report = r.json()["report"]
    assert report["active_content_removed"] == []
    assert any("aktive Inhalte" in w for w in report["warnings"])


def test_analyze_lists_active_content(client, active_pdf):
    r = post(client, active_pdf, url="/api/v1/pdf/analyze")
    assert r.status_code == 200
    assert len(r.json()["active_content"]) >= 7


def test_fail_closed_when_sanitizing_fails(client, active_pdf, monkeypatch):
    def broken(_pdf):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(sanitizer, "remove_active_content", broken)
    r = post(client, active_pdf)
    assert r.status_code == 422
    assert "aktive Inhalte" in r.json()["detail"]
    assert "kaputt" not in r.text


# ------------------------------------------------------------ präparierte Dateien


def test_flate_bomb_is_rejected(client, flate_bomb_pdf, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_stream_mb", 5)
    r = post(client, flate_bomb_pdf)
    assert r.status_code == 422
    assert "Sicherheitsgrenzen" in r.json()["detail"]


def test_flate_bomb_total_budget(client, flate_bomb_pdf, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_total_decoded_mb", 10)
    assert post(client, flate_bomb_pdf).status_code == 422


def test_pixel_bomb_is_rejected(client, pixel_bomb_pdf):
    r = post(client, pixel_bomb_pdf)
    assert r.status_code == 422
    assert post(client, pixel_bomb_pdf, url="/api/v1/pdf/analyze").status_code == 422


def test_timeout(client, image_pdf, monkeypatch):
    monkeypatch.setattr(get_settings(), "processing_timeout_s", 0)
    r = post(client, image_pdf)
    assert r.status_code == 422
    assert "zu lange" in r.json()["detail"]


def test_too_many_pages(client, text_pdf, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_pages", 0)
    assert post(client, text_pdf).status_code == 422


# ------------------------------------------------------------ Web-Sicherheit


@pytest.mark.parametrize("path", ["/", "/api/v1/config", "/static/app.js"])
def test_security_headers(client, path):
    r = client.get(path)
    assert r.status_code == 200
    h = r.headers
    assert "script-src 'self'" in h["content-security-policy"]
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer"
    assert h["cache-control"] == "no-store"
    assert len(h["x-request-id"]) == 16
    assert "server" not in h


def test_security_headers_on_errors(client):
    r = post(client, b"kein pdf")
    assert r.status_code == 400
    assert r.headers["x-content-type-options"] == "nosniff"


def test_frontend_has_no_inline_code(client):
    html = client.get("/").text
    assert "<script>" not in html and "<style>" not in html
    assert "style=" not in html and "onerror=" not in html and "onclick=" not in html
    assert "style=" not in client.get("/static/app.js").text


def test_cors_disabled_by_default(client):
    r = client.get("/api/v1/config", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    pre = client.options(URL, headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in pre.headers


def test_docs_disabled_by_default(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
    assert client.get("/api/v1/config").json()["docs_enabled"] is False


def test_untrusted_host_rejected():
    from app.main import app

    evil = TestClient(app, base_url="http://evil.example")
    assert evil.get("/health").status_code == 400


def test_error_messages_are_generic(client):
    r = post(client, b"%PDF-1.4 kaputt")
    assert r.status_code == 400
    assert r.json()["detail"] == "Die Datei ist keine gültige PDF"


def test_filename_is_sanitized(client, text_pdf):
    r = post(client, text_pdf, filename='..\\..\\evil"; x=1.pdf')
    assert r.status_code == 200
    cd = r.headers["content-disposition"]
    assert '"; x=1' not in cd and ".." not in cd and "\\" not in cd
    assert safe_filename_stem("a\r\nSet-Cookie: x.pdf") == "a__Set-Cookie_ x"
    assert safe_filename_stem("../../etc/passwd") == "passwd"
    assert safe_filename_stem("Bericht Ä.pdf") == "Bericht Ä"
    assert safe_filename_stem("") == "document"
    assert len(safe_filename_stem("x" * 500)) == 100


# ------------------------------------------------------------ Formular-Limits


def test_multiple_files_rejected(client, text_pdf):
    r = client.post(URL, files=[("file", ("a.pdf", text_pdf, "application/pdf")), ("file", ("b.pdf", text_pdf, "application/pdf"))])
    assert r.status_code == 400


def test_wrong_field_name_rejected(client, text_pdf):
    r = client.post(URL, files={"upload": ("a.pdf", text_pdf, "application/pdf")})
    assert r.status_code == 400


def test_unknown_field_rejected(client, text_pdf):
    r = post(client, text_pdf, evil="1")
    assert r.status_code == 422
    assert "1" not in str(r.json()["detail"][0].get("input", ""))


def test_too_many_fields_rejected(client, text_pdf):
    data = {f"f{i}": "x" for i in range(20)}
    assert post(client, text_pdf, **data).status_code == 400


def test_non_multipart_rejected(client):
    assert client.post(URL, json={"file": "x"}).status_code == 415


def test_body_limit_without_content_length():
    """Chunked-Upload ohne Content-Length wird trotzdem beim Empfang begrenzt."""

    async def echo(request: Request):
        body = await request.body()
        return PlainTextResponse(str(len(body)))

    tiny = Starlette(routes=[Route("/", echo, methods=["POST"])])
    tiny.add_middleware(SecurityMiddleware, max_body=10, hsts=False)
    c = TestClient(tiny)

    def gen():
        yield b"x" * 8
        yield b"x" * 8

    assert c.post("/", content=gen()).status_code == 413
    assert c.post("/", content=b"x" * 11).status_code == 413     # mit Content-Length
    assert c.post("/", content=b"x" * 5).text == "5"


def test_hsts_optional():
    async def ok(request):
        return PlainTextResponse("ok")

    tiny = Starlette(routes=[Route("/", ok)])
    tiny.add_middleware(SecurityMiddleware, max_body=10, hsts=True)
    assert "max-age" in TestClient(tiny).get("/").headers["strict-transport-security"]


# ------------------------------------------------------------ Integrität


def test_bundled_fonts_integrity():
    integrity.verify_bundled_fonts()


def test_tampered_font_detected(tmp_path, monkeypatch):
    sums = tmp_path / "SHA256SUMS"
    lines = integrity.CHECKSUM_FILE.read_text(encoding="ascii").splitlines()
    digest, name = lines[0].split(maxsplit=1)
    lines[0] = f"{'0' * 64} {name}"
    sums.write_text("\n".join(lines), encoding="ascii")
    monkeypatch.setattr(integrity, "CHECKSUM_FILE", sums)
    with pytest.raises(integrity.IntegrityError):
        integrity.verify_bundled_fonts()


# ------------------------------------------------------------ große Dokumente


def _pages_pdf(n: int) -> bytes:
    pdf = pikepdf.new()
    for i in range(n):
        page = pdf.add_blank_page(page_size=(595, 842))
        page.Contents = pdf.make_stream(f"BT /F1 12 Tf 72 720 Td (Seite {i + 1}) Tj ET".encode())
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def test_large_page_count_is_accepted(client):
    """Dokumente mit mehreren hundert bis tausend Seiten sind der Normalfall."""
    assert get_settings().max_pages >= 5000
    r = post(client, _pages_pdf(1200), url="/api/v1/pdf/analyze")
    assert r.status_code == 200
    assert r.json()["pages"] == 1200


def _image_pdf(width: int, height: int, raw: bytes) -> bytes:
    import zlib

    pdf = pikepdf.new()
    img = pikepdf.Stream(pdf, zlib.compress(raw, 6))
    img.Type, img.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
    img.Width, img.Height, img.BitsPerComponent = width, height, 8
    img.ColorSpace, img.Filter = pikepdf.Name.DeviceGray, pikepdf.Name.FlateDecode
    page = pdf.add_blank_page(page_size=(595, 842))
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=img))
    page.Contents = pdf.make_stream(b"q 595 0 0 842 0 0 cm /Im0 Do Q")
    out = io.BytesIO()
    pdf.save(out, compress_streams=False)
    return out.getvalue()


def test_big_image_allowed_by_its_dimensions(client, monkeypatch):
    """Ein Bild darf so groß entpacken, wie seine Maße es verlangen - auch über dem Stream-Limit."""
    monkeypatch.setattr(get_settings(), "max_stream_mb", 1)
    w, h = 2000, 1500                      # 3 MB Graustufen > 1 MB Stream-Limit
    r = post(client, _image_pdf(w, h, bytes([200]) * (w * h)), url="/api/v1/pdf/analyze")
    assert r.status_code == 200


def test_image_bomb_beyond_dimensions_rejected(client, monkeypatch):
    """Ein Bild, das deutlich mehr Daten entpackt als seine Maße erlauben, ist eine getarnte Bombe."""
    monkeypatch.setattr(get_settings(), "max_stream_mb", 1)
    w, h = 100, 100                        # erlaubt wären ~10 KB (bzw. das Stream-Limit von 1 MB)
    r = post(client, _image_pdf(w, h, bytes(20 * 1024 * 1024)))
    assert r.status_code == 422
