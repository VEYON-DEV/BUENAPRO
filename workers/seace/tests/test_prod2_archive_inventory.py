"""Offline archive inventory and exact member extraction contracts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import struct
from unittest.mock import patch
import warnings
import zipfile
import zlib

import pytest

from buenapro_worker.prod2.archive_inventory import (
    ArchiveInventoryError, _check_rar_encryption, _safe_name, extract_members, list_archive, POLICY_7Z,
)


PARENT = "11111111-1111-4111-8111-111111111111"
LIMITS = dict(max_source_bytes=128 * 1024 * 1024, max_expanded_bytes=256 * 1024 * 1024,
              max_member_bytes=128 * 1024 * 1024, max_members=2000)


def make_zip(tmp_path: Path, entries: list[tuple[str, bytes]], *, compression=zipfile.ZIP_STORED) -> Path:
    path = tmp_path / "original.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            for name, content in entries:
                archive.writestr(name, content)
    return path


def inventory(path: Path, **limits) -> dict:
    return list_archive(path, PARENT, **{**LIMITS, **limits})


def test_inventory_is_virtual_and_never_selects_by_name(tmp_path):
    source = make_zip(tmp_path, [("documento.pdf", b"%PDF-a"), ("formato.doc", b"word"),
        ("otra-version.docx", b"word"), ("attachment.zip", b"nested"), ("leer.txt", b"text"),
        ("directory/", b"")])
    original = source.read_bytes()
    result = inventory(source)
    assert [row["status"] for row in result["members"]] == [
        "candidate", "candidate", "candidate", "pending_nested_archive", "unsupported", "directory"]
    assert [row["format"] for row in result["members"][:3]] == ["pdf", "doc", "docx"]
    assert result["inventory_complete"] and not result["nested_contents_read"]
    assert result["parent_sha256"] == hashlib.sha256(original).hexdigest()
    assert source.read_bytes() == original
    assert list(tmp_path.iterdir()) == [source]
    assert inventory(source) == result
    assert len({row["candidate_id"] for row in result["members"]}) == 6


def test_member_ids_bind_parent_uuid_archive_bytes_and_member_position(tmp_path):
    source = make_zip(tmp_path, [("a.pdf", b"%PDF-a"), ("b.pdf", b"%PDF-b")])
    first = inventory(source)
    second_parent = list_archive(source, "22222222-2222-4222-8222-222222222222", **LIMITS)
    assert first["members"][0]["candidate_id"] != second_parent["members"][0]["candidate_id"]
    make_zip(tmp_path, [("a.pdf", b"%PDF-new"), ("b.pdf", b"%PDF-b")])
    changed = inventory(source)
    assert first["inventory_hash"] != changed["inventory_hash"]
    assert first["members"][1]["candidate_id"] != changed["members"][1]["candidate_id"]


def test_extract_only_selected_id_preserves_original_and_private_provenance(tmp_path):
    doc = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1synthetic"
    source = make_zip(tmp_path, [("DIR/public.doc", doc), ("unselected.pdf", b"%PDF-other"),
                               ("nested.rar", b"nested")])
    original, listing = source.read_bytes(), inventory(source)
    member = listing["members"][0]
    destination = tmp_path / "private"
    result = extract_members(source, listing, candidate_ids=[member["candidate_id"]], destination=destination)
    assert len(result) == 1
    extracted = Path(result[0]["path"])
    assert extracted.read_bytes() == doc
    assert extracted.name != "public.doc" and extracted.suffix == ".doc"
    assert result[0]["original_sha256"] == hashlib.sha256(doc).hexdigest()
    assert result[0]["parent_sha256"] == listing["parent_sha256"]
    assert result[0]["member_path"] == "DIR/public.doc"
    assert stat.S_IMODE(extracted.stat().st_mode) == 0o600
    assert stat.S_IMODE(extracted.parent.stat().st_mode) == 0o700
    assert source.read_bytes() == original
    assert not list(destination.rglob("*.pdf")) and not list(destination.rglob("*.rar"))
    # Replays verify byte bindings and do not unpack again.
    with patch("zipfile.ZipFile.open", side_effect=AssertionError("must use verified cache")):
        assert extract_members(source, listing, candidate_ids=[member["candidate_id"]],
                               destination=destination) == result


def test_different_archives_never_overwrite_member_outputs(tmp_path):
    first = make_zip(tmp_path, [("bases.pdf", b"%PDF-first")])
    first_listing = inventory(first)
    destination = tmp_path / "private"
    first_result = extract_members(first, first_listing,
        candidate_ids=[first_listing["members"][0]["candidate_id"]], destination=destination)
    make_zip(tmp_path, [("bases.pdf", b"%PDF-second")])
    second_listing = inventory(first)
    second_result = extract_members(first, second_listing,
        candidate_ids=[second_listing["members"][0]["candidate_id"]], destination=destination)
    assert first_result[0]["path"] != second_result[0]["path"]
    assert Path(first_result[0]["path"]).read_bytes() == b"%PDF-first"


@pytest.mark.parametrize("name", ["../bases.pdf", "/bases.pdf", "C:\\bases.pdf", "a/../../b.pdf",
    "a/./b.pdf", "a//b.pdf", "a\nname.pdf", "a\x00.pdf", "folder /b.pdf", "../", "．/a.pdf"])
def test_unsafe_paths_are_rejected(name):
    with pytest.raises(ArchiveInventoryError, match="unsafe_member_path"):
        _safe_name(name, rar=False)


@pytest.mark.parametrize("names", [("a.pdf", "a.pdf"), ("A.pdf", "a.pdf"),
    ("folder\\a.pdf", "folder/a.pdf"), ("ａ.pdf", "a.pdf"), ("é.pdf", "e\u0301.pdf")])
def test_duplicate_and_normalized_collisions_are_rejected(tmp_path, names):
    source = make_zip(tmp_path, [(name, b"%PDF-a") for name in names])
    with pytest.raises(ArchiveInventoryError, match="duplicate_or_colliding_member"):
        inventory(source)


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600, stat.S_IFSOCK | 0o600])
def test_zip_links_and_special_members_are_rejected(tmp_path, mode):
    source = tmp_path / "original.zip"
    member = zipfile.ZipInfo("bases.pdf")
    member.create_system = 3
    member.external_attr = mode << 16
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(member, b"%PDF-a")
    with pytest.raises(ArchiveInventoryError, match="linked_or_special_member"):
        inventory(source)


def test_zip_password_flags_are_rejected_before_extracting(tmp_path):
    source = make_zip(tmp_path, [("bases.pdf", b"%PDF-a")])
    content = bytearray(source.read_bytes())
    local, central = content.index(b"PK\x03\x04"), content.index(b"PK\x01\x02")
    for position in (local + 6, central + 8):
        struct.pack_into("<H", content, position, struct.unpack_from("<H", content, position)[0] | 1)
    source.write_bytes(content)
    with pytest.raises(ArchiveInventoryError, match="encrypted_or_unknown_encryption_member"):
        inventory(source)


@pytest.mark.parametrize("limit", ["max_source_bytes", "max_expanded_bytes", "max_member_bytes", "max_members"])
def test_each_configured_limit_is_enforced(tmp_path, limit):
    source = make_zip(tmp_path, [("a.pdf", b"%PDF-a"), ("b.pdf", b"%PDF-b")])
    with pytest.raises(ArchiveInventoryError):
        inventory(source, **{limit: 1})


def test_zip_member_ratio_is_bounded(tmp_path):
    source = make_zip(tmp_path, [("a.pdf", b"%PDF-" + b"0" * 2_000_000)], compression=zipfile.ZIP_DEFLATED)
    with pytest.raises(ArchiveInventoryError, match="archive_member_ratio_limit"):
        inventory(source)


def test_zip_false_counter_is_checked_before_zipinfo_allocation(tmp_path):
    source = make_zip(tmp_path, [("a.pdf", b"%PDF-a"), ("b.pdf", b"%PDF-b")])
    content = bytearray(source.read_bytes())
    position = content.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", content, position + 8, 1, 1)
    source.write_bytes(content)
    with patch("zipfile.ZipFile", side_effect=AssertionError("must count before allocation")):
        with pytest.raises(ArchiveInventoryError, match="archive_member_limit"):
            inventory(source, max_members=1)


def test_zip_comment_unsupported_by_stdlib_fails_closed(tmp_path):
    source = make_zip(tmp_path, [("a.pdf", b"%PDF-a")])
    with zipfile.ZipFile(source, "a") as archive:
        archive.comment = b"public comment PK\x05\x06 with text"
    # CPython's ZipFile searches the last signature even inside the comment.
    # Never emit a partial or fake inventory when its reader cannot open it.
    with pytest.raises(ArchiveInventoryError, match="archive_inventory_failed"):
        inventory(source)


def test_small_member_force_zip64_is_supported(tmp_path):
    source = tmp_path / "original.zip"
    with zipfile.ZipFile(source, "w") as archive:
        with archive.open("a.pdf", "w", force_zip64=True) as member:
            member.write(b"%PDF-a")
    listing = inventory(source)
    result = extract_members(source, listing, candidate_ids=[listing["members"][0]["candidate_id"]],
                             destination=tmp_path / "private")
    assert Path(result[0]["path"]).read_bytes() == b"%PDF-a"


def test_zip_directory_attribute_is_never_a_candidate(tmp_path):
    source = tmp_path / "original.zip"
    member = zipfile.ZipInfo("fake.pdf")
    member.create_system = 3
    member.external_attr = (stat.S_IFDIR | 0o700) << 16
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(member, b"")
    assert inventory(source)["members"][0]["status"] == "directory"


def test_large_member_does_not_inherit_old_twenty_megabyte_cap(tmp_path):
    payload = b"%PDF-" + b"x" * (21 * 1024 * 1024)
    source = make_zip(tmp_path, [("large.pdf", payload)])
    listing = inventory(source)
    result = extract_members(source, listing, candidate_ids=[listing["members"][0]["candidate_id"]],
                             destination=tmp_path / "private")
    assert Path(result[0]["path"]).stat().st_size == len(payload)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_invalid_timeout_is_rejected(tmp_path, timeout):
    source = make_zip(tmp_path, [("a.pdf", b"%PDF-a")])
    with pytest.raises(ArchiveInventoryError, match="invalid_archive_timeout"):
        inventory(source, timeout=timeout)


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_limits_are_rejected(tmp_path, value):
    source = make_zip(tmp_path, [("a.pdf", b"%PDF-a")])
    with pytest.raises(ArchiveInventoryError, match="invalid_archive_limits"):
        inventory(source, max_members=value)


@pytest.mark.parametrize("index", [1, 2])
def test_nested_and_unsupported_members_cannot_be_selected(tmp_path, index):
    source = make_zip(tmp_path, [("a.pdf", b"%PDF-a"), ("nested.zip", b"not-expanded"), ("a.txt", b"text")])
    listing = inventory(source)
    with pytest.raises(ArchiveInventoryError, match="selected_member_not_candidate"):
        extract_members(source, listing, candidate_ids=[listing["members"][index]["candidate_id"]],
                        destination=tmp_path / "private")
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize("ids", [["unknown"], ["duplicate", "duplicate"], "not-a-list"])
def test_bad_selections_are_rejected(tmp_path, ids):
    source = make_zip(tmp_path, [("a.pdf", b"%PDF-a")])
    with pytest.raises(ArchiveInventoryError):
        extract_members(source, inventory(source), candidate_ids=ids, destination=tmp_path / "private")


def test_mutated_archive_or_inventory_cannot_reuse_selection(tmp_path):
    source = make_zip(tmp_path, [("a.pdf", b"%PDF-a")])
    listing = inventory(source)
    altered = json.loads(json.dumps(listing))
    altered["members"][0]["member_path"] = "fake.pdf"
    with pytest.raises(ArchiveInventoryError, match="archive_inventory_changed_or_tampered"):
        extract_members(source, altered, candidate_ids=[listing["members"][0]["candidate_id"]],
                        destination=tmp_path / "private")
    make_zip(tmp_path, [("a.pdf", b"%PDF-changed")])
    with pytest.raises(ArchiveInventoryError, match="archive_inventory_changed_or_tampered"):
        extract_members(source, listing, candidate_ids=[listing["members"][0]["candidate_id"]],
                        destination=tmp_path / "private")


def test_wrong_member_format_blocks_output(tmp_path):
    source = make_zip(tmp_path, [("a.pdf", b"not PDF")])
    listing = inventory(source)
    with pytest.raises(ArchiveInventoryError, match="selected_member_format_mismatch"):
        extract_members(source, listing, candidate_ids=[listing["members"][0]["candidate_id"]],
                        destination=tmp_path / "private")
    assert not list((tmp_path / "private").rglob("*.pdf"))


def test_member_cache_tampering_and_symlink_are_rejected(tmp_path):
    source = make_zip(tmp_path, [("a.pdf", b"%PDF-a")])
    listing, destination = inventory(source), tmp_path / "private"
    args = dict(candidate_ids=[listing["members"][0]["candidate_id"]], destination=destination)
    extracted = extract_members(source, listing, **args)
    target = Path(extracted[0]["path"])
    target.write_bytes(b"%PDF-b")
    with pytest.raises(ArchiveInventoryError, match="member_cache_binding_mismatch"):
        extract_members(source, listing, **args)
    target.unlink()
    target.symlink_to(source)
    with pytest.raises(ArchiveInventoryError, match="unsafe_archive_destination"):
        extract_members(source, listing, **args)


@pytest.mark.parametrize("missing", ["original", "marker"])
def test_crash_between_member_and_manifest_writes_recovers_from_source(tmp_path, missing):
    source = make_zip(tmp_path, [("a.pdf", b"%PDF-a")])
    listing, destination = inventory(source), tmp_path / "private"
    args = dict(candidate_ids=[listing["members"][0]["candidate_id"]], destination=destination)
    first = extract_members(source, listing, **args)
    target = Path(first[0]["path"])
    (target if missing == "original" else target.with_suffix(".json")).unlink()
    assert extract_members(source, listing, **args) == first


def test_symlink_sources_and_destinations_are_rejected(tmp_path):
    source = make_zip(tmp_path, [("a.pdf", b"%PDF-a")])
    link = tmp_path / "source-link.zip"
    link.symlink_to(source)
    with pytest.raises(ArchiveInventoryError, match="unsafe_archive_source"):
        inventory(link)
    destination = tmp_path / "destination"
    destination.symlink_to(tmp_path, target_is_directory=True)
    listing = inventory(source)
    with pytest.raises(ArchiveInventoryError, match="unsafe_archive_destination"):
        extract_members(source, listing, candidate_ids=[listing["members"][0]["candidate_id"]],
                        destination=destination)


def rar_stub(tmp_path: Path) -> Path:
    source = tmp_path / "original.rar"
    source.write_bytes(b"Rar!\x1a\x07\x01\x00" + rar5_header(b"\x05\x00"))
    return source


def rar5_header(body: bytes) -> bytes:
    assert len(body) < 128
    header = bytes([len(body)]) + body
    return struct.pack("<I", zlib.crc32(header)) + header


@pytest.mark.parametrize("body", [b"\x04\x00",  # Archive encryption header.
    b"\x02\x01\x02\x00\x01\x01",  # File header, extra encryption record.
    b"\x03\x01\x02\x00\x01\x01"])  # Encrypted service record.
def test_rar5_password_headers_rejected_before_libarchive(tmp_path, body):
    source = tmp_path / "encrypted.rar"
    source.write_bytes(b"Rar!\x1a\x07\x01\x00" + rar5_header(body))
    with patch("buenapro_worker.prod2.archive_inventory._rar_metadata_bounded") as metadata:
        with pytest.raises(ArchiveInventoryError, match="encrypted_archive_unsupported"):
            inventory(source)
        metadata.assert_not_called()


@pytest.mark.parametrize("kind,flags", [(0x73, 0x80), (0x74, 4), (0x7a, 4)])
def test_rar4_password_flags_rejected_before_libarchive(tmp_path, kind, flags):
    body = struct.pack("<BHH", kind, flags, 7)
    source = tmp_path / "encrypted.rar"
    source.write_bytes(b"Rar!\x1a\x07\x00" + struct.pack("<H", zlib.crc32(body) & 0xffff) + body)
    with pytest.raises(ArchiveInventoryError, match="encrypted_archive_unsupported"):
        inventory(source)


@pytest.mark.parametrize("content", [b"Rar!\x1a\x07\x01\x00broken", b"Rar!\x1a\x07\x00broken",
    b"Rar!\x1a\x07\x01\x00" + rar5_header(b"\x02\x01\xff"),
    b"Rar!\x1a\x07\x01\x00" + rar5_header(b"\x02\x02\x7f")])
def test_malformed_rar_headers_fail_closed(tmp_path, content):
    source = tmp_path / "malformed.rar"
    source.write_bytes(content)
    with pytest.raises(ArchiveInventoryError, match="rar_header_invalid"):
        inventory(source)


def test_rar5_header_check_seeks_over_payload_without_decompression(tmp_path):
    source = tmp_path / "payload.rar"
    source.write_bytes(b"Rar!\x1a\x07\x01\x00" + rar5_header(b"\x02\x02\x04")
                       + b"data" + rar5_header(b"\x05\x00"))
    import time
    _check_rar_encryption(source, time.monotonic() + 1, 2)


def test_rar_metadata_and_streaming_are_bounded_and_literal(tmp_path):
    source = rar_stub(tmp_path)
    metadata = [{"name": "folder/Bases publicas.pdf", "size": 6, "kind": "file", "linked": False,
                 "encrypted": False}]
    with patch("buenapro_worker.prod2.archive_inventory._rar_metadata_bounded", return_value=metadata):
        listing = inventory(source)
        with patch("buenapro_worker.prod2.archive_inventory.shutil.which", return_value="/usr/bin/bsdtar"), \
             patch("buenapro_worker.prod2.archive_inventory.bounded_command", return_value=b"%PDF-a") as command:
            result = extract_members(source, listing, candidate_ids=[listing["members"][0]["candidate_id"]],
                                     destination=tmp_path / "private")
        assert Path(result[0]["path"]).read_bytes() == b"%PDF-a"
        assert command.call_args.args[0][-2:] == ["--", "folder/Bases publicas.pdf"]
        assert command.call_args.kwargs["limit"] == LIMITS["max_member_bytes"]
        assert 0 < command.call_args.kwargs["timeout"] <= 60


def test_rar_unicode_tool_spelling_preserves_inventory_identity(tmp_path):
    import unicodedata
    source = rar_stub(tmp_path)
    name = "folder/Resolución.pdf"
    metadata = [{"name": name, "size": 6, "kind": "file", "linked": False,
                 "encrypted": False}]
    with patch("buenapro_worker.prod2.archive_inventory._rar_metadata_bounded", return_value=metadata):
        listing = inventory(source)
        member = listing["members"][0]
        with patch("buenapro_worker.prod2.archive_inventory.shutil.which", return_value="/usr/bin/bsdtar"), \
             patch("buenapro_worker.prod2.archive_inventory.bounded_command",
                   side_effect=[ValueError("Local document tool failed"), b"%PDF-a"]) as command:
            result = extract_members(source, listing, candidate_ids=[member["candidate_id"]],
                                     destination=tmp_path / "private")
        assert command.call_count == 2
        assert command.call_args_list[0].args[0][-1] == name
        assert command.call_args_list[1].args[0][-1] == unicodedata.normalize("NFD", name)
        assert result[0]["member_path"] == name
        assert result[0]["candidate_id"] == member["candidate_id"]
        assert Path(result[0]["path"]).read_bytes() == b"%PDF-a"


@pytest.mark.parametrize("error", [TimeoutError("Local document tool timed out"),
    ValueError("Local document output exceeds size policy")])
def test_rar_unicode_spelling_does_not_repeat_timeout_or_size_failure(tmp_path, error):
    source = rar_stub(tmp_path)
    metadata = [{"name": "folder/Resolución.pdf", "size": 6, "kind": "file",
                 "linked": False, "encrypted": False}]
    with patch("buenapro_worker.prod2.archive_inventory._rar_metadata_bounded", return_value=metadata):
        listing = inventory(source)
        with patch("buenapro_worker.prod2.archive_inventory.shutil.which", return_value="/usr/bin/bsdtar"), \
             patch("buenapro_worker.prod2.archive_inventory.bounded_command", side_effect=error) as command:
            with pytest.raises(ArchiveInventoryError, match="archive_member_extraction_failed"):
                extract_members(source, listing, candidate_ids=[listing["members"][0]["candidate_id"]],
                                destination=tmp_path / "private")
        assert command.call_count == 1


@pytest.mark.parametrize("name,decoy", [("a[1].pdf", "a1.pdf"),
    ("a*.pdf", "another.pdf"), ("a?.pdf", "ab.pdf"),
    ("3 Invitacion[F][F][F].pdf", "3 InvitacionFFF.pdf")])
def test_native_bsdtar_reads_glob_characters_as_literal_names(tmp_path, name, decoy):
    import shutil
    import time
    from buenapro_worker.prod2.archive_inventory import _rar_member_bytes
    tool = shutil.which("bsdtar")
    if not tool:
        pytest.skip("Native bsdtar unavailable")
    source = tmp_path / "literal-names.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(name, b"%PDF-exact")
        archive.writestr(decoy, b"%PDF-decoy")
    assert _rar_member_bytes(tool, source, name, 1024, time.monotonic() + 10) == b"%PDF-exact"


@pytest.mark.parametrize("name", ["a\\b.pdf", "a\n.pdf"])
def test_rar_pattern_and_control_names_are_rejected(tmp_path, name):
    source = rar_stub(tmp_path)
    metadata = [{"name": name, "size": 6, "kind": "file", "linked": False, "encrypted": False}]
    with patch("buenapro_worker.prod2.archive_inventory._rar_metadata_bounded", return_value=metadata):
        with pytest.raises(ArchiveInventoryError):
            inventory(source)


@pytest.mark.parametrize("override", [{"encrypted": True}, {"encrypted": None}, {"linked": True},
                                       {"kind": "special"}, {"size": -1}])
def test_rar_unsafe_metadata_is_rejected(tmp_path, override):
    source = rar_stub(tmp_path)
    metadata = [{"name": "a.pdf", "size": 6, "kind": "file", "linked": False,
                 "encrypted": False, **override}]
    with patch("buenapro_worker.prod2.archive_inventory._rar_metadata_bounded", return_value=metadata):
        with pytest.raises(ArchiveInventoryError):
            inventory(source)


def test_rar_metadata_timeout_fails_closed(tmp_path):
    source = rar_stub(tmp_path)
    with patch("buenapro_worker.prod2.archive_inventory.bounded_command", side_effect=TimeoutError):
        with pytest.raises(ArchiveInventoryError, match="rar_metadata_failed_or_encrypted"):
            inventory(source)


def test_inventory_deadline_is_enforced(tmp_path):
    source = make_zip(tmp_path, [("a.pdf", b"%PDF-a")])
    with patch("buenapro_worker.prod2.archive_inventory.time.monotonic", side_effect=[0, 100]):
        with pytest.raises(ArchiveInventoryError, match="archive_timeout"):
            inventory(source, timeout=30)


def test_real_local_rar_metadata_when_historical_fixture_exists():
    # No member extraction, model calls or fixture content logging.
    root = Path(__file__).resolve().parents[3]
    source = root / "tmp/prod2-accepted10-20261006/1046937/dc9c6162-8b9e-4bf5-8364-3fed2e535804.rar"
    if not source.is_file():
        pytest.skip("Optional public historical RAR metadata fixture is unavailable")
    result = list_archive(source, source.stem, **LIMITS)
    assert result["archive_format"] == "rar"
    candidates = [row for row in result["members"] if row["status"] == "candidate"]
    assert sorted(row["format"] for row in candidates) == ["docx", "pdf"]
    assert all(row["status"] in {"candidate", "directory"} for row in result["members"])


def test_second_real_rar_metadata_is_complete_when_available():
    root = Path(__file__).resolve().parents[3]
    source = root / "tmp/prod2-accepted10-20261006/1046116/d4ac9dee-3d43-4b8d-bb8e-9a84d3a18bdd.rar"
    if not source.is_file():
        pytest.skip("Optional public historical RAR metadata fixture is unavailable")
    result = list_archive(source, source.stem, **LIMITS)
    candidates = [row for row in result["members"] if row["status"] == "candidate"]
    assert len(candidates) == 2 and all(row["format"] == "pdf" for row in candidates)


def test_real_local_rar_extracts_only_one_public_pdf_when_available(tmp_path):
    root = Path(__file__).resolve().parents[3]
    source = root / "tmp/prod2-accepted10-20261006/1046937/dc9c6162-8b9e-4bf5-8364-3fed2e535804.rar"
    if not source.is_file():
        pytest.skip("Optional public historical RAR fixture is unavailable")
    original_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    listing = list_archive(source, source.stem, **LIMITS)
    member = next(row for row in listing["members"] if row["format"] == "pdf")
    result = extract_members(source, listing, candidate_ids=[member["candidate_id"]], destination=tmp_path)
    assert len(result) == 1 and Path(result[0]["path"]).read_bytes().startswith(b"%PDF-")
    assert Path(result[0]["path"]).stat().st_size == member["size"]
    assert not list(tmp_path.rglob("*.docx"))
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_sha


def make_sevenzip(tmp_path, entries):
    import shutil
    import subprocess
    tool = shutil.which('bsdtar')
    if not tool:
        pytest.skip('Native bsdtar unavailable')
    root = tmp_path / 'sevenzip-input'
    root.mkdir()
    for name, content in entries:
        member = root / name
        member.parent.mkdir(parents=True, exist_ok=True)
        member.write_bytes(content)
    target = tmp_path / 'original.7z'
    result = subprocess.run([tool, '--format', '7zip', '-cf', str(target), '-C', str(root),
                             *[name for name, _ in entries]], capture_output=True, timeout=10)
    if result.returncode:
        pytest.skip('Native libarchive cannot create the offline 7z fixture')
    return target


def test_sevenzip_requires_opt_in_and_selective_extraction_preserves_provenance(tmp_path):
    word = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1word'
    source = make_sevenzip(tmp_path, [('folder/Bases públicas[1].pdf', b'%PDF-principal'),
        ('other.doc', word), ('nested.zip', b'not-opened'), ('other.docx', b'PK\x03\x04word')])
    original = source.read_bytes()
    with pytest.raises(ArchiveInventoryError, match='unsupported_archive_signature'):
        inventory(source)
    listing = inventory(source, policy_version=POLICY_7Z)
    assert listing['policy_version'] == POLICY_7Z and listing['archive_format'] == '7z'
    assert listing['inventory_complete'] and not listing['nested_contents_read']
    assert [m['status'] for m in listing['members']] == [
        'candidate', 'candidate', 'pending_nested_archive', 'candidate']
    selected = listing['members'][0]
    result = extract_members(source, listing, candidate_ids=[selected['candidate_id']],
                             destination=tmp_path / 'private')
    assert len(result) == 1 and Path(result[0]['path']).read_bytes() == b'%PDF-principal'
    assert result[0]['member_path'] == selected['member_path']
    assert result[0]['parent_sha256'] == hashlib.sha256(original).hexdigest()
    assert result[0]['original_sha256'] == hashlib.sha256(b'%PDF-principal').hexdigest()
    assert len(list((tmp_path / 'private').rglob('*.pdf'))) == 1
    assert not list((tmp_path / 'private').rglob('*.doc*'))
    assert source.read_bytes() == original
    # Bound 7z extraction replays exactly without another member extraction.
    from buenapro_worker.prod2 import archive_inventory as subject
    with patch.object(subject, '_rar_member_bytes', side_effect=AssertionError('verified member cache')):
        assert extract_members(source, listing, candidate_ids=[selected['candidate_id']],
                               destination=tmp_path / 'private') == result
    tampered = {**listing, 'policy_version': 'prod2_archive_inventory_v1'}
    with pytest.raises(ArchiveInventoryError):
        extract_members(source, tampered, candidate_ids=[selected['candidate_id']],
                        destination=tmp_path / 'private')


@pytest.mark.parametrize('limit', ['max_source_bytes', 'max_expanded_bytes', 'max_member_bytes', 'max_members'])
def test_sevenzip_all_configured_bounds_apply_before_selected_extraction(tmp_path, limit):
    source = make_sevenzip(tmp_path, [('a.pdf', b'%PDF-a'), ('b.pdf', b'%PDF-b')])
    with pytest.raises(ArchiveInventoryError):
        inventory(source, policy_version=POLICY_7Z, **{limit: 1})
    assert not (tmp_path / 'private').exists()


@pytest.mark.parametrize('override', [{'encrypted': True}, {'encrypted': None}, {'linked': True},
    {'kind': 'special'}, {'name': '../bad.pdf'}, {'name': 'a\\b.pdf'}, {'size': -1}])
def test_sevenzip_unsafe_metadata_is_never_a_candidate(tmp_path, override):
    source = tmp_path / 'original.7z'
    source.write_bytes(b'7z\xbc\xaf\x27\x1cfixture')
    raw = [{'name': 'a.pdf', 'size': 6, 'kind': 'file', 'linked': False, 'encrypted': False, **override}]
    with patch('buenapro_worker.prod2.archive_inventory._sevenzip_metadata_bounded', return_value=raw):
        with pytest.raises(ArchiveInventoryError):
            inventory(source, policy_version=POLICY_7Z)


@pytest.mark.parametrize('status', [-2, -1, 1])
def test_sevenzip_requires_library_to_prove_no_encrypted_entries(status):
    from unittest.mock import MagicMock
    from buenapro_worker.prod2.archive_inventory import _libarchive_metadata
    lib = MagicMock()
    lib.archive_read_new.return_value = 1
    lib.archive_read_open_filename.return_value = 0
    def header(_, target):
        target._obj.value = 1
        return 0
    lib.archive_read_next_header.side_effect = [header, 1]
    # Side-effect functions must be called, rather than returned by a list.
    seen = [False]
    def next_header(_, target):
        if seen[0]:
            return 1
        seen[0] = True
        return header(_, target)
    lib.archive_read_next_header.side_effect = next_header
    lib.archive_entry_pathname_utf8.return_value = b'a.pdf'
    lib.archive_entry_size_is_set.return_value = 1
    lib.archive_entry_size.return_value = 6
    lib.archive_entry_filetype.return_value = stat.S_IFREG
    lib.archive_entry_is_encrypted.return_value = 0
    lib.archive_entry_symlink.return_value = None
    lib.archive_entry_hardlink.return_value = None
    lib.archive_read_data_skip.return_value = 0
    lib.archive_read_has_encrypted_entries.return_value = status
    with patch('ctypes.util.find_library', return_value='archive'), patch('ctypes.CDLL', return_value=lib):
        with pytest.raises(ArchiveInventoryError, match='encrypted_or_unknown_encryption_member'):
            _libarchive_metadata('fixture.7z', 10, 1024, kind='7z')
    lib.archive_read_free.assert_called_once_with(1)


def test_sevenzip_malformed_headers_and_child_timeout_fail_closed(tmp_path):
    source = tmp_path / 'broken.7z'
    source.write_bytes(b'7z\xbc\xaf\x27\x1cbroken')
    with pytest.raises(ArchiveInventoryError, match='7z_metadata_failed_or_encrypted'):
        inventory(source, policy_version=POLICY_7Z)
    with patch('buenapro_worker.prod2.archive_inventory.bounded_command', side_effect=TimeoutError):
        with pytest.raises(ArchiveInventoryError, match='7z_metadata_failed_or_encrypted'):
            inventory(source, policy_version=POLICY_7Z)


@pytest.mark.parametrize('policy', [None, {}, True, 'unknown'])
def test_invalid_archive_policy_rejected_before_source_access(policy):
    with pytest.raises(ArchiveInventoryError, match='invalid_archive_inventory_policy'):
        list_archive('does-not-exist.7z', PARENT, **LIMITS, policy_version=policy)


def test_opt_in_is_a_new_inventory_version_and_legacy_zip_stays_exact(tmp_path):
    source = make_zip(tmp_path, [('a.pdf', b'%PDF-a')])
    before = inventory(source)
    modern = inventory(source, policy_version=POLICY_7Z)
    assert modern['inventory_hash'] != before['inventory_hash']
    assert modern['members'] == before['members']
    assert inventory(source) == before
