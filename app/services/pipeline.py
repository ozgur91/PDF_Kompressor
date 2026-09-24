"""Gesamtablauf: Fonts einbetten, Bilder verkleinern, speichern, validieren, beste Variante wählen."""

import logging
import time
from dataclasses import dataclass
from io import BytesIO

import pikepdf

from app.core.config import get_settings
from app.models import (
    AnalyzeResponse,
    CompressionLevel,
    CompressionReport,
    FallbackFont,
    FontInfo,
    FontStatus,
    ResultKind,
)
from app.services import fonts, images
from app.services.validator import validate

log = logging.getLogger(__name__)


class PdfProcessingError(Exception):
    status_code = 400


class InvalidPdfError(PdfProcessingError):
    status_code = 400


class EncryptedPdfError(PdfProcessingError):
    status_code = 422


class TooManyPagesError(PdfProcessingError):
    status_code = 422


@dataclass
class ProcessResult:
    data: bytes
    report: CompressionReport


def _open(data: bytes) -> pikepdf.Pdf:
    if not data.lstrip()[:5] == b"%PDF-":
        raise InvalidPdfError("Die Datei ist keine PDF")
    try:
        pdf = pikepdf.open(BytesIO(data))
    except pikepdf.PasswordError as exc:
        raise EncryptedPdfError("Die PDF ist passwortgeschützt") from exc
    except pikepdf.PdfError as exc:
        raise InvalidPdfError(f"Die PDF ist beschädigt: {exc}") from exc
    if pdf.is_encrypted:
        pdf.close()
        raise EncryptedPdfError("Die PDF ist verschlüsselt (Rechte-Beschränkung) und darf nicht verändert werden")
    max_pages = get_settings().max_pages
    if len(pdf.pages) > max_pages:
        pdf.close()
        raise TooManyPagesError(f"Die PDF hat mehr als {max_pages} Seiten")
    return pdf


def _save(pdf: pikepdf.Pdf) -> bytes:
    pdf.remove_unreferenced_resources()
    buf = BytesIO()
    pdf.save(
        buf,
        compress_streams=True,
        recompress_flate=True,
        object_stream_mode=pikepdf.ObjectStreamMode.generate,
        linearize=False,
        deterministic_id=True,
    )
    return buf.getvalue()


@dataclass
class _Attempt:
    data: bytes
    fonts: list[FontInfo]
    images_downsampled: int
    warnings: list[str]
    unembedded_left: int


def _attempt(data: bytes, level: CompressionLevel, fallback: FallbackFont, embed: bool, with_images: bool) -> _Attempt:
    settings = get_settings()
    with _open(data) as pdf:
        pages = len(pdf.pages)
        warnings: list[str] = []
        if embed:
            font_infos, w = fonts.embed_missing_fonts(pdf, fallback)
            warnings += w
        else:
            font_infos = fonts.analyze_fonts(pdf)
        downsampled = 0
        if with_images:
            downsampled, w = images.downsample_images(pdf, level.dpi, level.jpeg_quality, settings.downsample_threshold)
            warnings += w
        out = _save(pdf)

    unembedded_left = sum(f.status == FontStatus.skipped for f in font_infos)
    validate(out, pages, unembedded_left, settings.full_render_max_pages)
    return _Attempt(out, font_infos, downsampled, warnings, unembedded_left)


def _fonts_changed(attempt: _Attempt) -> bool:
    return any(f.status in (FontStatus.embedded, FontStatus.fallback) for f in attempt.fonts)


def process_pdf(data: bytes, level: CompressionLevel, fallback: FallbackFont, embed_fonts: bool) -> ProcessResult:
    start = time.perf_counter()
    with _open(data) as pdf:           # frühe Prüfung: gültig, nicht verschlüsselt, Seitenlimit
        original_fonts = fonts.analyze_fonts(pdf)

    warnings: list[str] = []
    result_kind = ResultKind.original
    out, font_infos, downsampled = data, original_fonts, 0

    try:
        full = _attempt(data, level, fallback, embed_fonts, with_images=True)
    except Exception as exc:  # inkl. ValidationError: lieber Original als kaputte Datei
        log.warning("Komprimierung fehlgeschlagen: %s", exc)
        warnings.append(f"Komprimierung verworfen: {exc}")
        full = None

    if full is not None and len(full.data) < len(data):
        result_kind, out, font_infos, downsampled = ResultKind.compressed, full.data, full.fonts, full.images_downsampled
        warnings += full.warnings
    elif embed_fonts and (full is None or _fonts_changed(full)):
        if full is not None:
            warnings.append("Komprimierung brachte keine Verkleinerung, es werden nur die Schriften eingebettet")
        try:
            fo = _attempt(data, level, fallback, True, with_images=False)
            if _fonts_changed(fo):
                result_kind, out, font_infos = ResultKind.fonts_only, fo.data, fo.fonts
                warnings += fo.warnings
        except Exception as exc:
            log.warning("Font-Einbettung fehlgeschlagen: %s", exc)
            warnings.append(f"Font-Einbettung verworfen: {exc}")
    elif full is not None:
        warnings.append("Die Datei ist bereits optimal, das Original wird unverändert zurückgegeben")

    if result_kind == ResultKind.original:
        font_infos = original_fonts
        downsampled = 0

    report = CompressionReport(
        original_size=len(data),
        result_size=len(out),
        result=result_kind,
        level=level,
        dpi=level.dpi,
        images_downsampled=downsampled,
        fonts=font_infos,
        warnings=warnings,
    )
    log.info(
        "PDF verarbeitet: %d -> %d Bytes, Ergebnis=%s, Stufe=%s, Fonts eingebettet=%d/Fallback=%d, %.2fs",
        report.original_size, report.result_size, result_kind.value, level.value,
        report.fonts_embedded, report.fonts_fallback, time.perf_counter() - start,
    )
    return ProcessResult(out, report)


def analyze_pdf(data: bytes, filename: str) -> AnalyzeResponse:
    with _open(data) as pdf:
        return AnalyzeResponse(
            filename=filename,
            size=len(data),
            pages=len(pdf.pages),
            fonts=fonts.analyze_fonts(pdf),
            images=images.analyze_images(pdf),
        )
