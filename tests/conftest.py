import io
import zlib

import pikepdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.main import app


@pytest.fixture(scope="session")
def client() -> TestClient:
    # "localhost" ist ein erlaubter Host (TrustedHostMiddleware)
    return TestClient(app, base_url="http://localhost")


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


@pytest.fixture(scope="session")
def active_pdf(text_pdf: bytes) -> bytes:
    """PDF mit typischen aktiven Inhalten plus einem harmlosen URI-Link, der erhalten bleiben muss."""
    with pikepdf.open(io.BytesIO(text_pdf)) as pdf:
        js = pdf.make_indirect(pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert('x')")))
        pdf.Root.OpenAction = js
        pdf.Root.AA = pikepdf.Dictionary(WC=js)
        attachment = pikepdf.AttachedFileSpec(pdf, b"MZ-kein-echtes-Programm", filename="tool.exe")
        pdf.attachments["tool.exe"] = attachment
        pdf.Root.Names.JavaScript = pikepdf.Dictionary(Names=pikepdf.Array([pikepdf.String("init"), js]))
        page = pdf.pages[0].obj
        page.AA = pikepdf.Dictionary(O=js)
        rect = pikepdf.Array([72, 72, 200, 100])
        launch = pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link, Rect=rect,
                                    A=pikepdf.Dictionary(S=pikepdf.Name.Launch, F=pikepdf.String("cmd.exe")))
        uri = pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link, Rect=rect,
                                 A=pikepdf.Dictionary(S=pikepdf.Name.URI, URI=pikepdf.String("https://www.aok.de/")))
        fileannot = pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.FileAttachment, Rect=rect,
                                       FS=attachment.obj)
        page.Annots = pdf.make_indirect(pikepdf.Array([pdf.make_indirect(launch), pdf.make_indirect(uri), pdf.make_indirect(fileannot)]))
        outline = pdf.make_indirect(pikepdf.Dictionary(Title=pikepdf.String("Start"), A=js))
        pdf.Root.Outlines = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Outlines, First=outline, Last=outline, Count=1))
        outline.Parent = pdf.Root.Outlines
        out = io.BytesIO()
        pdf.save(out)
    return out.getvalue()


def _with_xobject(base: bytes, stream_factory) -> bytes:
    with pikepdf.open(io.BytesIO(base)) as pdf:
        page = pdf.pages[0].obj
        res = page.Resources
        if "/XObject" not in res:
            res.XObject = pikepdf.Dictionary()
        res.XObject.Bomb = stream_factory(pdf)
        out = io.BytesIO()
        pdf.save(out, compress_streams=False)
    return out.getvalue()


@pytest.fixture(scope="session")
def flate_bomb_pdf(text_pdf: bytes) -> bytes:
    """20 MB Nullen, als Flate-Stream nur ca. 20 KB groß (Test setzt das Limit auf 5 MB)."""
    comp = zlib.compressobj(9)
    raw = b"".join(comp.compress(bytes(1024 * 1024)) for _ in range(20)) + comp.flush()

    def make(pdf):
        s = pikepdf.Stream(pdf, raw)
        s.Filter = pikepdf.Name.FlateDecode
        s.Type, s.Subtype, s.BBox = pikepdf.Name.XObject, pikepdf.Name.Form, pikepdf.Array([0, 0, 1, 1])
        return s

    return _with_xobject(text_pdf, make)


@pytest.fixture(scope="session")
def pixel_bomb_pdf(text_pdf: bytes) -> bytes:
    """Bild mit angeblich 50.000 x 50.000 Pixeln (2,5 Mrd.), aber nur wenigen Bytes Daten."""

    def make(pdf):
        s = pikepdf.Stream(pdf, bytes([0xFF, 0xD8, 0xFF, 0xD9]))
        s.Type, s.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
        s.Width, s.Height, s.BitsPerComponent = 50000, 50000, 8
        s.ColorSpace, s.Filter = pikepdf.Name.DeviceRGB, pikepdf.Name.DCTDecode
        return s

    return _with_xobject(text_pdf, make)
