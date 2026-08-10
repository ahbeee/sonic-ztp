import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    base_dir: Path
    data_dir: Path
    artifact_dir: Path
    database_url: str
    provisioning_interface: str
    provisioning_address: str
    public_base_url: str
    kea_binary: str
    kea_service: str
    kea_config_path: Path
    kea_staging_dir: Path
    kea_lease_file: Path
    allow_service_control: bool
    max_upload_bytes: int

    @classmethod
    def from_environment(cls) -> "Settings":
        base_dir = Path(os.getenv("ZTP_BASE_DIR", Path(__file__).resolve().parents[1]))
        data_dir = Path(os.getenv("ZTP_DATA_DIR", base_dir / "data"))
        artifact_dir = Path(os.getenv("ZTP_ARTIFACT_DIR", base_dir / "artifacts"))
        return cls(
            base_dir=base_dir,
            data_dir=data_dir,
            artifact_dir=artifact_dir,
            database_url=os.getenv("ZTP_DATABASE_URL", "sqlite:///{}".format(data_dir / "ztp.db")),
            provisioning_interface=os.getenv("ZTP_PROVISION_INTERFACE", "enp0s8"),
            provisioning_address=os.getenv("ZTP_PROVISION_ADDRESS", "192.168.56.200"),
            public_base_url=os.getenv("ZTP_PUBLIC_BASE_URL", "http://192.168.56.200:8080"),
            kea_binary=os.getenv("ZTP_KEA_BINARY", "/usr/sbin/kea-dhcp4"),
            kea_service=os.getenv("ZTP_KEA_SERVICE", "kea-dhcp4-server"),
            kea_config_path=Path(os.getenv("ZTP_KEA_CONFIG", "/etc/kea/kea-dhcp4.conf")),
            kea_staging_dir=Path(os.getenv("ZTP_KEA_STAGING_DIR", "/etc/kea/sonic-ztp")),
            kea_lease_file=Path(os.getenv("ZTP_KEA_LEASE_FILE", "/var/lib/kea/kea-leases4.csv")),
            allow_service_control=os.getenv("ZTP_ALLOW_SERVICE_CONTROL", "false").lower() == "true",
            max_upload_bytes=int(os.getenv("ZTP_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024 * 1024))),
        )


settings = Settings.from_environment()
