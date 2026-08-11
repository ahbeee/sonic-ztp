import json
import re
from collections import defaultdict
from typing import Iterable
from urllib.parse import quote

from app.models import Artifact, ConfigRevision, ProfileMatch, ProvisioningProfile
from app.services.kea import KeaProvider
from app.settings import Settings


STAGES = {
    "onie": {"label": "ONIE NOS installation", "response": "DHCP option 114 (default-url)"},
    "sonic": {"label": "Enterprise SONiC ZTP", "response": "DHCP option 67 (generated ztp.json URL)"},
}
MATCH_OPTIONS = {60: "Vendor Class Identifier", 61: "Client Identifier", 77: "User Class"}
MATCH_OPERATORS = {"starts_with": "Starts with", "equals": "Equals"}
SAFE_MATCH_VALUE = re.compile(r"^[A-Za-z0-9_.:/+#\-]{1,255}$")


def artifact_url(settings: Settings, artifact: Artifact) -> str:
    return "{}/files/{}/{}".format(
        settings.public_base_url.rstrip("/"), artifact.id, quote(artifact.original_name, safe="")
    )


def ztp_url(settings: Settings, profile: ProvisioningProfile) -> str:
    return "{}/ztp/{}/ztp.json".format(settings.public_base_url.rstrip("/"), profile.id)


def validate_match(option: int, operator: str, value: str) -> None:
    if option not in MATCH_OPTIONS:
        raise ValueError("Unsupported DHCP match option")
    if operator not in MATCH_OPERATORS:
        raise ValueError("Unsupported match operator")
    if not SAFE_MATCH_VALUE.fullmatch(value):
        raise ValueError("Match value contains unsupported characters")


def match_condition_expression(condition: ProfileMatch) -> str:
    validate_match(condition.option_code, condition.operator, condition.value)
    value_bytes = condition.value.encode("utf-8")
    if condition.option_code == 60:
        if condition.operator == "equals":
            return "option[60].text == '{}'".format(condition.value)
        return "substring(option[60].text,0,{}) == '{}'".format(len(condition.value), condition.value)
    encoded = value_bytes.hex()
    offset = 1 if condition.option_code == 77 else 0
    prefix = "{:02x}".format(len(value_bytes)) if condition.option_code == 77 else ""
    if condition.operator == "equals":
        return "option[{}].hex == 0x{}{}".format(condition.option_code, prefix, encoded)
    return "substring(option[{}].hex,{},{}) == 0x{}".format(
        condition.option_code, offset, len(value_bytes), encoded
    )


def match_expression(conditions: Iterable[ProfileMatch]) -> str:
    expressions = ["({})".format(match_condition_expression(item)) for item in conditions]
    if not expressions:
        raise ValueError("At least one client match is required")
    return " and ".join(expressions)


def match_summary(conditions: Iterable[ProfileMatch]) -> str:
    return " AND ".join(
        "Option {} {} {}".format(item.option_code, MATCH_OPERATORS[item.operator].lower(), item.value)
        for item in conditions
    )


def generated_ztp(profile: ProvisioningProfile, artifacts: dict[int, Artifact], settings: Settings) -> dict:
    sections = {}
    if profile.firmware_artifact_id in artifacts:
        sections["01-firmware"] = {
            "install": {"url": artifact_url(settings, artifacts[profile.firmware_artifact_id]), "set-default": True},
            "reboot-on-success": True,
        }
    if profile.configdb_artifact_id in artifacts:
        sections["02-configdb-json"] = {
            "url": {
                "source": artifact_url(settings, artifacts[profile.configdb_artifact_id]),
                "destination": "/etc/sonic/config_db.json",
            }
        }
    if profile.script_artifact_id in artifacts:
        sections["03-provisioning-script"] = {
            "plugin": {"url": artifact_url(settings, artifacts[profile.script_artifact_id])}
        }
    return {"ztp": sections}


def client_class(
    profile: ProvisioningProfile,
    conditions: list[ProfileMatch],
    artifacts: dict[int, Artifact],
    settings: Settings,
) -> dict:
    test = match_expression(conditions)
    if profile.stage == "onie":
        artifact = artifacts[profile.artifact_id]
        return {
            "name": "ztp-profile-{}".format(profile.id),
            "test": test,
            "option-data": [{"code": 114, "data": artifact_url(settings, artifact)}],
        }
    if profile.stage == "sonic":
        return {
            "name": "ztp-profile-{}".format(profile.id),
            "test": test,
            "option-data": [{"name": "boot-file-name", "data": ztp_url(settings, profile)}],
        }
    raise ValueError("Unsupported provisioning stage")


def group_matches(matches: Iterable[ProfileMatch]) -> dict[int, list[ProfileMatch]]:
    grouped = defaultdict(list)
    for item in sorted(matches, key=lambda row: (row.profile_id, row.position, row.id or 0)):
        grouped[item.profile_id].append(item)
    return dict(grouped)


def build_candidate(
    current: ConfigRevision,
    profiles: Iterable[ProvisioningProfile],
    matches: Iterable[ProfileMatch],
    artifacts: dict[int, Artifact],
    settings: Settings,
    kea: KeaProvider,
) -> ConfigRevision:
    config = json.loads(current.content)
    grouped = group_matches(matches)
    classes = []
    for profile in profiles:
        if profile.enabled is False or not grouped.get(profile.id):
            continue
        if profile.stage == "onie" and profile.artifact_id not in artifacts:
            continue
        classes.append(client_class(profile, grouped[profile.id], artifacts, settings))
    config["Dhcp4"]["client-classes"] = classes
    normalized, semantic_errors = kea.normalize_and_check(json.dumps(config))
    valid, output = (False, "; ".join(semantic_errors)) if semantic_errors else kea.binary_validate(normalized)
    return ConfigRevision(content=normalized, is_valid=valid, validation_output=output)
