import json
from dataclasses import replace

from app.services.kea import KeaProvider
from app.settings import settings
from app.models import Artifact, ConfigRevision, ProfileMatch, ProvisioningProfile
from app.services.profiles import build_candidate, generated_ztp, match_expression


def test_default_config_is_inert_and_bound():
    provider = KeaProvider(settings)
    config = provider.default_config()
    assert config["Dhcp4"]["subnet4"] == []
    assert config["Dhcp4"]["interfaces-config"]["interfaces"] == ["enp0s8"]
    assert provider.semantic_errors(config) == []


def test_rejects_wildcard_interface():
    provider = KeaProvider(settings)
    config = provider.default_config()
    config["Dhcp4"]["interfaces-config"]["interfaces"] = ["*"]
    assert provider.semantic_errors(config)
    normalized, errors = provider.normalize_and_check(json.dumps(config))
    assert normalized
    assert errors


def test_reads_only_active_leases(tmp_path):
    lease_file = tmp_path / "leases.csv"
    lease_file.write_text("address,hwaddr,client_id,valid_lifetime,expire,subnet_id,fqdn_fwd,fqdn_rev,hostname,state,user_context,pool_id\n192.168.56.20,52:54:00:12:34:56,,600,99,1,0,0,onie,0,,0\n192.168.56.21,52:54:00:00:00:01,,600,99,1,0,0,old,1,,0\n")
    provider = KeaProvider(replace(settings, kea_lease_file=lease_file))
    leases = provider.leases()
    assert len(leases) == 1
    assert leases[0]["hostname"] == "onie"


def test_apply_config_replaces_target(tmp_path):
    target = tmp_path / "kea-dhcp4.conf"
    provider = KeaProvider(replace(settings, kea_config_path=target))
    provider.apply_config('{"Dhcp4": {}}')
    assert target.read_text() == '{"Dhcp4": {}}'


def test_service_control_is_disabled_by_default():
    provider = KeaProvider(replace(settings, allow_service_control=False))
    success, output = provider.control_service("start")
    assert success is False
    assert "disabled" in output


def test_sonic_profile_uses_option_67():
    provider = KeaProvider(settings)
    current = ConfigRevision(content=json.dumps(provider.default_config()))
    artifact = Artifact(id=9, original_name="ztp.json", stored_name="x.json", size=1, sha256="0" * 64)
    profile = ProvisioningProfile(id=2, name="sonic", stage="sonic", artifact_id=0, configdb_artifact_id=9)
    matches = [ProfileMatch(id=1, profile_id=2, option_code=77, operator="equals", value="SONiC-ZTP", position=0)]
    revision = build_candidate(current, [profile], matches, {9: artifact}, settings, provider)
    option = json.loads(revision.content)["Dhcp4"]["client-classes"][0]["option-data"][0]
    assert option["name"] == "boot-file-name"
    assert option["data"].endswith("/ztp/2/ztp.json")


def test_match_expression_supports_configurable_option_60_and_77():
    option60 = ProfileMatch(profile_id=1, option_code=60, operator="starts_with", value="vendor_custom", position=0)
    assert match_expression([option60]) == "(substring(option[60].text,0,13) == 'vendor_custom')"
    option61 = ProfileMatch(profile_id=1, option_code=61, operator="starts_with", value="SONiC##", position=0)
    option77 = ProfileMatch(profile_id=1, option_code=77, operator="equals", value="SONiC-ZTP", position=1)
    expression = match_expression([option61, option77])
    assert "option[61]" in expression and "option[77]" in expression and " and " in expression


def test_generates_configdb_only_ztp_json():
    artifact = Artifact(id=9, original_name="config_db.json", stored_name="x.json", size=1, sha256="0" * 64)
    profile = ProvisioningProfile(id=4, name="leaf", stage="sonic", artifact_id=0, configdb_artifact_id=9)
    document = generated_ztp(profile, {9: artifact}, settings)
    assert list(document["ztp"]) == ["02-configdb-json"]
    assert document["ztp"]["02-configdb-json"]["url"]["source"].endswith("/files/9/config_db.json")
