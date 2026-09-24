# PDF-Kompression (FastAPI)

Backend zum Komprimieren von PDF-Dateien für den internen Einsatz. Es braucht nur Python und pip,
also keine externen Programme wie Ghostscript und kein Docker. Alle Abhängigkeiten stehen unter freizügigen Lizenzen,
es ist keine AGPL/GPL-Software dabei (siehe [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)).

## Funktionen
- **Bilder herunterrechnen** in 3 Stufen: `low` = 72 DPI, `medium` = 150 DPI, `high` = 300 DPI.
  Maßgeblich ist die *tatsächlich dargestellte* Auflösung auf der Seite. Bilder, die schon nahe an der Ziel-DPI
  liegen (≤ 1,5 × Ziel), bleiben unverändert.
- **Nur wenn es funktioniert**: Das Ergebnis wird geprüft (öffnen, Seitenzahl, Rendern jeder Seite, Schriften).
  Ausgeliefert wird es nur, wenn es gültig *und* kleiner ist. Sonst kommt das Original unverändert zurück.
- **Schriftarten einbetten**: Nicht eingebettete Schriften werden auf dem Server gesucht (z. B. Helvetica → Arial)
  und als Untergruppe eingebettet. Gibt es die Schrift nicht oder erlaubt ihre Lizenz (`fsType`) kein Einbetten,
  wird eine Fallback-Schrift eingebettet (Liberation Sans/Serif/Mono, metrisch kompatibel zu Arial/Times/Courier).
  Die Zeichenbreiten der PDF bleiben erhalten, damit sich das Layout nicht verschiebt.
- Mussten Schriften eingebettet werden und wird die Datei dadurch nicht kleiner, wird die Version **mit eingebetteten
  Schriften, aber unveränderten Bildern** geliefert (`fonts_only`).

## Start
```bash
py -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
- Weboberfläche: http://localhost:8000/ (alle Parameter einstellbar, Download bzw. Base64-Ansicht, Analyse)
- Swagger-UI: http://localhost:8000/docs

## Endpoints
Alle Endpoints erwarten `multipart/form-data`.

| Parameter | Werte | Default |
|---|---|---|
| `file` | PDF-Datei | – |
| `level` | `low` \| `medium` \| `high` | `medium` |
| `fallback_font` | `auto` \| `sans` \| `serif` \| `mono` | `auto` (je nach Schrift: Serif/Mono/Sans) |
| `embed_fonts` | `true` \| `false` | `true` |

### `POST /api/v1/pdf/compress` → PDF-Datei
```bash
curl -F "file=@bericht.pdf" -F level=medium -o bericht_compressed.pdf -D - http://localhost:8000/api/v1/pdf/compress
```
Der Report steht in den Response-Headern:

| Header | Bedeutung |
|---|---|
| `X-Result` | `compressed`, `fonts_only` oder `original` |
| `X-Original-Size` / `X-Result-Size` | Größe in Bytes |
| `X-Images-Downsampled` | Anzahl verkleinerter Bilder |
| `X-Fonts-Embedded` / `X-Fonts-Fallback` | eingebettete Schriften (Original bzw. Ersatz) |
| `X-Warnings` | Anzahl Warnungen (Details liefert der Base64-Endpoint) |

### `POST /api/v1/pdf/compress/base64` → JSON
```json
{
  "filename": "bericht_compressed.pdf",
  "content_base64": "JVBERi0xLjUK...",
  "report": {
    "original_size": 8760986, "result_size": 177185, "result": "compressed",
    "level": "medium", "dpi": 150, "images_downsampled": 1,
    "fonts": [{ "name": "Helvetica", "subtype": "Type1", "status": "embedded", "used_font": "ArialMT", "reason": null }],
    "warnings": []
  }
}
```
Beide Endpoints liefern für dieselbe Eingabe exakt dieselben PDF-Bytes.

### `POST /api/v1/pdf/analyze` → JSON
Zeigt Schriften (eingebettet ja/nein) und Bilder (Pixelgröße, effektive DPI), ohne die Datei zu verändern.

### Fehlercodes
`400` keine/beschädigte PDF · `413` Datei zu groß · `422` verschlüsselte PDF, zu viele Seiten oder ungültiger Parameter

## Konfiguration (Umgebungsvariablen oder `.env`)
| Variable | Default |
|---|---|
| `PDF_MAX_UPLOAD_MB` | `50` |
| `PDF_MAX_PAGES` | `2000` |
| `PDF_CORS_ORIGINS` | `["*"]` (JSON-Liste, für den Betrieb einschränken) |
| `PDF_FONT_DIRS` | System-Font-Ordner von Windows, Linux und macOS (JSON-Liste) |
| `PDF_DOWNSAMPLE_THRESHOLD` | `1.5` |

## Grenzen
- Übersprungen und unverändert bleiben (sicherheitshalber): CMYK-, 1-Bit-, 16-Bit-, JBIG2-, JPEG2000- und CCITT-Bilder,
  Bilder mit Farbschlüssel-Maske sowie Inline-Bilder.
- Nicht eingebettete **CID-Schriften** (Type0, meist Chinesisch/Japanisch/Koreanisch) und **symbolische Schriften**
  (Symbol, ZapfDingbats, Wingdings) werden nicht ersetzt, sondern als Warnung gemeldet.
- Welche Originalschriften gefunden werden, hängt vom Server ab. Unter Linux ohne Microsoft-Fonts wird für Arial/Times/Courier
  automatisch Liberation eingebettet.

## Design anpassen
- **Farben**: Sie stehen zentral im Block „AOK-Farben“ ganz oben in [app/static/index.html](app/static/index.html)
  (`--brand`, `--brand-strong`, `--brand-soft`, `--brand-accent`, `--on-brand`, jeweils für den Hell- und den Dunkelmodus).
  Die Werte sind **Näherungen** an das AOK-Grün und sollten durch die offiziellen Werte aus dem Corporate Design ersetzt werden.
- **Logo**: Die offizielle Logodatei als `app/static/logo.svg` ablegen. Sie erscheint dann automatisch im Header.
  Ohne Datei wird kein Logo angezeigt. Für PNG den `src` im `<img class="logo">` anpassen.

## Tests
```bash
pip install -r requirements-dev.txt
pytest
```
