# SONiC ZTP ISC DHCP Web UI

Dependency-free Python 3.9 web UI for the existing ISC DHCP and Nginx services on `10.101.113.253`.

Initial functions:

- display, validate, save, and apply an ISC `dhcpd.conf` candidate;
- back up and automatically restore the live configuration if restart fails;
- start and stop `isc-dhcp-server.service` through a restricted sudo rule;
- display ISC DHCP leases;
- upload and delete artifacts served by the existing Nginx document root.

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
