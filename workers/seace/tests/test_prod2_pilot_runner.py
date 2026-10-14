"""Paid derivative reuse must precede conversion and provider reservation."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("prod2_pilot_runner", Path(__file__).resolve().parents[3] / "scripts/run_prod2_pilot.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def paid_derivative(folder, content=b"%PDF-1.7\nverified derivative"):
    target = folder / "converted.pdf"
    target.write_bytes(content)
    bundle = {"original_sha256": "a" * 64, "analysis_sha256": hashlib.sha256(content).hexdigest()}
    (folder / "gemini_extraction-example.response.private.json").write_text(json.dumps(bundle))
    return target


def test_paid_derivative_is_pinned_without_reconversion(tmp_path, monkeypatch):
    target = paid_derivative(tmp_path)
    monkeypatch.setattr(runner, "prepare_pdf", lambda *args, **kwargs: pytest.fail("Reconversion would risk a second paid input"))
    assert runner.analysis_pdf(tmp_path,tmp_path/"original.rar","rar","a"*64)==(target,None)
    assert runner.analysis_pdf(tmp_path,tmp_path/"original.rar","rar","a"*64)==(target.resolve(),None)


def test_missing_paid_derivative_blocks_reconversion(tmp_path, monkeypatch):
    paid_derivative(tmp_path).unlink()
    monkeypatch.setattr(runner, "prepare_pdf", lambda *args, **kwargs: pytest.fail("Lost paid bytes must require reconciliation"))
    with pytest.raises(RuntimeError, match="Paid derivative unavailable"):
        runner.analysis_pdf(tmp_path,tmp_path/"original.rar","rar","a"*64)


def test_existing_large_paid_pdf_is_reused_without_new_extraction(tmp_path, monkeypatch):
    target=paid_derivative(tmp_path,b"%PDF-1.7\n" + b"0" * (20 * 1024 * 1024))
    monkeypatch.setattr(runner,"prepare_pdf",lambda *args,**kwargs:pytest.fail("Paid direct PDF must be reused"))
    assert runner.analysis_pdf(tmp_path,tmp_path/"original.pdf","pdf","a"*64)==(target,None)


def test_pinned_derivative_tampering_is_rejected(tmp_path):
    target=paid_derivative(tmp_path)
    runner.analysis_pdf(tmp_path,tmp_path/"original.rar","rar","a"*64)
    target.write_bytes(b"%PDF-different")
    with pytest.raises(RuntimeError,match="bytes changed"):
        runner.analysis_pdf(tmp_path,tmp_path/"original.rar","rar","a"*64)


def test_pinned_derivative_cannot_escape_candidate_folder(tmp_path):
    runner.private_write(tmp_path/"analysis-pdf.private.json",{
        "relative_path":"../outside.pdf","original_sha256":"a"*64,"analysis_sha256":"b"*64,"manifest":None})
    with pytest.raises(RuntimeError,match="bind the original"):
        runner.analysis_pdf(tmp_path,tmp_path/"original.rar","rar","a"*64)
