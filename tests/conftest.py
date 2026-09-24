import io

import pikepdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.main import app


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(app)


def _photo(width: int, height: int) -> bytes:
    im = Image.new("RGB", (width, height))
    px = im.load()
    for y in range(height):
        for x in range(width):
            px[x, y] = ((x * 255) // width, (y * 255) // height, (x ^ y) & 255)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _text_page(c: canvas.Canvas) -> None:
    c.setFont("Helvetica", 14)
    c.drawString(72, 780, "Hallo Welt – Größe äöü ß €")
    c.setFont("Times-Bold", 14)
    c.drawString(72, 760, "Times Bold Text")
    c.setFont("Courier", 12)
    c.drawString(72, 740, "Courier mono")


@pytest.fixture(scope="session")
def image_pdf() -> bytes:
    """Seite mit nicht eingebetteten Standard-Fonts und einem 600-DPI-Bild (1200x900 px auf 2x1,5 Zoll)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    _text_page(c)
    c.drawImage(ImageReader(io.BytesIO(_photo(1200, 900))), 72, 400, width=144, height=108)
    c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture(scope="session")
def text_pdf() -> bytes:
    """Reine Text-PDF mit nicht eingebetteten Standard-Fonts."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    _text_page(c)
    c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture(scope="session")
def unknown_font_pdf(text_pdf: bytes, client: TestClient) -> bytes:
    """Font mit /Widths und FontDescriptor, aber unbekanntem Namen und ohne Font-Programm."""
    r = client.post("/api/v1/pdf/compress", files={"file": ("t.pdf", text_pdf, "application/pdf")}, data={"level": "high"})
    assert r.status_code == 200
    with pikepdf.open(io.BytesIO(r.content)) as pdf:
        for font in pdf.pages[0].Resources.Font.values():
            fd = font.FontDescriptor
            for key in ("/FontFile", "/FontFile2", "/FontFile3"):
                if key in fd:
                    del fd[key]
            font.BaseFont = pikepdf.Name("/CorporateFantasy-Regular")
            fd.FontName = pikepdf.Name("/CorporateFantasy-Regular")
        out = io.BytesIO()
        pdf.save(out)
    return out.getvalue()


@pytest.fixture(scope="session")
def encrypted_pdf(text_pdf: bytes) -> bytes:
    with pikepdf.open(io.BytesIO(text_pdf)) as pdf:
        out = io.BytesIO()
        pdf.save(out, encryption=pikepdf.Encryption(user="geheim", owner="geheim"))
    return out.getvalue()
