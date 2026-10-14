"""Archive verified selected PROD2 evidence privately before scoped laptop cleanup.

Dry-run is the default. Only --execute uploads and removes inventoried binary
files; JSON, payment receipts, preparation pins and the local archive remain.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, BotoCoreError
from dotenv import dotenv_values

from buenapro_worker.prod2.batch_runner import private_write, read_private
from buenapro_worker.prod2.repository import hash_request

POLICY = "prod2_verified_laptop_cleanup_v1"
MAX_BYTES = 2 * 1024**3
MAX_FILES = 20000
CHUNK = 1024 * 1024
BINARY = {".pdf", ".doc", ".docx", ".zip", ".rar", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
SHA = re.compile(r"[a-f0-9]{64}")


class CleanupError(ValueError):
    """Sanitized archive/cleanup failure; original evidence remains untouched."""


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-folder", type=Path, required=True)
    p.add_argument("--verification-file", type=Path, required=True)
    p.add_argument("--archive-folder", type=Path, required=True)
    p.add_argument("--execute", action="store_true")
    return p


def safe_path(path, *, directory=False, missing=False):
    path = Path(path).absolute()
    base = (ROOT / "tmp").resolve()
    if ".." in path.parts or not path.is_relative_to(base):
        raise CleanupError("Cleanup paths must remain inside the repository tmp directory")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise CleanupError("Cleanup paths must not contain symlinks")
    if path.exists():
        mode = path.lstat().st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise CleanupError("Cleanup path is not a regular file or expected directory")
    elif not missing:
        raise CleanupError("Required cleanup evidence is missing")
    return path


def file_facts(path):
    path = safe_path(path)
    before = path.stat()
    if before.st_size > MAX_BYTES:
        raise CleanupError("One cleanup artifact exceeds the two GiB streaming limit")
    digest, total = hashlib.sha256(), 0
    with path.open("rb") as source:
        while chunk := source.read(CHUNK):
            total += len(chunk)
            if total > MAX_BYTES:
                raise CleanupError("Cleanup artifact exceeds the streaming limit")
            digest.update(chunk)
    after = path.stat()
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or total != before.st_size):
        raise CleanupError("Cleanup source changed during inventory")
    return {"logical_path": path.relative_to(ROOT).as_posix(), "sha256": digest.hexdigest(), "bytes": total,
            "binary": path.suffix.lower() in BINARY}


def _private(path):
    safe_path(path)
    return read_private(path)


def verified_run(run_folder, verification_file):
    run = safe_path(run_folder, directory=True)
    proof = _private(verification_file)
    ids = proof.get("verified_ids")
    if (not isinstance(ids, list) or len(ids) != 10 or len(set(ids)) != 10
            or any(type(i) is not int or i <= 0 for i in ids) or proof.get("llm1_complete") != 10
            or any(proof.get(k) is not True for k in ("pg_verified", "api_verified", "r2_verified"))
            or any(type(proof.get(k)) is not int or proof[k] != 0 for k in (
                "remote_cleanup_pending", "usage_unknown_attempts", "active_payments"))
            or proof.get("process_running") is not False):
        raise CleanupError("Cleanup requires verified ten-member completion and no active/unknown payments or process")
    manifest = _private(run / "manifest.private.json")
    if (manifest.get("terminal_stage") != "llm1" or manifest.get("selection_only")
            or manifest.get("recovery_folder") != str(run) or set(manifest.get("recovery_ids", [])) != set(ids)):
        raise CleanupError("Verification IDs do not bind the completed selected run")
    if proof.get("run_folder") not in (None, str(run)) or proof.get("manifest_hash") not in (None, hash_request(manifest)):
        raise CleanupError("Verification file belongs to different run inputs")
    reports = sorted(run.glob("report-*.private.json"))
    if not reports:
        raise CleanupError("Final immutable run report is missing")
    report = _private(reports[-1])
    records = report.get("records")
    if (report.get("run_id") != manifest.get("run_id") or report.get("llm1_complete") != 10
            or not isinstance(records, list) or len(records) != 10
            or {r.get("convocation_id") for r in records} != set(ids)):
        raise CleanupError("Final run report does not confirm exact ten-member completion")
    hashes = set()
    for record in records:
        conv = record["convocation_id"]
        outcome = _private(run / str(conv) / "outcome.private.json")
        if (record != outcome or outcome.get("status") != "llm1_complete" or outcome.get("accepted") is not True
                or outcome.get("recovery_manifest_hash") != hash_request(manifest)
                or not SHA.fullmatch(outcome.get("document_set_hash", ""))):
            raise CleanupError("Completed outcome or selected preparation hash is unbound")
        accepted = _private(run / str(conv) / "accepted-proof.private.json")
        if (accepted.get("convocation_id") != conv or accepted.get("run_id") != manifest.get("run_id")
                or outcome.get("accepted_proof_hash") != hash_request(accepted)):
            raise CleanupError("Final outcome differs from its accepted membership proof")
        hashes.add(outcome["document_set_hash"])
    return run, manifest, ids, hashes, hash_request(proof)


def _secret_path(path):
    return any(part.startswith(".env") or part in {"credentials", ".git", "node_modules", ".venv"}
               or part.lower().endswith((".pem", ".key")) for part in path.parts)


def inventory(run_folder, verification_file, archive_folder):
    run, manifest, ids, hashes, proof_hash = verified_run(run_folder, verification_file)
    archive = safe_path(archive_folder, directory=True, missing=True)
    roots = [run, safe_path(manifest["original_folder"], directory=True)]
    for field in ("detail_recovery_folders", "download_cache_folders", "prior_recovery_folders"):
        paths = manifest.get(field, [])
        if not isinstance(paths, list) or len(paths) > 8:
            raise CleanupError("Cleanup source folders exceed the bounded run manifest")
        roots.extend(safe_path(p, directory=True) for p in paths)
    roots = list(dict.fromkeys(roots))
    cache = safe_path(manifest["cache_folder"], directory=True)
    if any("keyword-cache" in part for root in [*roots, cache] for part in root.parts):
        raise CleanupError("Keyword query evidence is outside this cleanup scope")
    preparation = [safe_path(cache / ("selected-analysis-" + sha), directory=True) for sha in sorted(hashes)]
    allowed = roots + [cache]
    if any(archive == root or archive.is_relative_to(root) or root.is_relative_to(archive) for root in allowed):
        raise CleanupError("Audit archive folder must be separate from cleanup sources")
    files, case_owners = set(), {}
    for root in roots:
        files.update(path for path in root.glob("*.private.json") if not _secret_path(path.relative_to(ROOT)))
        for conv in ids:
            case = root / str(conv)
            if not case.exists():
                continue
            safe_path(case, directory=True)
            for path in case.rglob("*"):
                if _secret_path(path.relative_to(ROOT)):
                    continue
                if path.is_symlink():
                    raise CleanupError("Case evidence contains a symlink")
                if path.is_file():
                    files.add(path)
                    case_owners[path] = case.relative_to(ROOT).as_posix()
    for root in preparation:
        for path in root.rglob("*"):
            if _secret_path(path.relative_to(ROOT)):
                continue
            if path.is_symlink():
                raise CleanupError("Preparation evidence contains a symlink")
            if path.is_file():
                files.add(path)
    if len(files) > MAX_FILES:
        raise CleanupError("Cleanup inventory exceeds the bounded file count")
    entries = [{**file_facts(path), **({"case_path": case_owners[path]} if path in case_owners else {})}
               for path in sorted(files)]
    blobs = {}
    for entry in entries:
        previous = blobs.setdefault(entry["sha256"], entry)
        if previous["bytes"] != entry["bytes"]:
            raise CleanupError("Identical artifact SHA has contradictory byte counts")
    if sum(row["bytes"] for row in blobs.values()) > MAX_BYTES:
        raise CleanupError("Unique cleanup evidence exceeds the two GiB streaming limit")
    retained = payment_evidence(entries)
    for row in entries:
        row["delete"] = row["binary"] and row.get("case_path") not in retained
    held = [row for row in entries if row["binary"] and not row["delete"]]
    result = {"policy": POLICY, "run_folder": str(run), "run_id": manifest["run_id"],
        "manifest_hash": hash_request(manifest), "verification_hash": proof_hash,
        "verified_ids": sorted(ids), "files": entries, "unique_blobs": len(blobs),
        "unique_bytes": sum(row["bytes"] for row in blobs.values()),
        "binary_files": sum(row["delete"] for row in entries),
        "binary_bytes": sum(row["bytes"] for row in entries if row["delete"]),
        "retained_unreconciled_case_paths": sorted(retained),
        "retained_unreconciled_binary_files": len(held),
        "retained_unreconciled_binary_bytes": sum(row["bytes"] for row in held)}
    return result


def payment_evidence(entries):
    """A cloud backup cannot authorize deleting uncertain local paid evidence."""
    paths = {row["logical_path"]: ROOT / row["logical_path"] for row in entries}
    owners = {row["logical_path"]: row.get("case_path") for row in entries}
    retained = set()
    def uncertain(logical, message):
        case = owners[logical]
        if not case:
            raise CleanupError(message)
        retained.add(case)
    for logical, path in paths.items():
        name = path.name
        if name.endswith(".claim.private.json") and name.startswith(("selector-", "jev-", "gemini_extraction-")):
            response = logical.replace(".claim.private.json", ".response.private.json")
            if response not in paths:
                uncertain(logical, "Payment ownership has no reconciled local response")
        if name.endswith(".started.private.json") and name.startswith(("jev-attempt-", "gemini-selected-")):
            outcome_path = paths.get(logical.replace(".started.private.json", ".outcome.private.json"))
            if outcome_path is None:
                uncertain(logical, "A paid attempt is still missing its final outcome")
                continue
            value = _private(outcome_path)
            if name.startswith("jev-attempt-") and value.get("usage_known") is not True:
                uncertain(logical, "A Jev attempt still has unknown usage")
            if name.startswith("gemini-selected-") and (value.get("provider_usage_known") is not True
                    or value.get("remote_cleanup_pending") != 0 or value.get("remote_cleanup_unknown") is not False):
                uncertain(logical, "A Gemini attempt has unknown usage or outstanding remote cleanup")
    return retained


def recheck(entries):
    for entry in entries:
        if file_facts(ROOT / entry["logical_path"]) != _facts(entry):
            raise CleanupError("Inventoried file changed; cleanup cannot proceed")


def _facts(entry):
    return {field: entry[field] for field in ("logical_path", "sha256", "bytes", "binary")}


def verify_archive(path, value):
    """Verify logical inventory and every unique streamed blob before cloud use."""
    expected = {row["sha256"]: row["bytes"] for row in value["files"]}
    seen, manifest_seen, total = set(), False, 0
    try:
        with tarfile.open(safe_path(path), mode="r|gz") as archive:
            for member in archive:
                if not member.isfile() or member.size < 0:
                    raise CleanupError("Local archive contains unsupported members")
                stream = archive.extractfile(member)
                if member.name == "manifest.private.json":
                    if manifest_seen or member.size > 16 * 1024**2:
                        raise CleanupError("Local archive manifest exceeds bounds or repeats")
                    body = stream.read(16 * 1024**2 + 1)
                    if json.loads(body) != value:
                        raise CleanupError("Local archive manifest differs from the authorized inventory")
                    manifest_seen = True
                    continue
                digest_name = member.name.removeprefix("blobs/")
                if (member.name != "blobs/" + digest_name or digest_name not in expected
                        or digest_name in seen or member.size != expected[digest_name]):
                    raise CleanupError("Local archive blobs differ from the authorized inventory")
                digest, count = hashlib.sha256(), 0
                while chunk := stream.read(CHUNK):
                    count += len(chunk)
                    total += len(chunk)
                    if count > member.size or total > MAX_BYTES:
                        raise CleanupError("Local archive exceeds streamed evidence bounds")
                    digest.update(chunk)
                if count != member.size or digest.hexdigest() != digest_name:
                    raise CleanupError("Local archive blob SHA verification failed")
                seen.add(digest_name)
        if not manifest_seen or seen != set(expected):
            raise CleanupError("Local archive is missing its full authorized evidence")
    except (tarfile.TarError, OSError, ValueError, UnicodeError):
        raise CleanupError("Local evidence archive validation failed") from None


def build_archive(value, folder):
    folder = safe_path(folder, directory=True, missing=True)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    recheck(value["files"])
    name = "evidence-" + hash_request(value) + ".tar.gz"
    target = folder / name
    if target.exists():
        verify_archive(target, value)
        return target, file_facts(target)
    descriptor, temporary = tempfile.mkstemp(prefix=".archive-", suffix=".tar.gz", dir=folder)
    temporary = Path(temporary)
    seen = set()
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|") as archive:
                    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                    info = tarfile.TarInfo("manifest.private.json")
                    info.size, info.mode = len(content), 0o600
                    archive.addfile(info, io.BytesIO(content))
                    for row in value["files"]:
                        if row["sha256"] in seen:
                            continue
                        seen.add(row["sha256"])
                        source = ROOT / row["logical_path"]
                        if file_facts(source) != _facts(row):
                            raise CleanupError("Inventoried source changed before archive streaming")
                        info = tarfile.TarInfo("blobs/" + row["sha256"])
                        info.size, info.mode = row["bytes"], 0o600
                        with source.open("rb") as stream:
                            archive.addfile(info, stream)
                        if file_facts(source) != _facts(row):
                            raise CleanupError("Inventoried source changed during archive streaming")
            output.flush()
            os.fsync(output.fileno())
        if temporary.stat().st_size > MAX_BYTES:
            raise CleanupError("Backup archive exceeds the two GiB limit")
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    verify_archive(target, value)
    return target, file_facts(target)


def r2_client():
    env = dotenv_values(ROOT / ".env.local")
    required = ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    if not all(env.get(k) for k in required):
        raise CleanupError("Private archive R2 configuration is missing")
    client = boto3.client("s3", endpoint_url=env["R2_ENDPOINT"], aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"], region_name=env.get("R2_REGION") or "auto",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                      connect_timeout=15, read_timeout=120, retries={"max_attempts": 1}))
    return client, env["R2_BUCKET"]


def cloud_backup(client, bucket, archive, facts, run_name, inventory_hash):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", run_name):
        raise CleanupError("Archive run name is invalid")
    key = f"prod2/worker-archives/{run_name}/{facts['sha256']}.tar.gz"
    metadata = {"sha256": facts["sha256"], "inventory-sha256": inventory_hash, "source": "seace_prod2_worker_archive"}
    try:
        try:
            previous = client.head_object(Bucket=bucket, Key=key)
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise
            previous = None
        if previous is not None and (previous.get("ContentLength") != facts["bytes"]
                or any(previous.get("Metadata", {}).get(k) != v for k, v in metadata.items())):
            raise CleanupError("Existing private archive does not match its exact content identity")
        if previous is None:
            with safe_path(archive).open("rb") as source:
                client.put_object(Bucket=bucket, Key=key, Body=source, ContentLength=facts["bytes"],
                                  ContentType="application/gzip", Metadata=metadata)
        head = client.head_object(Bucket=bucket, Key=key)
        if head.get("ContentLength") != facts["bytes"] or any(head.get("Metadata", {}).get(k) != v for k, v in metadata.items()):
            raise CleanupError("Private cloud archive HEAD verification failed")
        response = client.get_object(Bucket=bucket, Key=key)
        stream, digest, total = response["Body"], hashlib.sha256(), 0
        try:
            while chunk := stream.read(CHUNK):
                total += len(chunk)
                if total > facts["bytes"] or total > MAX_BYTES:
                    raise CleanupError("Private cloud archive stream exceeds expected bytes")
                digest.update(chunk)
        finally:
            stream.close()
        if total != facts["bytes"] or digest.hexdigest() != facts["sha256"]:
            raise CleanupError("Private cloud archive GET SHA verification failed")
    except (ClientError, BotoCoreError, OSError):
        raise CleanupError("Private archive cloud upload or verification failed") from None
    return {"storage_key": key, "sha256": facts["sha256"], "bytes": facts["bytes"],
            "inventory_hash": inventory_hash, "head_verified": True, "get_sha_verified": True}


def delete_binaries(entries):
    binaries = [row for row in entries if row.get("delete", row["binary"])]
    # Validate the entire deletion set before removing its first member.
    recheck(binaries)
    for row in binaries:
        path = safe_path(ROOT / row["logical_path"])
        if file_facts(path) != _facts(row):
            raise CleanupError("Binary source changed immediately before deletion")
        path.unlink()
    return {"deleted_files": len(binaries), "deleted_bytes": sum(row["bytes"] for row in binaries)}


def execute_inventory(value, archive_folder):
    folder = safe_path(archive_folder, directory=True, missing=True)
    archive, facts = build_archive(value, folder)
    client, bucket = r2_client()
    try:
        backup = cloud_backup(client, bucket, archive, facts, Path(value["run_folder"]).name, hash_request(value))
    finally:
        client.close()
    recheck(value["files"])
    # The cloud identity is durable locally before the first binary is removed.
    pointer = folder / ("cloud-backup-" + facts["sha256"] + ".private.json")
    if pointer.exists():
        if _private(pointer) != backup:
            raise CleanupError("Cloud archive ownership receipt changed")
    else:
        private_write(pointer, backup)
    result = {"policy": POLICY, "verified_ids": value["verified_ids"], "archive": backup,
        "local_archive_path": str(archive), **delete_binaries(value["files"]),
        "retained_json_receipts_and_pins": True,
        **{k: value[k] for k in ("retained_unreconciled_case_paths", "retained_unreconciled_binary_files",
                               "retained_unreconciled_binary_bytes")},
        "finished_at": datetime.now(timezone.utc).isoformat()}
    private_write(folder / ("cleanup-summary-" + str(datetime.now(timezone.utc).timestamp()) + ".private.json"), result)
    return result


def main():
    args = parser().parse_args()
    value = inventory(args.run_folder, args.verification_file, args.archive_folder)
    if not args.execute:
        print(json.dumps({"policy": POLICY, "dry_run": True, "verified_ids": value["verified_ids"],
            **{k: value[k] for k in ("unique_blobs", "unique_bytes", "binary_files", "binary_bytes")},
            **{k: value[k] for k in ("retained_unreconciled_case_paths", "retained_unreconciled_binary_files",
                                   "retained_unreconciled_binary_bytes")},
            "cloud_calls": 0, "deleted_files": 0}, ensure_ascii=False))
        return 0
    result = execute_inventory(value, args.archive_folder)
    print(json.dumps({"policy": POLICY, "dry_run": False, "deleted_files": result["deleted_files"],
        "deleted_bytes": result["deleted_bytes"], "cloud_head_verified": True, "cloud_get_sha_verified": True,
        "evidence_retained": True,
        **{k: result[k] for k in ("retained_unreconciled_case_paths", "retained_unreconciled_binary_files",
                                "retained_unreconciled_binary_bytes")}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
