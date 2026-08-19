import json
import csv
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.settings import Settings


@dataclass
class KeaStatus:
    installed: bool
    active: bool
    enabled: bool
    service: str
    binary: str


class KeaProvider:
    def __init__(self, app_settings: Settings):
        self.settings = app_settings

    def default_config(self) -> Dict[str, Any]:
        return {
            "Dhcp4": {
                "interfaces-config": {"interfaces": [self.settings.provisioning_interface]},
                "lease-database": {
                    "type": "memfile",
                    "persist": True,
                    "name": "/var/lib/kea/kea-leases4.csv",
                },
                "valid-lifetime": 600,
                "renew-timer": 300,
                "rebind-timer": 480,
                "subnet4": [],
                "loggers": [
                    {
                        "name": "kea-dhcp4",
                        "output_options": [{"output": "syslog"}],
                        "severity": "INFO",
                    }
                ],
            }
        }

    def status(self) -> KeaStatus:
        installed = Path(self.settings.kea_binary).exists() or shutil.which("kea-dhcp4") is not None
        active = self._systemctl_state("is-active") == "active"
        enabled = self._systemctl_state("is-enabled") == "enabled"
        return KeaStatus(installed, active, enabled, self.settings.kea_service, self.settings.kea_binary)

    def control_service(self, action: str) -> Tuple[bool, str]:
        if action not in {"start", "stop", "restart"}:
            return False, "Unsupported Kea service action"
        if not self.settings.allow_service_control:
            return False, "Kea service control is disabled by server policy"
        unit = self.settings.kea_service
        if not unit.endswith(".service"):
            unit += ".service"
        try:
            result = subprocess.run(
                ["sudo", "-n", "/usr/bin/systemctl", action, unit],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return False, "Unable to {} Kea: {}".format(action, exc)
        state = self._systemctl_state("is-active")
        expected = "active" if action in {"start", "restart"} else "inactive"
        success = result.returncode == 0 and state == expected
        detail = result.stdout.strip()
        if success:
            detail = "Kea DHCP service is {}".format(state)
        elif not detail:
            detail = "systemctl returned {} and service state is {}".format(result.returncode, state)
        return success, detail

    def _systemctl_state(self, operation: str) -> str:
        try:
            result = subprocess.run(
                ["systemctl", operation, self.settings.kea_service],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                universal_newlines=True,
                timeout=5,
            )
            return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return "unavailable"

    def normalize_and_check(self, raw_content: str) -> Tuple[str, List[str]]:
        parsed = json.loads(raw_content)
        errors = self.semantic_errors(parsed)
        return json.dumps(parsed, indent=2, sort_keys=False), errors

    def semantic_errors(self, config: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        dhcp4 = config.get("Dhcp4")
        if not isinstance(dhcp4, dict):
            return ["Top-level Dhcp4 object is required"]
        interfaces = dhcp4.get("interfaces-config", {}).get("interfaces", [])
        if self.settings.provisioning_interface not in interfaces:
            errors.append("Kea must bind to provisioning interface {}".format(self.settings.provisioning_interface))
        if any(item in interfaces for item in ("*", "0.0.0.0", self.settings.provisioning_address)):
            errors.append("Wildcard or address-based interface binding is not allowed")
        subnets = dhcp4.get("subnet4", [])
        if not isinstance(subnets, list):
            errors.append("Dhcp4.subnet4 must be a list")
        return errors

    def binary_validate(self, normalized_content: str) -> Tuple[bool, str]:
        if not Path(self.settings.kea_binary).exists():
            return False, "Kea binary is not installed"
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            prefix="kea-candidate-",
            dir=str(self.settings.kea_staging_dir),
            delete=False,
        )
        try:
            handle.write(normalized_content)
            handle.close()
            os.chmod(handle.name, 0o644)
            result = subprocess.run(
                [self.settings.kea_binary, "-t", handle.name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                timeout=15,
            )
            return result.returncode == 0, result.stdout.strip() or "Configuration is valid"
        finally:
            Path(handle.name).unlink(missing_ok=True)

    def apply_config(self, normalized_content: str) -> None:
        target = self.settings.kea_config_path
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            prefix="kea-applied-",
            dir=str(target.parent),
            delete=False,
        )
        try:
            handle.write(normalized_content)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.chmod(handle.name, 0o640)
            os.replace(handle.name, target)
        finally:
            Path(handle.name).unlink(missing_ok=True)

    def leases(self) -> List[Dict[str, str]]:
        path = self.settings.kea_lease_file
        if not path.is_file():
            return []
        with path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        return [row for row in rows if row.get("state", "0") == "0"]
