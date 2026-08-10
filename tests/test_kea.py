import json
from dataclasses import replace

from app.services.kea import KeaProvider
from app.settings import settings
from app.models import Artifact, ConfigRevision, ProvisioningProfile
from app.services.profiles import build_candidate, match_expression


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


def test_sonic_profile_uses_option_67():
    provider = KeaProvider(settings)
    current = ConfigRevision(content=json.dumps(provider.default_config()))
    artifact = Artifact(id=9, original_name="ztp.json", stored_name="x.json", size=1, sha256="0" * 64)
    profile = ProvisioningProfile(id=2, name="sonic", stage="sonic", artifact_id=9, match_option=77, match_operator="equals", match_value="SONiC-ZTP")
    revision = build_candidate(current, [profile], {9: artifact}, settings, provider)
    option = json.loads(revision.content)["Dhcp4"]["client-classes"][0]["option-data"][0]
    assert option["name"] == "boot-file-name"
    assert option["data"].endswith("/files/9/ztp.json")


def test_match_expression_supports_configurable_option_60_and_77():
    option60 = ProvisioningProfile(match_option=60, match_operator="starts_with", match_value="vendor_custom")
    assert match_expression(option60) == "substring(option[60].text,0,13) == 'vendor_custom'"
    option77 = ProvisioningProfile(match_option=77, match_operator="equals", match_value="SONiC-ZTP")
    assert match_expression(option77) == "option[77].hex == 0x09534f4e69432d5a5450"
