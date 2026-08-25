# SONiC ZTP Server

A web-managed provisioning server for installing a NOS through ONIE and provisioning Broadcom Enterprise SONiC. This repository contains implementations for both Kea DHCP and ISC DHCP.

This project is intended for an isolated provisioning network. The web interface does not currently provide authentication; do not expose it directly to an untrusted network.

## DHCP implementations

| Implementation | Location | Intended deployment | Runtime |
| --- | --- | --- | --- |
| Kea DHCP (primary) | Repository root | Ubuntu 24.04 with `kea-dhcp4-server` | FastAPI/Uvicorn |
| ISC DHCP | [`isc-dhcp/`](isc-dhcp/) | Debian with an existing `isc-dhcp-server` and Nginx | Python standard library |

The implementations are independently deployable and must not manage the same
provisioning network at the same time. They share the same ONIE and Enterprise
SONiC workflow, but their configuration formats, validation commands, service
names, and runtime databases remain separate. See
[`isc-dhcp/README.md`](isc-dhcp/README.md) for the ISC-specific deployment.

## Features

- Configure a Kea DHCPv4 subnet, dynamic pool, gateway, DNS servers, and lease time from the web UI.
- Add static DHCP reservations by client MAC address.
- Validate a candidate with `kea-dhcp4 -t` before atomically applying it.
- Start and stop only the managed `kea-dhcp4-server.service` from the web UI.
- Upload NOS images, `config_db.json` files, and provisioning scripts with comments and SHA-256 checksums.
- Keep duplicate filenames as separate immutable artifact versions.
- Delete an artifact only when no provisioning profile references it.
- Match clients with DHCP option 60, 61, and/or 77 conditions.
- Send ONIE an installer URL with DHCP option 114.
- Send Enterprise SONiC a profile-specific `ztp.json` URL with DHCP option 67.
- Generate optional `01-firmware`, `02-configdb-json`, and `03-provisioning-script` sections.
- Display active Kea leases and recent audit events.

## Provisioning flow

### ONIE NOS installation

1. ONIE requests an address from Kea.
2. A matching ONIE profile returns DHCP option 114 containing the selected NOS image URL.
3. ONIE downloads and installs the image.

### Enterprise SONiC ZTP

1. Enterprise SONiC requests DHCP configuration, normally with option 77 set to `SONiC-ZTP` and a platform-specific option 61 client identifier.
2. A matching SONiC profile returns DHCP option 67 containing a generated `ztp.json` URL.
3. SONiC processes the selected firmware, config DB, and provisioning-script sections in numerical order.

Client identifiers vary by platform. Verify the actual DHCP packet before defining production match rules.

## Kea implementation requirements

- Ubuntu 24.04 (the current tested deployment)
- Python 3.10 or newer (Python 3.12 on Ubuntu 24.04 is tested)
- Kea DHCPv4 server
- A dedicated provisioning interface or isolated VLAN
- Root access for initial Kea and systemd setup

## Ubuntu installation

Install system packages:

```bash
sudo apt update
sudo apt install -y git python3-venv kea-dhcp4-server
```

Clone and install the application:

```bash
git clone https://github.com/ahbeee/sonic-ztp.git ~/sonic-ztp-server
cd ~/sonic-ztp-server
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
```

For a development run:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Then open `http://<server-ip>:8080/`.

## Service deployment

The files in `deploy/` currently describe the lab deployment used during development:

- account and install directory: `ahbee` / `/home/ahbee/sonic-ztp-server`
- provisioning interface: `enp0s8`
- provisioning address: `192.168.56.200`
- HTTP port: `8080`

Change those values in the service, setup script, and sudoers file before deploying under another account or network. After reviewing the files:

```bash
cd ~/sonic-ztp-server
sudo mkdir -p /etc/kea/sonic-ztp
sudo sh deploy/configure-kea.sh
sudo systemctl enable --now sonic-ztp-server.service
```

The setup script preserves the original Kea configuration as `/etc/kea/kea-dhcp4.conf.pre-sonic-ztp` and installs a narrowly scoped sudoers rule for Kea service control.

## Web workflow

1. Upload the required files on **Artifacts** and add comments that identify their version or purpose.
2. Create mutually exclusive ONIE or Enterprise SONiC profiles on **Profiles**.
3. Configure the DHCP scope and optional static reservations on **DHCP**.
4. Select **Apply candidate**. The live Kea configuration changes only if semantic checks and `kea-dhcp4 -t` both succeed.
5. Start DHCP from the web UI.
6. Boot the target device and monitor leases and its console.

Saving a scope, reservation, raw JSON candidate, or profile creates a new internal candidate. It does not alter the running Kea service until Apply succeeds.

## Configuration

The application reads these environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ZTP_BASE_DIR` | repository directory | Application data root |
| `ZTP_DATA_DIR` | `$ZTP_BASE_DIR/data` | SQLite and runtime data |
| `ZTP_ARTIFACT_DIR` | `$ZTP_BASE_DIR/artifacts` | Uploaded files |
| `ZTP_DATABASE_URL` | SQLite in the data directory | Database connection |
| `ZTP_PROVISION_INTERFACE` | `enp0s8` | Kea listening interface |
| `ZTP_PROVISION_ADDRESS` | `192.168.56.200` | Provisioning server address |
| `ZTP_PUBLIC_BASE_URL` | `http://192.168.56.200:8080` | URL embedded in DHCP and ZTP responses |
| `ZTP_KEA_BINARY` | `/usr/sbin/kea-dhcp4` | Kea executable |
| `ZTP_KEA_SERVICE` | `kea-dhcp4-server` | Managed systemd service |
| `ZTP_KEA_CONFIG` | `/etc/kea/kea-dhcp4.conf` | Live Kea configuration |
| `ZTP_KEA_STAGING_DIR` | `/etc/kea/sonic-ztp` | Atomic candidate staging directory |
| `ZTP_KEA_LEASE_FILE` | `/var/lib/kea/kea-leases4.csv` | Lease database CSV |
| `ZTP_ALLOW_SERVICE_CONTROL` | `false` | Permit web start/stop controls |
| `ZTP_MAX_UPLOAD_BYTES` | `8589934592` | Maximum artifact size |

## Data and repository policy

Runtime state is deliberately excluded from Git:

- `artifacts/` contains uploaded NOS images, JSON files, and scripts.
- `data/` contains the SQLite database and generated runtime files.
- `isc-dhcp/data/` contains the ISC implementation's SQLite database and
  candidate/backup configuration files.
- `ZTP.pdf` is vendor documentation and is not distributed in this repository.
- the earlier `aeon-ztps` source tree is not part of this implementation.

Only `.gitkeep` placeholders are tracked for the runtime directories. Back up `data/` and `artifacts/` together if the server state must be preserved.

## Development checks

```bash
. .venv/bin/activate
pytest
python -m ruff check app tests --select F401,F841,F821,F822,F823
```

The application tests use an isolated `tests/.runtime/` directory and do not modify production data.
