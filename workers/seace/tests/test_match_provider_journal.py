import importlib.util
import json
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("match_provider_journal", ROOT / "scripts/match_provider_journal.py")
journal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journal)
URL = "https://api.typesafe.ai/v1/systemone"


def invoke(folder, post, payload=None):
    return journal.dispatch_once(folder, "jev", payload or {"state": "sample"}, url=URL,
                                 api_key="test-secret", header_name="Authorization", post=post)


def test_success_reuses_saved_response_without_second_call(tmp_path):
    calls = []
    class Response:
        status_code = 200
        text = '{"usage":{"input_tokens":4}}'
    def post(*args, **kwargs):
        calls.append(kwargs)
        assert kwargs["allow_redirects"] is False
        return Response()
    assert invoke(tmp_path, post) == invoke(tmp_path, post)
    assert len(calls) == 1
    assert "test-secret" not in "".join(p.read_text() for p in tmp_path.iterdir())
    with pytest.raises(journal.ProviderBlocked):
        invoke(tmp_path, post, {"state": "changed"})


def test_transport_error_never_retries_or_logs_secret(tmp_path):
    def post(*args, **kwargs):
        raise requests.Timeout("do not log test-secret")
    with pytest.raises(journal.ProviderBlocked):
        invoke(tmp_path, post)
    with pytest.raises(journal.ProviderBlocked):
        invoke(tmp_path, lambda *a, **k: pytest.fail("must not call again"))
    assert "test-secret" not in "".join(p.read_text() for p in tmp_path.iterdir())


def test_http_rejection_is_preserved_without_retry(tmp_path):
    class Response:
        status_code = 429
        text = '{"error":"quota test-secret"}'
    with pytest.raises(journal.ProviderBlocked):
        invoke(tmp_path, lambda *a, **k: Response())
    saved = json.loads((tmp_path/"jev-response.private.json").read_text())
    assert saved["http_status"] == 429
    assert "test-secret" not in json.dumps(saved)
    with pytest.raises(journal.ProviderBlocked):
        invoke(tmp_path, lambda *a, **k: pytest.fail("must not call again"))


def test_endpoint_override_rejected_before_journal_or_network(tmp_path):
    with pytest.raises(ValueError):
        journal.dispatch_once(tmp_path, "jev", {}, url="https://elsewhere.invalid", api_key="secret",
                              header_name="Authorization", post=lambda *a, **k: pytest.fail("no call"))
    assert list(tmp_path.iterdir()) == []
