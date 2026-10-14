"""Upload private auxiliary PDFs to R2; SEACE remains the original source."""
from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, BotoCoreError
from dotenv import dotenv_values


class PreviewStorageError(RuntimeError):
    pass


def upload_preview(pdf: bytes, *, convocation_id: int, document_uuid: str,
                   original_sha256: str, env_path: str | Path = ".env.local") -> dict:
    if not pdf.startswith(b"%PDF-") or type(convocation_id) is not int or convocation_id <= 0:
        raise ValueError("A valid auxiliary PDF and convocatoria are required")
    code = str(UUID(document_uuid))
    sha = hashlib.sha256(pdf).hexdigest()
    key = f"prod2/{convocation_id}/{code}/{sha}.pdf"
    env = dotenv_values(env_path)
    required = ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    if not all(env.get(k) for k in required):
        raise PreviewStorageError("R2 configuration missing")
    client = boto3.client("s3", endpoint_url=env["R2_ENDPOINT"],
                          aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                          aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                          region_name=env.get("R2_REGION") or "auto",
                          config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                                        connect_timeout=15, read_timeout=60, retries={"max_attempts": 1}))
    reused = False
    try:
        try:
            prior = client.head_object(Bucket=env["R2_BUCKET"], Key=key)
            reused = prior.get("Metadata", {}).get("sha256") == sha and prior["ContentLength"] == len(pdf)
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise
        if not reused:
            client.put_object(Bucket=env["R2_BUCKET"], Key=key, Body=pdf, ContentType="application/pdf",
                              Metadata={"source": "seace_prod2", "sha256": sha,
                                        "original-sha256": original_sha256, "document-uuid": code})
        verify = client.head_object(Bucket=env["R2_BUCKET"], Key=key)
        if verify["ContentLength"] != len(pdf) or verify.get("Metadata", {}).get("sha256") != sha:
            raise PreviewStorageError("R2 preview verification failed")
    except (ClientError, BotoCoreError):
        raise PreviewStorageError("R2 auxiliary PDF upload failed") from None
    finally:
        client.close()
    return {"storage_key": key, "preview_sha256": sha, "preview_bytes": len(pdf), "reused": reused}
