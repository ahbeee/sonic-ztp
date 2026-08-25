# SONiC ZTP ISC DHCP Web UI

Dependency-free Python 3.9 web UI for the existing ISC DHCP and Nginx services on `10.101.113.253`.

Initial functions:

- display, validate, save, and apply an ISC `dhcpd.conf` candidate;
- back up and automatically restore the live configuration if restart fails;
- start and stop `isc-dhcp-server.service` through a restricted sudo rule;
- display ISC DHCP leases;
- upload and delete artifacts served by the existing Nginx document root.
- edit the DHCPv4 subnet, pool, gateway, DNS, and lease times through structured fields;
- add and remove static DHCP reservations through a managed candidate block.
- create ONIE profiles that return the selected installer with DHCP option 114;
- create Enterprise SONiC profiles that return a generated `ztp.json` with
  DHCP option 67;
- combine up to two option 60, 61, or 77 match conditions per profile;
- generate firmware, config DB, and provisioning-script ZTP sections;
- prevent deletion of artifacts referenced by provisioning profiles.
- display DHCP options 60, 61, and 77 alongside each lease;
- decode ISC `uid` values for option 61 and capture option 77 on future lease
  commit/renew events.

Structured changes update and validate the candidate only. Select **Validate,
apply and restart** separately to change the live DHCP service.

The UI listens on port 8080 and has no authentication. Use it only on a trusted lab network.

## Service

The deployed unit runs as `poc113@lab.edge-core.com`, listens on port 8080,
and is enabled at boot. Uploaded files are available through Nginx under
`http://10.101.113.253/ztp/files/`.

```bash
sudo systemctl status sonic-ztp-web.service
sudo journalctl -u sonic-ztp-web.service -n 100 --no-pager
```

Run the dependency-free tests on the Debian server:

```bash
cd /home/poc113@lab.edge-core.com/sonic-ztp-isc
python3 -m unittest discover -v
```
