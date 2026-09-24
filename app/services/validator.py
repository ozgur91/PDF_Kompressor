"""Prüft, ob eine erzeugte PDF gültig ist, bevor sie ausgeliefert wird."""

import logging
from io import BytesIO

import pikepdf
import pypdfium2 as pdfium

from app.services.fonts import count_unembedded

log = logging.getLogger(__name__)


class ValidationError(Exception):
    pass


def _pages_to_render(n: int, full_max: int) -> list[int]:
    if n <= full_max:
        return list(range(n))
    step = max(1, n // 10)
    return sorted({*range(5), n - 1, *range(5, n, step)})


def validate(data: bytes, expected_pages: int, max_unembedded: int, full_render_max: int) -> None:
    try:
        with pikepdf.open(BytesIO(data)) as pdf:
            if len(pdf.pages) != expected_pages:
                raise ValidationError(f"Seitenzahl {len(pdf.pages)} statt {expected_pages}")
            unembedded = count_unembedded(pdf)
            if unembedded > max_unembedded:
                raise ValidationError(f"{unembedded} Schriften nicht eingebettet (erwartet höchstens {max_unembedded})")
    except pikepdf.PdfError as exc:
        raise ValidationError(f"Ergebnis nicht lesbar: {exc}") from exc

    doc = pdfium.PdfDocument(data)
    try:
        if len(doc) != expected_pages:
            raise ValidationError("Seitenzahl beim Rendern abweichend")
        for i in _pages_to_render(len(doc), full_render_max):
            page = doc[i]
            try:
                page.render(scale=0.2).close()
            except Exception as exc:
                raise ValidationError(f"Seite {i + 1} nicht renderbar: {exc}") from exc
            finally:
                page.close()
    finally:
        doc.close()
