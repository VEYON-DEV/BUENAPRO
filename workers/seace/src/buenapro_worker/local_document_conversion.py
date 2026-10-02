"""Bounded laptop-only conversion. Archives never extract paths or execute members."""
from __future__ import annotations

import os
import json
import re
import select
import shutil
import stat
import subprocess
import tempfile
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
    named_bases = [name for name in bases if re.search(r"\bbases?\b", name, re.I)]
    selected = integrated or named_bases or bases or documents
    if len(selected) != 1:
        raise ValueError("Archive has no unambiguous official requirements document")
    return selected[0]


def select_requirements_members(names: list[str]) -> list[str]:
    primary = select_bases_member(names)
    if "integrad" in primary.casefold():
        return [primary]
    companions = [name for name in names if name != primary
                  and Path(name).suffix.lower() in {".pdf", ".docx"}
                  and re.search(r"\btdr\b|t[eé]rminos|eett|especificacion", name, re.I)]
    if len(companions) > 1:
        raise ValueError("Archive has ambiguous companion requirements")
    return [primary, *companions]


def extract_archive(path: Path, kind: str, *, max_bytes: int) -> Path:
    if kind == "zip":
        with zipfile.ZipFile(path) as archive:
            members = zip_members(archive, max_bytes)
            names = select_requirements_members([item.filename for item in members if not item.is_dir()])
            contents = []
            for name in names:
                with archive.open(name) as source:
                    contents.append((name, source.read(max_bytes + 1)))
    else:
        names = bounded_command(["/usr/bin/bsdtar", "-tf", str(path)], limit=1_000_000).decode("utf-8").splitlines()
        if len(names) > 2000:
            raise ValueError("Archive member count exceeds policy")
        names = select_requirements_members(names)
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
        contents = [(name, bounded_command(["/usr/bin/bsdtar", "-xOf", str(path), "--", name], limit=max_bytes))
                    for name in names]
    if sum(len(content) for _, content in contents) > max_bytes:
        raise ValueError("Archive document exceeds size policy")
    targets = []
    for index, (name, content) in enumerate(contents):
        target = path.parent / (f"archive-bases-{index}" + Path(name).suffix.lower())
        target.write_bytes(content)
        targets.append(target)
    if len(targets) == 1:
        return targets[0]
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    manifest = []
    start = 1
    for (name, _), target in zip(contents, targets):
        if target.suffix == ".docx":
            target = convert_docx(target, max_bytes=max_bytes)
        reader = PdfReader(target)
        if reader.is_encrypted or len(reader.pages) > 1500:
            raise ValueError("Archive PDF is encrypted or exceeds page policy")
        for page in reader.pages:
            writer.add_page(page)
        manifest.append({"member": name, "start_page": start, "end_page": start + len(reader.pages) - 1})
        start += len(reader.pages)
    if start > 1501:
        raise ValueError("Combined archive exceeds page policy")
    target = path.parent / "archive-combined.pdf"
    with target.open("wb") as output:
        writer.write(output)
    if target.stat().st_size > max_bytes:
        raise ValueError("Combined archive exceeds size policy")
    (path.parent / "local-document-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return target


def convert_docx(path: Path, *, max_bytes: int) -> Path:
    path = path.resolve()
    replacements = {}
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
                root = ElementTree.fromstring(xml)
                removed_templates = set()
                for relationship in list(root):
                    if relationship.get("TargetMode") != "External":
                        continue
                    relationship_type = str(relationship.get("Type", ""))
                    if relationship_type.endswith("/attachedTemplate"):
                        # Never follow author-machine template paths. Embedded document
                        # styles remain intact; sanitize ONLY the conversion derivative.
                        removed_templates.add(relationship.get("Id"))
                        root.remove(relationship)
                    elif not relationship_type.endswith("/hyperlink"):
                        raise ValueError("Word contains active external relationships")
                if removed_templates:
                    replacements[name] = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
                    owner = name.replace("/_rels/", "/").removesuffix(".rels")
                    if owner in names:
                        # Preserve Word's namespace aliases referenced in mc:Ignorable.
                        # XML reserialization renames aliases and can invalidate the file.
                        owner_xml = archive.read(owner)
                        replacements[owner] = re.sub(
                            rb'<(?:[A-Za-z0-9_]+:)?attachedTemplate\b[^>]*/>', b'', owner_xml,
                        )
        if replacements:
            sanitized = path.parent / "sanitized-bases.docx"
            with zipfile.ZipFile(sanitized, "w", compression=zipfile.ZIP_DEFLATED) as output:
                for member in members:
                    output.writestr(member.filename, replacements.get(member.filename, archive.read(member.filename)))
            path = sanitized
    soffice = shutil.which("soffice")
    if not soffice:
        raise RuntimeError("LibreOffice is unavailable for local DOCX conversion")
    profile = Path(tempfile.mkdtemp(prefix="libreoffice-profile-", dir=path.parent))
    # Isolated profile: very high macro security and no automatic link updates.
    (profile / "user").mkdir(exist_ok=True)
    (profile / "user" / "registrymodifications.xcu").write_text(
        '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
        '<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop><prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop></item>'
        '<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item></oor:items>', encoding="utf-8")
    # The bundled macOS headless runtime needs its virtual renderer and fontconfig.
    # Its default Cocoa PDF export can abort (SfxBaseModel Io Abort 27).
    environment = {**os.environ, "SAL_USE_VCLPLUGIN": "svp"}
    soffice_path = Path(soffice).resolve()
    bundled_fonts = ((soffice_path.parents[2] / "native/libreoffice-headless/libreoffice/LibreOfficeDev.app/Contents/Resources/fontconfig/fonts.conf")
                     if len(soffice_path.parents) >= 3 else None)
    if bundled_fonts and bundled_fonts.is_file():
        environment["FONTCONFIG_FILE"] = str(bundled_fonts)
    output_directory = profile / "converted"
    output_directory.mkdir()
    subprocess.run([soffice, "--headless", "--nologo", "--nodefault", "--norestore", "--nolockcheck",
                    f"-env:UserInstallation={profile.as_uri()}", "--convert-to", "pdf:writer_pdf_Export",
                    "--outdir", str(output_directory), str(path)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=120,
                   env=environment)
    output = output_directory / path.with_suffix(".pdf").name
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
