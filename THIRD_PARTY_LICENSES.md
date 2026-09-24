# Lizenzen von Drittkomponenten

Alle Komponenten stehen unter freizügigen Lizenzen und dürfen im Unternehmen (auch intern als Dienst) genutzt werden.
Es wird bewusst **keine** AGPL/GPL-Software verwendet (z. B. kein PyMuPDF und kein Ghostscript).

## Laufzeit (`requirements.txt`, inkl. indirekter Abhängigkeiten)
| Paket | Lizenz | Hinweis |
|---|---|---|
| fastapi | MIT | |
| starlette | BSD-3-Clause | Abhängigkeit von FastAPI |
| pydantic, pydantic-core, pydantic-settings | MIT | |
| uvicorn | BSD-3-Clause | ohne die optionalen `[standard]`-Pakete |
| h11 | MIT | HTTP-Parser von uvicorn |
| click | BSD-3-Clause | Kommandozeile von uvicorn |
| anyio | MIT | |
| python-multipart | Apache-2.0 | |
| python-dotenv | BSD-3-Clause | `.env`-Unterstützung von pydantic-settings |
| pikepdf | MPL-2.0 | Nur geänderte pikepdf-Dateien selbst wären offenzulegen, eigener Code ist nicht betroffen |
| qpdf (in pikepdf enthalten) | Apache-2.0 | |
| lxml | BSD-3-Clause | Abhängigkeit von pikepdf |
| Pillow | MIT-CMU (HPND) | |
| fonttools | MIT | |
| pypdfium2 / PDFium | Apache-2.0 bzw. BSD-3-Clause | |
| packaging | Apache-2.0 oder BSD-2-Clause | |
| idna | BSD-3-Clause | |
| annotated-doc, annotated-types, typing-inspection | MIT | |
| typing-extensions | PSF-2.0 | |

## Mitgelieferte Schriften (`app/fonts/`)
| Schrift | Lizenz |
|---|---|
| Liberation Sans / Serif / Mono 2.1.5 | SIL Open Font License 1.1, siehe [app/fonts/OFL.txt](app/fonts/OFL.txt) |

Die OFL erlaubt das Mitliefern, Einbetten und Weitergeben. Die Schriften dürfen nur nicht einzeln verkauft werden.

## Systemschriften
Schriften aus dem Betriebssystem (z. B. Arial unter Windows) werden nur eingebettet, wenn ihr Lizenz-Flag
(`OS/2 fsType`) das erlaubt („Installable", „Editable" oder „Preview & Print"). Bei „Restricted License"
oder „Bitmap only" wird stattdessen die Liberation-Fallback-Schrift eingebettet.

## Nur Entwicklung und Prüfung (`requirements-dev.txt`)
Diese Pakete werden im Betrieb **nicht** installiert.

| Paket | Lizenz |
|---|---|
| pytest (+ pluggy, iniconfig) | MIT |
| httpx, httpcore | BSD-3-Clause |
| reportlab | BSD-3-Clause |
| pip-audit (+ pip-api, cyclonedx-python-lib, py-serializable, cachecontrol, requests …) | Apache-2.0 |
| bandit (+ stevedore) | Apache-2.0 |
| pip-tools (+ build, pyproject-hooks, wheel) | BSD / MIT |
| certifi | MPL-2.0 |
| weitere indirekte Pakete (rich, pygments, pyyaml, urllib3, platformdirs …) | MIT, BSD oder Apache-2.0 |

Stand der Prüfung: alle Pakete der Lock-Dateien, ausgelesen aus den Paket-Metadaten. Keine Copyleft-Lizenz (GPL/AGPL/LGPL).
