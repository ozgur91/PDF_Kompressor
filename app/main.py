import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.formparsers import MultiPartParser

from app.api.pdf import router as pdf_router
from app.core.config import APP_DIR, get_settings
from app.core.integrity import verify_bundled_fonts
from app.core.security import RequestIdFilter, SecurityMiddleware, unhandled_exception_handler
from app.services.fonts import system_font_index

STATIC_DIR = APP_DIR / "static"

_handler = logging.StreamHandler()
_handler.addFilter(RequestIdFilter())
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s",
    handlers=[_handler],
)

settings = get_settings()

# Uploads im Arbeitsspeicher halten: Starlette schreibt Dateien sonst ab 1 MB als Temp-Datei auf die Platte
MultiPartParser.spool_max_size = settings.max_upload_bytes + 1024 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Manipulierte Ersatzschriften -> App startet nicht
    verify_bundled_fonts()
    # System-Schriften im Hintergrund indexieren, damit der erste Request nicht warten muss
    threading.Thread(target=system_font_index, name="font-index", daemon=True).start()
    yield


app = FastAPI(
    title="PDF-Kompression",
    description="Komprimiert PDF-Dateien (3 DPI-Stufen), bettet fehlende Schriftarten ein und entfernt aktive Inhalte.",
    version="1.1.0",
    lifespan=lifespan,
    # Swagger/ReDoc laden Skripte von einem CDN und legen die API offen -> im Betrieb aus
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

app.add_exception_handler(Exception, unhandled_exception_handler)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
        expose_headers=[
            "Content-Disposition",
            "X-Original-Size",
            "X-Result-Size",
            "X-Result",
            "X-Compression-Level",
            "X-Images-Downsampled",
            "X-Fonts-Embedded",
            "X-Fonts-Fallback",
            "X-Active-Content-Removed",
            "X-Warnings",
            "X-Request-ID",
        ],
    )
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
# als letzte hinzugefügt = äußerste Middleware: gilt auch für abgelehnte Hosts und Fehlerantworten
app.add_middleware(SecurityMiddleware, max_body=settings.max_upload_bytes + 64 * 1024, hsts=settings.hsts)

app.include_router(pdf_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/v1/config", tags=["system"])
def client_config() -> dict:
    """Einstellungen, die das Frontend braucht."""
    return {"max_upload_mb": settings.max_upload_mb, "docs_enabled": settings.enable_docs}


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
