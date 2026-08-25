#!/usr/bin/env python3
import cgi
import html
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.parse
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(os.getenv("ZTP_BASE_DIR", Path(__file__).resolve().parent))
DATA_DIR = Path(os.getenv("ZTP_DATA_DIR", BASE_DIR / "data"))
DHCP_CONFIG = Path(os.getenv("ZTP_DHCP_CONFIG", "/etc/dhcp/dhcpd.conf"))
DHCP_LEASES = Path(os.getenv("ZTP_DHCP_LEASES", "/var/lib/dhcp/dhcpd.leases"))
ARTIFACT_DIR = Path(os.getenv("ZTP_ARTIFACT_DIR", "/var/www/html/ztp/files"))
PUBLIC_BASE_URL = os.getenv("ZTP_PUBLIC_BASE_URL", "http://10.101.113.253/ztp/files")
GENERATED_DIR = Path(os.getenv("ZTP_GENERATED_DIR", "/var/www/html/ztp/generated"))
GENERATED_BASE_URL = os.getenv("ZTP_GENERATED_BASE_URL", "http://10.101.113.253/ztp/generated")
LISTEN_ADDRESS = os.getenv("ZTP_LISTEN_ADDRESS", "0.0.0.0")
LISTEN_PORT = int(os.getenv("ZTP_LISTEN_PORT", "8080"))
MAX_UPLOAD_BYTES = int(os.getenv("ZTP_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024 * 1024)))
DB_PATH = DATA_DIR / "ztp.db"
CANDIDATE_PATH = DATA_DIR / "dhcpd-candidate.conf"
DHCPD = "/usr/sbin/dhcpd"
SYSTEMCTL = "/usr/bin/systemctl"
SERVICE = "isc-dhcp-server.service"

STYLE = """
:root{color-scheme:dark;--bg:#08111b;--panel:#111d29;--line:#26394b;--text:#e8f1f8;--muted:#9bb0c2;--blue:#4ea1ff;--green:#55d68b;--red:#ff6b6b}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px system-ui,sans-serif}nav{padding:16px 3%;border-bottom:1px solid var(--line);display:flex;gap:22px}nav a,a{color:var(--blue);text-decoration:none}main{max-width:1200px;margin:28px auto;padding:0 20px}.hero,.actions{display:flex;align-items:center;justify-content:space-between;gap:16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px}article{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:22px;margin:18px 0;overflow-x:auto}h1,h2{margin-top:0}.badge{padding:7px 12px;border-radius:99px;background:#26394b}.ok{background:#153d2a;color:#87e7ae}.bad{background:#492326;color:#ffaaaa}button,.button{border:0;border-radius:8px;padding:10px 15px;background:#24415f;color:white;cursor:pointer}.danger{background:#6a292d}.primary{background:#176fc1}form.inline{display:inline}textarea,input,select{width:100%;background:#07111a;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:10px}textarea{min-height:520px;font:13px ui-monospace,monospace}label{display:block;margin:12px 0 6px;color:var(--muted)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line);vertical-align:top}.muted{color:var(--muted)}pre{white-space:pre-wrap;background:#07111a;padding:14px;border-radius:8px;overflow:auto}.flash{border-left:4px solid var(--blue)}.flash.error{border-color:var(--red)}code{overflow-wrap:anywhere}@media(max-width:700px){.hero{display:block}.actions{align-items:flex-start;flex-direction:column}}
"""


def db():
    connection = sqlite3.connect(str(DB_PATH))
    connection.row_factory = sqlite3.Row
    return connection


def initialize():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    if not CANDIDATE_PATH.exists() and DHCP_CONFIG.exists():
        shutil.copyfile(str(DHCP_CONFIG), str(CANDIDATE_PATH))
    with db() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL UNIQUE, size INTEGER NOT NULL,
            comment TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, hostname TEXT NOT NULL,
            mac TEXT NOT NULL UNIQUE, ip_address TEXT NOT NULL UNIQUE,
            comment TEXT NOT NULL DEFAULT '')""")
        connection.execute("""CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            stage TEXT NOT NULL, option1 INTEGER NOT NULL, operator1 TEXT NOT NULL,
            value1 TEXT NOT NULL, option2 INTEGER, operator2 TEXT, value2 TEXT,
            installer_artifact_id INTEGER, firmware_artifact_id INTEGER,
            config_artifact_id INTEGER, script_artifact_id INTEGER,
            comment TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1)""")


def service_state():
    result = subprocess.run(["sudo", "-n", SYSTEMCTL, "is-active", SERVICE], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=8)
    return result.stdout.strip() or "unknown"


def service_action(action):
    if action not in {"start", "stop", "restart"}:
        return False, "Unsupported service action"
    result = subprocess.run(["sudo", "-n", SYSTEMCTL, action, SERVICE], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
    detail = result.stdout.strip() or ("DHCP service is " + service_state())
    return result.returncode == 0, detail


def validate_config(content):
    handle = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
    try:
        handle.write(content)
        handle.close()
        result = subprocess.run([DHCPD, "-t", "-cf", handle.name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20)
        return result.returncode == 0, result.stdout.strip() or "Configuration validation passed"
    finally:
        Path(handle.name).unlink(missing_ok=True)


def parse_scope(content):
    subnet = re.search(r"subnet\s+([0-9.]+)\s+netmask\s+([0-9.]+)\s*\{", content)
    pool = re.search(r"range\s+([0-9.]+)\s+([0-9.]+)\s*;", content)
    gateway = re.search(r"(?m)^[ \t]*option\s+routers\s+([^;]+);", content)
    dns = re.search(r"(?m)^[ \t]*option\s+domain-name-servers\s+([^;]+);", content)
    default_lease = re.search(r"default-lease-time\s+(\d+)\s*;", content)
    max_lease = re.search(r"max-lease-time\s+(\d+)\s*;", content)
    if not subnet or not pool:
        return None
    network = ipaddress.ip_network("{}/{}".format(subnet.group(1), subnet.group(2)), strict=False)
    return {"subnet": str(network), "pool_start": pool.group(1), "pool_end": pool.group(2), "gateway": gateway.group(1).strip() if gateway else "", "dns": dns.group(1).strip() if dns else "", "default_lease": default_lease.group(1) if default_lease else "600", "max_lease": max_lease.group(1) if max_lease else "7200"}


def replace_first(pattern, replacement, content, label):
    updated, count = re.subn(pattern, replacement, content, count=1)
    if count != 1:
        raise ValueError("Unable to locate " + label + " in candidate")
    return updated


def update_scope(content, values):
    network = ipaddress.ip_network(values["subnet"], strict=False)
    start = ipaddress.ip_address(values["pool_start"])
    end = ipaddress.ip_address(values["pool_end"])
    if network.version != 4 or start not in network or end not in network or start > end:
        raise ValueError("Pool must be an ordered IPv4 range inside the subnet")
    server_ip = ipaddress.ip_address("10.101.113.253")
    if start <= server_ip <= end:
        raise ValueError("Pool cannot include the ZTP server address 10.101.113.253")
    gateway = values["gateway"].strip()
    if gateway and ipaddress.ip_address(gateway) not in network:
        raise ValueError("Gateway must be inside the DHCP subnet")
    dns_items = [item.strip() for item in values["dns"].split(",") if item.strip()]
    for item in dns_items: ipaddress.ip_address(item)
    default_lease = int(values["default_lease"])
    max_lease = int(values["max_lease"])
    if not 60 <= default_lease <= max_lease <= 604800:
        raise ValueError("Lease times must satisfy 60 <= default <= max <= 604800")
    content = replace_first(r"subnet\s+[0-9.]+\s+netmask\s+[0-9.]+\s*\{", "subnet {} netmask {} {{".format(network.network_address, network.netmask), content, "subnet")
    content = replace_first(r"range\s+[0-9.]+\s+[0-9.]+\s*;", "range {} {};".format(start, end), content, "pool")
    content = replace_first(r"default-lease-time\s+\d+\s*;", "default-lease-time {};".format(default_lease), content, "default lease time")
    content = replace_first(r"max-lease-time\s+\d+\s*;", "max-lease-time {};".format(max_lease), content, "maximum lease time")
    router_line = "option routers {};".format(gateway) if gateway else "# option routers intentionally omitted;"
    content = replace_first(r"(?m)^[ \t]*(?:option\s+routers\s+[^;]+;|# option routers intentionally omitted;)", "  " + router_line, content, "router option")
    dns_line = "option domain-name-servers {};".format(", ".join(dns_items)) if dns_items else "# option domain-name-servers intentionally omitted;"
    content = replace_first(r"(?m)^[ \t]*(?:option\s+domain-name-servers\s+[^;]+;|# option domain-name-servers intentionally omitted;)", "  " + dns_line, content, "DNS option")
    return content


def reservation_block(reservations):
    lines = ["### BEGIN SONIC-ZTP MANAGED RESERVATIONS ###"]
    for item in reservations:
        lines.extend(["host {} {{".format(item["hostname"]), "  hardware ethernet {};".format(item["mac"]), "  fixed-address {};".format(item["ip_address"]), "}"])
    lines.append("### END SONIC-ZTP MANAGED RESERVATIONS ###")
    return "\n".join(lines)


def update_reservations(content, reservations):
    block = reservation_block(reservations)
    pattern = r"\n?### BEGIN SONIC-ZTP MANAGED RESERVATIONS ###.*?### END SONIC-ZTP MANAGED RESERVATIONS ###\n?"
    if re.search(pattern, content, re.S):
        return re.sub(pattern, "\n" + block + "\n", content, count=1, flags=re.S)
    return content.rstrip() + "\n\n" + block + "\n"


OPTION_NAMES = {60: "vendor-class-identifier", 61: "dhcp-client-identifier", 77: "user-class"}


def match_condition(option, operator, value):
    option = int(option)
    if option not in OPTION_NAMES or operator not in {"equals", "starts_with"}:
        raise ValueError("Unsupported DHCP match condition")
    if not re.fullmatch(r"[A-Za-z0-9_.:/+#-]{1,255}", value):
        raise ValueError("Match value contains unsupported characters")
    source = "option " + OPTION_NAMES[option]
    if operator == "equals":
        return '{} = "{}"'.format(source, value)
    return 'substring({}, 0, {}) = "{}"'.format(source, len(value.encode("utf-8")), value)


def profile_expression(profile):
    conditions = [match_condition(profile["option1"], profile["operator1"], profile["value1"])]
    if profile["option2"] and profile["value2"]:
        conditions.append(match_condition(profile["option2"], profile["operator2"], profile["value2"]))
    return " and ".join(conditions)


def artifact_url(item):
    return PUBLIC_BASE_URL.rstrip("/") + "/" + urllib.parse.quote(item["stored_name"])


def write_generated_ztp(profile, artifacts):
    sections = {}
    mapping = {item["id"]: item for item in artifacts}
    if profile["firmware_artifact_id"] in mapping:
        sections["01-firmware"] = {"install": {"url": artifact_url(mapping[profile["firmware_artifact_id"]]), "set-default": True}, "reboot-on-success": True}
    if profile["config_artifact_id"] in mapping:
        sections["02-configdb-json"] = {"url": {"source": artifact_url(mapping[profile["config_artifact_id"]]), "destination": "/etc/sonic/config_db.json"}}
    if profile["script_artifact_id"] in mapping:
        sections["03-provisioning-script"] = {"plugin": {"url": artifact_url(mapping[profile["script_artifact_id"]])}}
    target = GENERATED_DIR / ("profile-{}.json".format(profile["id"]))
    target.write_text(json.dumps({"ztp": sections}, indent=2) + "\n", encoding="utf-8")
    return GENERATED_BASE_URL.rstrip("/") + "/" + target.name


def profile_block(profiles, artifacts):
    mapping = {item["id"]: item for item in artifacts}
    lines = ["### BEGIN SONIC-ZTP MANAGED PROFILES ###"]
    if any(item["stage"] == "onie" and item["enabled"] for item in profiles):
        lines.append("option default-url code 114 = text;")
    for item in profiles:
        if not item["enabled"]: continue
        lines.extend(['class "sonic-ztp-profile-{}" {{'.format(item["id"]), "  match if {};".format(profile_expression(item))])
        if item["stage"] == "onie":
            artifact = mapping.get(item["installer_artifact_id"])
            if not artifact: raise ValueError("ONIE profile references a missing installer")
            lines.append('  option default-url "{}";'.format(artifact_url(artifact)))
        else:
            lines.append('  filename "{}/profile-{}.json";'.format(GENERATED_BASE_URL.rstrip("/"), item["id"]))
        lines.append("}")
    lines.append("### END SONIC-ZTP MANAGED PROFILES ###")
    return "\n".join(lines)


def update_profiles(content, profiles, artifacts):
    block = profile_block(profiles, artifacts)
    pattern = r"\n?### BEGIN SONIC-ZTP MANAGED PROFILES ###.*?### END SONIC-ZTP MANAGED PROFILES ###\n?"
    if re.search(pattern, content, re.S):
        return re.sub(pattern, "\n" + block + "\n", content, count=1, flags=re.S)
    return content.rstrip() + "\n\n" + block + "\n"


def apply_candidate():
    content = CANDIDATE_PATH.read_text(encoding="utf-8")
    valid, output = validate_config(content)
    if not valid:
        return False, output
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = DATA_DIR / ("dhcpd.conf." + timestamp + ".bak")
    original = DHCP_CONFIG.read_text(encoding="utf-8")
    backup.write_text(original, encoding="utf-8")
    try:
        DHCP_CONFIG.write_text(content, encoding="utf-8")
        success, detail = service_action("restart")
        if not success:
            DHCP_CONFIG.write_text(original, encoding="utf-8")
            service_action("restart")
            return False, "Restart failed; previous configuration restored. " + detail
        return True, "Validated, applied, and restarted ISC DHCP. Backup: " + str(backup)
    except Exception:
        DHCP_CONFIG.write_text(original, encoding="utf-8")
        raise


def parse_leases():
    if not DHCP_LEASES.exists():
        return []
    latest = {}
    for address, block in re.findall(r"lease\s+([0-9.]+)\s*\{(.*?)\n\}", DHCP_LEASES.read_text(errors="replace"), re.S):
        def field(pattern):
            match = re.search(pattern, block)
            return match.group(1) if match else ""
        uid = field(r'uid\s+"((?:\\.|[^"])*)"')
        latest[address] = {"address": address, "state": field(r"binding state\s+([^;]+)"), "mac": field(r"hardware ethernet\s+([^;]+)"), "starts": field(r"starts\s+[^ ]+\s+([^;]+)"), "ends": field(r"ends\s+[^ ]+\s+([^;]+)"), "vendor": decode_isc_value(field(r'set vendor-class-identifier\s*=\s*"((?:\\.|[^"])*)"')), "option61": decode_isc_value(uid)}
    return sorted(latest.values(), key=lambda item: tuple(int(x) for x in item["address"].split(".")))


def decode_isc_value(value):
    if not value:
        return ""
    output = bytearray()
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 3 < len(value) and value[index + 1:index + 4].isdigit():
            output.append(int(value[index + 1:index + 4], 8)); index += 4
        elif value[index] == "\\" and index + 1 < len(value):
            output.extend(value[index + 1].encode("latin-1", errors="replace")); index += 2
        else:
            output.extend(value[index].encode("latin-1", errors="replace")); index += 1
    data = bytes(output)
    if data and all(32 <= byte <= 126 for byte in data):
        return data.decode("ascii")
    if len(data) > 1 and data[0] in (0, 1) and all(32 <= byte <= 126 for byte in data[1:]):
        return "0x{:02x} + {}".format(data[0], data[1:].decode("ascii"))
    return "0x" + data.hex()


def page(title, body, message="", error=False):
    flash = ""
    if message:
        flash = '<article class="flash{}">{}</article>'.format(" error" if error else "", html.escape(message))
    return """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{0} · SONiC ZTP</title><style>{1}</style></head><body><nav><strong>SONiC ZTP · ISC DHCP</strong><a href="/">Dashboard</a><a href="/dhcp">DHCP</a><a href="/profiles">Profiles</a><a href="/artifacts">Artifacts</a></nav><main>{2}{3}</main></body></html>""".format(html.escape(title), STYLE, flash, body).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "SonicZtpIsc/0.1"

    def send_page(self, title, body, message="", error=False, status=200):
        payload = page(title, body, message, error)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(payload)

    def redirect(self, target, message="", error=False):
        query = urllib.parse.urlencode({"message": message, "error": "1" if error else "0"}) if message else ""
        self.send_response(303)
        self.send_header("Location", target + (("?" + query) if query else ""))
        self.end_headers()

    def query_message(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        return query.get("message", [""])[0], query.get("error", ["0"])[0] == "1"

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        message, error = self.query_message()
        if path == "/health":
            payload = json.dumps({"status": "ok", "dhcp": service_state()}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload); return
        if path == "/":
            state = service_state()
            leases = parse_leases()
            with db() as connection: count = connection.execute("SELECT count(*) FROM artifacts").fetchone()[0]
            body = '<section class="hero"><div><h1>Provisioning dashboard</h1><p class="muted">10.101.113.253 · eth0 · ISC DHCP</p></div><span class="badge {}">{}</span></section><section class="grid"><article><h2>DHCP leases</h2><p>{} recorded addresses</p><a href="/dhcp">Manage DHCP →</a></article><article><h2>Artifacts</h2><p>{} stored files</p><a href="/artifacts">Manage artifacts →</a></article></section><article><h2>Safety</h2><p>Candidate changes are validated with <code>dhcpd -t</code>. Apply backs up the live file and restores it if DHCP cannot restart.</p></article>'.format("ok" if state == "active" else "bad", html.escape(state), len(leases), count)
            self.send_page("Dashboard", body, message, error); return
        if path == "/dhcp":
            candidate = CANDIDATE_PATH.read_text(encoding="utf-8") if CANDIDATE_PATH.exists() else ""
            rows = "".join("<tr><td>{address}</td><td>{mac}</td><td>{state}</td><td>{ends}</td><td>{vendor}</td><td>{option61}</td></tr>".format(**{k: html.escape(v) for k,v in item.items()}) for item in parse_leases())
            state = service_state()
            scope = parse_scope(candidate)
            with db() as connection: reservations = connection.execute("SELECT * FROM reservations ORDER BY id").fetchall()
            scope_form = ""
            if scope:
                safe = {key: html.escape(value) for key, value in scope.items()}
                scope_form = '''<article><h2>DHCPv4 scope</h2><p class="muted">Save updates and validates the candidate only. Apply it separately below.</p><form method="post" action="/dhcp/scope"><div class="grid"><div><label>Subnet (CIDR)</label><input name="subnet" value="{subnet}" required><label>Pool start</label><input name="pool_start" value="{pool_start}" required><label>Pool end</label><input name="pool_end" value="{pool_end}" required></div><div><label>Gateway</label><input name="gateway" value="{gateway}"><label>DNS servers (comma separated)</label><input name="dns" value="{dns}"><label>Default lease time</label><input type="number" name="default_lease" value="{default_lease}" required><label>Maximum lease time</label><input type="number" name="max_lease" value="{max_lease}" required></div></div><p><button>Save scope to candidate</button></p></form></article>'''.format(**safe)
            reservation_rows = "".join('<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td><form class="inline" method="post" action="/dhcp/reservations/{}/delete"><button class="danger">Delete</button></form></td></tr>'.format(html.escape(item["hostname"]), html.escape(item["mac"]), html.escape(item["ip_address"]), html.escape(item["comment"]), item["id"]) for item in reservations)
            reservation_form = '''<article><h2>Static IP reservations</h2><form method="post" action="/dhcp/reservations"><div class="grid"><div><label>Hostname</label><input name="hostname" placeholder="leaf-01" required><label>MAC address</label><input name="mac" placeholder="52:54:00:12:34:56" required></div><div><label>IPv4 address</label><input name="ip_address" placeholder="10.101.113.100" required><label>Comment</label><input name="comment"></div></div><p><button>Add to candidate</button></p></form><table><thead><tr><th>Hostname</th><th>MAC</th><th>IP</th><th>Comment</th><th></th></tr></thead><tbody>{}</tbody></table></article>'''.format(reservation_rows)
            body = '<section class="hero"><div><h1>ISC DHCP</h1><p class="muted">Live file: {}</p></div><span class="badge {}">{}</span></section>{}{}<article><div class="actions"><div><form class="inline" method="post" action="/dhcp/start"><button>Start</button></form> <form class="inline" method="post" action="/dhcp/stop"><button class="danger">Stop</button></form></div></div><form method="post" action="/dhcp/candidate"><label>Advanced: complete candidate dhcpd.conf</label><textarea name="content" spellcheck="false">{}</textarea><p><button>Save candidate</button> <button class="primary" formaction="/dhcp/apply">Validate, apply and restart</button></p></form></article><article><h2>Leases</h2><p class="muted">Option 61 is read from the ISC uid field.</p><table><thead><tr><th>Address</th><th>MAC</th><th>State</th><th>Ends</th><th>Option 60<br>Vendor Class</th><th>Option 61<br>Client ID</th></tr></thead><tbody>{}</tbody></table></article>'.format(html.escape(str(DHCP_CONFIG)), "ok" if state == "active" else "bad", html.escape(state), scope_form, reservation_form, html.escape(candidate), rows)
            self.send_page("DHCP", body, message, error); return
        if path == "/profiles":
            with db() as connection:
                profiles = connection.execute("SELECT * FROM profiles ORDER BY id DESC").fetchall()
                artifacts = connection.execute("SELECT * FROM artifacts ORDER BY id DESC").fetchall()
            artifact_options = '<option value="">None</option>' + "".join('<option value="{}">#{} · {}{}</option>'.format(item["id"], item["id"], html.escape(item["original_name"]), " — " + html.escape(item["comment"][:80]) if item["comment"] else "") for item in artifacts)
            profile_rows = "".join('<tr><td>{}</td><td>{}</td><td><code>{}</code></td><td>{}</td><td><form class="inline" method="post" action="/profiles/{}/delete" onsubmit="return confirm(\'Delete this profile?\')"><button class="danger">Delete</button></form></td></tr>'.format(html.escape(item["name"]), html.escape(item["stage"].upper()), html.escape(profile_expression(item)), '<a href="{}/profile-{}.json">ztp.json</a>'.format(GENERATED_BASE_URL.rstrip("/"), item["id"]) if item["stage"] == "sonic" else "Option 114", item["id"]) for item in profiles)
            body = '''<h1>Provisioning profiles</h1><article><form method="post" action="/profiles"><label>Profile name</label><input name="name" maxlength="120" required><label>Stage</label><select name="stage" id="profile-stage"><option value="onie">ONIE NOS installation</option><option value="sonic">Enterprise SONiC ZTP</option></select><h3>Client matches</h3><p class="muted">Both populated conditions must match. SONiC normally uses option 61 starts with SONiC## and option 77 equals SONiC-ZTP.</p><div class="grid"><div><label>Condition 1 option</label><select name="option1"><option value="60">60 · Vendor Class</option><option value="61">61 · Client Identifier</option><option value="77">77 · User Class</option></select><label>Operator</label><select name="operator1"><option value="starts_with">Starts with</option><option value="equals">Equals</option></select><label>Value</label><input name="value1" value="onie_vendor" required></div><div><label>Condition 2 option (optional)</label><select name="option2"><option value="">None</option><option value="60">60 · Vendor Class</option><option value="61">61 · Client Identifier</option><option value="77">77 · User Class</option></select><label>Operator</label><select name="operator2"><option value="equals">Equals</option><option value="starts_with">Starts with</option></select><label>Value</label><input name="value2"></div></div><h3>Artifacts</h3><div class="grid"><div><label>ONIE NOS installer</label><select name="installer_artifact_id">{options}</select><label>01-firmware</label><select name="firmware_artifact_id">{options}</select></div><div><label>02-configdb-json</label><select name="config_artifact_id">{options}</select><label>03-provisioning-script</label><select name="script_artifact_id">{options}</select></div></div><label>Comment</label><input name="comment" maxlength="2000"><p><button class="primary">Create profile and candidate</button></p></form></article><article><h2>Configured profiles</h2><table><thead><tr><th>Name</th><th>Stage</th><th>Match</th><th>Response</th><th></th></tr></thead><tbody>{rows}</tbody></table></article><script>document.querySelector('#profile-stage').addEventListener('change',function(){{const sonic=this.value==='sonic',f=this.form;f.option1.value=sonic?'61':'60';f.operator1.value='starts_with';f.value1.value=sonic?'SONiC##':'onie_vendor';f.option2.value=sonic?'77':'';f.operator2.value='equals';f.value2.value=sonic?'SONiC-ZTP':'';}});</script>'''.format(options=artifact_options, rows=profile_rows)
            self.send_page("Profiles", body, message, error); return
        if path == "/artifacts":
            with db() as connection: items = connection.execute("SELECT * FROM artifacts ORDER BY id DESC").fetchall()
            rows = "".join('<tr><td><a href="{}/{}">{}</a></td><td>{}</td><td>{}</td><td><form class="inline" method="post" action="/artifacts/{}/delete" onsubmit="return confirm(\'Delete this file?\')"><button class="danger">Delete</button></form></td></tr>'.format(PUBLIC_BASE_URL.rstrip("/"), urllib.parse.quote(item["stored_name"]), html.escape(item["original_name"]), item["size"], html.escape(item["comment"]), item["id"]) for item in items)
            body = '<h1>Artifacts</h1><article><form method="post" action="/artifacts" enctype="multipart/form-data"><label>File</label><input type="file" name="file" required><label>Comment</label><input name="comment" maxlength="2000"><p><button class="primary">Upload</button></p></form></article><article><table><thead><tr><th>Name</th><th>Bytes</th><th>Comment</th><th></th></tr></thead><tbody>{}</tbody></table></article>'.format(rows)
            self.send_page("Artifacts", body, message, error); return
        self.send_error(404)

    def form(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_UPLOAD_BYTES + 1024 * 1024: raise ValueError("Request is too large")
        return urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/dhcp/scope":
                form = self.form()
                values = {key: form.get(key, [""])[0] for key in ("subnet", "pool_start", "pool_end", "gateway", "dns", "default_lease", "max_lease")}
                content = update_scope(CANDIDATE_PATH.read_text(encoding="utf-8"), values)
                valid, output = validate_config(content)
                if not valid: self.redirect("/dhcp", output, True); return
                CANDIDATE_PATH.write_text(content, encoding="utf-8")
                self.redirect("/dhcp", "Scope saved to candidate; Apply is still required"); return
            if path == "/dhcp/reservations":
                form = self.form()
                hostname = form.get("hostname", [""])[0].strip().lower()
                mac = form.get("mac", [""])[0].strip().lower()
                address = form.get("ip_address", [""])[0].strip()
                comment = form.get("comment", [""])[0].strip()[:2000]
                if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname): raise ValueError("Invalid hostname")
                if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac): raise ValueError("Invalid MAC address")
                ip = ipaddress.ip_address(address)
                scope = parse_scope(CANDIDATE_PATH.read_text(encoding="utf-8"))
                if not scope or ip not in ipaddress.ip_network(scope["subnet"]): raise ValueError("Static IP must be inside the DHCP subnet")
                if str(ip) == "10.101.113.253": raise ValueError("Static IP cannot be the ZTP server address")
                with db() as connection:
                    current = [dict(item) for item in connection.execute("SELECT * FROM reservations ORDER BY id")]
                    prospective = current + [{"hostname": hostname, "mac": mac, "ip_address": str(ip), "comment": comment}]
                    content = update_reservations(CANDIDATE_PATH.read_text(encoding="utf-8"), prospective)
                    valid, output = validate_config(content)
                    if not valid: self.redirect("/dhcp", output, True); return
                    try: connection.execute("INSERT INTO reservations(hostname,mac,ip_address,comment) VALUES(?,?,?,?)", (hostname, mac, str(ip), comment))
                    except sqlite3.IntegrityError: raise ValueError("MAC address or static IP already exists")
                    CANDIDATE_PATH.write_text(content, encoding="utf-8")
                self.redirect("/dhcp", "Static reservation added to candidate; Apply is still required"); return
            reservation_match = re.fullmatch(r"/dhcp/reservations/(\d+)/delete", path)
            if reservation_match:
                reservation_id = int(reservation_match.group(1))
                with db() as connection:
                    item = connection.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
                    if not item: raise ValueError("Reservation not found")
                    remaining = [dict(row) for row in connection.execute("SELECT * FROM reservations WHERE id<>? ORDER BY id", (reservation_id,))]
                    content = update_reservations(CANDIDATE_PATH.read_text(encoding="utf-8"), remaining)
                    valid, output = validate_config(content)
                    if not valid: self.redirect("/dhcp", output, True); return
                    connection.execute("DELETE FROM reservations WHERE id=?", (reservation_id,))
                    CANDIDATE_PATH.write_text(content, encoding="utf-8")
                self.redirect("/dhcp", "Static reservation removed from candidate; Apply is still required"); return
            if path == "/dhcp/candidate":
                content = self.form().get("content", [""])[0]
                valid, output = validate_config(content)
                if not valid: self.redirect("/dhcp", output, True); return
                CANDIDATE_PATH.write_text(content, encoding="utf-8")
                self.redirect("/dhcp", output); return
            if path == "/dhcp/apply":
                content = self.form().get("content", [""])[0]
                valid, output = validate_config(content)
                if not valid: self.redirect("/dhcp", output, True); return
                CANDIDATE_PATH.write_text(content, encoding="utf-8")
                success, output = apply_candidate(); self.redirect("/dhcp", output, not success); return
            if path in {"/dhcp/start", "/dhcp/stop"}:
                success, output = service_action(path.rsplit("/", 1)[-1]); self.redirect("/dhcp", output, not success); return
            if path == "/profiles":
                form = self.form()
                def optional_id(name):
                    value = form.get(name, [""])[0]
                    return int(value) if value else None
                name = form.get("name", [""])[0].strip()[:120]
                stage = form.get("stage", [""])[0]
                if not name or stage not in {"onie", "sonic"}: raise ValueError("Invalid profile name or stage")
                option1 = int(form.get("option1", ["0"])[0]); operator1 = form.get("operator1", [""])[0]; value1 = form.get("value1", [""])[0].strip()
                option2 = optional_id("option2"); operator2 = form.get("operator2", [""])[0] if option2 else None; value2 = form.get("value2", [""])[0].strip() if option2 else None
                match_condition(option1, operator1, value1)
                if option2: match_condition(option2, operator2, value2)
                installer = optional_id("installer_artifact_id"); firmware = optional_id("firmware_artifact_id"); config = optional_id("config_artifact_id"); script = optional_id("script_artifact_id")
                if stage == "onie" and not installer: raise ValueError("ONIE profile requires a NOS installer")
                if stage == "sonic" and not any((firmware, config, script)): raise ValueError("SONiC profile requires at least one ZTP section artifact")
                with db() as connection:
                    cursor = connection.execute("""INSERT INTO profiles(name,stage,option1,operator1,value1,option2,operator2,value2,installer_artifact_id,firmware_artifact_id,config_artifact_id,script_artifact_id,comment) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (name, stage, option1, operator1, value1, option2, operator2, value2, installer, firmware, config, script, form.get("comment", [""])[0].strip()[:2000]))
                    profile_id = cursor.lastrowid
                    profiles = connection.execute("SELECT * FROM profiles ORDER BY id").fetchall()
                    artifacts = connection.execute("SELECT * FROM artifacts ORDER BY id").fetchall()
                    content = update_profiles(CANDIDATE_PATH.read_text(encoding="utf-8"), profiles, artifacts)
                    valid, output = validate_config(content)
                    if not valid: raise ValueError(output)
                    profile = connection.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
                    if stage == "sonic": write_generated_ztp(profile, artifacts)
                    CANDIDATE_PATH.write_text(content, encoding="utf-8")
                self.redirect("/profiles", "Profile created; Apply the DHCP candidate to activate it"); return
            profile_delete = re.fullmatch(r"/profiles/(\d+)/delete", path)
            if profile_delete:
                profile_id = int(profile_delete.group(1))
                with db() as connection:
                    item = connection.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
                    if not item: raise ValueError("Profile not found")
                    connection.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
                    profiles = connection.execute("SELECT * FROM profiles ORDER BY id").fetchall()
                    artifacts = connection.execute("SELECT * FROM artifacts ORDER BY id").fetchall()
                    content = update_profiles(CANDIDATE_PATH.read_text(encoding="utf-8"), profiles, artifacts)
                    valid, output = validate_config(content)
                    if not valid: raise ValueError(output)
                    CANDIDATE_PATH.write_text(content, encoding="utf-8")
                (GENERATED_DIR / "profile-{}.json".format(profile_id)).unlink(missing_ok=True)
                self.redirect("/profiles", "Profile deleted from candidate; Apply is still required"); return
            if path == "/artifacts":
                form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD":"POST", "CONTENT_TYPE":self.headers.get("Content-Type", ""), "CONTENT_LENGTH":self.headers.get("Content-Length", "0")})
                upload = form["file"]
                original = Path(upload.filename or "upload.bin").name
                suffix = Path(original).suffix[:20]
                stored = uuid.uuid4().hex + suffix
                target = ARTIFACT_DIR / stored
                size = 0
                with target.open("xb") as output:
                    while True:
                        chunk = upload.file.read(1024 * 1024)
                        if not chunk: break
                        size += len(chunk)
                        if size > MAX_UPLOAD_BYTES: raise ValueError("Uploaded file is too large")
                        output.write(chunk)
                comment = form.getfirst("comment", "")[:2000]
                with db() as connection: connection.execute("INSERT INTO artifacts(original_name,stored_name,size,comment,created_at) VALUES(?,?,?,?,?)", (original, stored, size, comment, datetime.now(timezone.utc).isoformat()))
                self.redirect("/artifacts", "Uploaded " + original); return
            match = re.fullmatch(r"/artifacts/(\d+)/delete", path)
            if match:
                with db() as connection:
                    item = connection.execute("SELECT * FROM artifacts WHERE id=?", (int(match.group(1)),)).fetchone()
                    if item:
                        used = connection.execute("SELECT name FROM profiles WHERE installer_artifact_id=? OR firmware_artifact_id=? OR config_artifact_id=? OR script_artifact_id=?", (item["id"], item["id"], item["id"], item["id"])).fetchone()
                        if used: raise ValueError("Artifact is used by profile " + used["name"])
                        (ARTIFACT_DIR / item["stored_name"]).unlink(missing_ok=True); connection.execute("DELETE FROM artifacts WHERE id=?", (item["id"],))
                self.redirect("/artifacts", "Artifact deleted"); return
            self.send_error(404)
        except Exception as exc:
            self.send_page("Error", "<h1>Operation failed</h1><pre>{}</pre>".format(html.escape(str(exc))), status=500)

    def log_message(self, fmt, *args):
        print("{} {}".format(self.address_string(), fmt % args), flush=True)


if __name__ == "__main__":
    initialize()
    print("SONiC ZTP ISC UI listening on {}:{}".format(LISTEN_ADDRESS, LISTEN_PORT), flush=True)
    ThreadingHTTPServer((LISTEN_ADDRESS, LISTEN_PORT), Handler).serve_forever()
