"""PWA 灵感回流 + 定时任务测试。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app import create_app

@pytest.fixture()
def client(tmp_path: Path):
    with TestClient(create_app(tmp_path / "ws")) as c:
        yield c

def test_pwa_static_mounted(client: TestClient):
    r = client.get("/pwa/index.html")
    assert r.status_code == 200
    assert "灵感速记" in r.text

def test_pwa_manifest_and_sw(client: TestClient):
    assert client.get("/pwa/manifest.webmanifest").status_code == 200
    assert client.get("/pwa/sw.js").status_code == 200

def test_inspiration_endpoint_appends(client: TestClient):
    book = client.post("/api/books", json={"title": "灵感"}).json()["dir"]
    r = client.post(
        f"/api/books/{book}/inspiration",
        json={"notes": [{"text": "矿洞里的诵经声其实是阵眼", "kind": "foreshadow", "ts": 1761600000000}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 1
    # 落盘到 materials/inspiration.md
    import subprocess
    path = Path(r.json()["path"])
    assert "materials/inspiration.md" in path.as_posix()

def test_inspiration_empty_rejected(client: TestClient):
    book = client.post("/api/books", json={"title": "空灵感"}).json()["dir"]
    assert client.post(f"/api/books/{book}/inspiration", json={"notes": []}).status_code == 400
    assert client.post(f"/api/books/{book}/inspiration", json={"notes": [{"text": "  "}]}).status_code == 400

def test_scheduler_disabled_by_default(client: TestClient):
    info = client.get("/api/jobs/scheduler").json()
    assert info.get("available") is True
    assert info.get("enabled") is False
