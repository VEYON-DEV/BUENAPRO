"""Private temporary copy of a downloaded PROD6 document, so it is fetched only once.

``download_file`` identifies the document (sha256/MIME/class) and ``extract_tdr`` reads
it. Without a copy, the reader downloads the same file again, doubling the requests
the source sees. The copy is a temporary analysis input, never a permanent store: it
lives under the receipt directory, is named by its sha256, is verified when read, and
the runtime deletes it once the reading is saved (or the document goes to manual review).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path

LOGGER = logging.getLogger(__name__)
SHA256 = re.compile(r"[0-9a-f]{64}")


class SourceCache:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _path(self, sha256: object) -> Path | None:
        # Only a real sha256 names a file; "pending:<id>" or garbage never touches the disk.
        if not isinstance(sha256, str) or not SHA256.fullmatch(sha256):
            return None
        return self.directory / sha256

    def store(self, content: bytes) -> str | None:
        """Save ``content`` privately (dir 0700, file 0600) and return its sha256.

        The cache is an optimization: any filesystem problem returns None and the
        reader falls back to downloading.
        """
        sha256 = hashlib.sha256(content).hexdigest()
        temporary: str | None = None
        try:
            if self.directory.is_symlink():
                raise OSError("source cache must not be a symlink")
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.directory.chmod(0o700)
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix=".tmp-", delete=False) as stream:
                temporary = stream.name
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / sha256)
            temporary = None
            return sha256
        except OSError as error:
            LOGGER.warning("Source cache not written: %s", type(error).__name__)
            return None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def load(self, sha256: object) -> bytes | None:
        """The stored bytes if present and still hashing to ``sha256``; otherwise None.

        A corrupt or swapped file is deleted, so the caller downloads the document again.
        """
        path = self._path(sha256)
        if path is None:
            return None
        try:
            if path.is_symlink() or not path.is_file():
                return None
            content = path.read_bytes()
        except OSError:
            return None
        if hashlib.sha256(content).hexdigest() != sha256:
            self.discard(sha256)
            return None
        return content

    def discard(self, sha256: object) -> None:
        path = self._path(sha256)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                LOGGER.warning("Source cache not removed: %s", type(error).__name__)
