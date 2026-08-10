import json
from typing import Iterable
from urllib.parse import quote

from app.models import Artifact, ConfigRevision, ProvisioningProfile
from app.services.kea import KeaProvider
from app.settings import Settings


STAGES = {
    "onie": {
        "label": "ONIE NOS installation",
        "match": "DHCP option 60 starts with onie_vendor",
        "response": "DHCP option 114 (default-url)",
    },
    "sonic": {
        "label": "Enterprise SONiC ZTP",
        "match": "DHCP option 77 equals SONiC-ZTP",
        "response": "DHCP option 67 (ztp.json URL)",
    },
}


def artifact_url(settings: Settings, artifact: Artifact) -> str:
    filename = quote(artifact.original_name, safe="")
    return "{}/files/{}/{}".format(settings.public_base_url.rstrip("/"), artifact.id, filename)


def client_class(profile: ProvisioningProfile, artifact: Artifact, settings: Settings) -> dict:
    url = artifact_url(settings, artifact)
    if profile.stage == "onie":
        return {
            "name": "ztp-profile-{}".format(profile.id),
            "test": "substring(option[60].text,0,11) == 'onie_vendor'",
            "option-data": [{"code": 114, "data": url}],
        }
    if profile.stage == "sonic":
        return {
            "name": "ztp-profile-{}".format(profile.id),
            "test": "option[77].hex == 0x09534f4e69432d5a5450",
            "option-data": [{"name": "boot-file-name", "data": url}],
        }
    raise ValueError("Unsupported provisioning stage")


def build_candidate(
    current: ConfigRevision,
    profiles: Iterable[ProvisioningProfile],
    artifacts: dict[int, Artifact],
    settings: Settings,
    kea: KeaProvider,
) -> ConfigRevision:
    config = json.loads(current.content)
    dhcp4 = config["Dhcp4"]
    dhcp4["client-classes"] = [
        client_class(profile, artifacts[profile.artifact_id], settings)
        for profile in profiles
        if profile.enabled is not False
        and profile.artifact_id in artifacts
        and artifacts[profile.artifact_id].enabled is not False
    ]
    normalized, semantic_errors = kea.normalize_and_check(json.dumps(config))
    if semantic_errors:
        valid, output = False, "; ".join(semantic_errors)
    else:
        valid, output = kea.binary_validate(normalized)
    return ConfigRevision(content=normalized, is_valid=valid, validation_output=output)
