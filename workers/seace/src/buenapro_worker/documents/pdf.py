from __future__ import annotations

import hashlib
import re
import unicodedata


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_mime(data: bytes) -> str:
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return "application/octet-stream"


def _fold(text: str) -> str:
    """Lowercase, no accents, every run of other characters as one space."""
    plain = "".join(char for char in unicodedata.normalize("NFKD", str(text or "").casefold())
                    if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", plain).strip()


# The name is evidence of the role of a file whatever its content classifier said: a TDR is
# called TDR, "terminos de referencia", "requerimiento" or RTM; an EETT "EETT" or
# "especificaciones tecnicas". Digits glued to the word ("TDR1") do not hide it.
_TDR_NAME = re.compile(r"\btdr\d*\b|\bterminos? de ref\w*|\brtm\b|\brequer\w*")
_EETT_NAME = re.compile(r"\beett\d*\b|\bee tt\b|\bespecificaci\w*")
_BASES_NAME = re.compile(r"\bbases?\b")
_ACTA_NAME = re.compile(r"\bactas?\b")
ARCHIVE_EXTENSIONS = (".rar", ".zip", ".7z")


def is_archive_name(filename: str) -> bool:
    return str(filename or "").strip().lower().endswith(ARCHIVE_EXTENSIONS)


def classify_document(filename: str, mime: str) -> str:
    """Role of a file from its name: tdr, eett, bases, acta or otro.

    The name decides for any format: a Word or an archive named TDR is still the TDR, even
    though only a PDF can be read. ``mime`` is kept for callers; readability is decided where
    the document is read, not here.
    """
    name = _fold(filename)
    if _TDR_NAME.search(name):
        return "tdr"
    if _EETT_NAME.search(name):
        return "eett"
    if _BASES_NAME.search(name):
        return "bases"
    if _ACTA_NAME.search(name):
        return "acta"
    return "otro"


# Suppliers' own papers mention the requirement they declare compliance with
# ("Declaracion jurada de cumplimiento de RTM", "Documentos que acreditan el cumplimiento
# de EETT"): by name alone they are not the requirement.
_SUPPLIER_PAPER_NAME = re.compile(
    r"\bdeclaracion\w*|\bddjj\b|\bdj\b|\bjurada\b|\bcumplimiento\b|\bacredit\w*|\banexos?\b|"
    r"\bformatos?\b|\bcartas?\b|\bcci\b|\boferta\b|\bcotiza\w*|\bficha\b|\bautorizacion\b|"
    r"\bpropuesta\b|\bexperiencia\b|\bcurriculum\b|\bcv\b|\brnp\b|\bruc\b|\bconstancia\b|"
    r"\bdd jj\b|\bsustentatori\w*|\badjuntar\b|\bsegun\b|\bcumplir\b|\brequisitos del proveedor\b")


def classify_requirement_file(filename: str, mime: str = "") -> str | None:
    """``tdr`` or ``eett`` when the name says a file IS the requirement; None otherwise.

    Stricter than ``classify_document``: used for files that are not the contract's primary
    requirement (attachments), where a name that merely cites the TDR/EETT is common.
    """
    name = _fold(filename)
    if _SUPPLIER_PAPER_NAME.search(name):
        return None
    role = classify_document(filename, mime)
    return role if role in {"tdr", "eett"} else None
