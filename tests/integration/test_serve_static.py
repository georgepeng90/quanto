import socket
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from quanto.api.app import create_app
from quanto.cli import main


def test_spa_fallback_does_not_shadow_api(tmp_path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html>工作台</html>", encoding="utf-8")
    (frontend / "app.js").write_text("window.workbench = true", encoding="utf-8")
    client = TestClient(create_app(tmp_path / "workspace", frontend), base_url="http://127.0.0.1:8765")
    for path in ("/", "/runs/example", "/experiments", "/models", "/models/example"):
        response = client.get(path)
        assert response.status_code == 200
        assert "工作台" in response.text
        assert "no-cache" in response.headers["cache-control"]
    assert client.get("/app.js").status_code == 200
    assert client.get("/missing.js").status_code == 404
    assert client.get("/api/runs").status_code == 401
    token = client.get("/api/session").json()["token"]
    for path in ("/api", "/api/missing"):
        response = client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"
    assert client.get("/", headers={"Host": "evil.test"}).status_code == 403
    assert client.get("/app.js", headers={"Origin": "http://evil.test"}).status_code == 403
    assert client.get("/../session.token").status_code == 404


def test_serve_rejects_missing_build(tmp_path, monkeypatch, capsys):
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)
    assert main(["serve"]) == 2
    assert "npm.cmd --prefix web run build" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


def test_serve_rejects_occupied_port(tmp_path, monkeypatch, capsys):
    from quanto import cli
    monkeypatch.setattr(cli, "validate_frontend", lambda *args: None)
    monkeypatch.chdir(tmp_path)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        assert main(["serve", "--port", str(occupied.getsockname()[1])]) == 2
    assert "PORT_IN_USE" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


def test_builtin_templates_are_registered_once(tmp_path):
    app = create_app(tmp_path, templates_dir=Path("strategies"))
    first = app.state.service.list_resource("strategies").items
    assert {item["manifest"]["name"] for item in first} == {"buy_hold", "trend_rotation"}
    again = create_app(tmp_path, templates_dir=Path("strategies"))
    assert [item["id"] for item in again.state.service.list_resource("strategies").items] == [item["id"] for item in first]


def test_frontend_freshness_checks_sources_and_outputs(tmp_path):
    import pytest
    from quanto.workbench.frontend import frontend_sources, validate_frontend
    web = tmp_path / "web"
    dist = web / "dist"
    dist.mkdir(parents=True)
    (web / "src").mkdir()
    source = web / "src" / "main.ts"
    source.write_text("original")
    index = dist / "index.html"
    index.write_text("<html>original</html>")
    manifest = {"sources": frontend_sources(web), "outputs": {"index.html": hashlib.sha256(index.read_bytes()).hexdigest()}}
    (dist / "build-manifest.json").write_text(json.dumps(manifest))
    validate_frontend(tmp_path, dist)
    source.write_text("changed")
    with pytest.raises(ValueError, match="FRONTEND_BUILD_INVALID"):
        validate_frontend(tmp_path, dist)
    source.write_text("original")
    index.write_text("corrupt")
    with pytest.raises(ValueError, match="FRONTEND_BUILD_INVALID"):
        validate_frontend(tmp_path, dist)


def test_workspace_records_default_and_explicit_artifact_location(tmp_path):
    from quanto.workbench.store import WorkbenchStore
    default = WorkbenchStore(tmp_path / "data" / "workbench")
    assert default.artifact_root == tmp_path / "outputs" / "workbench"
    config = json.loads((default.root / "workspace.json").read_text())
    assert Path(config["artifact_root"]) == default.artifact_root
    custom = WorkbenchStore(tmp_path / "custom", artifact_root=tmp_path / "reports")
    assert WorkbenchStore(custom.root).artifact_root == custom.artifact_root
