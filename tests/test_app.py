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
        response = client.post("/artifacts", data={"comment": "lab candidate"}, files={"file": ("ztp.json", io.BytesIO(b'{"ztp": {}}'), "application/json")}, follow_redirects=False)
        assert response.status_code == 303
        page = client.get("/artifacts")
        assert "ztp.json" in page.text
        assert "lab candidate" in page.text
        artifact_id = int(page.text.split("Artifact #", 1)[1].split(" ", 1)[0])
        served = client.get(f"/files/{artifact_id}/ztp.json")
        assert served.status_code == 200
        assert served.content == b'{"ztp": {}}'
        assert client.get(f"/files/{artifact_id}/wrong-name.json").status_code == 404


def test_same_artifact_filename_creates_distinct_records():
    with TestClient(app) as client:
        for payload in (b"build-one", b"build-two"):
            response = client.post("/artifacts", files={"file": ("sonic-vs.bin", io.BytesIO(payload), "application/octet-stream")}, follow_redirects=False)
            assert response.status_code == 303
        page = client.get("/artifacts")
        assert page.text.count("sonic-vs.bin") >= 2


def test_create_onie_profile_generates_versioned_candidate():
    with TestClient(app) as client:
        client.post("/artifacts", files={"file": ("onie-installer.bin", io.BytesIO(b"image"), "application/octet-stream")})
        page = client.get("/artifacts")
        artifact_id = int(page.text.split("Artifact #", 1)[1].split(" ", 1)[0])
        profile_name = f"onie-test-{artifact_id}"
        response = client.post("/profiles", data={"name": profile_name, "stage": "onie", "artifact_id": artifact_id, "match_option": 60, "match_operator": "starts_with", "match_value": "custom_onie", "comment": "test"}, follow_redirects=False)
        assert response.status_code == 303
        config = client.get("/api/dhcp/config").json()["content"]["Dhcp4"]
        generated = config["client-classes"][-1]
        assert generated["option-data"][0]["code"] == 114
        assert "custom_onie" in generated["test"]
        assert f"/files/{artifact_id}/onie-installer.bin" in generated["option-data"][0]["data"]
        assert profile_name in client.get("/profiles").text


def test_only_one_profile_per_stage_remains_enabled():
    with TestClient(app) as client:
        client.post("/artifacts", files={"file": ("new.bin", io.BytesIO(b"new"), "application/octet-stream")})
        artifact_id = int(client.get("/artifacts").text.split("Artifact #", 1)[1].split(" ", 1)[0])
        client.post("/profiles", data={"name": f"onie-new-{artifact_id}", "stage": "onie", "artifact_id": artifact_id})
        config = client.get("/api/dhcp/config").json()["content"]["Dhcp4"]
        onie_classes = [item for item in config["client-classes"] if item["option-data"][0].get("code") == 114]
        assert len(onie_classes) == 1
        assert f"/files/{artifact_id}/new.bin" in onie_classes[0]["option-data"][0]["data"]
