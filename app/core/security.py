"""Web-Sicherheit: Security-Header, Größenlimit für Request-Bodies, Request-ID, sichere Fehlerantworten."""

import logging
import re
import unicodedata
import uuid
from contextvars import ContextVar

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; "
    "connect-src 'self'; font-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Permitted-Cross-Domain-Policies": "none",
}


class RequestIdFilter(logging.Filter):
    """Hängt die Request-ID an jede Log-Zeile (funktioniert auch im Threadpool, da ContextVars kopiert werden)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class SecurityMiddleware:
    """Reine ASGI-Middleware (streamt, puffert keine Bodies).

    - vergibt eine Request-ID (Header X-Request-ID, im Log)
    - begrenzt die Body-Größe schon beim Empfang, auch ohne Content-Length (Chunked-Upload)
    - setzt Security-Header und Cache-Control: no-store auf jede Antwort
    """

    def __init__(self, app: ASGIApp, max_body: int, hsts: bool):
        self.app = app
        self.max_body = max_body
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        rid = uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        try:
            length = Headers(scope=scope).get("content-length")
            if length is not None and (not length.isdigit() or int(length) > self.max_body):
                await self._reject(scope, send, rid)
                return

            received = 0

            async def limited_receive() -> Message:
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > self.max_body:
                        raise HTTPException(413, "Die Datei ist zu groß")
                return message

            async def secured_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers = MutableHeaders(scope=message)
                    for key, value in SECURITY_HEADERS.items():
                        headers.setdefault(key, value)
                    headers["Cache-Control"] = "no-store"
                    headers["X-Request-ID"] = rid
                    if self.hsts:
                        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
                await send(message)

            await self.app(scope, limited_receive, secured_send)
        finally:
            request_id_var.reset(token)

    async def _reject(self, scope: Scope, send: Send, rid: str) -> None:
        response = JSONResponse({"detail": "Die Datei ist zu groß"}, status_code=413)
        response.headers.update(SECURITY_HEADERS)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = rid
        response.headers["Connection"] = "close"
        await response(scope, _empty_receive, send)


async def _empty_receive() -> Message:
    return {"type": "http.disconnect"}


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Unerwartete Fehler: nur Request-ID an den Client, Details (Stacktrace) nur ins Log."""
    rid = request_id_var.get()
    log.exception("Unerwarteter Fehler")
    return JSONResponse(
        {"detail": f"Interner Fehler. Referenz: {rid}"},
        status_code=500,
    )


_UNSAFE = re.compile(r"[^A-Za-z0-9._ ()\-äöüÄÖÜß]")


def safe_filename_stem(name: str | None, default: str = "document", max_len: int = 100) -> str:
    """Bereinigt einen vom Client gelieferten Dateinamen für Content-Disposition und JSON.

    Entfernt Pfade, Steuerzeichen (CR/LF -> keine Header-Injection), Anführungszeichen und alles außer
    Buchstaben, Ziffern und wenigen Satzzeichen; begrenzt die Länge.
    """
    if not name:
        return default
    name = unicodedata.normalize("NFC", name)
    name = re.split(r"[\\/]", name)[-1]                  # nur der letzte Pfadteil
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    name = _UNSAFE.sub("_", name).strip(" .")
    return name[:max_len] or default
