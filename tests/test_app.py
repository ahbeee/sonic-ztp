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
        assert isinstance(config["content"]["Dhcp4"]["subnet4"], list)
        dhcp_page = client.get("/dhcp")
        assert "Kea DHCP candidate" in dhcp_page.text
        assert 'interfaces-config' in dhcp_page.text
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


def test_multiple_profiles_per_stage_remain_available():
    with TestClient(app) as client:
        client.post("/artifacts", files={"file": ("new.bin", io.BytesIO(b"new"), "application/octet-stream")})
        artifact_id = int(client.get("/artifacts").text.split("Artifact #", 1)[1].split(" ", 1)[0])
        client.post("/profiles", data={"name": f"onie-new-{artifact_id}", "stage": "onie", "artifact_id": artifact_id, "match_option": "60", "match_operator": "equals", "match_value": f"vendor_{artifact_id}"})
        config = client.get("/api/dhcp/config").json()["content"]["Dhcp4"]
        onie_classes = [item for item in config["client-classes"] if item["option-data"][0].get("code") == 114]
        assert any(f"/files/{artifact_id}/new.bin" in item["option-data"][0]["data"] for item in onie_classes)


def test_sonic_profile_generates_configdb_only_json():
    with TestClient(app) as client:
        client.post("/artifacts", files={"file": ("leaf_config_db.json", io.BytesIO(b'{"DEVICE_METADATA": {}}'), "application/json")})
        artifact_id = int(client.get("/artifacts").text.split("Artifact #", 1)[1].split(" ", 1)[0])
        response = client.post("/profiles", data={
            "name": f"sonic-leaf-{artifact_id}", "stage": "sonic", "artifact_id": "0",
            "match_option": ["61", "77"], "match_operator": ["starts_with", "equals"],
            "match_value": ["SONiC##", "SONiC-ZTP"], "configdb_artifact_id": str(artifact_id),
        }, follow_redirects=False)
        assert response.status_code == 303
        config = client.get("/api/dhcp/config").json()["content"]["Dhcp4"]
        generated_class = max(
            (item for item in config["client-classes"] if "/ztp/" in item["option-data"][0]["data"]),
            key=lambda item: int(item["option-data"][0]["data"].split("/ztp/")[1].split("/")[0]),
        )
        profile_id = int(generated_class["option-data"][0]["data"].split("/ztp/")[1].split("/")[0])
        document = client.get(f"/ztp/{profile_id}/ztp.json").json()
        assert list(document["ztp"]) == ["02-configdb-json"]
        assert document["ztp"]["02-configdb-json"]["url"]["source"].endswith(f"/files/{artifact_id}/leaf_config_db.json")


def test_scope_form_generates_subnet_and_pool():
    with TestClient(app) as client:
        response = client.post("/dhcp/scope", data={
            "subnet": "192.168.56.0/24",
            "pool_start": "192.168.56.101",
            "pool_end": "192.168.56.199",
            "gateway": "",
            "dns_servers": "8.8.8.8, 1.1.1.1",
            "lease_time": "600",
        }, follow_redirects=False)
        assert response.status_code == 303
        config = client.get("/api/dhcp/config").json()["content"]["Dhcp4"]
        subnet = config["subnet4"][0]
        assert subnet["subnet"] == "192.168.56.0/24"
        assert subnet["pools"] == [{"pool": "192.168.56.101 - 192.168.56.199"}]
        assert subnet["option-data"][0]["name"] == "domain-name-servers"


def test_scope_rejects_server_address_inside_pool():
    with TestClient(app) as client:
        response = client.post("/dhcp/scope", data={
            "subnet": "192.168.56.0/24", "pool_start": "192.168.56.190",
            "pool_end": "192.168.56.210", "lease_time": "600",
        })
        assert response.status_code == 422
        assert "server address" in response.json()["detail"]
