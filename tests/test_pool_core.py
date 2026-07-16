from fastapi.testclient import TestClient

from mcg.api.app import create_app
from mcg.config import AppConfig


def test_models_endpoint_and_ui_load(tmp_path):
    cfg = AppConfig()
    cfg.gateway.api_keys = ["sk-test"]
    cfg.gateway.admin_password = "admin"
    cfg.gateway.data_dir = str(tmp_path)
    app = create_app(config=cfg)
    c = TestClient(app)

    health = c.get("/health")
    assert health.status_code == 200
    assert health.json()["models"] >= 1

    models = c.get("/v1/models", headers={"Authorization": "Bearer sk-test"})
    assert models.status_code == 200
    ids = [m["id"] for m in models.json()["data"]]
    assert "m365-copilot" in ids

    ui = c.get("/ui")
    assert ui.status_code == 200
    assert "账号" in ui.text
    assert "/v1/models" in ui.text
