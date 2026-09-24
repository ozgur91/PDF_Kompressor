"""Schutz vor präparierten Dateien: Zeitbudget und Obergrenzen für entpackte Daten.

Bevor eine PDF verarbeitet wird, prüft `inspect_streams` jeden Stream der Datei, ohne ihn
vollständig zu entpacken. So werden Dekompressions-Bomben (wenige KB, die zu GB aufgehen) und
Pixel-Bomben (riesige Bildmaße) abgelehnt, bevor qpdf, Pillow oder pdfium sie anfassen.
"""

import base64
import binascii
import time
import zlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import pikepdf
from pikepdf import Array, Name

from app.core.config import Settings

MB = 1024 * 1024
# größtmögliche Ausdehnung pro Filter (für Filter, die nicht gestreamt geprüft werden)
LZW_MAX_RATIO = 2730          # 12-Bit-Code kann bis zu 4096 Bytes ergeben
RUNLENGTH_MAX_RATIO = 64      # 2 Byte -> 128 Byte
WHITESPACE = bytes([0x20, 0x09, 0x0A, 0x0D, 0x0C, 0x00])
IMAGE_FILTERS = {"/DCTDecode", "/JPXDecode", "/JBIG2Decode", "/CCITTFaxDecode"}


class ProcessingTimeout(Exception):
    """Das Zeitbudget für die Datei ist aufgebraucht."""


class ResourceLimitExceeded(Exception):
    """Die Datei überschreitet eine Sicherheitsgrenze (Größe, Pixel, Anzahl)."""


@dataclass
class Guard:
    deadline: float
    max_stream: int
    max_total: int
    max_pixels: int
    used_total: int = 0


_current: ContextVar[Guard | None] = ContextVar("pdf_guard", default=None)


@contextmanager
def guarded(settings: Settings):
    """Aktiviert Zeitbudget und Limits für den aktuellen Verarbeitungsvorgang."""
    token = _current.set(
        Guard(
            deadline=time.monotonic() + settings.processing_timeout_s,
            max_stream=settings.max_stream_mb * MB,
            max_total=settings.max_total_decoded_mb * MB,
            max_pixels=settings.max_image_pixels,
        )
    )
    try:
        yield
    finally:
        _current.reset(token)


def check_deadline() -> None:
    """In Schleifen aufrufen; wirft ProcessingTimeout, wenn das Zeitbudget abgelaufen ist."""
    guard = _current.get()
    if guard is not None and time.monotonic() > guard.deadline:
        raise ProcessingTimeout()


def _filters(stream: pikepdf.Stream) -> list[str]:
    f = stream.get("/Filter")
    if f is None:
        return []
    return [str(x) for x in f] if isinstance(f, Array) else [str(f)]


def _inflate_size(data: bytes, limit: int, keep: bool) -> tuple[int, bytes | None]:
    """Entpackt Flate-Daten gestreamt und bricht ab, sobald `limit` überschritten ist.

    Rückgabe: (Größe, Daten). Daten nur mit keep=True, wenn danach ein weiterer Filter folgt.
    """
    d = zlib.decompressobj()
    size, out = 0, []
    chunk = data
    try:
        while chunk:
            piece = d.decompress(chunk, MB)
            size += len(piece)
            if size > limit:
                return size, None
            if keep:
                out.append(piece)
            chunk = d.unconsumed_tail
            if not piece and not chunk:
                break
        rest = d.flush()
    except zlib.error:
        # beschädigte Daten: qpdf/pdfium lesen nur, was dekodierbar ist -> bisherige Größe zählt
        return size, (b"".join(out) if keep else None)
    size += len(rest)
    if keep:
        out.append(rest)
    return size, (b"".join(out) if keep else None)


def _hex(data: bytes) -> bytes:
    digits = bytes(c for c in data.split(b">", 1)[0] if c in b"0123456789abcdefABCDEF")
    if len(digits) % 2:
        digits += b"0"
    return binascii.unhexlify(digits)


def _a85(data: bytes) -> bytes:
    body = data.strip()
    if body.startswith(b"<~"):
        body = body[2:]
    body = body.split(b"~>", 1)[0]
    return base64.a85decode(body, ignorechars=WHITESPACE)


def decoded_size_bound(stream: pikepdf.Stream, limit: int) -> int:
    """Obere Schranke der entpackten Größe eines Streams (> limit bedeutet: zu groß).

    Flate wird wirklich (gestreamt, bis limit) entpackt, ASCII-Filter werden dekodiert,
    für LZW und RunLength wird die maximale Ausdehnung angenommen.
    """
    filters = _filters(stream)
    data: bytes | None = stream.read_raw_bytes()
    size = len(data)
    for i, f in enumerate(filters):
        more = i + 1 < len(filters)
        if f in ("/FlateDecode", "/Fl"):
            if data is None:
                return limit + 1  # nicht prüfbar -> vorsichtshalber als zu groß werten
            size, data = _inflate_size(data, limit, keep=more)
        elif f in ("/ASCIIHexDecode", "/AHx"):
            try:
                data = _hex(data) if data is not None else None
                size = len(data) if data is not None else size // 2 + 1
            except (binascii.Error, ValueError):
                data, size = None, size // 2 + 1
        elif f in ("/ASCII85Decode", "/A85"):
            try:
                data = _a85(data) if data is not None else None
                size = len(data) if data is not None else size
            except ValueError:
                data = None
        elif f in ("/LZWDecode", "/LZW"):
            size, data = size * LZW_MAX_RATIO, None
        elif f in ("/RunLengthDecode", "/RL"):
            size, data = size * RUNLENGTH_MAX_RATIO, None
        elif f in IMAGE_FILTERS:
            break  # Bildfilter: Größe wird über die Pixelgrenze geprüft
        else:
            return limit + 1  # unbekannter Filter
        if size > limit:
            return size
    return size


def _components(colorspace) -> int:
    """Farbkanäle eines Bild-Farbraums (unbekannt -> 4, also großzügig nach oben)."""
    if isinstance(colorspace, Name):
        return {"/DeviceGray": 1, "/G": 1, "/DeviceRGB": 3, "/RGB": 3, "/DeviceCMYK": 4, "/CMYK": 4}.get(str(colorspace), 4)
    if isinstance(colorspace, Array) and len(colorspace):
        kind = str(colorspace[0])
        if kind in ("/Indexed", "/I", "/Separation", "/Pattern"):
            return 1
        if kind == "/ICCBased" and len(colorspace) > 1:
            try:
                return int(colorspace[1].get("/N", 4))
            except (TypeError, ValueError):
                return 4
        if kind == "/DeviceN" and len(colorspace) > 1 and isinstance(colorspace[1], Array):
            return max(1, len(colorspace[1]))
        if kind in ("/CalRGB", "/Lab"):
            return 3
    return 4


def _image_limit(obj: pikepdf.Stream, pixels: int, max_stream: int) -> int:
    """Erlaubte entpackte Größe eines Bildes: so groß, wie seine Maße es verlangen (+10 %), mindestens max_stream.

    Echte hochauflösende Scans passen so immer, eine als Bild getarnte Bombe nicht.
    """
    try:
        bpc = int(obj.get("/BitsPerComponent", 8))
    except (TypeError, ValueError):
        bpc = 8
    bpc = bpc if 1 <= bpc <= 16 else 16
    comps = 1 if obj.get("/ImageMask", False) else _components(obj.get("/ColorSpace"))
    expected = pixels * comps * bpc // 8 + pixels // 8   # + Zeilen-Prädiktor-Bytes
    return max(max_stream, int(expected * 1.1))


def inspect_streams(pdf: pikepdf.Pdf, settings: Settings) -> None:
    """Prüft alle Objekte der Datei gegen die Sicherheitsgrenzen. Wirft ResourceLimitExceeded."""
    guard = _current.get()
    max_stream = guard.max_stream if guard else settings.max_stream_mb * MB
    max_total = guard.max_total if guard else settings.max_total_decoded_mb * MB
    max_pixels = guard.max_pixels if guard else settings.max_image_pixels

    total = objects = images = fonts = 0
    for obj in pdf.objects:
        objects += 1
        if objects > settings.max_objects:
            raise ResourceLimitExceeded("zu viele Objekte")
        if objects % 256 == 0:
            check_deadline()
        if isinstance(obj, pikepdf.Dictionary) and obj.get("/Type") == Name.Font:
            fonts += 1
            if fonts > settings.max_fonts:
                raise ResourceLimitExceeded("zu viele Schriften")
        if not isinstance(obj, pikepdf.Stream):
            continue
        limit = max_stream
        if obj.get("/Subtype") == Name.Image:
            images += 1
            if images > settings.max_images:
                raise ResourceLimitExceeded("zu viele Bilder")
            try:
                pixels = int(obj.get("/Width", 0)) * int(obj.get("/Height", 0))
            except (TypeError, ValueError):
                raise ResourceLimitExceeded("ungültige Bildmaße") from None
            if pixels > max_pixels or pixels < 0:
                raise ResourceLimitExceeded("Bild mit zu vielen Pixeln")
            limit = _image_limit(obj, pixels, max_stream)
        size = decoded_size_bound(obj, limit)
        if size > limit:
            raise ResourceLimitExceeded("Stream entpackt zu groß")
        total += size
        if total > max_total:
            raise ResourceLimitExceeded("Datei entpackt zu groß")
    if guard is not None:
        guard.used_total = total
