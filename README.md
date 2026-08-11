# SONiC ZTP Server

A modern, web-managed provisioning service for ONIE and Enterprise SONiC.

The current milestone provides:

- FastAPI health and JSON APIs
- a server-rendered dashboard
- Kea DHCP status, draft configuration, binary validation, revision history, restore, and lease visibility
- GUI provisioning profiles that generate Kea client classes for ONIE option 114 and SONiC ZTP option 67
- Multiple AND-combined client matches using DHCP option 60, 61, or 77
- Per-profile Enterprise SONiC ztp.json generation with optional firmware, config_db, and provisioning-script sections
- safe artifact upload, editable comments, SHA-256 calculation, and HTTP download
- SQLite persistence and audit events
- systemd deployment for Ubuntu

DHCP service control and scope activation are deliberately disabled until the
provisioning subnet is approved.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Open `http://192.168.56.200:8080`.

Useful endpoints:

- `/health` — application and Kea status
- `/api/dhcp/config` — current candidate revision
- `/api/dhcp/leases` — active Kea leases
- `/docs` — interactive OpenAPI documentation

## Safety defaults

- Kea binds only to the configured provisioning interface (`enp0s8`).
- The generated initial Kea configuration contains no subnet or pool.
- Starting/stopping Kea from the web application is disabled by default.
- Uploaded artifacts are stored outside Git and addressed by a generated ID.
- Reusing an original filename creates a separate artifact; older uploads are never overwritten.
- Provisioning references use immutable `/files/{artifact-id}/{filename}` URLs, so duplicate original names remain unambiguous.
- Kea candidate files use `/etc/kea/sonic-ztp`, which works with Ubuntu's default AppArmor profile without weakening it.
- Multiple active profiles may route distinct device identities to distinct generated ztp.json documents.
- Profile changes create validated candidate revisions only; applying configuration and controlling DHCP remain locked.
