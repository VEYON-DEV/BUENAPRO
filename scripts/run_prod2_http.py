"""Run a bounded PROD2 keyword search -> native ficha -> all observed documents over HTTP."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workers/seace/src"))
from buenapro_worker.prod2.client import Prod2HttpClient
from buenapro_worker.prod2.documents import download_document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--object", choices=["Bien", "Servicio"], required=True)
    parser.add_argument("--from-date", default="")
    parser.add_argument("--to-date", default="")
    parser.add_argument("--nomenclature", help="Exact procedure on the first search page; otherwise first row")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "manifest.json"
    if manifest_path.exists():
        parser.error("Output manifest already exists; choose a fresh output directory")
    manifest = {"at": datetime.now(timezone.utc).isoformat(), "transport": "httpx",
                "browser_used": False, "db_writes": False, "model_calls": False,
                "query": {"keyword": args.keyword, "year": args.year, "object": args.object,
                          "published_from": args.from_date, "published_to": args.to_date}}
    with httpx.Client(timeout=60, follow_redirects=True, verify=True) as client:
        source = Prod2HttpClient(client)
        search = source.search(args.keyword, year=args.year, object_type=args.object,
                               published_from=args.from_date, published_to=args.to_date)
        manifest["search"] = search
        if not search["rows"]:
            raise SystemExit("No rows received; no detail or document download attempted")
        selected = next((i for i, row in enumerate(search["rows"])
                         if row["nomenclature"] == args.nomenclature), None) if args.nomenclature else 0
        if selected is None:
            raise SystemExit("Requested nomenclature was not found on the first page")
        detail = source.detail(selected)
        manifest["detail"] = detail
        if not detail["document_inventory_complete"]:
            raise SystemExit("Document inventory is paginated or unverified; complete download cannot be claimed")
        downloads = []
        for document in detail["documents"]:
            suffix = Path(document["filename"]).suffix.lower()
            kind = {".pdf": "pdf", ".zip": "zip", ".docx": "zip", ".xlsx": "zip",
                    ".doc": "ole", ".xls": "ole", ".rar": "rar"}.get(suffix)
            if kind is None:
                raise SystemExit("Unsupported observed file extension; stopped before downloading it")
            # Use UUID names: public filenames cannot introduce traversal or overwrite another document.
            target = args.output_dir / (document["uuid"] + suffix)
            result = download_document(client, document["uuid"], target, expected_format=kind)
            downloads.append({**result, "filename": document["filename"], "local_file": target.name})
        manifest["downloads"] = downloads
        manifest["flow_verified"] = bool(downloads) and len(downloads) == len(detail["documents"])
    with os.fdopen(os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        json.dump(manifest, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps({"flow_verified": manifest["flow_verified"], "transport": "httpx",
                      "browser_used": False, "search_counter": search["counter"],
                      "nomenclature": detail["nomenclature"], "schedule_stages": len(detail["schedule"]),
                      "item_states": detail["item_states"], "downloaded_documents": len(downloads),
                      "documents": [{k: d[k] for k in ("filename", "format", "bytes", "sha256")}
                                    | ({"pages": d["pages"]} if "pages" in d else {}) for d in downloads],
                      "manifest": str(manifest_path.resolve())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
