"""Effektive Bild-Auflösung ermitteln und Bilder auf die Ziel-DPI herunterrechnen."""

import logging
import math
import warnings
import zlib
from dataclasses import dataclass, field
from io import BytesIO

import pikepdf
from PIL import Image
from pikepdf import Array, Dictionary, Name

from app.core.config import get_settings
from app.models import ImageInfo
from app.services.guard import check_deadline
from app.services.pdfwalk import page_resources

log = logging.getLogger(__name__)

# Pixel-Bomben: Pillow bricht über dieser Grenze ab (Warnung wird zum Fehler)
Image.MAX_IMAGE_PIXELS = get_settings().max_image_pixels
warnings.simplefilter("error", Image.DecompressionBombWarning)

Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1, 0, 0, 1, 0, 0)
MAX_FORM_DEPTH = 12

UNSUPPORTED_FILTERS = {"/JBIG2Decode", "/JPXDecode", "/CCITTFaxDecode"}
SUPPORTED_COLORSPACES = {"/DeviceRGB", "/DeviceGray"}


def _mul(m: Matrix, n: Matrix) -> Matrix:
    """m × n (PDF-Konvention: Zeilenvektoren)."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


@dataclass
class ImageUsage:
    obj: pikepdf.Stream
    min_dpi: float = math.inf      # kleinste effektive DPI = größte Darstellung
    uses: int = 0


@dataclass
class PlacementMap:
    images: dict[tuple[int, int], ImageUsage] = field(default_factory=dict)


def _walk(stream_owner, resources: Dictionary | None, ctm: Matrix, pmap: PlacementMap, depth: int, visiting: set) -> None:
    if resources is None or depth > MAX_FORM_DEPTH:
        return
    xobjects = resources.get("/XObject")
    if not isinstance(xobjects, Dictionary):
        return
    try:
        ops = pikepdf.parse_content_stream(stream_owner, "q Q cm Do")
    except pikepdf.PdfError as exc:
        log.debug("Content-Stream nicht lesbar: %s", exc)
        return

    stack: list[Matrix] = []
    for i, (operands, operator) in enumerate(ops):
        if i % 2048 == 0:
            check_deadline()
        op = str(operator)
        if op == "q":
            stack.append(ctm)
        elif op == "Q":
            if stack:
                ctm = stack.pop()
        elif op == "cm" and len(operands) == 6:
            ctm = _mul(tuple(float(x) for x in operands), ctm)
        elif op == "Do" and operands:
            xobj = xobjects.get(str(operands[0]))
            if not isinstance(xobj, pikepdf.Stream):
                continue
            subtype = xobj.get("/Subtype")
            if subtype == Name.Image:
                _record(xobj, ctm, pmap)
            elif subtype == Name.Form:
                key = xobj.objgen
                if key in visiting:
                    continue
                matrix = tuple(float(x) for x in xobj.get("/Matrix", Array(IDENTITY)))
                form_res = xobj.get("/Resources", resources)
                visiting.add(key)
                _walk(xobj, form_res, _mul(matrix, ctm), pmap, depth + 1, visiting)
                visiting.discard(key)


def _record(img: pikepdf.Stream, ctm: Matrix, pmap: PlacementMap) -> None:
    a, b, c, d, _, _ = ctm
    w_pt, h_pt = math.hypot(a, b), math.hypot(c, d)
    if w_pt < 1e-3 or h_pt < 1e-3:
        return
    width, height = int(img.get("/Width", 0)), int(img.get("/Height", 0))
    if not width or not height:
        return
    dpi = min(width / (w_pt / 72), height / (h_pt / 72))
    usage = pmap.images.setdefault(img.objgen, ImageUsage(img))
    usage.min_dpi = min(usage.min_dpi, dpi)
    usage.uses += 1


def build_placement_map(pdf: pikepdf.Pdf) -> PlacementMap:
    pmap = PlacementMap()
    for page in pdf.pages:
        check_deadline()
        user_unit = float(page.obj.get("/UserUnit", 1))
        _walk(page.obj, page_resources(page.obj), (user_unit, 0, 0, user_unit, 0, 0), pmap, 0, set())
    return pmap


# ---------------------------------------------------------------- Downsampling


def _filters(img: pikepdf.Stream) -> list[str]:
    f = img.get("/Filter")
    if f is None:
        return []
    return [str(x) for x in f] if isinstance(f, Array) else [str(f)]


def _colorspace_ok(img: pikepdf.Stream) -> tuple[bool, str | None]:
    cs = img.get("/ColorSpace")
    if cs is None:
        return False, "kein Farbraum"
    if isinstance(cs, Name):
        return (str(cs) in SUPPORTED_COLORSPACES, None if str(cs) in SUPPORTED_COLORSPACES else f"Farbraum {cs}")
    if isinstance(cs, Array) and len(cs) == 2 and cs[0] == Name.ICCBased:
        n = int(cs[1].get("/N", 0))
        return (n in (1, 3), None if n in (1, 3) else f"ICC mit {n} Kanälen")
    name = str(cs[0]) if isinstance(cs, Array) and len(cs) else str(cs)
    return False, f"Farbraum {name}"


def skip_reason(img: pikepdf.Stream) -> str | None:
    if img.get("/ImageMask", False):
        return "Bildmaske"
    if int(img.get("/BitsPerComponent", 8)) != 8:
        return f"{int(img.get('/BitsPerComponent', 0))} Bit pro Kanal"
    bad = UNSUPPORTED_FILTERS.intersection(_filters(img))
    if bad:
        return f"Filter {', '.join(sorted(bad))}"
    if "/Decode" in img:
        return "Decode-Array"
    if "/Mask" in img:
        return "Farbschlüssel-/Stencil-Maske"
    ok, reason = _colorspace_ok(img)
    if not ok:
        return reason
    smask = img.get("/SMask")
    if smask is not None:
        if not isinstance(smask, pikepdf.Stream) or int(smask.get("/BitsPerComponent", 8)) != 8 or "/Matte" in smask:
            return "nicht unterstützte Transparenzmaske"
        if UNSUPPORTED_FILTERS.intersection(_filters(smask)):
            return "Transparenzmaske mit nicht unterstütztem Filter"
    return None


def _encode_jpeg(im: Image.Image, quality: int) -> bytes:
    buf = BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True, progressive=False)
    return buf.getvalue()


def _raw_len(s: pikepdf.Stream) -> int:
    return len(s.read_raw_bytes())


def downsample_image(pdf: pikepdf.Pdf, img: pikepdf.Stream, scale: float, quality: int) -> bool:
    """Rechnet ein Bild mit Faktor `scale` (< 1) herunter. True, wenn ersetzt."""
    pil = pikepdf.PdfImage(img).as_pil_image()
    if pil.mode not in ("RGB", "L"):
        return False
    new_size = (max(1, round(pil.width * scale)), max(1, round(pil.height * scale)))
    # reducing_gap: erst schnell ganzzahlig vorverkleinern, dann Lanczos - bei großen Scans ein Vielfaches schneller
    resized = pil.resize(new_size, Image.Resampling.LANCZOS, reducing_gap=3.0)

    candidates = [(_encode_jpeg(resized, quality), Name.DCTDecode)]
    if "/DCTDecode" not in _filters(img):
        # verlustfreie Bilder (Screenshots, Grafiken) sind mit Flate oft kleiner und bleiben scharf
        candidates.append((zlib.compress(resized.tobytes(), 9), Name.FlateDecode))
    data, filt = min(candidates, key=lambda c: len(c[0]))

    smask = img.get("/SMask")
    new_mask = None
    old_size = _raw_len(img)
    new_total = len(data)
    if smask is not None:
        mask_pil = pikepdf.PdfImage(smask).as_pil_image().convert("L").resize(new_size, Image.Resampling.LANCZOS, reducing_gap=3.0)
        new_mask = zlib.compress(mask_pil.tobytes(), 9)
        old_size += _raw_len(smask)
        new_total += len(new_mask)

    if new_total >= old_size:
        return False

    img.write(data, filter=filt)
    for key in ("/DecodeParms", "/Interpolate"):
        if key in img:
            del img[key]
    img.Width, img.Height = new_size
    img.BitsPerComponent = 8
    if isinstance(img.ColorSpace, Name):
        img.ColorSpace = Name.DeviceRGB if resized.mode == "RGB" else Name.DeviceGray

    if new_mask is not None:
        smask.write(new_mask, filter=Name.FlateDecode)
        if "/DecodeParms" in smask:
            del smask["/DecodeParms"]
        smask.Width, smask.Height = new_size
        smask.BitsPerComponent = 8
        smask.ColorSpace = Name.DeviceGray
    return True


def downsample_images(pdf: pikepdf.Pdf, target_dpi: int, quality: int, threshold: float) -> tuple[int, list[str]]:
    pmap = build_placement_map(pdf)
    count, notes = 0, []
    for usage in pmap.images.values():
        check_deadline()
        if usage.min_dpi <= target_dpi * threshold:
            continue
        if skip_reason(usage.obj) is not None:
            continue
        try:
            if downsample_image(pdf, usage.obj, target_dpi / usage.min_dpi, quality):
                count += 1
        except Exception as exc:
            log.warning("Bild %s nicht verarbeitet: %s", usage.obj.objgen, exc)
            notes.append("Ein Bild konnte nicht verkleinert werden und bleibt unverändert")
    return count, notes


def analyze_images(pdf: pikepdf.Pdf) -> list[ImageInfo]:
    pmap = build_placement_map(pdf)
    infos = []
    for usage in pmap.images.values():
        img = usage.obj
        filters = _filters(img)
        infos.append(
            ImageInfo(
                width=int(img.get("/Width", 0)),
                height=int(img.get("/Height", 0)),
                effective_dpi=round(usage.min_dpi, 1) if math.isfinite(usage.min_dpi) else None,
                filter=",".join(f.lstrip("/") for f in filters) or None,
                skip_reason=skip_reason(img),
            )
        )
    return infos
