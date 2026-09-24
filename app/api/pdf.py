import base64
import logging
import threading
import time
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException

from app.core.config import get_settings
from app.core.security import safe_filename_stem
from app.models import AnalyzeResponse, CompressBase64Response, CompressionLevel, FallbackFont
from app.services.pipeline import PdfProcessingError, ProcessResult, analyze_pdf, process_pdf

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/pdf", tags=["pdf"])

CHUNK = 1024 * 1024
# Formular-Limits: genau eine Datei, wenige kurze Textfelder
FORM_LIMITS = {"max_files": 1, "max_fields": 10, "max_part_size": 16 * 1024}

_slots = threading.BoundedSemaphore(get_settings().max_concurrent)


class CompressParams(BaseModel):
    """Textfelder des Compress-Formulars (unbekannte Felder werden abgelehnt)."""

    model_config = ConfigDict(extra="forbid")

    level: CompressionLevel = CompressionLevel.medium
    fallback_font: FallbackFont = FallbackFont.auto
    embed_fonts: bool = True
    remove_active_content: bool = True


class AnalyzeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _form_schema(params: type[BaseModel]) -> dict:
    """OpenAPI-Beschreibung des Multipart-Formulars (für Swagger, falls aktiviert)."""
    schema = params.model_json_schema()
    props = {"file": {"type": "string", "format": "binary", "description": "PDF-Datei"}, **schema.get("properties", {})}
    return {
        "requestBody": {
            "required": True,
            "content": {"multipart/form-data": {"schema": {"type": "object", "required": ["file"], "properties": props}}},
        }
    }


async def parse_form(request: Request, params_model: type[BaseModel]):
    """Liest das Formular mit strengen Limits. Rückgabe: (Datei-Bytes, Dateiname, Parameter)."""
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Erwartet wird multipart/form-data")
    try:
        form = await request.form(**FORM_LIMITS)
    except MultiPartException:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ungültiges Formular") from None
    try:
        items = form.multi_items()
        files = [(k, v) for k, v in items if isinstance(v, UploadFile)]
        fields = [(k, v) for k, v in items if not isinstance(v, UploadFile)]
        if len(files) != 1 or files[0][0] != "file":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Genau eine Datei im Feld 'file' erwartet")
        names = [k for k, _ in fields]
        if len(names) != len(set(names)):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Doppelte Formularfelder")
        try:
            params = params_model.model_validate(dict(fields))
        except ValidationError as exc:
            # nur Feldname und Fehlertyp zurückgeben, keine Eingabewerte
            errors = [{"loc": list(e["loc"]), "type": e["type"], "msg": e["msg"]} for e in exc.errors(include_input=False)]
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, errors) from None

        upload = files[0][1]
        data = await _read_limited(upload)
        return data, upload.filename, params
    finally:
        await form.close()


async def _read_limited(file: UploadFile) -> bytes:
    limit = get_settings().max_upload_bytes
    chunks, size = [], 0
    while chunk := await file.read(CHUNK):
        size += len(chunk)
        if size > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"Datei größer als {get_settings().max_upload_mb} MB")
        chunks.append(chunk)
    if not size:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Leere Datei")
    return b"".join(chunks)


def _run_limited(fn, *args):
    """Läuft im Threadpool: wartet auf einen freien Verarbeitungsplatz (Parallelitätslimit)."""
    settings = get_settings()
    if not _slots.acquire(timeout=settings.queue_timeout_s):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Der Dienst ist ausgelastet, bitte später erneut versuchen",
            headers={"Retry-After": "10"},
        )
    try:
        return fn(*args)
    finally:
        _slots.release()


async def _execute(request: Request, endpoint: str, fn, *args):
    start = time.perf_counter()
    client = request.client.host if request.client else "-"
    try:
        result = await run_in_threadpool(_run_limited, fn, *args)
    except PdfProcessingError as exc:
        log.info("audit endpoint=%s client=%s status=%d dauer=%.2fs", endpoint, client, exc.status_code, time.perf_counter() - start)
        raise HTTPException(exc.status_code, str(exc)) from exc
    log.info("audit endpoint=%s client=%s status=200 dauer=%.2fs", endpoint, client, time.perf_counter() - start)
    return result


def output_filename(upload_name: str | None) -> str:
    return f"{safe_filename_stem(upload_name)}_compressed.pdf"


async def run_compression(request: Request, endpoint: str) -> tuple[ProcessResult, str]:
    data, filename, p = await parse_form(request, CompressParams)
    result = await _execute(
        request, endpoint, process_pdf, data, p.level, p.fallback_font, p.embed_fonts, p.remove_active_content
    )
    return result, output_filename(filename)


@router.post(
    "/compress",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}, "description": "Die (komprimierte) PDF-Datei"}},
    summary="PDF komprimieren (Datei zurück)",
    openapi_extra=_form_schema(CompressParams),
)
async def compress_file(request: Request) -> Response:
    result, name = await run_compression(request, "compress")
    report = result.report
    ascii_name = name.encode("ascii", "replace").decode().replace("?", "_")
    headers = {
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
        "X-Original-Size": str(report.original_size),
        "X-Result-Size": str(report.result_size),
        "X-Result": report.result.value,
        "X-Compression-Level": report.level.value,
        "X-Images-Downsampled": str(report.images_downsampled),
        "X-Fonts-Embedded": str(report.fonts_embedded),
        "X-Fonts-Fallback": str(report.fonts_fallback),
        "X-Active-Content-Removed": str(len(report.active_content_removed)),
        "X-Warnings": str(len(report.warnings)),
    }
    return Response(content=result.data, media_type="application/pdf", headers=headers)


@router.post(
    "/compress/base64",
    response_model=CompressBase64Response,
    summary="PDF komprimieren (Base64 zurück)",
    openapi_extra=_form_schema(CompressParams),
)
async def compress_base64(request: Request) -> CompressBase64Response:
    result, name = await run_compression(request, "compress/base64")
    return CompressBase64Response(
        filename=name,
        content_base64=base64.b64encode(result.data).decode("ascii"),
        report=result.report,
    )


@router.post(
    "/analyze",
    response_model=AnalyzeResponse,
    summary="PDF analysieren (Schriften, Bilder, aktive Inhalte)",
    openapi_extra=_form_schema(AnalyzeParams),
)
async def analyze(request: Request) -> AnalyzeResponse:
    data, filename, _ = await parse_form(request, AnalyzeParams)
    return await _execute(request, "analyze", analyze_pdf, data, f"{safe_filename_stem(filename)}.pdf")
