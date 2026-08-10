import json
import re
from typing import Iterable
from urllib.parse import quote

from app.models import Artifact, ConfigRevision, ProvisioningProfile
from app.services.kea import KeaProvider
from app.settings import Settings


STAGES = {
    "onie": {
        "label": "ONIE NOS installation",
        "default_option": 60,
        "default_operator": "starts_with",
        "default_value": "onie_vendor",
        "response": "DHCP option 114 (default-url)",
    },
    "sonic": {
        "label": "Enterprise SONiC ZTP",
        "default_option": 77,
        "default_operator": "equals",
        "default_value": "SONiC-ZTP",
        "response": "DHCP option 67 (ztp.json URL)",
    },
}

MATCH_OPTIONS = {60: "Vendor Class Identifier", 77: "User Class"}
MATCH_OPERATORS = {"starts_with": "Starts with", "equals": "Equals"}
SAFE_MATCH_VALUE = re.compile(r"^[A-Za-z0-9_.:/+\-]{1,255}$")


def artifact_url(settings: Settings, artifact: Artifact) -> str:
    filename = quote(artifact.original_name, safe="")
    return "{}/files/{}/{}".format(settings.public_base_url.rstrip("/"), artifact.id, filename)


def validate_match(option: int, operator: str, value: str) -> None:
    if option not in MATCH_OPTIONS:
        raise ValueError("Unsupported DHCP match option")
    if operator not in MATCH_OPERATORS:
        raise ValueError("Unsupported match operator")
    if not SAFE_MATCH_VALUE.fullmatch(value):
        raise ValueError("Match value may contain letters, numbers, dot, underscore, colon, slash, plus, and hyphen")


def match_expression(profile: ProvisioningProfile) -> str:
    validate_match(profile.match_option, profile.match_operator, profile.match_value)
    value = profile.match_value
    if profile.match_option == 60:
        if profile.match_operator == "equals":
            return "option[60].text == '{}'".format(value)
        return "substring(option[60].text,0,{}) == '{}'".format(len(value), value)
    encoded = value.encode("utf-8").hex()
    if profile.match_operator == "equals":
        # DHCP user-class values use a one-byte length prefix.
        return "option[77].hex == 0x{:02x}{}".format(len(value.encode("utf-8")), encoded)
    return "substring(option[77].hex,1,{}) == 0x{}".format(len(value.encode("utf-8")), encoded)


def match_summary(profile: ProvisioningProfile) -> str:
    return "DHCP option {} {} {}".format(
        profile.match_option,
        MATCH_OPERATORS[profile.match_operator].lower(),
        profile.match_value,
    )


def client_class(profile: ProvisioningProfile, artifact: Artifact, settings: Settings) -> dict:
    url = artifact_url(settings, artifact)
    test = match_expression(profile)
    if profile.stage == "onie":
        return {
            "name": "ztp-profile-{}".format(profile.id),
            "test": test,
            "option-data": [{"code": 114, "data": url}],
        }
    if profile.stage == "sonic":
        return {
            "name": "ztp-profile-{}".format(profile.id),
            "test": test,
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
