"""Erkennung und Einbettung nicht eingebetteter Schriftarten.

Vorgehen pro nicht eingebetteter Simple-Font (Type1/TrueType):
1. passende Schriftdatei im System suchen (Name + Stil, Alias-Tabelle für die Standard-14-Fonts)
2. Einbettungs-Lizenz der Schrift prüfen (OS/2 fsType)
3. sonst Fallback auf eine mitgelieferte Liberation-Schrift
4. auf die benötigten Zeichen subsetten und als FontFile2/FontFile3 einbetten
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import pikepdf
from fontTools import agl
from fontTools.encodings.StandardEncoding import StandardEncoding
from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTCollection, TTFont
from pikepdf import Array, Dictionary, Name

from app.core.config import BUNDLED_FONT_DIR, get_settings
from app.models import FallbackFont, FontInfo, FontStatus
from app.services.guard import check_deadline
from app.services.pdfwalk import iter_resources, object_key

log = logging.getLogger(__name__)
# fontTools meldet harmlose Details (Zeitstempel, unbekannte Tabellen) sehr gesprächig
logging.getLogger("fontTools").setLevel(logging.ERROR)

SIMPLE_SUBTYPES = {"/Type1", "/TrueType", "/MMType1"}
SYMBOLIC_NAMES = ("symbol", "zapfdingbats", "dingbats", "wingdings", "webdings", "marlett")

# Standard-14 / gängige PostScript-Namen -> Familie einer verbreiteten, metrisch kompatiblen Schrift
FAMILY_ALIASES = {
    "helvetica": "arial",
    "arialmt": "arial",
    "helveticaneue": "arial",
    "times": "timesnewroman",
    "timesroman": "timesnewroman",
    "timesnewromanps": "timesnewroman",
    "courier": "couriernew",
    "couriernewps": "couriernew",
}

# Familie -> Fallback-Kategorie (Liberation ist metrisch kompatibel zu Arial/Times/Courier)
SERIF_HINTS = ("times", "serif", "roman", "georgia", "garamond", "cambria", "bookman", "palatino", "minion", "caslon", "baskerville")
MONO_HINTS = ("courier", "mono", "consol", "lucidaconsole", "menlo", "typewriter", "code")

LIBERATION = {
    FallbackFont.sans: "LiberationSans",
    FallbackFont.serif: "LiberationSerif",
    FallbackFont.mono: "LiberationMono",
}

FS_TYPE_RESTRICTED = 0x0002
FS_TYPE_NO_SUBSET = 0x0100
FS_TYPE_BITMAP_ONLY = 0x0200

FLAG_FIXED = 1
FLAG_SERIF = 2
FLAG_SYMBOLIC = 4
FLAG_NONSYMBOLIC = 32
FLAG_ITALIC = 64


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def strip_subset_prefix(name: str) -> str:
    return re.sub(r"^[A-Z]{6}\+", "", name.lstrip("/"))


@dataclass(frozen=True)
class FontRequest:
    """Was die PDF über eine Schrift aussagt."""

    base_name: str      # ohne Subset-Präfix, z. B. "Arial-BoldMT"
    family: str         # normalisiert, z. B. "arial"
    bold: bool
    italic: bool
    serif: bool
    fixed: bool
    symbolic: bool


def parse_font_request(font: Dictionary, descriptor: Dictionary | None) -> FontRequest:
    base = strip_subset_prefix(str(font.get("/BaseFont", "/Unknown")))
    lower = base.lower()
    flags = int(descriptor.get("/Flags", 0)) if descriptor is not None else 0
    weight = int(descriptor.get("/FontWeight", 400)) if descriptor is not None else 400

    style_part = re.split(r"[,-]", base, maxsplit=1)
    family_raw = style_part[0]
    family = _norm(family_raw)
    family = re.sub(r"(ps)?mt$", "", family) or family            # ArialMT, TimesNewRomanPSMT
    family = re.sub(r"(bold|italic|oblique|regular)+$", "", family) or family
    family = FAMILY_ALIASES.get(family, family)

    bold = weight >= 600 or any(k in lower for k in ("bold", "black", "heavy", "semibold", "demi"))
    italic = bool(flags & FLAG_ITALIC) or "italic" in lower or "oblique" in lower
    symbolic = any(k in _norm(base) for k in SYMBOLIC_NAMES) or (bool(flags & FLAG_SYMBOLIC) and not flags & FLAG_NONSYMBOLIC)
    serif = bool(flags & FLAG_SERIF) or any(h in family for h in SERIF_HINTS)
    fixed = bool(flags & FLAG_FIXED) or any(h in family for h in MONO_HINTS)
    return FontRequest(base, family, bold, italic, serif, fixed, symbolic)


# ---------------------------------------------------------------- Analyse


@dataclass
class PdfFontRef:
    font: Dictionary
    descriptor: Dictionary | None
    subtype: str
    embedded: bool


def _font_descriptor(font: Dictionary) -> tuple[Dictionary | None, str]:
    subtype = str(font.get("/Subtype", "/Unknown"))
    if subtype == "/Type0":
        descendants = font.get("/DescendantFonts")
        if isinstance(descendants, Array) and len(descendants) and isinstance(descendants[0], Dictionary):
            return descendants[0].get("/FontDescriptor"), subtype
        return None, subtype
    return font.get("/FontDescriptor"), subtype


def _is_embedded(descriptor: Dictionary | None) -> bool:
    return descriptor is not None and any(k in descriptor for k in ("/FontFile", "/FontFile2", "/FontFile3"))


def collect_fonts(pdf: pikepdf.Pdf) -> list[PdfFontRef]:
    refs: list[PdfFontRef] = []
    seen: set = set()
    for res in iter_resources(pdf):
        fonts = res.get("/Font")
        if not isinstance(fonts, Dictionary):
            continue
        for font in fonts.values():
            if not isinstance(font, Dictionary):
                continue
            key = object_key(font)
            if key in seen:
                continue
            seen.add(key)
            descriptor, subtype = _font_descriptor(font)
            embedded = subtype == "/Type3" or _is_embedded(descriptor)
            refs.append(PdfFontRef(font, descriptor if isinstance(descriptor, Dictionary) else None, subtype, embedded))
    return refs


def analyze_fonts(pdf: pikepdf.Pdf) -> list[FontInfo]:
    result = []
    for ref in collect_fonts(pdf):
        name = strip_subset_prefix(str(ref.font.get("/BaseFont", "/" + ref.subtype.lstrip("/"))))
        result.append(
            FontInfo(
                name=name,
                subtype=ref.subtype.lstrip("/"),
                status=FontStatus.already_embedded if ref.embedded else FontStatus.skipped,
            )
        )
    return result


def count_unembedded(pdf: pikepdf.Pdf) -> int:
    return sum(not r.embedded for r in collect_fonts(pdf))


# ---------------------------------------------------------------- System-Font-Index


@dataclass(frozen=True)
class FontFileEntry:
    path: str
    index: int          # Index innerhalb einer .ttc-Collection, sonst 0
    ps_name: str        # normalisiert
    full_name: str      # normalisiert
    family: str         # normalisiert
    bold: bool
    italic: bool


def _name(tt: TTFont, *ids: int) -> str:
    table = tt["name"]
    for nid in ids:
        rec = table.getDebugName(nid)
        if rec:
            return rec
    return ""


def _entry_from_ttfont(tt: TTFont, path: str, index: int) -> FontFileEntry:
    os2 = tt["OS/2"] if "OS/2" in tt else None
    sel = os2.fsSelection if os2 is not None else 0
    mac = tt["head"].macStyle
    return FontFileEntry(
        path=path,
        index=index,
        ps_name=_norm(_name(tt, 6)),
        full_name=_norm(_name(tt, 4)),
        family=_norm(_name(tt, 16, 1)),
        bold=bool(sel & 0x20) or bool(mac & 0x01),
        italic=bool(sel & 0x01) or bool(mac & 0x02),
    )


@lru_cache(maxsize=1)
def system_font_index() -> tuple[FontFileEntry, ...]:
    entries: list[FontFileEntry] = []
    for d in get_settings().font_dirs:
        root = Path(d)
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            suffix = path.suffix.lower()
            if suffix not in (".ttf", ".otf", ".ttc"):
                continue
            try:
                if suffix == ".ttc":
                    coll = TTCollection(str(path), lazy=True)
                    for i, tt in enumerate(coll.fonts):
                        entries.append(_entry_from_ttfont(tt, str(path), i))
                    coll.close()
                else:
                    with TTFont(str(path), lazy=True) as tt:
                        entries.append(_entry_from_ttfont(tt, str(path), 0))
            except Exception as exc:  # beschädigte oder exotische Font-Dateien ignorieren
                log.debug("Font %s übersprungen: %s", path, exc)
    log.info("System-Font-Index: %d Schriften", len(entries))
    return tuple(entries)


def find_system_font(req: FontRequest) -> FontFileEntry | None:
    index = system_font_index()
    wanted = _norm(req.base_name)
    for e in index:
        if wanted and wanted in (e.ps_name, e.full_name):
            return e
    candidates = [e for e in index if e.family == req.family]
    for e in candidates:
        if e.bold == req.bold and e.italic == req.italic:
            return e
    return None


def fallback_entry(req: FontRequest, choice: FallbackFont) -> FontFileEntry:
    if choice == FallbackFont.auto:
        choice = FallbackFont.mono if req.fixed else FallbackFont.serif if req.serif else FallbackFont.sans
    style = {(False, False): "Regular", (True, False): "Bold", (False, True): "Italic", (True, True): "BoldItalic"}[
        (req.bold, req.italic)
    ]
    name = f"{LIBERATION[choice]}-{style}"
    path = BUNDLED_FONT_DIR / f"{name}.ttf"
    return FontFileEntry(str(path), 0, _norm(name), _norm(name), _norm(LIBERATION[choice]), req.bold, req.italic)


def load_ttfont(entry: FontFileEntry) -> TTFont:
    # recalcTimestamp=False: sonst landet die aktuelle Uhrzeit im Font und die Ausgabe ist nicht reproduzierbar
    number = entry.index if entry.path.lower().endswith(".ttc") else -1
    return TTFont(entry.path, fontNumber=number, recalcTimestamp=False)


def embedding_allowed(tt: TTFont) -> tuple[bool, str | None]:
    if "OS/2" not in tt:
        return True, None
    fs_type = tt["OS/2"].fsType
    if fs_type & FS_TYPE_BITMAP_ONLY:
        return False, "Lizenz erlaubt nur Bitmap-Einbettung"
    if fs_type & 0x000F == FS_TYPE_RESTRICTED:
        return False, "Lizenz verbietet das Einbetten (fsType Restricted)"
    return True, None


# ---------------------------------------------------------------- Encoding


def _cp1252_names() -> list[str | None]:
    names: list[str | None] = []
    for code in range(256):
        try:
            ch = bytes([code]).decode("cp1252")
        except UnicodeDecodeError:
            names.append(None)
            continue
        names.append(agl.UV2AGL.get(ord(ch)) if code >= 32 else None)
    # WinAnsi-Besonderheiten laut PDF-Spezifikation
    names[0xA0] = "space"
    names[0xAD] = "hyphen"
    return names


def _macroman_names() -> list[str | None]:
    names: list[str | None] = []
    for code in range(256):
        ch = bytes([code]).decode("mac_roman")
        names.append(agl.UV2AGL.get(ord(ch)) if code >= 32 else None)
    return names


BASE_ENCODINGS = {
    "/WinAnsiEncoding": _cp1252_names(),
    "/MacRomanEncoding": _macroman_names(),
    "/StandardEncoding": [None if n == ".notdef" else n for n in StandardEncoding],
}


def glyph_names_for(font: Dictionary) -> tuple[list[str | None], bool]:
    """Code -> Glyphname für eine Simple-Font. Zweiter Wert: True, wenn /Encoding fehlte."""
    enc = font.get("/Encoding")
    if enc is None:
        return list(BASE_ENCODINGS["/StandardEncoding"]), True
    if isinstance(enc, Name):
        return list(BASE_ENCODINGS.get(str(enc), BASE_ENCODINGS["/StandardEncoding"])), False
    names = list(BASE_ENCODINGS.get(str(enc.get("/BaseEncoding", "/StandardEncoding")), BASE_ENCODINGS["/StandardEncoding"]))
    code = 0
    for item in enc.get("/Differences", Array()):
        if isinstance(item, Name):
            if 0 <= code < 256:
                names[code] = str(item).lstrip("/")
            code += 1
        else:
            code = int(item)
    return names, False


def _to_unicode(glyph_name: str) -> str:
    try:
        return agl.toUnicode(glyph_name)
    except Exception:
        return ""


# ---------------------------------------------------------------- Einbettung


def _subset_tag(program: bytes) -> str:
    """Deterministisches Subset-Präfix (gleiche Eingabe -> gleiche Ausgabe)."""
    digest = hashlib.sha256(program).digest()
    return "".join(chr(ord("A") + b % 26) for b in digest[:6])


def _scale(v: float, upem: int) -> int:
    return round(v * 1000 / upem)


def _build_font_program(tt: TTFont, unicodes: set[int]) -> tuple[bytes, bool]:
    """Subsettet die Schrift. Rückgabe: (Bytes, ist_cff)."""
    is_cff = "CFF " in tt or "CFF2" in tt
    fs_type = tt["OS/2"].fsType if "OS/2" in tt else 0
    if not fs_type & FS_TYPE_NO_SUBSET and unicodes:
        opts = Options()
        opts.notdef_outline = True
        opts.layout_features = []
        opts.name_IDs = ["*"]
        opts.name_languages = ["*"]
        opts.drop_tables += ["DSIG", "GSUB", "GPOS", "GDEF", "kern"]
        sub = Subsetter(opts)
        sub.populate(unicodes=unicodes)
        sub.subset(tt)
    buf = BytesIO()
    tt.save(buf)
    return buf.getvalue(), is_cff


def _stem_v(tt: TTFont) -> int:
    weight = tt["OS/2"].usWeightClass if "OS/2" in tt else 400
    return max(50, round(10 + 220 * (weight - 50) / 900))


def embed_simple_font(pdf: pikepdf.Pdf, ref: PdfFontRef, req: FontRequest, entry: FontFileEntry, is_fallback: bool) -> str:
    """Bettet `entry` in die Font `ref` ein. Gibt den verwendeten PostScript-Namen zurück."""
    font = ref.font
    tt = load_ttfont(entry)
    cmap = tt.getBestCmap() or {}
    upem = tt["head"].unitsPerEm
    hmtx = tt["hmtx"]

    names, encoding_missing = glyph_names_for(font)
    first = int(font.get("/FirstChar", 0))
    last = int(font.get("/LastChar", 255))
    first, last = max(0, min(first, 255)), max(0, min(last, 255))

    unicodes: set[int] = set()
    code_glyph: dict[int, str] = {}
    for code in range(first, last + 1):
        gname = names[code]
        if not gname:
            continue
        u = _to_unicode(gname)
        if len(u) == 1 and ord(u) in cmap:
            unicodes.add(ord(u))
            code_glyph[code] = cmap[ord(u)]

    # Breiten vor dem Subsetting berechnen (nur nötig, wenn die PDF keine hat)
    computed_widths = None
    if "/Widths" not in font:
        if code_glyph:
            first, last = min(code_glyph), max(code_glyph)
        missing = _scale(hmtx[".notdef"][0], upem) if ".notdef" in hmtx.metrics else 0
        computed_widths = [
            _scale(hmtx[code_glyph[c]][0], upem) if c in code_glyph else missing for c in range(first, last + 1)
        ]

    os2 = tt["OS/2"] if "OS/2" in tt else None
    head, hhea, post = tt["head"], tt["hhea"], tt["post"]
    ps_name = _name(tt, 6).replace(" ", "") or entry.ps_name
    italic_angle = float(post.italicAngle)
    fixed = bool(post.isFixedPitch)
    ascent = _scale(hhea.ascent, upem)
    descent = _scale(hhea.descent, upem)
    cap_height = _scale(os2.sCapHeight, upem) if os2 is not None and getattr(os2, "sCapHeight", 0) else ascent
    bbox = [_scale(head.xMin, upem), _scale(head.yMin, upem), _scale(head.xMax, upem), _scale(head.yMax, upem)]
    stem_v = _stem_v(tt)

    program, is_cff = _build_font_program(tt, unicodes)
    tt.close()

    font_name = f"{_subset_tag(program)}+{ps_name}"
    flags = FLAG_NONSYMBOLIC
    if fixed or req.fixed:
        flags |= FLAG_FIXED
    serif = "liberationserif" == entry.family if is_fallback else req.serif
    if serif:
        flags |= FLAG_SERIF
    if req.italic or italic_angle:
        flags |= FLAG_ITALIC

    stream = pikepdf.Stream(pdf, program)
    descriptor = Dictionary(
        Type=Name.FontDescriptor,
        FontName=Name("/" + font_name),
        Flags=flags,
        FontBBox=Array(bbox),
        ItalicAngle=italic_angle,
        Ascent=ascent,
        Descent=descent,
        CapHeight=cap_height,
        StemV=stem_v,
    )
    old = ref.descriptor
    if old is not None and "/MissingWidth" in old:
        descriptor.MissingWidth = old.MissingWidth

    if is_cff:
        stream.Subtype = Name("/OpenType")
        descriptor.FontFile3 = stream
    else:
        stream.Length1 = len(program)
        descriptor.FontFile2 = stream
        font.Subtype = Name.TrueType

    font.FontDescriptor = pdf.make_indirect(descriptor)
    font.BaseFont = Name("/" + font_name)

    if encoding_missing:
        # Standard-14-Fonts haben implizit StandardEncoding -> explizit machen
        diffs: list = []
        for code in range(first, last + 1):
            gname = names[code]
            if gname:
                diffs += [code, Name("/" + gname)]
        font.Encoding = Dictionary(Type=Name.Encoding, Differences=Array(diffs))

    if computed_widths is not None:
        font.FirstChar = first
        font.LastChar = last
        font.Widths = Array(computed_widths)

    return ps_name


def embed_missing_fonts(pdf: pikepdf.Pdf, fallback: FallbackFont) -> tuple[list[FontInfo], list[str]]:
    """Bettet alle nicht eingebetteten Simple-Fonts ein. Rückgabe: (Font-Report, Warnungen)."""
    infos: list[FontInfo] = []
    warnings: list[str] = []
    for ref in collect_fonts(pdf):
        check_deadline()
        name = strip_subset_prefix(str(ref.font.get("/BaseFont", "/Unbenannt")))
        subtype = ref.subtype.lstrip("/")
        if ref.embedded:
            infos.append(FontInfo(name=name, subtype=subtype, status=FontStatus.already_embedded))
            continue

        if ref.subtype not in SIMPLE_SUBTYPES:
            reason = f"{subtype}-Schriften (z. B. CJK) werden nicht automatisch eingebettet"
            infos.append(FontInfo(name=name, subtype=subtype, status=FontStatus.skipped, reason=reason))
            warnings.append(f"Schrift '{name}': {reason}")
            continue

        req = parse_font_request(ref.font, ref.descriptor)
        if req.symbolic:
            reason = "symbolische Schrift, kein sicherer Ersatz möglich"
            infos.append(FontInfo(name=name, subtype=subtype, status=FontStatus.skipped, reason=reason))
            warnings.append(f"Schrift '{name}': {reason}")
            continue

        reason = None
        entry = find_system_font(req)
        if entry is not None:
            try:
                with load_ttfont(entry) as tt:
                    allowed, reason = embedding_allowed(tt)
            except Exception:
                log.warning("Systemschrift %s nicht lesbar", entry.path, exc_info=True)
                allowed, reason = False, "Systemschrift nicht lesbar"
            if not allowed:
                entry = None
        else:
            reason = "Originalschrift nicht auf dem Server vorhanden"

        is_fallback = entry is None
        if is_fallback:
            entry = fallback_entry(req, fallback)

        try:
            used = embed_simple_font(pdf, ref, req, entry, is_fallback)
        except Exception:
            log.exception("Einbetten einer Schrift fehlgeschlagen")
            msg = "Einbetten fehlgeschlagen"
            infos.append(FontInfo(name=name, subtype=subtype, status=FontStatus.skipped, reason=msg))
            warnings.append(f"Schrift '{name}': {msg}")
            continue

        infos.append(
            FontInfo(
                name=name,
                subtype=subtype,
                status=FontStatus.fallback if is_fallback else FontStatus.embedded,
                used_font=used,
                reason=reason if is_fallback else None,
            )
        )
    return infos, warnings
