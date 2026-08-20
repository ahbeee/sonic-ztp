#!/usr/bin/env python3
import cgi
import html
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
LISTEN_ADDRESS = os.getenv("ZTP_LISTEN_ADDRESS", "0.0.0.0")
LISTEN_PORT = int(os.getenv("ZTP_LISTEN_PORT", "8080"))
MAX_UPLOAD_BYTES = int(os.getenv("ZTP_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024 * 1024)))
DB_PATH = DATA_DIR / "ztp.db"
CANDIDATE_PATH = DATA_DIR / "dhcpd-candidate.conf"
DHCPD = "/usr/sbin/dhcpd"
SYSTEMCTL = "/usr/bin/systemctl"
SERVICE = "isc-dhcp-server.service"

STYLE = """
:root{color-scheme:dark;--bg:#08111b;--panel:#111d29;--line:#26394b;--text:#e8f1f8;--muted:#9bb0c2;--blue:#4ea1ff;--green:#55d68b;--red:#ff6b6b}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px system-ui,sans-serif}nav{padding:16px 3%;border-bottom:1px solid var(--line);display:flex;gap:22px}nav a,a{color:var(--blue);text-decoration:none}main{max-width:1200px;margin:28px auto;padding:0 20px}.hero,.actions{display:flex;align-items:center;justify-content:space-between;gap:16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px}article{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:22px;margin:18px 0}h1,h2{margin-top:0}.badge{padding:7px 12px;border-radius:99px;background:#26394b}.ok{background:#153d2a;color:#87e7ae}.bad{background:#492326;color:#ffaaaa}button,.button{border:0;border-radius:8px;padding:10px 15px;background:#24415f;color:white;cursor:pointer}.danger{background:#6a292d}.primary{background:#176fc1}form.inline{display:inline}textarea,input{width:100%;background:#07111a;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:10px}textarea{min-height:520px;font:13px ui-monospace,monospace}label{display:block;margin:12px 0 6px;color:var(--muted)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line);vertical-align:top}.muted{color:var(--muted)}pre{white-space:pre-wrap;background:#07111a;padding:14px;border-radius:8px;overflow:auto}.flash{border-left:4px solid var(--blue)}.flash.error{border-color:var(--red)}code{overflow-wrap:anywhere}@media(max-width:700px){.hero{display:block}.actions{align-items:flex-start;flex-direction:column}}
"""


def db():
    connection = sqlite3.connect(str(DB_PATH))
    connection.row_factory = sqlite3.Row
    return connection


def initialize():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    if not CANDIDATE_PATH.exists() and DHCP_CONFIG.exists():
        shutil.copyfile(str(DHCP_CONFIG), str(CANDIDATE_PATH))
    with db() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL UNIQUE, size INTEGER NOT NULL,
            comment TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""")


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
        latest[address] = {"address": address, "state": field(r"binding state\s+([^;]+)"), "mac": field(r"hardware ethernet\s+([^;]+)"), "starts": field(r"starts\s+[^ ]+\s+([^;]+)"), "ends": field(r"ends\s+[^ ]+\s+([^;]+)"), "vendor": field(r'set vendor-class-identifier\s*=\s*"([^"]*)"')}
    return sorted(latest.values(), key=lambda item: tuple(int(x) for x in item["address"].split(".")))


def page(title, body, message="", error=False):
    flash = ""
    if message:
        flash = '<article class="flash{}">{}</article>'.format(" error" if error else "", html.escape(message))
    return """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{0} · SONiC ZTP</title><style>{1}</style></head><body><nav><strong>SONiC ZTP · ISC DHCP</strong><a href="/">Dashboard</a><a href="/dhcp">DHCP</a><a href="/artifacts">Artifacts</a></nav><main>{2}{3}</main></body></html>""".format(html.escape(title), STYLE, flash, body).encode()


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
            rows = "".join("<tr><td>{address}</td><td>{mac}</td><td>{state}</td><td>{ends}</td><td>{vendor}</td></tr>".format(**{k: html.escape(v) for k,v in item.items()}) for item in parse_leases())
            state = service_state()
            body = '<section class="hero"><div><h1>ISC DHCP</h1><p class="muted">Live file: {}</p></div><span class="badge {}">{}</span></section><article><div class="actions"><div><form class="inline" method="post" action="/dhcp/start"><button>Start</button></form> <form class="inline" method="post" action="/dhcp/stop"><button class="danger">Stop</button></form></div></div><form method="post" action="/dhcp/candidate"><label>Candidate dhcpd.conf</label><textarea name="content" spellcheck="false">{}</textarea><p><button>Save candidate</button> <button class="primary" formaction="/dhcp/apply">Validate, apply and restart</button></p></form></article><article><h2>Leases</h2><table><thead><tr><th>Address</th><th>MAC</th><th>State</th><th>Ends</th><th>Vendor class</th></tr></thead><tbody>{}</tbody></table></article>'.format(html.escape(str(DHCP_CONFIG)), "ok" if state == "active" else "bad", html.escape(state), html.escape(candidate), rows)
            self.send_page("DHCP", body, message, error); return
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
                    if item: (ARTIFACT_DIR / item["stored_name"]).unlink(missing_ok=True); connection.execute("DELETE FROM artifacts WHERE id=?", (item["id"],))
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
