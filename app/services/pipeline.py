"""Gesamtablauf: aktive Inhalte entfernen, Fonts einbetten, Bilder verkleinern, speichern, validieren, beste Variante wählen.

Sicherheitsprinzipien:
- Jede Datei wird vorab gegen Größen-, Pixel- und Anzahlgrenzen geprüft (guard.inspect_streams).
- Alles läuft unter einem Zeitbudget (guard.guarded / check_deadline).
- Clients bekommen nur feste, generische Meldungen; technische Details stehen ausschließlich im Log.
- Fail closed: Enthält eine PDF aktive Inhalte und lässt sie sich nicht bereinigen, wird sie abgelehnt,
  statt das Original mit den aktiven Inhalten auszuliefern.
"""

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
from app.services import fonts, images, sanitizer
from app.services.guard import ProcessingTimeout, ResourceLimitExceeded, guarded, inspect_streams
from app.services.validator import validate

log = logging.getLogger(__name__)


class PdfProcessingError(Exception):
    status_code = 400
    message = "Die Datei konnte nicht verarbeitet werden"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.message)


class InvalidPdfError(PdfProcessingError):
    status_code = 400
    message = "Die Datei ist keine gültige PDF"


class EncryptedPdfError(PdfProcessingError):
    status_code = 422
    message = "Verschlüsselte oder rechte-beschränkte PDFs werden nicht verarbeitet"


class TooManyPagesError(PdfProcessingError):
    status_code = 422


class LimitExceededError(PdfProcessingError):
    status_code = 422
    message = "Die Datei überschreitet die Sicherheitsgrenzen (Größe, Bildauflösung oder Komplexität)"


class ProcessingTimeoutError(PdfProcessingError):
    status_code = 422
    message = "Die Verarbeitung hat zu lange gedauert"


class ActiveContentError(PdfProcessingError):
    status_code = 422
    message = "Die PDF enthält aktive Inhalte, die nicht sicher entfernt werden konnten"


@dataclass
class ProcessResult:
    data: bytes
    report: CompressionReport


def _open(data: bytes, inspect: bool = True) -> pikepdf.Pdf:
    """Öffnet und prüft eine PDF. Muss innerhalb von `guarded(...)` aufgerufen werden.

    inspect=False überspringt die (aufwendige) Stream-Prüfung, wenn genau diese Bytes schon geprüft wurden.
    """
    settings = get_settings()
    if not data.lstrip()[:5] == b"%PDF-":
        raise InvalidPdfError()
    try:
        pdf = pikepdf.open(BytesIO(data))
    except pikepdf.PasswordError as exc:
        raise EncryptedPdfError() from exc
    except pikepdf.PdfError as exc:
        log.info("PDF nicht lesbar: %s", exc)
        raise InvalidPdfError() from exc
    try:
        if pdf.is_encrypted:
            raise EncryptedPdfError()
        if len(pdf.pages) > settings.max_pages:
            raise TooManyPagesError(f"Die PDF hat mehr als {settings.max_pages} Seiten")
        if inspect:
            inspect_streams(pdf, settings)
    except ResourceLimitExceeded as exc:
        pdf.close()
        log.warning("Sicherheitsgrenze überschritten: %s", exc)
        raise LimitExceededError() from exc
    except BaseException:
        pdf.close()
        raise
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
    active_removed: list[str]


def _attempt(
    data: bytes, level: CompressionLevel, fallback: FallbackFont, embed: bool, with_images: bool, sanitize: bool
) -> _Attempt:
    settings = get_settings()
    with _open(data, inspect=False) as pdf:   # dieselben Bytes wurden in _process bereits geprüft
        pages = len(pdf.pages)
        warnings: list[str] = []
        active_removed = sanitizer.remove_active_content(pdf) if sanitize else []
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
    validate(out, pages, unembedded_left, settings.full_render_max_pages, require_no_active=sanitize)
    return _Attempt(out, font_infos, downsampled, warnings, active_removed)


def _fonts_changed(attempt: _Attempt) -> bool:
    return any(f.status in (FontStatus.embedded, FontStatus.fallback) for f in attempt.fonts)


def _try(label: str, fn, warnings: list[str]):
    """Führt einen Versuch aus. Zeit- und Größenlimits brechen ab, alle anderen Fehler werden verworfen."""
    try:
        return fn()
    except ProcessingTimeout as exc:
        raise ProcessingTimeoutError() from exc
    except PdfProcessingError:
        raise
    except Exception:
        log.warning("%s fehlgeschlagen", label, exc_info=True)
        warnings.append(f"{label} wurde verworfen, weil das Ergebnis nicht einwandfrei war")
        return None


def process_pdf(
    data: bytes, level: CompressionLevel, fallback: FallbackFont, embed_fonts: bool, remove_active: bool = True
) -> ProcessResult:
    start = time.perf_counter()
    settings = get_settings()
    try:
        with guarded(settings):
            return _process(data, level, fallback, embed_fonts, remove_active, start)
    except ProcessingTimeout as exc:
        raise ProcessingTimeoutError() from exc


def _process(data, level, fallback, embed_fonts, remove_active, start) -> ProcessResult:
    with _open(data) as pdf:           # frühe Prüfung: gültig, nicht verschlüsselt, Limits
        original_fonts = fonts.analyze_fonts(pdf)
        active_found = sanitizer.find_active_content(pdf)

    sanitize = remove_active and bool(active_found)
    warnings: list[str] = []
    result_kind = ResultKind.original
    out, font_infos, downsampled, removed = data, original_fonts, 0, []

    full = _try("Die Komprimierung", lambda: _attempt(data, level, fallback, embed_fonts, True, sanitize), warnings)

    if full is not None and len(full.data) < len(data):
        result_kind, out, font_infos = ResultKind.compressed, full.data, full.fonts
        downsampled, removed = full.images_downsampled, full.active_removed
        warnings += full.warnings
    elif sanitize or (embed_fonts and (full is None or _fonts_changed(full))):
        # Verkleinern hat nichts gebracht (oder ist fehlgeschlagen) - trotzdem bereinigen bzw. Schriften einbetten
        if full is not None:
            warnings.append("Die Komprimierung brachte keine Verkleinerung, die Bilder bleiben unverändert")
        light = _try("Die Bereinigung", lambda: _attempt(data, level, fallback, embed_fonts, False, sanitize), warnings)
        if light is not None and (sanitize or _fonts_changed(light)):
            result_kind = ResultKind.sanitized if sanitize else ResultKind.fonts_only
            out, font_infos, removed = light.data, light.fonts, light.active_removed
            warnings += light.warnings
    elif full is not None:
        warnings.append("Die Datei ist bereits optimal, das Original wird unverändert zurückgegeben")

    if sanitize and result_kind == ResultKind.original:
        # fail closed: niemals eine Datei mit aktiven Inhalten ausliefern, wenn Bereinigung verlangt war
        raise ActiveContentError()
    if active_found and not remove_active:
        warnings.append("Die PDF enthält aktive Inhalte; sie wurden auf Wunsch nicht entfernt")

    if result_kind == ResultKind.original:
        font_infos, downsampled = original_fonts, 0

    report = CompressionReport(
        original_size=len(data),
        result_size=len(out),
        result=result_kind,
        level=level,
        dpi=level.dpi,
        images_downsampled=downsampled,
        fonts=font_infos,
        active_content_removed=removed,
        warnings=warnings,
    )
    log.info(
        "PDF verarbeitet: %d -> %d Bytes, Ergebnis=%s, Stufe=%s, Fonts eingebettet=%d/Fallback=%d, aktive Inhalte entfernt=%d, %.2fs",
        report.original_size, report.result_size, result_kind.value, level.value,
        report.fonts_embedded, report.fonts_fallback, len(removed), time.perf_counter() - start,
    )
    return ProcessResult(out, report)


def analyze_pdf(data: bytes, filename: str) -> AnalyzeResponse:
    try:
        with guarded(get_settings()), _open(data) as pdf:
            return AnalyzeResponse(
                filename=filename,
                size=len(data),
                pages=len(pdf.pages),
                fonts=fonts.analyze_fonts(pdf),
                images=images.analyze_images(pdf),
                active_content=sanitizer.find_active_content(pdf),
            )
    except ProcessingTimeout as exc:
        raise ProcessingTimeoutError() from exc
