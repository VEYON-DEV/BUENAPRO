"""Extract page-addressed text locally from existing company PDFs. No inference."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from pypdf import PdfReader

VERSION = "company_document_text_v1"


def redact_identifiers(text: str) -> str:
    # Do not erase order numbers or corporate RUCs merely because they have digits.
    return re.sub(r"(?i)(\bD\.?N\.?I\.?[^\n]{0,20}?)\b\d{8}\b", r"\1[omitido]", text)


def private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".writing")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def extract_pdf(path: Path, expected_sha: str, output: Path) -> dict:
    content_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if content_sha != expected_sha:
        raise ValueError("Company PDF hash changed")
    if output.exists():
        prior = json.loads(output.read_text())
        if prior.get("sha256") == expected_sha and prior.get("version") == VERSION:
            return prior
        raise ValueError("Existing text receipt is incompatible")
    pages = []
    pdf = PdfReader(path)
    native_result = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"], capture_output=True, text=True, timeout=180)
    if native_result.returncode:
        raise RuntimeError("Native PDF text extraction failed")
    native_pages = native_result.stdout.split("\f")
    if len(native_pages) == len(pdf.pages) + 1 and not native_pages[-1].strip():
        native_pages.pop()
    if len(native_pages) != len(pdf.pages):
        raise ValueError("Native page count differs from original PDF")
    for number, page in enumerate(pdf.pages, 1):
            native = native_pages[number - 1].strip()
            resources = page.get("/Resources") or {}
            xobjects = resources.get("/XObject") or {}
            images = any(obj.get_object().get("/Subtype") == "/Image" for obj in xobjects.values())
            use_ocr = len(native) < 250 or (images and len(native) < 1200)
            ocr = ""
            if use_ocr:
                with tempfile.TemporaryDirectory(prefix="company-page-", dir=output.parent) as folder:
                    image = Path(folder) / "page.png"
                    render = subprocess.run(["pdftoppm", "-f", str(number), "-l", str(number), "-r", "180", "-singlefile", "-png", str(path), str(image.with_suffix(''))], capture_output=True, timeout=180)
                    if render.returncode:
                        raise RuntimeError("Local PDF rendering failed")
                    result = subprocess.run(["tesseract", str(image), "stdout", "-l", "spa+eng", "--psm", "3"],
                        env=os.environ | {"OMP_THREAD_LIMIT": "1"}, capture_output=True, text=True, timeout=180)
                    if result.returncode:
                        raise RuntimeError("Local OCR failed")
                    ocr = result.stdout.strip()
            text = ocr if len(ocr) > len(native) * 0.75 and len(ocr) > 80 else native
            pages.append({"page": number, "method": "local_ocr" if text == ocr and ocr else "native_text",
                "native_characters": len(native), "ocr_attempted": use_ocr,
                "characters": len(redact_identifiers(text)), "text": redact_identifiers(text),
                "review_required": len(text) < 80})
    receipt = {"version": VERSION, "sha256": expected_sha, "page_count": len(pages), "pages": pages,
        "ocr_pages": sum(p["method"] == "local_ocr" for p in pages),
        "review_pages": [p["page"] for p in pages if p["review_required"]],
        "full_text": "\n\n".join(f"[Página {p['page']}]\n{p['text']}" for p in pages),
        "scope": "all_original_pages", "identifiers_redacted": "explicit_DNI_only", "inference_calls": 0}
    private_json(output, receipt)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    baseline = json.loads((args.folder / "baseline.private.json").read_text())
    outputs = args.folder / "texts"
    outputs.mkdir(mode=0o700, exist_ok=True)
    def extract(document):
        result = extract_pdf(Path(document["local_path"]), document["sha256"], outputs / f"{document['sha256']}.json")
        print(json.dumps({"document_id": document["id"], "pages": result["page_count"],
            "ocr_pages": result["ocr_pages"], "review_pages": result["review_pages"]}), flush=True)
        return result
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        results = list(executor.map(extract, baseline["documents"]))
    compact = {"documents": len(results), "pages": sum(r["page_count"] for r in results),
        "ocr_pages": sum(r["ocr_pages"] for r in results), "review_pages": sum(len(r["review_pages"]) for r in results),
        "text_characters": sum(len(r["full_text"]) for r in results), "model_calls": 0}
    private_json(args.folder / "text-preflight.private.json", compact)
    print(json.dumps(compact), flush=True)


if __name__ == "__main__":
    main()
