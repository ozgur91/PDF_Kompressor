"""Integritätsprüfung der mitgelieferten Dateien (Schutz gegen manipulierte Ersatzschriften)."""

import hashlib

from app.core.config import BUNDLED_FONT_DIR

CHECKSUM_FILE = BUNDLED_FONT_DIR / "SHA256SUMS"


class IntegrityError(RuntimeError):
    pass


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify_bundled_fonts() -> None:
    """Prüft jede Font-Datei gegen SHA256SUMS. Fehlende, zusätzliche oder veränderte Dateien -> IntegrityError."""
    expected: dict[str, str] = {}
    for line in CHECKSUM_FILE.read_text(encoding="ascii").splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            expected[name.strip().lstrip("*")] = digest.lower()

    present = {p.name for p in BUNDLED_FONT_DIR.glob("*.ttf")}
    if present != set(expected):
        raise IntegrityError(f"Font-Dateien weichen von SHA256SUMS ab: {sorted(present ^ set(expected))}")
    for name, digest in expected.items():
        if _sha256(BUNDLED_FONT_DIR / name) != digest:
            raise IntegrityError(f"Prüfsumme stimmt nicht: {name}")
