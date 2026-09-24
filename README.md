# PDF-Kompression (FastAPI)

Ausführliche technische Dokumentation (alle Dateien und Funktionen): [docs/DOKUMENTATION.md](docs/DOKUMENTATION.md) ·
Sicherheitskonzept und Betriebs-Checkliste: [SECURITY.md](SECURITY.md)

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
- **Aktive Inhalte entfernen** (Standard): JavaScript, automatische Aktionen, Programmstarts, eingebettete Dateien,
  Multimedia und XFA werden entfernt, normale Links bleiben. Eine Datei mit aktiven Inhalten wird nie unbereinigt ausgeliefert
  (`sanitized`, falls sonst nichts verkleinert wurde; `422`, falls die Bereinigung fehlschlägt).
- **Gehärtet für den Produktivbetrieb**: Schutz vor Dekompressions- und Pixel-Bomben, Zeit- und Parallelitätslimits,
  strenge Security-Header und CSP, gepinnte Abhängigkeiten mit Hashes. Details in [SECURITY.md](SECURITY.md).

## Start
```bash
py -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install --require-hashes -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-server-header
```
- Weboberfläche: http://localhost:8000/ (alle Parameter einstellbar, Download bzw. Base64-Ansicht, Analyse)
- Swagger-UI (nur zur Entwicklung): mit `PDF_ENABLE_DOCS=true` unter http://localhost:8000/docs
- Für den Produktivbetrieb (Reverse Proxy, Host-Namen, Dienstkonto …) siehe die Checkliste in [SECURITY.md](SECURITY.md#4-betrieb-härtungs-checkliste).

## Endpoints
Alle Endpoints erwarten `multipart/form-data`.

| Parameter | Werte | Default |
|---|---|---|
| `file` | PDF-Datei | – |
| `level` | `low` \| `medium` \| `high` | `medium` |
| `fallback_font` | `auto` \| `sans` \| `serif` \| `mono` | `auto` (je nach Schrift: Serif/Mono/Sans) |
| `embed_fonts` | `true` \| `false` | `true` |
| `remove_active_content` | `true` \| `false` | `true` |

Nur `/analyze` erwartet allein das Feld `file`. Unbekannte Felder, mehrere Dateien oder mehr als 10 Felder werden abgelehnt.

### `POST /api/v1/pdf/compress` → PDF-Datei
```bash
curl -F "file=@bericht.pdf" -F level=medium -o bericht_compressed.pdf -D - http://localhost:8000/api/v1/pdf/compress
```
Der Report steht in den Response-Headern:

| Header | Bedeutung |
|---|---|
| `X-Result` | `compressed`, `fonts_only`, `sanitized` oder `original` |
| `X-Original-Size` / `X-Result-Size` | Größe in Bytes |
| `X-Images-Downsampled` | Anzahl verkleinerter Bilder |
| `X-Fonts-Embedded` / `X-Fonts-Fallback` | eingebettete Schriften (Original bzw. Ersatz) |
| `X-Active-Content-Removed` | Anzahl entfernter aktiver Inhalte |
| `X-Request-ID` | Referenz für Rückfragen und Log-Suche |
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
    "active_content_removed": ["Dokument beim Öffnen: Aktion JavaScript"],
    "warnings": []
  }
}
```
Beide Endpoints liefern für dieselbe Eingabe exakt dieselben PDF-Bytes.

### `POST /api/v1/pdf/analyze` → JSON
Zeigt Schriften (eingebettet ja/nein), Bilder (Pixelgröße, effektive DPI) und gefundene aktive Inhalte, ohne die Datei zu verändern.

### Fehlercodes
`400` keine/beschädigte PDF oder ungültiges Formular · `413` Datei zu groß · `415` kein `multipart/form-data` ·
`422` verschlüsselte PDF, Sicherheitsgrenze überschritten, Zeitbudget abgelaufen, aktive Inhalte nicht entfernbar oder ungültiger Parameter ·
`503` ausgelastet (mit `Retry-After`)

## Konfiguration (Umgebungsvariablen oder `.env`)
| Variable | Default |
|---|---|
| `PDF_MAX_UPLOAD_MB` | `50` |
| `PDF_MAX_PAGES` | `5000` |
| `PDF_ALLOWED_HOSTS` | `["localhost","127.0.0.1"]` (JSON-Liste, im Betrieb auf den Servernamen setzen) |
| `PDF_CORS_ORIGINS` | `[]` (CORS aus) |
| `PDF_ENABLE_DOCS` | `false` |
| `PDF_HSTS` | `false` |
| `PDF_FONT_DIRS` | System-Font-Ordner von Windows, Linux und macOS (JSON-Liste) |
| `PDF_DOWNSAMPLE_THRESHOLD` | `1.5` |

Die Sicherheitsgrenzen (Stream-Größen, Pixel, Zeitbudget, Parallelität …) sind in [SECURITY.md](SECURITY.md#23-schutz-vor-präparierten-dateien--appservicesguardpy) beschrieben.

## Grenzen
- Übersprungen und unverändert bleiben (sicherheitshalber): CMYK-, 1-Bit-, 16-Bit-, JBIG2-, JPEG2000- und CCITT-Bilder,
  Bilder mit Farbschlüssel-Maske sowie Inline-Bilder.
- Nicht eingebettete **CID-Schriften** (Type0, meist Chinesisch/Japanisch/Koreanisch) und **symbolische Schriften**
  (Symbol, ZapfDingbats, Wingdings) werden nicht ersetzt, sondern als Warnung gemeldet.
- Welche Originalschriften gefunden werden, hängt vom Server ab. Unter Linux ohne Microsoft-Fonts wird für Arial/Times/Courier
  automatisch Liberation eingebettet.

## Design anpassen
- **Farben**: Sie stehen zentral im Block „AOK-Farben“ ganz oben in [app/static/app.css](app/static/app.css)
  (`--brand`, `--brand-strong`, `--brand-soft`, `--brand-accent`, `--on-brand`, jeweils für den Hell- und den Dunkelmodus).
  Die Werte sind **Näherungen** an das AOK-Grün und sollten durch die offiziellen Werte aus dem Corporate Design ersetzt werden.
- **Logo**: Die offizielle Logodatei als `app/static/logo.svg` ablegen. Sie erscheint dann automatisch im Header.
  Ohne Datei wird kein Logo angezeigt. Für PNG den `src` im `<img id="logo">` in [app/static/index.html](app/static/index.html) anpassen.
- Wegen der Content-Security-Policy gehören Styles nach `app.css` und Skripte nach `app.js`, nicht inline ins HTML.

## Tests
```bash
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
pytest
python -m pip_audit -r requirements.txt --require-hashes
python -m bandit -r app -ll
```

## Abhängigkeiten aktualisieren
Direkte Abhängigkeiten stehen in `requirements.in` bzw. `requirements-dev.in`. Die Lock-Dateien mit Hashes werden so neu erzeugt:
```bash
pip-compile --generate-hashes --strip-extras --allow-unsafe --upgrade -o requirements.txt requirements.in
pip-compile --generate-hashes --strip-extras --allow-unsafe --upgrade -o requirements-dev.txt requirements-dev.in
```
Danach Tests, `pip-audit` und `bandit` ausführen.
