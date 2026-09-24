"""Hilfsfunktionen zum Durchlaufen von Seiten-Ressourcen (inkl. Form-XObjects und Annotationen)."""

from collections.abc import Iterator

import pikepdf
from pikepdf import Dictionary, Name


def object_key(obj: pikepdf.Object) -> tuple[int, int] | int:
    """Eindeutiger Schlüssel: Objektnummer bei indirekten Objekten, sonst die Python-Identität."""
    objgen = obj.objgen
    return objgen if objgen != (0, 0) else id(obj)


def page_resources(page: pikepdf.Dictionary) -> pikepdf.Dictionary | None:
    """/Resources einer Seite, inklusive Vererbung über den Seitenbaum."""
    node = page
    for _ in range(64):
        res = node.get("/Resources")
        if res is not None:
            return res
        node = node.get("/Parent")
        if node is None:
            return None
    return None


def iter_resources(pdf: pikepdf.Pdf) -> Iterator[pikepdf.Dictionary]:
    """Alle Resource-Dictionaries der Datei, jeweils nur einmal."""
    seen: set = set()
    stack: list[pikepdf.Dictionary] = []

    for page in pdf.pages:
        res = page_resources(page.obj)
        if res is not None:
            stack.append(res)
        for annot in page.obj.get("/Annots", pikepdf.Array()):
            if not isinstance(annot, Dictionary):
                continue
            ap = annot.get("/AP")
            if not isinstance(ap, Dictionary):
                continue
            for key in ("/N", "/R", "/D"):
                entry = ap.get(key)
                if entry is None:
                    continue
                streams = [entry] if isinstance(entry, pikepdf.Stream) else list(entry.values()) if isinstance(entry, Dictionary) else []
                for s in streams:
                    if isinstance(s, pikepdf.Stream) and "/Resources" in s:
                        stack.append(s.Resources)

    while stack:
        res = stack.pop()
        if not isinstance(res, Dictionary):
            continue
        key = object_key(res)
        if key in seen:
            continue
        seen.add(key)
        yield res

        xobjects = res.get("/XObject")
        if isinstance(xobjects, Dictionary):
            for xobj in xobjects.values():
                if isinstance(xobj, pikepdf.Stream) and xobj.get("/Subtype") == Name.Form and "/Resources" in xobj:
                    stack.append(xobj.Resources)
        patterns = res.get("/Pattern")
        if isinstance(patterns, Dictionary):
            for pat in patterns.values():
                if isinstance(pat, pikepdf.Stream) and "/Resources" in pat:
                    stack.append(pat.Resources)
        # Type3-Fonts haben eigene Ressourcen
        fonts = res.get("/Font")
        if isinstance(fonts, Dictionary):
            for font in fonts.values():
                if isinstance(font, Dictionary) and "/Resources" in font:
                    stack.append(font.Resources)
