from enum import Enum

from pydantic import BaseModel, Field


class CompressionLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"

    @property
    def dpi(self) -> int:
        return {"low": 72, "medium": 150, "high": 300}[self.value]

    @property
    def jpeg_quality(self) -> int:
        return {"low": 60, "medium": 75, "high": 85}[self.value]


class FallbackFont(str, Enum):
    auto = "auto"
    sans = "sans"
    serif = "serif"
    mono = "mono"


class ResultKind(str, Enum):
    compressed = "compressed"
    fonts_only = "fonts_only"
    sanitized = "sanitized"     # aktive Inhalte entfernt, Bilder unverändert
    original = "original"


class FontStatus(str, Enum):
    already_embedded = "already_embedded"
    embedded = "embedded"
    fallback = "fallback"
    skipped = "skipped"


class FontInfo(BaseModel):
    name: str
    subtype: str
    status: FontStatus
    used_font: str | None = None
    reason: str | None = None


class ImageInfo(BaseModel):
    width: int
    height: int
    effective_dpi: float | None = Field(None, description="Dargestellte Auflösung auf der Seite (größte Verwendung)")
    filter: str | None = None
    downsampled: bool = False
    skip_reason: str | None = None


class CompressionReport(BaseModel):
    original_size: int
    result_size: int
    result: ResultKind
    level: CompressionLevel
    dpi: int
    images_downsampled: int = 0
    fonts: list[FontInfo] = []
    active_content_removed: list[str] = Field([], description="Entfernte aktive Inhalte (JavaScript, Aktionen, eingebettete Dateien …)")
    warnings: list[str] = []

    @property
    def fonts_embedded(self) -> int:
        return sum(f.status == FontStatus.embedded for f in self.fonts)

    @property
    def fonts_fallback(self) -> int:
        return sum(f.status == FontStatus.fallback for f in self.fonts)


class CompressBase64Response(BaseModel):
    filename: str
    content_base64: str
    report: CompressionReport


class AnalyzeResponse(BaseModel):
    filename: str
    size: int
    pages: int
    fonts: list[FontInfo]
    images: list[ImageInfo]
    active_content: list[str] = Field([], description="Gefundene aktive Inhalte")
