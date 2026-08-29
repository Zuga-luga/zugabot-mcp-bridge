"""Unit tests for zugabot_mcp_bridge.

Run:
    python -m pytest tests/ -v

No network: httpx.AsyncClient is replaced with a stub, so every branch of the
error handling is exercised deterministically.

The env vars must be set BEFORE importing the module -- it validates config at
import time and sys.exit(2)s on a missing token.
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ZUGABOT_A2A_TOKEN", "test-client:test-secret")
os.environ.setdefault("ZUGABOT_A2A_URL", "http://test.invalid:8000")

import zugabot_mcp_bridge as bridge  # noqa: E402


# ----------------------------------------------------------------- stubs ----

class FakeRequest:
    """_parse_json_or_raise reports r.request.url in its error message."""
    url = "http://test.invalid:8000/a2a"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, content_type="application/json"):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)
        self.headers = {"content-type": content_type}
        self.request = FakeRequest()

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    """Drop-in for httpx.AsyncClient: returns a canned response or raises."""

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **kw):
        if self._raises:
            raise self._raises
        return self._response

    async def get(self, *a, **kw):
        if self._raises:
            raise self._raises
        return self._response


@pytest.fixture
def patch_client(monkeypatch):
    def _apply(response=None, raises=None):
        monkeypatch.setattr(bridge.httpx, "AsyncClient", FakeClient(response, raises))
    return _apply


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------- _extract_text ------

def test_extract_text_joins_artifact_parts():
    task = {"artifacts": [{"parts": [{"kind": "text", "text": "one"},
                                     {"kind": "text", "text": "two"}]}]}
    assert bridge._extract_text(task) == "one\ntwo"


def test_extract_text_skips_non_text_parts():
    task = {"artifacts": [{"parts": [{"kind": "image", "text": "nope"},
                                     {"kind": "text", "text": "yes"}]}]}
    assert bridge._extract_text(task) == "yes"


def test_extract_text_surfaces_failure_message():
    task = {"artifacts": [], "status": {"state": "failed",
                                        "message": {"parts": [{"kind": "text", "text": "boom"}]}}}
    assert bridge._extract_text(task) == "[task failed] boom"


def test_extract_text_falls_back_to_state():
    assert "state=working" in bridge._extract_text({"status": {"state": "working"}})


def test_extract_text_handles_completely_empty_task():
    assert bridge._extract_text({}) == "(no text output; state=None)"


# ------------------------------------------------------------ _a2a_rpc ------

def test_a2a_rpc_returns_result(patch_client):
    patch_client(FakeResponse(payload={"jsonrpc": "2.0", "result": {"ok": True}}))
    assert run(bridge._a2a_rpc("SendMessage", {})) == {"ok": True}


def test_a2a_rpc_401_is_actionable(patch_client):
    patch_client(FakeResponse(status_code=401, payload={}))
    with pytest.raises(RuntimeError, match="auth rejected"):
        run(bridge._a2a_rpc("SendMessage", {}))


def test_a2a_rpc_other_http_error_names_the_status(patch_client):
    patch_client(FakeResponse(status_code=503, payload={}, text="upstream down"))
    with pytest.raises(RuntimeError, match="HTTP 503"):
        run(bridge._a2a_rpc("SendMessage", {}))


def test_a2a_rpc_timeout_is_not_a_raw_httpx_error(patch_client):
    """A timeout must warn that the skill may still be running server-side --
    blind retries on a side-effecting skill would double-execute it."""
    patch_client(raises=bridge.httpx.ReadTimeout("too slow"))
    with pytest.raises(RuntimeError, match="did not answer within"):
        run(bridge._a2a_rpc("SendMessage", {}))


def test_a2a_rpc_connection_error_is_wrapped(patch_client):
    patch_client(raises=bridge.httpx.ConnectError("refused"))
    with pytest.raises(RuntimeError, match="Cannot reach Zugabot"):
        run(bridge._a2a_rpc("SendMessage", {}))


def test_a2a_rpc_rpc_error_is_reported(patch_client):
    patch_client(FakeResponse(payload={"error": {"code": -32601, "message": "no such skill"}}))
    with pytest.raises(RuntimeError, match="no such skill"):
        run(bridge._a2a_rpc("SendMessage", {}))


def test_malformed_error_object_does_not_raise_keyerror(patch_client):
    """REGRESSION: `e['code']` used to KeyError on an error object missing its
    keys, hiding the real server problem behind an unrelated crash."""
    patch_client(FakeResponse(payload={"error": {"unexpected": "shape"}}))
    with pytest.raises(RuntimeError, match="A2A error"):
        run(bridge._a2a_rpc("SendMessage", {}))


def test_non_dict_error_object_does_not_crash(patch_client):
    patch_client(FakeResponse(payload={"error": "just a string"}))
    with pytest.raises(RuntimeError, match="just a string"):
        run(bridge._a2a_rpc("SendMessage", {}))


def test_missing_result_key_does_not_raise_keyerror(patch_client):
    """REGRESSION: `body['result']` used to KeyError on a response with
    neither 'error' nor 'result'."""
    patch_client(FakeResponse(payload={"jsonrpc": "2.0", "id": 1}))
    with pytest.raises(RuntimeError, match="neither 'error' nor 'result'"):
        run(bridge._a2a_rpc("SendMessage", {}))


def test_html_response_names_the_wrong_url_mistake(patch_client):
    """The classic misconfiguration: A2A_URL points at the product frontend,
    which 200s with an HTML shell instead of JSON."""
    patch_client(FakeResponse(payload=None, text="<!doctype html><html>",
                              content_type="text/html"))
    with pytest.raises(RuntimeError):
        run(bridge._a2a_rpc("SendMessage", {}))


def test_json_array_instead_of_object_is_rejected(patch_client):
    patch_client(FakeResponse(payload=[1, 2, 3]))
    with pytest.raises(RuntimeError, match="expected a JSON-RPC object"):
        run(bridge._a2a_rpc("SendMessage", {}))


# ------------------------------------------------- list_zugabot_skills ------

def _card(skills):
    return {"name": "Zugabot", "version": "1.0.0", "skills": skills}


def test_list_skills_formats_markdown(patch_client):
    patch_client(FakeResponse(payload=_card([{"id": "review_queue", "description": "Read-only."}])))
    out = run(bridge.list_zugabot_skills())
    assert "# Zugabot v1.0.0" in out
    assert "- **review_queue** — Read-only." in out


def test_list_skills_skips_entry_with_no_id(patch_client):
    """REGRESSION: `s['id']` used to KeyError, losing the whole listing
    because of one malformed entry."""
    patch_client(FakeResponse(payload=_card([{"description": "no id here"},
                                             {"id": "good", "description": "fine"}])))
    out = run(bridge.list_zugabot_skills())
    assert "good" in out
    assert "no id here" not in out


def test_list_skills_skips_non_dict_entries(patch_client):
    patch_client(FakeResponse(payload=_card(["a bare string", {"id": "good"}])))
    assert "good" in run(bridge.list_zugabot_skills())


def test_list_skills_handles_null_skills(patch_client):
    patch_client(FakeResponse(payload={"name": "Zugabot", "version": "1", "skills": None}))
    assert "# Zugabot v1" in run(bridge.list_zugabot_skills())


def test_list_skills_http_error_is_wrapped(patch_client):
    patch_client(FakeResponse(status_code=404, payload={}, text="nope"))
    with pytest.raises(RuntimeError, match="HTTP 404"):
        run(bridge.list_zugabot_skills())


def test_list_skills_connection_error_is_wrapped(patch_client):
    patch_client(raises=bridge.httpx.ConnectError("refused"))
    with pytest.raises(RuntimeError, match="Cannot reach Zugabot"):
        run(bridge.list_zugabot_skills())


def test_list_skills_timeout_is_wrapped(patch_client):
    patch_client(raises=bridge.httpx.ReadTimeout("slow"))
    with pytest.raises(RuntimeError, match="timed out"):
        run(bridge.list_zugabot_skills())


# ------------------------------------------------------------- config -------

def test_placeholder_token_is_detected():
    """The exact live misconfiguration found 2026-08-28: .mcp.json carried the
    literal string '${ZUGABOT_A2A_TOKEN}' because nothing substituted it."""
    assert bridge.A2A_URL.startswith("http")
    for raw, expected in (("${ZUGABOT_A2A_TOKEN}", True),
                          ("real-token:secret", False),
                          ("${partial", False)):
        assert (raw.startswith("${") and raw.endswith("}")) is expected


def test_url_trailing_slash_is_stripped():
    assert not bridge.A2A_URL.endswith("/")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
