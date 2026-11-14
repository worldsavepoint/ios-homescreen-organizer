"""Local HTTP isolation regressions; all reads use a generated file fixture."""
import asyncio
import importlib

import pytest
from starlette.testclient import TestClient

from fake_phone import build


@pytest.fixture
def web(tmp_path, monkeypatch):
    path = tmp_path / "phone.plist"
    build(path, as_dict=True)
    monkeypatch.setenv("HOMESCREEN_FIXTURE", str(path))
    monkeypatch.setenv("HOMESCREEN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("HOMESCREEN_UI_TOKEN", "launch-test-secret")
    monkeypatch.delenv("HOMESCREEN_HEALTH_SECRET", raising=False)
    monkeypatch.delenv("HOMESCREEN_READ_ONLY", raising=False)
    import homescreen.config as config
    import homescreen.ui as ui
    importlib.reload(config)
    importlib.reload(ui)
    client = TestClient(ui.app, base_url="http://127.0.0.1:8765")
    return ui, client


@pytest.mark.parametrize("path", ["/api/state", "/api/devices", "/api/backups", "/icon?id=com.fake.app"])
def test_sensitive_gets_require_authentication(web, path):
    ui, client = web
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Token": "wrong"}).status_code == 401


def test_public_assets_never_disclose_launch_token(web):
    ui, client = web
    assert ui.TOKEN == "launch-test-secret"
    for path in ("/", "/static/app.js", "/static/index.html"):
        response = client.get(path)
        assert response.status_code == 200
        assert ui.TOKEN not in response.text
        assert "__TOKEN__" not in response.text
    assert "window.HS_TOKEN" not in client.get("/").text


def test_bootstrap_creates_private_cookie_and_health_requires_header(web):
    ui, client = web
    assert client.get("/v1/health").status_code == 401
    assert client.post("/v1/authenticate").status_code == 401
    h = {"X-Token": ui.TOKEN, "Origin": "http://127.0.0.1:8765"}
    response = client.post("/v1/authenticate", headers=h)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert client.get("/api/state").status_code == 200
    assert client.get("/v1/health").status_code == 401  # cookies cannot impersonate readiness
    assert client.get("/v1/health", headers=h).json() == {"app": "homescreen", "ready": True}
    assert client.post("/api/read", headers={"Origin": "http://127.0.0.1:8765"}).json()["loaded"]
    assert client.post("/api/read").status_code == 403  # cookie writes require same-origin proof


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://localhost:8765", "http://127.0.0.1:9999"])
def test_rejects_foreign_origin_even_with_token(web, origin):
    ui, client = web
    h = {"X-Token": ui.TOKEN, "Origin": origin}
    assert client.get("/api/state", headers=h).status_code == 403
    assert client.post("/api/read", headers=h).status_code == 403


def test_local_host_and_browser_fetch_metadata_are_checked(web):
    ui, client = web
    h = {"X-Token": ui.TOKEN}
    assert client.get("/api/state", headers=h).status_code == 200
    assert client.get("/api/state", headers={**h, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/static/app.js", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/api/state", headers={**h, "Origin": "http://127.0.0.1:8765"}).status_code == 200


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/api/state", "/missing"])
def test_responses_forbid_framing_and_caching_sensitive_content(web, path):
    ui, client = web
    response = client.get(path, headers={"X-Token": ui.TOKEN})
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "script-src 'self'" in csp
    assert "object-src 'none'" in csp and "base-uri 'none'" in csp
    assert response.headers["cache-control"] == "no-store"


def test_icons_are_limited_to_loaded_layout_including_widgets(web, monkeypatch):
    ui, client = web
    h = {"X-Token": ui.TOKEN}
    calls = []
    async def icon(bundle):
        calls.append(bundle)
        return b"fake-png"
    monkeypatch.setattr(ui.session(), "icon", icon)
    assert client.get("/icon?id=com.unknown.app", headers=h).status_code == 404
    assert calls == []
    client.post("/api/read", headers=h)
    lay = ui.session().require()
    widget = next(n for n in lay.nodes.values() if n.kind == "widget")
    widget.raw["elements"][0]["containerBundleIdentifier"] = "com.widget.actual-container"
    for bundle in (next(n.bundle_id for n in lay.nodes.values() if n.kind == "app"), "com.widget.actual-container"):
        response = client.get("/icon", params={"id": bundle}, headers=h)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert client.get("/icon?id=com.unknown.app", headers=h).status_code == 404
    assert "com.unknown.app" not in calls


def test_readonly_is_visible_and_write_routes_reject_before_session(web, monkeypatch):
    ui, client = web
    monkeypatch.setenv("HOMESCREEN_READ_ONLY", "1")
    h = {"X-Token": ui.TOKEN}
    assert client.get("/api/state", headers=h).json()["read_only"] is True
    assert client.post("/api/read", headers=h).json()["read_only"] is True
    async def forbidden(*args):
        pytest.fail("read-only request reached a device write method")
    monkeypatch.setattr(ui.session(), "apply", forbidden)
    monkeypatch.setattr(ui.session(), "restore", forbidden)
    for path in ("/api/apply", "/api/restore"):
        response = client.post(path, json={}, headers=h)
        assert response.status_code == 403 and "read-only" in response.json()["error"]["message"]


def test_preview_widget_size_cannot_inject_attributes(web, tmp_path):
    from homescreen import preview
    ui, client = web
    client.post("/api/read", headers={"X-Token": ui.TOKEN})
    lay = ui.session().require()
    widget = next(n for n in lay.nodes.values() if n.kind == "widget")
    widget.raw["gridSize"] = 'small" onclick="alert(1)'
    out = asyncio.run(preview.render(ui.session(), tmp_path / "preview.html", show_original=False))
    html = out.read_text()
    assert 'onclick="alert(1)' not in html
    assert '<div class="widget w-small" style="grid-row:' in html


@pytest.mark.parametrize("path", ["/api/op", "/api/read", "/api/apply", "/api/device", "/api/restore"])
def test_operations_cannot_run_without_authentication(web, path):
    ui, client = web
    assert client.post(path, json={}).status_code == 401


def test_cli_browser_receives_fragment_but_console_does_not(web, monkeypatch, capsys):
    import sys
    from types import SimpleNamespace
    ui, _ = web
    opened = []
    monkeypatch.setattr(ui.config, "enable_device_switching", lambda: None)
    monkeypatch.setattr(ui.threading, "Timer", lambda delay, fn, args: SimpleNamespace(start=lambda: opened.append(args[0])))
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *a, **kw: None))
    ui.main(port=8765)
    assert opened == ["http://127.0.0.1:8765/#token=launch-test-secret"]
    assert ui.TOKEN not in capsys.readouterr().out


def test_browser_cookie_cannot_authorize_foreign_origin_or_bootstrap(web):
    ui, client = web
    own = {"Origin": "http://127.0.0.1:8765"}
    client.post("/v1/authenticate", headers={**own, "X-Token": ui.TOKEN})
    assert client.get("/api/state").status_code == 200
    foreign = {"Origin": "http://localhost:8888"}
    assert client.get("/api/state", headers=foreign).status_code == 403
    assert client.post("/api/read", headers=foreign).status_code == 403
    assert client.post("/v1/authenticate", headers=own).status_code == 401
    assert client.get("/v1/health", headers=own).status_code == 401


@pytest.fixture
def challenged_web(web, monkeypatch):
    ui, _ = web
    monkeypatch.setenv("HOMESCREEN_HEALTH_SECRET", "health-test-secret")
    importlib.reload(ui)
    return ui, TestClient(ui.app, base_url="http://127.0.0.1:8765")


@pytest.mark.parametrize(("challenge", "proof"), [
    ("a" * 64, "1e77da5d53b28f8a1f18848b771c204e246d699c21c0716b2add7d6be5dd5794"),
    ("b" * 64, "dcb468846dba07818eb34b557b2760f4ef56263159528de19e23f6e16e961d02"),
])
def test_health_proves_backend_identity_without_sending_secrets(challenged_web, challenge, proof):
    ui, client = challenged_web
    response = client.get("/v1/health", headers={"X-Homescreen-Challenge": challenge})
    assert response.status_code == 200
    assert response.json() == {"app": "homescreen", "ready": True, "proof": proof}
    assert response.headers["cache-control"] == "no-store"
    assert ui.TOKEN not in response.text and "health-test-secret" not in response.text
    assert client.get("/api/state", headers={"X-Homescreen-Challenge": challenge}).status_code == 401


@pytest.mark.parametrize("challenge", ["", "a" * 63, "a" * 65, "z" * 64, "a" * 10000])
def test_health_rejects_invalid_or_unbounded_challenges(challenged_web, challenge):
    _, client = challenged_web
    response = client.get("/v1/health", headers={"X-Homescreen-Challenge": challenge})
    assert response.status_code == 400
    assert "proof" not in response.json()
    assert response.headers["cache-control"] == "no-store"


def test_ui_credentials_cannot_bypass_challenge_health_mode(challenged_web):
    ui, client = challenged_web
    own = {"Origin": "http://127.0.0.1:8765", "X-Token": ui.TOKEN}
    assert client.post("/v1/authenticate", headers=own).status_code == 200
    for headers in ({}, own):
        assert client.get("/v1/health", headers=headers).status_code == 400
    assert client.get("/v1/health?challenge=" + "a" * 64, headers=own).status_code == 400
    assert client.get("/v1/health", headers={"X-Homescreen-Challenge": "a" * 64,
        "Origin": "http://evil.example"}).status_code == 403


@pytest.mark.parametrize("path", ["/v1/authenticate", "/api/auth"])
def test_authenticate_keeps_canonical_and_legacy_routes(web, path):
    ui, client = web
    assert client.post(path, headers={"X-Token": ui.TOKEN}).status_code == 200
    assert client.get("/api/state").status_code == 200


@pytest.mark.parametrize("path", ["/v1/health", "/health"])
def test_health_keeps_canonical_and_legacy_routes(challenged_web, path):
    _, client = challenged_web
    response = client.get(path, headers={"X-Homescreen-Challenge": "a" * 64})
    assert response.status_code == 200
    assert response.json()["proof"] == "1e77da5d53b28f8a1f18848b771c204e246d699c21c0716b2add7d6be5dd5794"


def test_authentication_and_permission_errors_use_structured_envelope(web):
    ui, client = web
    response = client.get("/api/state")
    assert response.status_code == 401
    error = response.json()["error"]
    assert set(error) == {"message", "type", "param", "code"}
    assert isinstance(error["message"], str)
    assert error["type"] == "authentication_error" and error["code"] == "invalid_credentials"
    assert error["param"] is None
    response = client.get("/api/state", headers={"X-Token": ui.TOKEN, "Origin": "https://evil.example"})
    assert response.status_code == 403
    error = response.json()["error"]
    assert set(error) == {"message", "type", "param", "code"}
    assert error["type"] == "permission_error" and error["code"] == "invalid_origin"
    assert error["param"] == "origin"


def test_invalid_health_challenge_uses_structured_error(challenged_web):
    _, client = challenged_web
    response = client.get("/v1/health", headers={"X-Homescreen-Challenge": "invalid"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert set(error) == {"message", "type", "param", "code"}
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "invalid_health_challenge" and error["param"] == "challenge"


@pytest.mark.parametrize(("error", "message"), [
    ({"message": "Sign in again", "type": "authentication_error", "param": None, "code": "invalid_credentials"}, "Sign in again"),
    ("Layout cannot be moved", "Layout cannot be moved"),
])
def test_browser_displays_structured_and_legacy_error_messages(web, error, message):
    import json
    import shutil
    import subprocess
    executable = shutil.which("node")
    if not executable:
        pytest.skip("JavaScript runtime unavailable")
    _, client = web
    source = client.get("/static/app.js").text
    harness = r'''
const vm = require('vm');
const data = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const source = data.source.slice(0, data.source.indexOf('\nasync function op('));
vm.runInNewContext(source + "\napi('/api/state').catch(e => console.log(e.message));", {
  URLSearchParams, location: {hash: ''}, console,
  fetch: async () => ({ok: false, status: 401, json: async () => ({error: data.error})}),
});
'''
    result = subprocess.run([executable, "-e", harness], input=json.dumps({"source": source, "error": error}),
                            text=True, capture_output=True, check=True)
    assert result.stdout.strip() == message


def test_browser_bootstrap_calls_versioned_endpoint(web):
    import json
    import shutil
    import subprocess
    executable = shutil.which("node")
    if not executable:
        pytest.skip("JavaScript runtime unavailable")
    _, client = web
    source = client.get("/static/app.js").text
    harness = r'''
const vm = require('vm');
const data = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const source = data.source.slice(0, data.source.indexOf('\nasync function op('));
const calls = [];
vm.runInNewContext(source + "\nauthentication.then(() => console.log(JSON.stringify(calls)));", {
  URLSearchParams, console, calls,
  location: {hash: '#token=secret', pathname: '/', search: ''},
  history: {replaceState: (...args) => calls.push(['history', ...args])},
  fetch: async (path, options) => {calls.push(['fetch', path, options.headers['X-Token']]); return {ok: true};},
});
'''
    result = subprocess.run([executable, "-e", harness], input=json.dumps({"source": source}),
                            text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == [["history", None, "", "/"], ["fetch", "/v1/authenticate", "secret"]]
