import io

from fastapi.testclient import TestClient

from app.main import app


def test_health():
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["provisioning_interface"] == "enp0s8"


def test_dashboard_and_dhcp_pages():
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/dhcp").status_code == 200
        config = client.get("/api/dhcp/config").json()
        assert config["content"]["Dhcp4"]["subnet4"] == []
        leases = client.get("/api/dhcp/leases").json()
        assert "leases" in leases


def test_restore_revision():
    with TestClient(app) as client:
        revision = client.get("/api/dhcp/config").json()["revision"]
        response = client.post(f"/dhcp/revisions/{revision}/restore", follow_redirects=False)
        assert response.status_code == 303
        restored = client.get("/api/dhcp/config").json()
        assert restored["revision"] > revision
        assert restored["is_valid"] is False


def test_artifact_upload_and_download():
    with TestClient(app) as client:
        response = client.post("/artifacts", files={"file": ("ztp.json", io.BytesIO(b'{"ztp": {}}'), "application/json")}, follow_redirects=False)
        assert response.status_code == 303
        page = client.get("/artifacts")
        assert "ztp.json" in page.text
