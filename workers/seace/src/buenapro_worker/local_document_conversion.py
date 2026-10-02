"""Bounded laptop-only conversion. Archives never extract paths or execute members."""
from __future__ import annotations

import os
import re
import select
import shutil
import stat
import subprocess
import time
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree


def safe_member(name: str) -> None:
    path = PurePosixPath(name.replace("\\", "/"))
    if not name or path.is_absolute() or ".." in path.parts or ":" in name or "\x00" in name:
        raise ValueError("Archive contains an unsafe member path")


def bounded_command(command: list[str], *, limit: int, timeout: float = 30) -> bytes:
    """Bound stdout while running, not after an archive could exhaust RAM/disk."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    content = bytearray()
    deadline = time.monotonic() + timeout
    try:
        assert process.stdout is not None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Local document tool timed out")
            ready, _, _ = select.select([process.stdout], [], [], min(remaining, 1))
            if not ready:
                continue
            chunk = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > limit:
                raise ValueError("Local document output exceeds size policy")
        if process.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
            raise ValueError("Local document tool failed")
        return bytes(content)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout:
            process.stdout.close()


def zip_members(archive: zipfile.ZipFile, max_bytes: int) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > 2000 or sum(item.file_size for item in members) > max_bytes * 2:
        raise ValueError("Archive expansion exceeds size policy")
    for item in members:
        safe_member(item.filename)
        mode = item.external_attr >> 16
        if stat.S_ISLNK(mode) or item.flag_bits & 1:
            raise ValueError("Encrypted or linked archive member is not supported")
        if item.file_size > max_bytes or item.file_size > max(1, item.compress_size) * 1000:
            raise ValueError("Archive member exceeds size or ratio policy")
    return members


def select_bases_member(names: list[str]) -> str:
    executable = {".exe", ".dll", ".com", ".bat", ".cmd", ".sh", ".js", ".ps1", ".msi", ".vbs", ".scr"}
    for name in names:
        safe_member(name)
        if Path(name).suffix.lower() in executable:
            raise ValueError("Archive contains executable members")
    documents = [name for name in names if Path(name).suffix.lower() in {".pdf", ".docx"}]
    bases = [name for name in documents if re.search(r"base|t[eé]rminos|\btdr\b|eett|especificacion", name, re.I)]
    # Prefer explicitly named integrated bases, never combine unrelated members.
    integrated = [name for name in bases if "integrad" in name.casefold()]
    selected = integrated or bases or documents
    if len(selected) != 1:
        raise ValueError("Archive has no unambiguous official requirements document")
    return selected[0]


def extract_archive(path: Path, kind: str, *, max_bytes: int) -> Path:
    if kind == "zip":
        with zipfile.ZipFile(path) as archive:
            members = zip_members(archive, max_bytes)
            name = select_bases_member([item.filename for item in members if not item.is_dir()])
            with archive.open(name) as source:
                content = source.read(max_bytes + 1)
    else:
        names = bounded_command(["/usr/bin/bsdtar", "-tf", str(path)], limit=1_000_000).decode("utf-8").splitlines()
        if len(names) > 2000:
            raise ValueError("Archive member count exceeds policy")
        name = select_bases_member(names)
        listing = bounded_command(["/usr/bin/bsdtar", "-tvf", str(path)], limit=2_000_000).decode("utf-8").splitlines()
        total = 0
        for row in listing:
            fields = row.split(maxsplit=8)
            if len(fields) < 9 or fields[0][0] not in {"-", "d"}:
                raise ValueError("Archive contains linked or unrecognized members")
            size = int(fields[4])
            if size > max_bytes:
                raise ValueError("Archive member exceeds size policy")
            total += size
        if total > max_bytes * 2:
            raise ValueError("Archive expansion exceeds size policy")
        content = bounded_command(["/usr/bin/bsdtar", "-xOf", str(path), "--", name], limit=max_bytes)
    if len(content) > max_bytes:
        raise ValueError("Archive document exceeds size policy")
    target = path.parent / ("archive-bases" + Path(name).suffix.lower())
    target.write_bytes(content)
    return target


def convert_docx(path: Path, *, max_bytes: int) -> Path:
    with zipfile.ZipFile(path) as archive:
        members = zip_members(archive, max_bytes)
        names = [item.filename for item in members]
        if "word/document.xml" not in names:
            raise ValueError("Not a supported Word document")
        if any("vba" in name.casefold() or "/embeddings/" in name.casefold() for name in names):
            raise ValueError("Word macros or embedded objects are not supported")
        for name in names:
            if name.endswith(".xml"):
                xml = archive.read(name)
                if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                    raise ValueError("Word contains unsafe XML entities")
            if name.endswith(".rels"):
                xml = archive.read(name)
                if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                    raise ValueError("Word relationships contain unsafe XML")
                for relationship in ElementTree.fromstring(xml):
                    if relationship.get("TargetMode") == "External" and not str(relationship.get("Type", "")).endswith("/hyperlink"):
                        raise ValueError("Word contains active external relationships")
    soffice = shutil.which("soffice")
    if not soffice:
        raise RuntimeError("LibreOffice is unavailable for local DOCX conversion")
    profile = path.parent / "libreoffice-profile"
    profile.mkdir(exist_ok=True)
    # Isolated profile: very high macro security and no automatic link updates.
    (profile / "user").mkdir(exist_ok=True)
    (profile / "user" / "registrymodifications.xcu").write_text(
        '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
        '<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop><prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop></item>'
        '<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item></oor:items>', encoding="utf-8")
    subprocess.run([soffice, "--headless", "--nologo", "--nodefault", "--norestore", "--nolockcheck",
                    f"-env:UserInstallation={profile.as_uri()}", "--convert-to", "pdf:writer_pdf_Export",
                    "--outdir", str(path.parent), str(path)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=120)
    output = path.with_suffix(".pdf")
    if not output.exists() or output.stat().st_size > max_bytes:
        raise ValueError("Word PDF conversion failed or exceeds size policy")
    return output


def prepare_pdf(path: Path, extension: str, *, max_bytes: int) -> Path:
    if extension in {"rar", "zip"}:
        path = extract_archive(path, extension, max_bytes=max_bytes)
        extension = path.suffix.lstrip(".").lower()
    if extension == "docx":
        path = convert_docx(path, max_bytes=max_bytes)
    if not path.read_bytes().startswith(b"%PDF-"):
        raise ValueError("Official document is not a valid PDF")
    subprocess.run(["pdfinfo", str(path)], capture_output=True, check=True, timeout=30)
    return path
