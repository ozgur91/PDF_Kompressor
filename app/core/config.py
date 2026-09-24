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
    max_pages: int = 2000
    font_dirs: list[str] = _default_font_dirs()
    cors_origins: list[str] = ["*"]
    # Bilder werden erst heruntergerechnet, wenn sie diesen Faktor über der Ziel-DPI liegen
    downsample_threshold: float = 1.5
    # Ab dieser Seitenzahl wird bei der Validierung nur stichprobenartig gerendert
    full_render_max_pages: int = 20

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
