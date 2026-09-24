"""Aktive Inhalte erkennen und entfernen (Content Disarm).

Das Tool selbst führt keine PDF-Inhalte aus (pdfium ist ohne JavaScript-Engine gebaut). Aktive Inhalte
würden aber an den Empfänger weitergereicht und könnten in dessen PDF-Viewer ausgeführt werden.
Entfernt werden deshalb alles, was beim Öffnen oder bei Interaktion Code ausführt, Programme startet,
Daten versendet oder Dateien mitbringt. Normale Links (URI) und interne Sprungmarken (GoTo) bleiben erhalten.
"""

import pikepdf
from pikepdf import Array, Dictionary, Name

from app.services.guard import check_deadline

DANGEROUS_ACTIONS = {
    "/JavaScript",       # Skript
    "/Launch",           # startet Programme / öffnet Dateien
    "/SubmitForm",       # sendet Formulardaten an eine URL
    "/ImportData",       # liest Daten aus einer Datei
    "/GoToR",            # öffnet eine andere (entfernte) PDF
    "/GoToE",            # öffnet eine eingebettete PDF
    "/Rendition",        # Multimedia, kann JavaScript enthalten
    "/Movie",
    "/Sound",
    "/RichMediaExecute",
    "/GoTo3DView",
}
DANGEROUS_ANNOTS = {"/FileAttachment", "/RichMedia", "/Movie", "/Sound", "/Screen", "/3D"}
MAX_DEPTH = 64


class _Scanner:
    """Durchläuft die PDF einmal; mit remove=True werden Funde gleich entfernt."""

    def __init__(self, pdf: pikepdf.Pdf, remove: bool):
        self.pdf = pdf
        self.remove = remove
        self.findings: list[str] = []
        self._seen: set = set()

    def _hit(self, what: str) -> None:
        self.findings.append(what)

    def _first_visit(self, obj) -> bool:
        key = obj.objgen if obj.objgen != (0, 0) else id(obj)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    # --- Aktionen

    def _action_dangerous(self, action, depth: int = 0) -> str | None:
        """Name der ersten gefährlichen Aktion in einer Kette (/Next), sonst None."""
        if depth > MAX_DEPTH or not isinstance(action, Dictionary):
            return None
        s = str(action.get("/S", ""))
        if s in DANGEROUS_ACTIONS:
            return s.lstrip("/")
        nxt = action.get("/Next")
        items = nxt if isinstance(nxt, Array) else [nxt] if nxt is not None else []
        for item in items:
            found = self._action_dangerous(item, depth + 1)
            if found:
                return found
        return None

    def _clean_action_key(self, holder: Dictionary, key: str, where: str) -> None:
        action = holder.get(key)
        if action is None:
            return
        found = self._action_dangerous(action)
        if found:
            self._hit(f"{where}: Aktion {found}")
            if self.remove:
                # ganze Kette entfernen - eine harmlose Folgeaktion ist den Aufwand nicht wert
                del holder[key]

    def _clean_aa(self, holder: Dictionary, where: str) -> None:
        """/AA = automatisch ausgelöste Aktionen (Öffnen, Schließen, Maus, Tastatur) - immer entfernen."""
        if "/AA" in holder:
            self._hit(f"{where}: automatische Aktionen (AA)")
            if self.remove:
                del holder["/AA"]

    def _clean_af(self, holder: Dictionary, where: str) -> None:
        """/AF = verknüpfte Dateien (PDF 2.0), also eingebettete Dateien."""
        if "/AF" in holder:
            self._hit(f"{where}: verknüpfte Dateien (AF)")
            if self.remove:
                del holder["/AF"]

    # --- Bereiche

    def catalog(self) -> None:
        root = self.pdf.Root
        open_action = root.get("/OpenAction")
        if isinstance(open_action, Dictionary):
            self._clean_action_key(root, "/OpenAction", "Dokument beim Öffnen")
        self._clean_aa(root, "Dokument")
        self._clean_af(root, "Dokument")

        names = root.get("/Names")
        if isinstance(names, Dictionary):
            for key, label in (("/JavaScript", "Dokument-JavaScript"), ("/EmbeddedFiles", "eingebettete Dateien")):
                if key in names:
                    self._hit(label)
                    if self.remove:
                        del names[key]
        if "/Collection" in root:
            self._hit("PDF-Portfolio")
            if self.remove:
                del root["/Collection"]

        acro = root.get("/AcroForm")
        if isinstance(acro, Dictionary):
            if "/XFA" in acro:
                self._hit("XFA-Formular")
                if self.remove:
                    del acro["/XFA"]
            if "/NeedsRendering" in acro and self.remove:
                del acro["/NeedsRendering"]
            for field in acro.get("/Fields", Array()):
                self._field(field, 0)

        outlines = root.get("/Outlines")
        if isinstance(outlines, Dictionary):
            self._outline(outlines.get("/First"), 0)

    def _field(self, field, depth: int) -> None:
        if depth > MAX_DEPTH or not isinstance(field, Dictionary) or not self._first_visit(field):
            return
        check_deadline()
        self._clean_aa(field, "Formularfeld")
        self._clean_action_key(field, "/A", "Formularfeld")
        for kid in field.get("/Kids", Array()):
            self._field(kid, depth + 1)

    def _outline(self, item, depth: int) -> None:
        # Lesezeichen: /First = erstes Kind, /Next = nächstes Geschwister (iterativ gegen tiefe Ketten)
        count = 0
        while isinstance(item, Dictionary) and depth <= MAX_DEPTH and self._first_visit(item):
            count += 1
            if count % 256 == 0:
                check_deadline()
            self._clean_action_key(item, "/A", "Lesezeichen")
            self._outline(item.get("/First"), depth + 1)
            item = item.get("/Next")

    def pages(self) -> None:
        for number, page in enumerate(self.pdf.pages, start=1):
            check_deadline()
            obj = page.obj
            where = f"Seite {number}"
            self._clean_aa(obj, where)
            self._clean_af(obj, where)
            annots = obj.get("/Annots")
            if not isinstance(annots, Array):
                continue
            keep = Array()
            changed = False
            for annot in annots:
                if not isinstance(annot, Dictionary):
                    keep.append(annot)
                    continue
                subtype = str(annot.get("/Subtype", ""))
                if subtype in DANGEROUS_ANNOTS:
                    self._hit(f"{where}: Annotation {subtype.lstrip('/')}")
                    changed = True
                    continue
                self._clean_aa(annot, f"{where}: Annotation")
                self._clean_af(annot, f"{where}: Annotation")
                self._clean_action_key(annot, "/A", f"{where}: Link")
                keep.append(annot)
            if changed and self.remove:
                obj.Annots = keep

    def run(self) -> list[str]:
        self.catalog()
        self.pages()
        return self.findings


def find_active_content(pdf: pikepdf.Pdf) -> list[str]:
    """Listet aktive Inhalte, ohne die Datei zu verändern."""
    return _Scanner(pdf, remove=False).run()


def remove_active_content(pdf: pikepdf.Pdf) -> list[str]:
    """Entfernt aktive Inhalte und gibt die Liste der entfernten Elemente zurück."""
    return _Scanner(pdf, remove=True).run()
