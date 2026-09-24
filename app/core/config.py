import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_DIR = Path(__file__).resolve().parent.parent
BUNDLED_FONT_DIR = APP_DIR / "fonts"


def _default_font_dirs() -> list[str]:
    dirs = []
    windir = os.environ.get("WINDIR")
    if windir:
        dirs.append(str(Path(windir) / "Fonts"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(str(Path(local) / "Microsoft" / "Windows" / "Fonts"))
    dirs += [
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        str(Path.home() / ".fonts"),
        "/Library/Fonts",
        "/System/Library/Fonts",
    ]
    return dirs


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PDF_", env_file=".env", extra="ignore")

    max_upload_mb: int = 50
    max_pages: int = 5000
    font_dirs: list[str] = _default_font_dirs()
    # Bilder werden erst heruntergerechnet, wenn sie diesen Faktor über der Ziel-DPI liegen
    downsample_threshold: float = 1.5
    # Ab dieser Seitenzahl wird bei der Validierung nur stichprobenartig gerendert
    full_render_max_pages: int = 20

    # --- Web-Sicherheit
    # Leer = CORS aus (nur gleiche Origin, z. B. das mitgelieferte Frontend)
    cors_origins: list[str] = []
    # Erlaubte Host-Header; im Betrieb auf den echten Servernamen setzen
    allowed_hosts: list[str] = ["localhost", "127.0.0.1"]
    # Swagger/ReDoc/OpenAPI (laden Skripte von einem CDN) - im Betrieb aus
    enable_docs: bool = False
    # Strict-Transport-Security senden (nur hinter TLS aktivieren)
    hsts: bool = False

    # --- Schutz vor präparierten Dateien
    max_concurrent: int = max(1, os.cpu_count() or 1)   # gleichzeitige Verarbeitungen
    queue_timeout_s: float = 10                         # Wartezeit auf einen freien Platz, danach 503
    processing_timeout_s: float = 300                   # Zeitbudget pro Datei (große Scans brauchen Minuten)
    max_stream_mb: int = 100                            # entpackte Größe pro Stream (Bilder: nach Bildmaßen)
    # entpackte Größe aller Streams zusammen; wird Stream für Stream geprüft, begrenzt also Rechenzeit, nicht Speicher
    max_total_decoded_mb: int = 50_000
    max_image_pixels: int = 100_000_000                 # Pixel pro Bild
    max_objects: int = 2_000_000                        # PDF-Objekte pro Datei
    max_fonts: int = 5000                               # zusammengefügte PDFs bringen oft eigene Schriften mit
    max_images: int = 20_000

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
