import base64
from dataclasses import dataclass
from pathlib import PurePath
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from app.core.config import get_settings
from app.models import AnalyzeResponse, CompressBase64Response, CompressionLevel, FallbackFont
from app.services.pipeline import PdfProcessingError, ProcessResult, analyze_pdf, process_pdf

router = APIRouter(prefix="/api/v1/pdf", tags=["pdf"])

CHUNK = 1024 * 1024


@dataclass
class CompressParams:
    file: UploadFile
    level: CompressionLevel
    fallback_font: FallbackFont
    embed_fonts: bool


def compress_params(
    file: UploadFile = File(..., description="PDF-Datei"),
    level: CompressionLevel = Form(CompressionLevel.medium, description="low = 72 DPI, medium = 150 DPI, high = 300 DPI"),
    fallback_font: FallbackFont = Form(
        FallbackFont.auto, description="Ersatzschrift, falls die Originalschrift nicht verfügbar ist"
    ),
    embed_fonts: bool = Form(True, description="Nicht eingebettete Schriften einbetten"),
) -> CompressParams:
    return CompressParams(file, level, fallback_font, embed_fonts)


async def read_upload(request: Request, file: UploadFile) -> bytes:
    limit = get_settings().max_upload_bytes
    too_large = HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"Datei größer als {get_settings().max_upload_mb} MB")
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > limit + 64 * 1024:  # etwas Luft für Multipart-Overhead
        raise too_large
    chunks, size = [], 0
    while chunk := await file.read(CHUNK):
        size += len(chunk)
        if size > limit:
            raise too_large
        chunks.append(chunk)
    if not size:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Leere Datei")
    return b"".join(chunks)


def output_filename(upload_name: str | None) -> str:
    stem = PurePath(upload_name or "document.pdf").stem or "document"
    return f"{stem}_compressed.pdf"


async def run_compression(request: Request, params: CompressParams) -> ProcessResult:
    data = await read_upload(request, params.file)
    try:
        return await run_in_threadpool(process_pdf, data, params.level, params.fallback_font, params.embed_fonts)
    except PdfProcessingError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.post(
    "/compress",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}, "description": "Die (komprimierte) PDF-Datei"}},
    summary="PDF komprimieren (Datei zurück)",
)
async def compress_file(request: Request, params: CompressParams = Depends(compress_params)) -> Response:
    result = await run_compression(request, params)
    report = result.report
    name = output_filename(params.file.filename)
    ascii_name = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    headers = {
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
        "X-Original-Size": str(report.original_size),
        "X-Result-Size": str(report.result_size),
        "X-Result": report.result.value,
        "X-Compression-Level": report.level.value,
        "X-Images-Downsampled": str(report.images_downsampled),
        "X-Fonts-Embedded": str(report.fonts_embedded),
        "X-Fonts-Fallback": str(report.fonts_fallback),
        "X-Warnings": str(len(report.warnings)),
    }
    return Response(content=result.data, media_type="application/pdf", headers=headers)


@router.post("/compress/base64", response_model=CompressBase64Response, summary="PDF komprimieren (Base64 zurück)")
async def compress_base64(request: Request, params: CompressParams = Depends(compress_params)) -> CompressBase64Response:
    result = await run_compression(request, params)
    return CompressBase64Response(
        filename=output_filename(params.file.filename),
        content_base64=base64.b64encode(result.data).decode("ascii"),
        report=result.report,
    )


@router.post("/analyze", response_model=AnalyzeResponse, summary="PDF analysieren (Schriften und Bilder)")
async def analyze(request: Request, file: UploadFile = File(...)) -> AnalyzeResponse:
    data = await read_upload(request, file)
    try:
        return await run_in_threadpool(analyze_pdf, data, file.filename or "document.pdf")
    except PdfProcessingError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
