# Sicherheitskonzept – PDF-Kompressor

Dieses Dokument beschreibt die Sicherheitsmaßnahmen des PDF-Kompressors für den Betrieb in einem
Hochrisiko-Umfeld (Verarbeitung von Dokumenten mit Gesundheitsdaten). Es richtet sich an Betrieb,
IT-Sicherheit (ISB) und Entwicklung.

> **Einordnung:** Die Maßnahmen reduzieren das Risiko deutlich, eine absolute Sicherheit gibt es nicht.
> Vor dem Produktivstart wird eine Freigabe bzw. ein Penetrationstest durch die IT-Sicherheit empfohlen
> (siehe [Restrisiken](#7-restrisiken)).

## 1. Bedrohungsmodell

| Angreifer / Quelle | Ziel | Hauptmaßnahmen |
|---|---|---|
| Präparierte PDF (Upload) | Code auf dem Server ausführen | Keine Ausführung von PDF-Inhalten (pdfium ohne JavaScript/XFA), keine `eval`/`exec`/`subprocess`/`pickle`, gepatchte Bibliotheken |
| Präparierte PDF (Upload) | Server lahmlegen (DoS) | Größen-, Pixel-, Objekt- und Seitenlimits, Prüfung auf Dekompressions-Bomben, Zeitbudget, Parallelitätslimit |
| Präparierte PDF (Upload) | Schadcode an Empfänger weiterreichen | Aktive Inhalte werden standardmäßig entfernt (JavaScript, Launch, eingebettete Dateien …), fail closed |
| Nutzer im Netz | Browser-Angriffe (XSS, Clickjacking) | strenge Content-Security-Policy, keine Inline-Skripte, Ausgabe-Maskierung, `X-Frame-Options: DENY` |
| Nutzer im Netz | Informationen ausspähen | generische Fehlermeldungen, kein `server`-Header, API-Docs aus, `Cache-Control: no-store` |
| Lieferkette | manipulierte oder untergeschobene Pakete | gepinnte Versionen mit SHA-256-Hashes, pip-audit, Dependabot, Prüfsummen der mitgelieferten Schriften |

## 2. Maßnahmen im Detail

### 2.1 Keine Ausführung von Inhalten
- **pdfium** (Rendering zur Validierung) ist ohne V8-JavaScript-Engine und ohne XFA gebaut
  (`pypdfium2.version.PDFIUM_INFO.flags == ()`). PDF-JavaScript kann auf dem Server nicht ausgeführt werden.
- **pikepdf/qpdf** und **fontTools** lesen und schreiben nur Datenstrukturen, sie führen nichts aus.
- Der Code enthält keine Aufrufe von `eval`, `exec`, `subprocess`, `os.system`, `pickle` und keine ausgehenden Netzwerkverbindungen
  (geprüft mit bandit und per Code-Suche). Empfehlung: ausgehenden Traffic des Servers per Firewall sperren.
- Hochgeladene Dateien werden **nie auf die Festplatte geschrieben** (auch nicht als Temp-Datei), sondern nur im Arbeitsspeicher verarbeitet.

### 2.2 Aktive Inhalte entfernen (Content Disarm) – `app/services/sanitizer.py`
Standardmäßig (Parameter `remove_active_content=true`) werden entfernt:

| Element | Warum |
|---|---|
| `OpenAction` mit gefährlicher Aktion, `/AA` (automatische Aktionen) an Dokument, Seiten, Annotationen, Formularfeldern | wird beim Öffnen, Schließen oder bei Mausbewegungen ausgelöst |
| Aktionen `JavaScript`, `Launch`, `SubmitForm`, `ImportData`, `GoToR`, `GoToE`, `Rendition`, `Movie`, `Sound`, `RichMediaExecute`, `GoTo3DView` (auch in `/Next`-Ketten, Links, Lesezeichen, Formularfeldern) | führt Code aus, startet Programme, versendet Daten, lädt fremde Dateien |
| Dokument-JavaScript (`/Names/JavaScript`) | läuft beim Öffnen |
| eingebettete Dateien (`/Names/EmbeddedFiles`, `/AF`, `FileAttachment`-Annotationen), PDF-Portfolios (`/Collection`) | können Schadprogramme enthalten |
| `RichMedia`, `Movie`, `Sound`, `Screen`, `3D`-Annotationen | Multimedia/3D, kann Skripte enthalten |
| XFA-Formulare | eigene Skript-Umgebung |

Erhalten bleiben normale Web-Links (`URI`) und interne Sprungmarken (`GoTo`).
**Fail closed:** Enthält eine PDF aktive Inhalte und lässt sich nicht bereinigen, wird sie mit `422` abgelehnt.
Das Original mit aktiven Inhalten wird nie ausgeliefert. Nach der Bereinigung wird das Ergebnis erneut geprüft.
Mit `remove_active_content=false` bleiben die Inhalte erhalten, und der Report enthält eine Warnung.

### 2.3 Schutz vor präparierten Dateien – `app/services/guard.py`
Vor jeder Verarbeitung prüft `inspect_streams` **jeden Stream der Datei**, bevor qpdf, Pillow oder pdfium ihn entpacken:

| Grenze | Default | Variable |
|---|---|---|
| Upload-Größe (auch ohne `Content-Length`/bei Chunked-Upload) | 50 MB | `PDF_MAX_UPLOAD_MB` |
| Seiten | 5000 | `PDF_MAX_PAGES` |
| entpackte Größe pro Stream (Flate wird gestreamt geprüft, ohne komplett zu entpacken) | 100 MB; **Bilder**: so viel, wie ihre Maße verlangen (Breite × Höhe × Kanäle × Bittiefe, +10 %) | `PDF_MAX_STREAM_MB` |
| entpackte Größe aller Streams (Stream für Stream geprüft, begrenzt Rechenzeit, nicht Speicher) | 50 GB | `PDF_MAX_TOTAL_DECODED_MB` |
| Pixel pro Bild (auch in Pillow erzwungen) | 100 Mio. | `PDF_MAX_IMAGE_PIXELS` |
| Objekte / Schriften / Bilder pro Datei | 2 Mio. / 5.000 / 20.000 | `PDF_MAX_OBJECTS`, `PDF_MAX_FONTS`, `PDF_MAX_IMAGES` |
| Zeitbudget pro Datei | 300 s | `PDF_PROCESSING_TIMEOUT_S` |
| gleichzeitige Verarbeitungen | Anzahl CPU-Kerne | `PDF_MAX_CONCURRENT` |
| Wartezeit auf freien Platz, danach `503` | 10 s | `PDF_QUEUE_TIMEOUT_S` |

Für LZW- und RunLength-Streams wird die maximal mögliche Ausdehnung angenommen, unbekannte Filter gelten als zu groß.

Die Grenzen sind auf große Dokumente ausgelegt (Messung auf einem Arbeitsplatzrechner): Bericht mit 1500 Seiten ca. 3 s, Scan mit 400 Seiten in 300 DPI (3,5 GB entpackt) ca. 55 s.
Formulare dürfen genau eine Datei (Feld `file`) und höchstens 10 kurze Textfelder enthalten; unbekannte Felder werden abgelehnt.

### 2.4 Web-Sicherheit – `app/core/security.py`, `app/main.py`
- **Security-Header** auf jeder Antwort, auch auf Fehlern:
  `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; font-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'`,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy`,
  `Cross-Origin-Opener-Policy`/`Cross-Origin-Resource-Policy: same-origin`, `Cache-Control: no-store`,
  optional `Strict-Transport-Security` (`PDF_HSTS=true`, nur hinter TLS).
- Das **Frontend** enthält kein Inline-JavaScript, kein Inline-CSS und keine Inline-Event-Handler. Alle Server-Daten werden vor der Ausgabe maskiert.
- **CORS ist aus** (nur gleiche Origin). Freigaben nur explizit über `PDF_CORS_ORIGINS`, ohne Credentials.
- **Host-Header-Prüfung** (`PDF_ALLOWED_HOSTS`, Default `["localhost", "127.0.0.1"]`) – im Betrieb auf den echten Servernamen setzen.
- **Swagger/ReDoc/OpenAPI sind aus** (`PDF_ENABLE_DOCS=false`); sie würden Skripte von einem CDN laden.
- **Keine Informationslecks:** Clients erhalten feste Meldungen ohne Exception-Texte, Pfade oder Versionsnummern.
  Bei unerwarteten Fehlern gibt es nur eine Referenz (`X-Request-ID`), die Details stehen im Log.
- **Dateinamen** aus dem Upload werden bereinigt (keine Pfade, Steuerzeichen, Anführungszeichen; max. 100 Zeichen) – Schutz gegen Header-Injection.

### 2.5 Datenschutz und Protokollierung
- Es werden **keine Dateiinhalte und keine Dateinamen** protokolliert (Dateinamen können personenbezogene Daten enthalten).
- Pro Anfrage gibt es eine Audit-Zeile: Request-ID, Endpoint, Client-IP, HTTP-Status, Dauer, dazu Größen, Ergebnis und die Anzahl entfernter aktiver Inhalte.
- Antworten werden nicht zwischengespeichert (`Cache-Control: no-store`).

### 2.6 Lieferkette
- **Gepinnte Versionen mit SHA-256-Hashes** für alle direkten und indirekten Pakete (`requirements.txt`, `requirements-dev.txt`, erzeugt aus `*.in` mit pip-tools).
  Installation nur mit `pip install --require-hashes …`; ein verändertes Paket wird dabei abgelehnt.
- Die Paketnamen wurden gegen die offiziellen PyPI-Projekte und deren Quell-Repositories geprüft (Schutz gegen *Slopsquatting* bzw. *Typosquatting*).
- `uvicorn` läuft ohne die optionalen Zusatzpakete (`[standard]`), um die Angriffsfläche klein zu halten.
- **pip-audit** (bekannte Schwachstellen) und **bandit** (Code-Analyse) laufen in der CI bei jedem Push, jedem Pull Request und **wöchentlich**.
  **Dependabot** schlägt Updates für Pakete und GitHub Actions vor. Die Actions sind auf Commit-SHAs gepinnt.
- Die mitgelieferten Ersatzschriften werden beim Start gegen `app/fonts/SHA256SUMS` geprüft. Bei Abweichung startet die App nicht.

## 3. KI-Sicherheitsrisiken (OWASP Top 10 for LLM Applications 2025)

Die Anwendung **enthält kein KI-Modell, kein LLM, keine Prompts, keine Embeddings und ruft keine KI-Dienste auf**.
Sie verarbeitet PDFs ausschließlich mit klassischen, deterministischen Bibliotheken.

| Risiko | Bewertung |
|---|---|
| LLM01 Prompt Injection | nicht anwendbar, es gibt keine Prompts oder Modelle |
| LLM02 Sensitive Information Disclosure | nicht anwendbar für KI; Daten verlassen den Server nicht (keine ausgehenden Verbindungen) |
| LLM03 Supply Chain | **relevant** (allgemein für die Lieferkette): siehe 2.6 |
| LLM04 Data and Model Poisoning | nicht anwendbar, es gibt kein Modell und kein Training |
| LLM05 Improper Output Handling | nicht anwendbar für KI; allgemein: Ausgaben werden maskiert (Frontend), Dateinamen bereinigt |
| LLM06 Excessive Agency | nicht anwendbar, kein Agent und keine Werkzeugaufrufe |
| LLM07 System Prompt Leakage | nicht anwendbar |
| LLM08 Vector and Embedding Weaknesses | nicht anwendbar |
| LLM09 Misinformation | nicht anwendbar |
| LLM10 Unbounded Consumption | nicht anwendbar für KI; allgemein: Ressourcenlimits, siehe 2.3 |

**Relevantes KI-Risiko: KI-unterstützt erstellter Code.** Der Code wurde mit KI-Unterstützung geschrieben. Gegenmaßnahmen:
automatisierte Tests (inkl. Sicherheitstests), statische Analyse (bandit), Abhängigkeits-Audit (pip-audit), geprüfte Paketnamen
und gepinnte Hashes. **Vor dem Produktivstart ist eine menschliche Code-Review empfohlen**, besonders für
`app/services/sanitizer.py`, `app/services/guard.py` und `app/core/security.py`.

## 4. Betrieb: Härtungs-Checkliste

- [ ] Installation ausschließlich mit Hashes: `pip install --require-hashes -r requirements.txt`
- [ ] Start mit:
  ```
  uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-server-header --proxy-headers --forwarded-allow-ips=<IP des Reverse Proxy> --timeout-keep-alive 5 --limit-concurrency 64
  ```
- [ ] Vorgelagerter **Reverse Proxy mit TLS**; die App selbst nur an `127.0.0.1` binden. Danach `PDF_HSTS=true` setzen.
- [ ] **Timeouts und Upload-Größe am Proxy** an die App anpassen: Lese-/Antwort-Timeout mindestens `PDF_PROCESSING_TIMEOUT_S` + 30 s (Standard also ≥ 330 s), maximale Body-Größe ≥ `PDF_MAX_UPLOAD_MB` (z. B. nginx `proxy_read_timeout 330s; client_max_body_size 51m;`).
- [ ] `PDF_ALLOWED_HOSTS` auf den echten Servernamen setzen (JSON-Liste, z. B. `["pdf.intern.example"]`).
- [ ] Eigenes **Dienstkonto ohne Administratorrechte**; Programmordner und virtuelle Umgebung für dieses Konto **schreibgeschützt**.
- [ ] **Ausgehende Verbindungen** des Servers per Firewall sperren (die App braucht keine).
- [ ] `PDF_ENABLE_DOCS` bleibt `false`; `PDF_CORS_ORIGINS` bleibt leer, solange keine anderen Webanwendungen zugreifen müssen.
- [ ] Logs (stdout) zentral sammeln; die Audit-Zeilen enthalten keine Inhalte.
- [ ] Optional: Uploads zusätzlich über die vorhandene Virenschutz-/ICAP-Lösung scannen.
- [ ] Updates: Dependabot-PRs und CI-Ergebnisse regelmäßig prüfen und zeitnah einspielen, besonders für pikepdf, pypdfium2, Pillow und fontTools.

## 5. Konfiguration (Sicherheitsrelevant)

| Variable | Default | Hinweis |
|---|---|---|
| `PDF_ALLOWED_HOSTS` | `["localhost","127.0.0.1"]` | im Betrieb anpassen |
| `PDF_CORS_ORIGINS` | `[]` | leer = CORS aus |
| `PDF_ENABLE_DOCS` | `false` | Swagger/ReDoc/OpenAPI |
| `PDF_HSTS` | `false` | nur hinter TLS aktivieren |
| weitere Limits | siehe Tabelle 2.3 | |

## 6. Prüfen

```bash
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
python -m pytest -q                          # inkl. tests/test_security.py
python -m pip_audit -r requirements.txt --require-hashes
python -m bandit -r app -ll
```

## 7. Restrisiken

| Restrisiko | Einordnung / Gegenmaßnahme |
|---|---|
| Unbekannte Speicherfehler (0-Day) in den C-Bibliotheken qpdf, pdfium, libjpeg/zlib (Pillow), FreeType | Das ist das Hauptrisiko jeder PDF-Verarbeitung. Gegenmaßnahmen: zeitnahe Updates, Limits, keine Admin-Rechte, Ausgangs-Firewall. Eine zusätzliche Prozess-Isolation (Sandbox) wurde bewusst nicht umgesetzt und kann bei Bedarf nachgerüstet werden. |
| Rechenintensive, aber gültige Seiten beim Rendern in pdfium | Ein laufender C-Aufruf kann das Zeitbudget nicht unterbrechen. Die Render-Auflösung ist klein (20 %) und die Zahl der gerenderten Seiten begrenzt. |
| Keine Anmeldung in der App | Bewusste Entscheidung: Die Umgebung ist abgesichert. Der Zugriff muss über Netzsegmentierung bzw. Reverse Proxy begrenzt sein. |
| Inhalte, die keine aktiven Inhalte sind (z. B. Phishing-Text oder -Links in der PDF) | Werden nicht erkannt; normale Links bleiben bewusst erhalten. |

## 8. Sicherheitslücken melden

Vermutete Schwachstellen bitte **nicht** als öffentliches Issue melden, sondern direkt an die verantwortliche
Person bzw. die IT-Sicherheit des Betreibers, mit Beschreibung, betroffener Version und nach Möglichkeit einer Beispieldatei.
