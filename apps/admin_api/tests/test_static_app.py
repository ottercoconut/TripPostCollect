from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from apps.admin_api.app.main import create_app


def test_production_app_serves_spa_and_preserves_api_404(monkeypatch, tmp_path: Path) -> None:
    static_dir = tmp_path / "dist"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<html><body>admin-spa</body></html>", encoding="utf-8")
    (assets_dir / "app.js").write_text("window.adminReady = true;", encoding="utf-8")
    monkeypatch.setenv("TRIPPOST_ADMIN_STATIC_DIR", str(static_dir))

    client = TestClient(create_app())

    assert client.get("/").text == "<html><body>admin-spa</body></html>"
    assert client.get("/records/123").text == "<html><body>admin-spa</body></html>"
    assert client.get("/assets/app.js").text == "window.adminReady = true;"
    assert client.get("/api/not-found").status_code == 404
