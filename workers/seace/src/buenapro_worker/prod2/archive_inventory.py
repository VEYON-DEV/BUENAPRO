"""Bounded virtual ZIP/RAR inventories, with explicit opt-in for 7z.

No member is selected by its filename or role. PDF/DOC/DOCX members are candidates;
nested archives remain explicit pending records and are never recursively opened.
RAR/7z metadata comes from OS libarchive in a time-bounded child process. Selected
members are streamed with bsdtar using escaped literal member operands.
Original archives are never modified, and extracted originals are NOT conversions.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import sys
import tempfile
import time
import unicodedata
from uuid import UUID
import zipfile
import zlib

from buenapro_worker.local_document_conversion import bounded_command


POLICY_VERSION = "prod2_archive_inventory_v1"
POLICY_7Z = "prod2_archive_inventory_7z_v2"
SUPPORTED = {"pdf", "doc", "docx"}
NESTED = {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "tgz"}
MAX_RATIO = 1000
_RAR4 = b"Rar!\x1a\x07\x00"
_RAR5 = b"Rar!\x1a\x07\x01\x00"
_7Z = b"7z\xbc\xaf\x27\x1c"


def inventory_policy(manifest: dict) -> str:
    """Absent policy preserves the frozen ZIP/RAR-only contract."""
    policy = manifest.get("archive_inventory_policy", POLICY_VERSION)
    if not isinstance(policy, str) or policy not in {POLICY_VERSION, POLICY_7Z}:
        raise ArchiveInventoryError("invalid_archive_inventory_policy")
    return policy


class ArchiveInventoryError(ValueError):
    """A bounded inventory/extraction failed; messages never expose member names."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ArchiveInventoryError("archive_timeout")
    return remaining


def _sha(path: Path, deadline: float) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            _remaining(deadline)
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(name: str, *, rar: bool) -> str:
    if (not isinstance(name, str) or not name or len(name.encode("utf-8")) > 4096
            or any(unicodedata.category(char).startswith("C") for char in name)):
        raise ArchiveInventoryError("unsafe_member_path")
    normalized = unicodedata.normalize("NFKC", name.replace("\\", "/"))
    if normalized.startswith("/") or ":" in normalized:
        raise ArchiveInventoryError("unsafe_member_path")
    parts = normalized.rstrip("/").split("/")
    if any(part in {"", ".", ".."} or part.rstrip(" .") != part for part in parts):
        raise ArchiveInventoryError("unsafe_member_path")
    # Native backslashes remain ambiguous separators. Glob characters are
    # escaped at the bsdtar boundary, never passed as operand patterns.
    if rar and "\\" in name:
        raise ArchiveInventoryError("rar_member_pattern_unsupported")
    return "/".join(parts).casefold()


def _limits(max_source_bytes: int, max_expanded_bytes: int, max_member_bytes: int,
            max_members: int) -> dict:
    values = {"max_source_bytes": max_source_bytes, "max_expanded_bytes": max_expanded_bytes,
              "max_member_bytes": max_member_bytes, "max_members": max_members}
    if any(type(value) is not int or value <= 0 for value in values.values()):
        raise ArchiveInventoryError("invalid_archive_limits")
    return values


def _records(raw: list[dict], parent_uuid: str, parent_sha: str, *, kind: str,
             limits: dict, source_bytes: int, deadline: float) -> tuple[list[dict], int]:
    if len(raw) > limits["max_members"]:
        raise ArchiveInventoryError("archive_member_limit")
    seen, records, expanded = set(), [], 0
    for index, entry in enumerate(raw):
        _remaining(deadline)
        name = entry["name"]
        normalized = _safe_name(name, rar=kind in {"rar", "7z"})
        if normalized in seen:
            raise ArchiveInventoryError("duplicate_or_colliding_member")
        seen.add(normalized)
        if entry.get("linked") or entry.get("kind") not in {"file", "directory"}:
            raise ArchiveInventoryError("linked_or_special_member")
        if entry.get("encrypted") is not False:
            raise ArchiveInventoryError("encrypted_or_unknown_encryption_member")
        size = entry["size"]
        if type(size) is not int or size < 0 or size > limits["max_member_bytes"]:
            raise ArchiveInventoryError("archive_member_size_limit")
        expanded += size
        if expanded > limits["max_expanded_bytes"]:
            raise ArchiveInventoryError("archive_expansion_limit")
        compressed = entry.get("compressed_size")
        if compressed is not None and size > max(1, compressed) * MAX_RATIO:
            raise ArchiveInventoryError("archive_member_ratio_limit")
        extension = Path(name.replace("\\", "/")).suffix.lstrip(".").lower()
        status = ("directory" if entry["kind"] == "directory" else
                  "candidate" if extension in SUPPORTED else
                  "pending_nested_archive" if extension in NESTED else "unsupported")
        record = {"parent_uuid": parent_uuid, "parent_sha256": parent_sha,
                  "member_index": index, "member_path": name, "size": size,
                  "format": extension or None, "status": status}
        record["candidate_id"] = "archive:" + _hash(record)
        if compressed is not None:
            record["compressed_size"] = compressed
        records.append(record)
    if expanded > max(1, source_bytes) * MAX_RATIO:
        raise ArchiveInventoryError("archive_total_ratio_limit")
    return records, expanded


def _zip_preflight(path: Path, max_members: int, deadline: float) -> None:
    """Count bounded central-directory records before ZipFile allocates ZipInfos.

    ZIP64 directories are explicitly unsupported in this source-size-bounded
    implementation. Ordinary small files written with force_zip64 remain valid.
    """
    with path.open("rb") as source:
        length = path.stat().st_size
        source.seek(max(0, length - 65557))
        tail = source.read(65557)
        position = len(tail)
        while True:
            position = tail.rfind(b"PK\x05\x06", 0, position)
            if position < 0:
                raise ArchiveInventoryError("zip_directory_invalid")
            if (len(tail) - position >= 22 and position + 22
                    + struct.unpack_from("<H", tail, position + 20)[0] == len(tail)):
                break
        _, disk, central_disk, disk_count, count, size, offset, comment_size = struct.unpack_from(
            "<4s4H2IH", tail, position)
        if disk or central_disk or disk_count != count:
            raise ArchiveInventoryError("multivolume_archive_unsupported")
        if count > max_members:
            raise ArchiveInventoryError("archive_member_limit")
        if count == 65535 or size == 0xffffffff or offset == 0xffffffff:
            raise ArchiveInventoryError("zip64_directory_unsupported")
        end_position = length - len(tail) + position
        if position + 22 + comment_size != len(tail) or offset + size != end_position:
            raise ArchiveInventoryError("zip_directory_invalid")
        source.seek(offset)
        actual = 0
        while source.tell() < end_position:
            _remaining(deadline)
            header = source.read(46)
            if len(header) != 46 or header[:4] != b"PK\x01\x02":
                raise ArchiveInventoryError("zip_directory_invalid")
            actual += 1
            if actual > max_members:
                raise ArchiveInventoryError("archive_member_limit")
            filename, extra, comment = struct.unpack_from("<3H", header, 28)
            next_position = source.tell() + filename + extra + comment
            if next_position > end_position:
                raise ArchiveInventoryError("zip_directory_invalid")
            source.seek(next_position)
        if actual != count:
            raise ArchiveInventoryError("zip_directory_invalid")


def _zip_metadata(path: Path, *, max_members: int, deadline: float) -> list[dict]:
    _zip_preflight(path, max_members, deadline)
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > max_members:
            raise ArchiveInventoryError("archive_member_limit")
        result = []
        for entry in entries:
            mode = entry.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            kind = "directory" if entry.is_dir() or file_type == stat.S_IFDIR else "file"
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                kind = "special"
            result.append({"name": entry.orig_filename, "kind": kind,
                           "linked": stat.S_ISLNK(mode), "encrypted": bool(entry.flag_bits & 1),
                           "size": entry.file_size, "compressed_size": entry.compress_size})
        return result


def _vint(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 63, 7):
        if position >= len(data):
            raise ArchiveInventoryError("rar_header_invalid")
        byte = data[position]
        position += 1
        value |= (byte & 0x7f) << shift
        if byte < 128:
            return value, position
    raise ArchiveInventoryError("rar_header_invalid")


def _check_rar_encryption(path: Path, deadline: float, max_members: int) -> None:
    """Inspect bounded RAR4/5 headers, seeking over compressed payloads.

    Some OS libarchive RAR5 readers report encryption detection unsupported.
    RAR4 password flags and RAR5 encryption headers/file encryption extra records
    provide an independent check. Unknown/malformed structures fail closed; CRCs
    validate header framing, not authenticity. No member content is unpacked.
    """
    total_size = path.stat().st_size
    with path.open("rb") as source:
        signature = source.read(8)
        version = 5 if signature == _RAR5 else 4 if signature.startswith(_RAR4) else 0
        if not version:
            raise ArchiveInventoryError("unsupported_archive_signature")
        source.seek(len(_RAR5 if version == 5 else _RAR4))
        headers = 0
        while source.tell() < total_size:
            _remaining(deadline)
            headers += 1
            if headers > max_members * 4 + 16:
                raise ArchiveInventoryError("archive_header_limit")
            if version == 4:
                common = source.read(7)
                if len(common) != 7:
                    raise ArchiveInventoryError("rar_header_invalid")
                crc, kind, flags, size = struct.unpack("<HBHH", common)
                if size < 7:
                    raise ArchiveInventoryError("rar_header_invalid")
                header = common + source.read(size - 7)
                if len(header) != size or (zlib.crc32(header[2:]) & 0xffff) != crc:
                    raise ArchiveInventoryError("rar_header_invalid")
                if (kind == 0x73 and flags & 0x80) or (kind in {0x74, 0x7a} and flags & 4):
                    raise ArchiveInventoryError("encrypted_archive_unsupported")
                data_size = 0
                if flags & 0x8000:
                    if len(header) < 11:
                        raise ArchiveInventoryError("rar_header_invalid")
                    data_size = struct.unpack_from("<I", header, 7)[0]
                    if kind in {0x74, 0x7a} and flags & 0x100:
                        if len(header) < 40:
                            raise ArchiveInventoryError("rar_header_invalid")
                        data_size |= struct.unpack_from("<I", header, 32)[0] << 32
            else:
                crc_bytes = source.read(4)
                encoded = bytearray()
                for _ in range(9):
                    byte = source.read(1)
                    if not byte:
                        raise ArchiveInventoryError("rar_header_invalid")
                    encoded.extend(byte)
                    if byte[0] < 128:
                        break
                size, _ = _vint(bytes(encoded), 0)
                if len(crc_bytes) != 4 or not 2 <= size <= 1024 * 1024:
                    raise ArchiveInventoryError("rar_header_invalid")
                header = source.read(size)
                if (len(header) != size or zlib.crc32(bytes(encoded) + header)
                        != struct.unpack("<I", crc_bytes)[0]):
                    raise ArchiveInventoryError("rar_header_invalid")
                kind, position = _vint(header, 0)
                flags, position = _vint(header, position)
                extra_size, data_size = 0, 0
                if flags & 1:
                    extra_size, position = _vint(header, position)
                if flags & 2:
                    data_size, position = _vint(header, position)
                if kind == 4:
                    raise ArchiveInventoryError("encrypted_archive_unsupported")
                if extra_size > len(header) - position:
                    raise ArchiveInventoryError("rar_header_invalid")
                if extra_size:
                    extra_position = len(header) - extra_size
                    while extra_position < len(header):
                        record_size, record_start = _vint(header, extra_position)
                        record_end = record_start + record_size
                        if not record_size or record_end > len(header):
                            raise ArchiveInventoryError("rar_header_invalid")
                        record_kind, after_kind = _vint(header, record_start)
                        if after_kind > record_end:
                            raise ArchiveInventoryError("rar_header_invalid")
                        if kind in {2, 3} and record_kind == 1:
                            raise ArchiveInventoryError("encrypted_archive_unsupported")
                        extra_position = record_end
            next_position = source.tell() + data_size
            if next_position > total_size:
                raise ArchiveInventoryError("rar_header_invalid")
            source.seek(next_position)


def _rar_metadata(path: str, max_members: int, max_expanded_bytes: int) -> list[dict]:
    """Internal child-process entry point: list headers without unpacking members.

    A bounded header check handles encryption even if libarchive cannot report it.
    Typed metadata avoids parsing ambiguous textual tar listing columns.
    """
    _check_rar_encryption(Path(path), time.monotonic() + 30, max_members)
    return _libarchive_metadata(path, max_members, max_expanded_bytes, kind="rar")


def _libarchive_metadata(path: str, max_members: int, max_expanded_bytes: int,
                         *, kind: str) -> list[dict]:
    """Typed, bounded top-level metadata; 7z requires proven encryption status.

    7z can carry encoded or encrypted headers. libarchive must decode the entire
    top-level header set successfully and affirm no encrypted entries. Unlike RAR,
    there is no independent header parser permitting an unknown encryption status.
    No data member is selected or extracted by this inventory operation.
    """
    if kind not in {"rar", "7z"}:
        raise ArchiveInventoryError("unsupported_archive_signature")
    library = ctypes.util.find_library("archive")
    if not library:
        raise ArchiveInventoryError("libarchive_unavailable")
    lib = ctypes.CDLL(library)
    signatures = {
        "archive_read_new": (ctypes.c_void_p, []),
        "archive_read_support_filter_all": (ctypes.c_int, [ctypes.c_void_p]),
        "archive_read_support_format_all": (ctypes.c_int, [ctypes.c_void_p]),
        "archive_read_open_filename": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]),
        "archive_read_next_header": (ctypes.c_int, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]),
        "archive_read_data_skip": (ctypes.c_int, [ctypes.c_void_p]),
        "archive_read_has_encrypted_entries": (ctypes.c_int, [ctypes.c_void_p]),
        "archive_read_free": (ctypes.c_int, [ctypes.c_void_p]),
        "archive_entry_pathname_utf8": (ctypes.c_char_p, [ctypes.c_void_p]),
        "archive_entry_filetype": (ctypes.c_uint, [ctypes.c_void_p]),
        "archive_entry_size": (ctypes.c_longlong, [ctypes.c_void_p]),
        "archive_entry_size_is_set": (ctypes.c_int, [ctypes.c_void_p]),
        "archive_entry_is_encrypted": (ctypes.c_int, [ctypes.c_void_p]),
        "archive_entry_symlink": (ctypes.c_char_p, [ctypes.c_void_p]),
        "archive_entry_hardlink": (ctypes.c_char_p, [ctypes.c_void_p]),
    }
    try:
        for name, (restype, argtypes) in signatures.items():
            function = getattr(lib, name)
            function.restype, function.argtypes = restype, argtypes
    except AttributeError as exc:
        raise ArchiveInventoryError("libarchive_metadata_unsupported") from exc
    archive = lib.archive_read_new()
    if not archive:
        raise ArchiveInventoryError("libarchive_open_failed")
    try:
        lib.archive_read_support_filter_all(archive)
        lib.archive_read_support_format_all(archive)
        if lib.archive_read_open_filename(archive, os.fsencode(path), 10240) != 0:
            raise ArchiveInventoryError(kind + "_headers_unreadable_or_encrypted")
        records, total = [], 0
        while True:
            entry = ctypes.c_void_p()
            status = lib.archive_read_next_header(archive, ctypes.byref(entry))
            if status == 1:  # ARCHIVE_EOF
                break
            if status != 0 or not entry.value:
                raise ArchiveInventoryError(kind + "_headers_unreadable_or_encrypted")
            if len(records) >= max_members:
                raise ArchiveInventoryError("archive_member_limit")
            filename = lib.archive_entry_pathname_utf8(entry)
            if not filename or not lib.archive_entry_size_is_set(entry):
                raise ArchiveInventoryError(kind + "_metadata_incomplete")
            filename = filename.decode("utf-8", errors="strict")
            _safe_name(filename, rar=True)
            if lib.archive_entry_is_encrypted(entry):
                raise ArchiveInventoryError("encrypted_or_unknown_encryption_member")
            size = int(lib.archive_entry_size(entry))
            total += size
            if size < 0 or total > max_expanded_bytes:
                raise ArchiveInventoryError("archive_expansion_limit")
            file_type = lib.archive_entry_filetype(entry)
            records.append({"name": filename, "size": size,
                "kind": "file" if file_type == stat.S_IFREG else
                        "directory" if file_type == stat.S_IFDIR else "special",
                "linked": bool(lib.archive_entry_symlink(entry) or lib.archive_entry_hardlink(entry)),
                "encrypted": bool(lib.archive_entry_is_encrypted(entry))})
            if lib.archive_read_data_skip(archive) != 0:
                raise ArchiveInventoryError(kind + "_headers_unreadable_or_encrypted")
        # -2/-1 mean unsupported/unknown in some RAR5 builds. The independent
        # complete header check above already rejected encrypted payloads/headers.
        encrypted = lib.archive_read_has_encrypted_entries(archive)
        if encrypted == 1 or (kind == "7z" and encrypted != 0):
            raise ArchiveInventoryError("encrypted_or_unknown_encryption_member")
        return records
    finally:
        lib.archive_read_free(archive)


def _rar_metadata_bounded(path: Path, limits: dict, deadline: float) -> list[dict]:
    command = [sys.executable, "-c",
        "import sys,json;sys.path.insert(0,sys.argv[1]);"
        "from buenapro_worker.prod2.archive_inventory import _rar_metadata;"
        "print(json.dumps(_rar_metadata(sys.argv[2],int(sys.argv[3]),int(sys.argv[4]))))",
        str(Path(__file__).resolve().parents[2]), str(path), str(limits["max_members"]),
        str(limits["max_expanded_bytes"])]
    try:
        raw = bounded_command(command, limit=max(16384, limits["max_members"] * 26000),
                              timeout=_remaining(deadline))
        result = json.loads(raw)
        if not isinstance(result, list):
            raise ArchiveInventoryError("rar_metadata_incomplete")
        return result
    except ArchiveInventoryError:
        raise
    except Exception as exc:
        raise ArchiveInventoryError("rar_metadata_failed_or_encrypted") from exc


def _sevenzip_metadata_bounded(path: Path, limits: dict, deadline: float) -> list[dict]:
    command = [sys.executable, "-c",
        "import sys,json;sys.path.insert(0,sys.argv[1]);"
        "from buenapro_worker.prod2.archive_inventory import _libarchive_metadata;"
        "print(json.dumps(_libarchive_metadata(sys.argv[2],int(sys.argv[3]),int(sys.argv[4]),kind='7z')))",
        str(Path(__file__).resolve().parents[2]), str(path), str(limits["max_members"]),
        str(limits["max_expanded_bytes"])]
    try:
        raw = bounded_command(command, limit=max(16384, limits["max_members"] * 26000),
                              timeout=_remaining(deadline))
        result = json.loads(raw)
        if not isinstance(result, list):
            raise ArchiveInventoryError("7z_metadata_incomplete")
        return result
    except ArchiveInventoryError:
        raise
    except Exception as exc:
        raise ArchiveInventoryError("7z_metadata_failed_or_encrypted") from exc


def list_archive(path: Path | str, parent_uuid: str, *, max_source_bytes: int,
                 max_expanded_bytes: int, max_member_bytes: int, max_members: int,
                 timeout: float = 30, policy_version: str = POLICY_VERSION) -> dict:
    """Return a complete virtual inventory; no extraction, selection or conversion.

    ``candidate_id`` is stable for the same official parent UUID, archive bytes,
    member position/path/size/format. A changed archive creates a new inventory.
    ``inventory_complete`` means all top-level members were inventoried, not that
    their contents or nested archives were read or validated as Word/PDF documents.
    """
    inventory_policy({"archive_inventory_policy": policy_version})
    limits = _limits(max_source_bytes, max_expanded_bytes, max_member_bytes, max_members)
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
        raise ArchiveInventoryError("invalid_archive_timeout")
    deadline, path = time.monotonic() + timeout, Path(path)
    try:
        parent_uuid = str(UUID(str(parent_uuid)))
        if path.is_symlink() or not path.is_file():
            raise ArchiveInventoryError("unsafe_archive_source")
        source_bytes = path.stat().st_size
        if not 0 < source_bytes <= max_source_bytes:
            raise ArchiveInventoryError("archive_source_size_limit")
        with path.open("rb") as source:
            signature = source.read(8)
        if signature.startswith((_RAR4, _RAR5)):
            kind = "rar"
        elif signature.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            kind = "zip"
        elif signature.startswith(_7Z) and policy_version == POLICY_7Z:
            kind = "7z"
        else:
            raise ArchiveInventoryError("unsupported_archive_signature")
        parent_sha = _sha(path, deadline)
        if kind == "rar":
            _check_rar_encryption(path, deadline, max_members)
        if kind == "zip":
            raw = _zip_metadata(path, max_members=max_members, deadline=deadline)
        elif kind == "rar":
            raw = _rar_metadata_bounded(path.resolve(), limits, deadline)
        else:
            raw = _sevenzip_metadata_bounded(path.resolve(), limits, deadline)
        members, expanded = _records(raw, parent_uuid, parent_sha, kind=kind, limits=limits,
                                    source_bytes=source_bytes, deadline=deadline)
        if path.stat().st_size != source_bytes or _sha(path, deadline) != parent_sha:
            raise ArchiveInventoryError("archive_source_changed")
        inventory = {"policy_version": policy_version, "parent_uuid": parent_uuid,
            "parent_sha256": parent_sha, "archive_format": kind, "source_bytes": source_bytes,
            "expanded_bytes": expanded, "limits": limits, "members": members,
            "inventory_complete": True, "nested_contents_read": False}
        return {**inventory, "inventory_hash": _hash(inventory)}
    except ArchiveInventoryError:
        raise
    except Exception as exc:
        raise ArchiveInventoryError("archive_inventory_failed") from exc


def _private_directory(destination: Path, parent_uuid: str, parent_sha: str) -> Path:
    if destination.is_symlink():
        raise ArchiveInventoryError("unsafe_archive_destination")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = destination.resolve()
    for component in (parent_uuid, parent_sha):
        directory = directory / component
        if directory.is_symlink():
            raise ArchiveInventoryError("unsafe_archive_destination")
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    return directory


def _signature(content: bytes, extension: str) -> None:
    signatures = {"pdf": b"%PDF-", "doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "docx": b"PK\x03\x04"}
    if not content.startswith(signatures[extension]):
        raise ArchiveInventoryError("selected_member_format_mismatch")


def extract_members(path: Path | str, inventory: dict, *, candidate_ids: list[str],
                    destination: Path | str, timeout: float = 60) -> list[dict]:
    """Extract only selected candidate IDs to private paths and return SHA bindings.

    This revalidates the complete frozen inventory and archive bytes first. It never
    recurses, converts Word, picks document versions, or returns a partial success.
    Exact existing private outputs can be reused. The archive remains the original.
    """
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
        raise ArchiveInventoryError("invalid_archive_timeout")
    deadline, path = time.monotonic() + timeout, Path(path)
    if not isinstance(candidate_ids, list) or any(not isinstance(code, str) for code in candidate_ids):
        raise ArchiveInventoryError("invalid_candidate_ids")
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ArchiveInventoryError("duplicate_selected_candidate")
    try:
        fresh = list_archive(path, inventory["parent_uuid"], **inventory["limits"],
                             timeout=_remaining(deadline), policy_version=inventory["policy_version"])
        if inventory != fresh:
            raise ArchiveInventoryError("archive_inventory_changed_or_tampered")
        by_id = {member["candidate_id"]: member for member in fresh["members"]}
        selected = []
        for code in candidate_ids:
            if code not in by_id or by_id[code]["status"] != "candidate":
                raise ArchiveInventoryError("selected_member_not_candidate")
            selected.append(by_id[code])
        if not selected:
            return []
        directory = _private_directory(Path(destination), fresh["parent_uuid"], fresh["parent_sha256"])
        result = []
        for member in selected:
            _remaining(deadline)
            basename = member["candidate_id"].removeprefix("archive:")
            target = directory / (basename + "." + member["format"])
            marker = directory / (basename + ".json")
            if target.is_symlink() or marker.is_symlink():
                raise ArchiveInventoryError("unsafe_archive_destination")
            binding = {**member, "inventory_hash": fresh["inventory_hash"]}
            if target.exists() or marker.exists():
                if (target.exists() and not target.is_file()) or (marker.exists() and not marker.is_file()):
                    raise ArchiveInventoryError("unsafe_archive_destination")
                if marker.exists():
                    pinned = json.loads(marker.read_text(encoding="utf-8"))
                    if any(pinned.get(key) != value for key, value in binding.items()):
                        raise ArchiveInventoryError("member_cache_binding_mismatch")
                    if target.exists():
                        if (target.stat().st_size != member["size"]
                                or _sha(target, deadline) != pinned.get("original_sha256")):
                            raise ArchiveInventoryError("member_cache_binding_mismatch")
                        os.chmod(target, 0o600)
                        os.chmod(marker, 0o600)
                        result.append({**pinned, "path": str(target)})
                        continue
                # Crash between the two atomic writes: the incomplete pair is
                # never trusted as a cache. Safely recreate it from the archive.
            limit = fresh["limits"]["max_member_bytes"]
            if fresh["archive_format"] == "zip":
                with zipfile.ZipFile(path) as archive:
                    entry = archive.infolist()[member["member_index"]]
                    with archive.open(entry) as source:
                        chunks, total = [], 0
                        while chunk := source.read(min(65536, limit + 1 - total)):
                            _remaining(deadline)
                            total += len(chunk)
                            if total > limit or total > member["size"]:
                                raise ArchiveInventoryError("selected_member_size_mismatch")
                            chunks.append(chunk)
                        content = b"".join(chunks)
            else:
                tool = shutil.which("bsdtar")
                if not tool:
                    raise ArchiveInventoryError("bsdtar_unavailable")
                content = _rar_member_bytes(tool, path, member["member_path"], limit, deadline)
            if len(content) != member["size"]:
                raise ArchiveInventoryError("selected_member_size_mismatch")
            _signature(content, member["format"])
            record = {**binding, "original_sha256": hashlib.sha256(content).hexdigest()}
            # Private temporary outputs are atomically published; member paths are
            # metadata only and are never used as destination paths.
            fd, temporary = tempfile.mkstemp(prefix=".member-", dir=directory)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(content)
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            fd, temporary = tempfile.mkstemp(prefix=".manifest-", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(record, output, ensure_ascii=False, sort_keys=True)
                os.replace(temporary, marker)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            result.append({**record, "path": str(target)})
        if _sha(path, deadline) != fresh["parent_sha256"]:
            raise ArchiveInventoryError("archive_source_changed")
        return result
    except ArchiveInventoryError:
        raise
    except Exception as exc:
        raise ArchiveInventoryError("archive_member_extraction_failed") from exc


def _rar_member_bytes(tool: str, path: Path, name: str, limit: int, deadline: float) -> bytes:
    """Keep the original identity while allowing libarchive's Unicode spelling.

    macOS libarchive can expose an NFC archive name as NFD. Inventory validation
    already forbids Unicode-colliding names and unsafe paths. Only a tool
    failure permits an equivalent spelling; timeout and output limits still fail.
    """
    spellings = list(dict.fromkeys([name, unicodedata.normalize("NFD", name),
                                   unicodedata.normalize("NFC", name)]))
    for index, spelling in enumerate(spellings):
        if _safe_name(spelling, rar=True) != _safe_name(name, rar=True):
            raise ArchiveInventoryError("rar_member_spelling_mismatch")
        operand = "".join("\\" + char if char in "*?[]" else char for char in spelling)
        try:
            return bounded_command([tool, "-xOf", str(path.resolve()), "--", operand],
                                   limit=limit, timeout=_remaining(deadline))
        except ValueError as error:
            if str(error) != "Local document tool failed" or index == len(spellings) - 1:
                raise
    raise ArchiveInventoryError("archive_member_extraction_failed")
