import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.pdf import router as pdf_router
from app.core.config import APP_DIR, get_settings
from app.services.fonts import system_font_index

STATIC_DIR = APP_DIR / "static"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # System-Schriften im Hintergrund indexieren, damit der erste Request nicht warten muss
    threading.Thread(target=system_font_index, name="font-index", daemon=True).start()
    yield


app = FastAPI(
    title="PDF-Kompression",
    description="Komprimiert PDF-Dateien (3 DPI-Stufen) und bettet fehlende Schriftarten ein.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    expose_headers=[
        "Content-Disposition",
        "X-Original-Size",
        "X-Result-Size",
        "X-Result",
        "X-Compression-Level",
        "X-Images-Downsampled",
        "X-Fonts-Embedded",
        "X-Fonts-Fallback",
        "X-Warnings",
    ],
)

app.include_router(pdf_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/v1/config", tags=["system"])
def client_config() -> dict:
    """Einstellungen, die das Frontend braucht."""
    return {"max_upload_mb": get_settings().max_upload_mb}


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
