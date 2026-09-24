# Lizenzen von Drittkomponenten

Alle Komponenten stehen unter freizügigen Lizenzen und dürfen im Unternehmen (auch intern als Dienst) genutzt werden.
Es wird bewusst **keine** AGPL/GPL-Software verwendet (z. B. kein PyMuPDF und kein Ghostscript).

## Laufzeit (`requirements.txt`)
| Paket | Lizenz | Hinweis |
|---|---|---|
| fastapi | MIT | |
| starlette | BSD-3-Clause | Abhängigkeit von FastAPI |
| pydantic, pydantic-settings | MIT | |
| uvicorn | BSD-3-Clause | |
| python-multipart | Apache-2.0 | |
| pikepdf | MPL-2.0 | Nur geänderte pikepdf-Dateien selbst wären offenzulegen, eigener Code ist nicht betroffen |
| qpdf (in pikepdf enthalten) | Apache-2.0 | |
| Pillow | MIT-CMU (HPND) | |
| fonttools | MIT | |
| pypdfium2 / PDFium | Apache-2.0 bzw. BSD-3-Clause | |

## Mitgelieferte Schriften (`app/fonts/`)
| Schrift | Lizenz |
|---|---|
| Liberation Sans / Serif / Mono 2.1.5 | SIL Open Font License 1.1, siehe [app/fonts/OFL.txt](app/fonts/OFL.txt) |

Die OFL erlaubt das Mitliefern, Einbetten und Weitergeben. Die Schriften dürfen nur nicht einzeln verkauft werden.

## Systemschriften
Schriften aus dem Betriebssystem (z. B. Arial unter Windows) werden nur eingebettet, wenn ihr Lizenz-Flag
(`OS/2 fsType`) das erlaubt („Installable", „Editable" oder „Preview & Print"). Bei „Restricted License"
oder „Bitmap only" wird stattdessen die Liberation-Fallback-Schrift eingebettet.

## Nur Entwicklung (`requirements-dev.txt`)
| Paket | Lizenz |
|---|---|
| pytest | MIT |
| httpx | BSD-3-Clause |
| reportlab | BSD-3-Clause |
