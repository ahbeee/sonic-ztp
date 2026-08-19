import json
import ipaddress
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import SessionLocal, init_database
from app.models import Artifact, AuditEvent, ConfigRevision, DhcpReservation, DhcpScope, ProfileMatch, ProvisioningProfile
from app.services.artifacts import store_stream
from app.services.kea import KeaProvider
from app.services.profiles import (
    MATCH_OPERATORS,
    MATCH_OPTIONS,
    STAGES,
    artifact_url,
    build_candidate,
    generated_ztp,
    group_matches,
    match_summary,
    validate_match,
    ztp_url,
)
from app.settings import settings


settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.artifact_dir.mkdir(parents=True, exist_ok=True)
kea = KeaProvider(settings)
templates = Jinja2Templates(directory=str(settings.base_dir / "app" / "templates"))


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def latest_revision(session: Session) -> ConfigRevision:
    revision = session.scalar(select(ConfigRevision).order_by(desc(ConfigRevision.id)).limit(1))
    if revision is None:
        revision = ConfigRevision(content=json.dumps(kea.default_config(), indent=2), validation_output="Not validated")
        session.add(revision)
        session.commit()
    return revision


def regenerate_profile_candidate(session: Session) -> ConfigRevision:
    profiles = session.scalars(select(ProvisioningProfile).order_by(ProvisioningProfile.id)).all()
    matches = session.scalars(select(ProfileMatch).order_by(ProfileMatch.profile_id, ProfileMatch.position)).all()
    artifacts = {item.id: item for item in session.scalars(select(Artifact)).all()}
    revision = build_candidate(latest_revision(session), profiles, matches, artifacts, settings, kea)
    session.add(revision)
    session.flush()
    session.add(AuditEvent(
        action="profiles.generate",
        outcome="success" if revision.is_valid else "failed",
        detail="revision={}".format(revision.id),
    ))
    return revision


def scope_readiness(scope: DhcpScope | None, revision: ConfigRevision, active_profiles: int) -> list[dict]:
    return [
        {"label": "DHCP subnet and address pool", "ready": scope is not None},
        {"label": "At least one active provisioning profile", "ready": active_profiles > 0},
        {"label": "Kea candidate validation", "ready": revision.is_valid},
        {"label": "Candidate applied to Kea", "ready": revision.applied},
    ]


def scope_candidate(revision: ConfigRevision, scope: DhcpScope, reservations: list[DhcpReservation] | None = None) -> str:
    config = json.loads(revision.content)
    dhcp4 = config["Dhcp4"]
    option_data = []
    if scope.gateway:
        option_data.append({"name": "routers", "data": scope.gateway})
    if scope.dns_servers:
        option_data.append({"name": "domain-name-servers", "data": scope.dns_servers})
    dhcp4["valid-lifetime"] = scope.lease_time
    dhcp4["renew-timer"] = max(1, scope.lease_time // 2)
    dhcp4["rebind-timer"] = max(1, int(scope.lease_time * 0.8))
    subnet = {
        "id": 1,
        "subnet": scope.subnet,
        "pools": [{"pool": "{} - {}".format(scope.pool_start, scope.pool_end)}],
    }
    if reservations:
        subnet["reservations"] = [
            dict(
                {"hw-address": item.hw_address, "ip-address": item.ip_address},
                **({"hostname": item.hostname} if item.hostname else {}),
            )
            for item in reservations
        ]
    if option_data:
        subnet["option-data"] = option_data
    dhcp4["subnet4"] = [subnet]
    return json.dumps(config, indent=2)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    with SessionLocal() as session:
        latest_revision(session)
    yield


app = FastAPI(title="SONiC ZTP Server", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(settings.base_dir / "app" / "static")), name="static")


@app.get("/health")
def health():
    status = kea.status()
    return {
        "status": "ok",
        "version": app.version,
        "kea": {"installed": status.installed, "active": status.active, "enabled": status.enabled},
        "provisioning_interface": settings.provisioning_interface,
        "provisioning_address": settings.provisioning_address,
    }


@app.get("/api/dhcp/status")
def dhcp_status():
    return kea.status().__dict__


@app.get("/api/dhcp/config")
def dhcp_config(session: Session = Depends(get_session)):
    revision = latest_revision(session)
    return {
        "revision": revision.id,
        "is_valid": revision.is_valid,
        "applied": revision.applied,
        "content": json.loads(revision.content),
        "validation_output": revision.validation_output,
    }


@app.get("/api/dhcp/leases")
def dhcp_leases():
    return {"count": len(kea.leases()), "leases": kea.leases()}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    status = kea.status()
    artifacts = session.scalars(select(Artifact).order_by(desc(Artifact.id)).limit(5)).all()
    events = session.scalars(select(AuditEvent).order_by(desc(AuditEvent.id)).limit(8)).all()
    revision = latest_revision(session)
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "status": status, "artifacts": artifacts, "events": events, "revision": revision,
         "settings": settings},
    )


@app.get("/dhcp", response_class=HTMLResponse)
def dhcp_page(request: Request, session: Session = Depends(get_session)):
    revision = latest_revision(session)
    scope = session.get(DhcpScope, 1)
    reservations = session.scalars(select(DhcpReservation).order_by(DhcpReservation.ip_address)).all()
    active_profiles = len(session.scalars(select(ProvisioningProfile).where(ProvisioningProfile.enabled.is_(True))).all())
    return templates.TemplateResponse(
        "dhcp.html",
        {"request": request, "status": kea.status(), "revision": revision, "scope": scope,
        "readiness": scope_readiness(scope, revision, active_profiles), "active_profiles": active_profiles,
        "reservations": reservations, "leases": kea.leases(), "settings": settings},
    )


@app.post("/dhcp/scope")
def save_dhcp_scope(
    subnet: str = Form(...),
    pool_start: str = Form(...),
    pool_end: str = Form(...),
    gateway: str = Form(""),
    dns_servers: str = Form(""),
    lease_time: int = Form(600),
    session: Session = Depends(get_session),
):
    try:
        network = ipaddress.ip_network(subnet.strip(), strict=True)
        start = ipaddress.ip_address(pool_start.strip())
        end = ipaddress.ip_address(pool_end.strip())
        gateway_ip = ipaddress.ip_address(gateway.strip()) if gateway.strip() else None
        dns = [ipaddress.ip_address(item.strip()) for item in dns_servers.split(",") if item.strip()]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid IPv4 scope: {}".format(exc))
    if network.version != 4 or start.version != 4 or end.version != 4:
        raise HTTPException(status_code=422, detail="Only DHCPv4 scope is currently supported")
    if start not in network or end not in network or start > end:
        raise HTTPException(status_code=422, detail="Pool must be ordered and contained in the subnet")
    if start in (network.network_address, network.broadcast_address) or end in (network.network_address, network.broadcast_address):
        raise HTTPException(status_code=422, detail="Pool cannot include network or broadcast addresses")
    server_ip = ipaddress.ip_address(settings.provisioning_address)
    if server_ip in network and start <= server_ip <= end:
        raise HTTPException(status_code=422, detail="Pool cannot include the ZTP server address {}".format(server_ip))
    if gateway_ip and gateway_ip not in network:
        raise HTTPException(status_code=422, detail="Gateway must be inside the subnet")
    if not 60 <= lease_time <= 604800:
        raise HTTPException(status_code=422, detail="Lease time must be between 60 and 604800 seconds")
    scope = session.get(DhcpScope, 1) or DhcpScope(id=1)
    scope.subnet = str(network)
    scope.pool_start = str(start)
    scope.pool_end = str(end)
    scope.gateway = str(gateway_ip) if gateway_ip else ""
    scope.dns_servers = ", ".join(str(item) for item in dns)
    scope.lease_time = lease_time
    reservations = session.scalars(select(DhcpReservation).order_by(DhcpReservation.id)).all()
    content = scope_candidate(latest_revision(session), scope, reservations)
    revision = ConfigRevision(content=content, is_valid=False, validation_output="Saved; validation will run when Apply candidate is selected")
    session.add(scope)
    session.add(revision)
    session.flush()
    session.add(AuditEvent(action="dhcp.scope", outcome="success", detail="revision={} subnet={}".format(revision.id, scope.subnet)))
    session.commit()
    return RedirectResponse("/dhcp", status_code=303)


@app.post("/dhcp/reservations")
def add_dhcp_reservation(
    hw_address: str = Form(...),
    ip_address: str = Form(...),
    hostname: str = Form(""),
    session: Session = Depends(get_session),
):
    scope = session.get(DhcpScope, 1)
    if scope is None:
        raise HTTPException(status_code=422, detail="Save the DHCP scope before adding a reservation")
    mac = hw_address.strip().lower().replace("-", ":")
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        raise HTTPException(status_code=422, detail="MAC address must use the format 52:54:00:12:34:56")
    try:
        address = ipaddress.ip_address(ip_address.strip())
        network = ipaddress.ip_network(scope.subnet)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid reservation address: {}".format(exc))
    if address.version != 4 or address not in network:
        raise HTTPException(status_code=422, detail="Static IP must be an IPv4 address inside the DHCP subnet")
    if address in (network.network_address, network.broadcast_address, ipaddress.ip_address(settings.provisioning_address)):
        raise HTTPException(status_code=422, detail="Static IP cannot be the network, broadcast, or ZTP server address")
    name = hostname.strip()
    if name and (len(name) > 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", name)):
        raise HTTPException(status_code=422, detail="Hostname contains unsupported characters")
    reservation = DhcpReservation(hw_address=mac, ip_address=str(address), hostname=name)
    session.add(reservation)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="That MAC address or static IP is already reserved")
    reservations = session.scalars(select(DhcpReservation).order_by(DhcpReservation.id)).all()
    revision = ConfigRevision(
        content=scope_candidate(latest_revision(session), scope, reservations),
        is_valid=False,
        validation_output="Reservation saved; validation will run when Apply candidate is selected",
    )
    session.add(revision)
    session.add(AuditEvent(action="dhcp.reservation.add", outcome="success", detail="mac={} ip={}".format(mac, address)))
    session.commit()
    return RedirectResponse("/dhcp", status_code=303)


@app.post("/dhcp/reservations/{reservation_id}/delete")
def delete_dhcp_reservation(reservation_id: int, session: Session = Depends(get_session)):
    reservation = session.get(DhcpReservation, reservation_id)
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    detail = "mac={} ip={}".format(reservation.hw_address, reservation.ip_address)
    session.delete(reservation)
    session.flush()
    scope = session.get(DhcpScope, 1)
    if scope is not None:
        reservations = session.scalars(select(DhcpReservation).order_by(DhcpReservation.id)).all()
        session.add(ConfigRevision(
            content=scope_candidate(latest_revision(session), scope, reservations),
            is_valid=False,
            validation_output="Reservation deleted; validation will run when Apply candidate is selected",
        ))
    session.add(AuditEvent(action="dhcp.reservation.delete", outcome="success", detail=detail))
    session.commit()
    return RedirectResponse("/dhcp", status_code=303)


@app.post("/dhcp/draft")
def save_dhcp_draft(content: str = Form(...), session: Session = Depends(get_session)):
    try:
        normalized, semantic_errors = kea.normalize_and_check(content)
        output = "; ".join(semantic_errors) if semantic_errors else "Draft saved; binary validation not run"
        revision = ConfigRevision(content=normalized, is_valid=False, validation_output=output)
        outcome = "rejected" if semantic_errors else "success"
    except (json.JSONDecodeError, ValueError) as exc:
        session.add(AuditEvent(action="dhcp.draft", outcome="rejected", detail=str(exc)))
        session.commit()
        raise HTTPException(status_code=422, detail="Invalid JSON: {}".format(exc))
    session.add(revision)
    session.add(AuditEvent(action="dhcp.draft", outcome=outcome, detail=output))
    session.commit()
    return RedirectResponse("/dhcp", status_code=303)


@app.post("/dhcp/apply")
def apply_dhcp_candidate(session: Session = Depends(get_session)):
    revision = latest_revision(session)
    try:
        normalized, semantic_errors = kea.normalize_and_check(revision.content)
        valid, output = (False, "; ".join(semantic_errors)) if semantic_errors else kea.binary_validate(normalized)
    except (json.JSONDecodeError, ValueError) as exc:
        normalized, valid, output = revision.content, False, "Invalid JSON: {}".format(exc)
    revision.is_valid = valid
    revision.applied = False
    revision.validation_output = output
    if valid:
        try:
            kea.apply_config(normalized)
            revision.content = normalized
            revision.applied = True
            output = "Validation passed and candidate applied to {}".format(settings.kea_config_path)
            revision.validation_output = output
        except OSError as exc:
            revision.applied = False
            output = "Validation passed but apply failed: {}".format(exc)
            revision.validation_output = output
    session.add(AuditEvent(action="dhcp.apply", outcome="success" if revision.applied else "failed", detail=output))
    session.commit()
    return RedirectResponse("/dhcp", status_code=303)


@app.post("/dhcp/start")
def start_dhcp_service(session: Session = Depends(get_session)):
    revision = latest_revision(session)
    if not revision.applied:
        revision.validation_output = "Start blocked: Apply candidate successfully before starting DHCP"
        session.add(AuditEvent(action="dhcp.start", outcome="blocked", detail=revision.validation_output))
        session.commit()
        return RedirectResponse("/dhcp", status_code=303)
    success, output = kea.control_service("start")
    revision.validation_output = output
    session.add(AuditEvent(action="dhcp.start", outcome="success" if success else "failed", detail=output))
    session.commit()
    return RedirectResponse("/dhcp", status_code=303)


@app.post("/dhcp/stop")
def stop_dhcp_service(session: Session = Depends(get_session)):
    revision = latest_revision(session)
    success, output = kea.control_service("stop")
    revision.validation_output = output
    session.add(AuditEvent(action="dhcp.stop", outcome="success" if success else "failed", detail=output))
    session.commit()
    return RedirectResponse("/dhcp", status_code=303)


@app.post("/dhcp/revisions/{revision_id}/restore")
def restore_dhcp_revision(revision_id: int, session: Session = Depends(get_session)):
    source = session.get(ConfigRevision, revision_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    restored = ConfigRevision(
        content=source.content,
        is_valid=False,
        validation_output="Restored from revision #{}; validation required".format(source.id),
    )
    session.add(restored)
    session.add(AuditEvent(action="dhcp.restore", outcome="success", detail="source={}".format(source.id)))
    session.commit()
    return RedirectResponse("/dhcp", status_code=303)


@app.get("/artifacts", response_class=HTMLResponse)
def artifacts_page(request: Request, session: Session = Depends(get_session)):
    artifacts = session.scalars(select(Artifact).order_by(desc(Artifact.id))).all()
    return templates.TemplateResponse("artifacts.html", {"request": request, "artifacts": artifacts})


@app.get("/profiles", response_class=HTMLResponse)
def profiles_page(request: Request, session: Session = Depends(get_session)):
    profiles = session.scalars(select(ProvisioningProfile).order_by(desc(ProvisioningProfile.id))).all()
    artifacts = session.scalars(select(Artifact).where(Artifact.enabled.is_(True)).order_by(desc(Artifact.id))).all()
    artifact_map = {item.id: item for item in artifacts}
    matches = session.scalars(select(ProfileMatch).order_by(ProfileMatch.profile_id, ProfileMatch.position)).all()
    return templates.TemplateResponse("profiles.html", {
        "request": request,
        "profiles": profiles,
        "artifacts": artifacts,
        "firmware_artifacts": [item for item in artifacts if item.original_name.lower().endswith((".bin", ".img"))],
        "json_artifacts": [item for item in artifacts if item.original_name.lower().endswith(".json")],
        "script_artifacts": [item for item in artifacts if item.original_name.lower().endswith((".sh", ".py"))],
        "artifact_map": artifact_map,
        "stages": STAGES,
        "match_options": MATCH_OPTIONS,
        "match_operators": MATCH_OPERATORS,
        "profile_matches": group_matches(matches),
        "match_summary": match_summary,
        "artifact_url": lambda item: artifact_url(settings, item),
        "ztp_url": lambda item: ztp_url(settings, item),
        "revision": latest_revision(session),
    })


@app.post("/profiles")
def create_profile(
    name: str = Form(...),
    stage: str = Form(...),
    artifact_id: int = Form(0),
    match_option: list[int] = Form(...),
    match_operator: list[str] = Form(...),
    match_value: list[str] = Form(...),
    firmware_artifact_id: int = Form(0),
    configdb_artifact_id: int = Form(0),
    script_artifact_id: int = Form(0),
    comment: str = Form(""),
    session: Session = Depends(get_session),
):
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 120:
        raise HTTPException(status_code=422, detail="Profile name is required and must not exceed 120 characters")
    if stage not in STAGES:
        raise HTTPException(status_code=422, detail="Unsupported provisioning stage")
    if not (len(match_option) == len(match_operator) == len(match_value)) or not match_option:
        raise HTTPException(status_code=422, detail="Client match fields are incomplete")
    conditions = []
    for position, (option, operator, value) in enumerate(zip(match_option, match_operator, match_value)):
        clean_value = value.strip()
        try:
            validate_match(option, operator, clean_value)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Match {}: {}".format(position + 1, exc))
        conditions.append((option, operator, clean_value, position))
    selected_ids = [item for item in (artifact_id, firmware_artifact_id, configdb_artifact_id, script_artifact_id) if item]
    available = {item.id for item in session.scalars(select(Artifact).where(Artifact.id.in_(selected_ids))).all()} if selected_ids else set()
    if any(item not in available for item in selected_ids):
        raise HTTPException(status_code=422, detail="One or more selected artifacts are unavailable")
    if stage == "onie" and not artifact_id:
        raise HTTPException(status_code=422, detail="ONIE profile requires a NOS image")
    if stage == "sonic" and not any((firmware_artifact_id, configdb_artifact_id, script_artifact_id)):
        raise HTTPException(status_code=422, detail="SONiC profile requires at least one ZTP section")
    profile = ProvisioningProfile(
        name=clean_name,
        stage=stage,
        artifact_id=artifact_id,
        match_option=conditions[0][0],
        match_operator=conditions[0][1],
        match_value=conditions[0][2],
        firmware_artifact_id=firmware_artifact_id or None,
        configdb_artifact_id=configdb_artifact_id or None,
        script_artifact_id=script_artifact_id or None,
        comment=comment.strip()[:4000],
    )
    session.add(profile)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="Profile name already exists")
    for option, operator, value, position in conditions:
        session.add(ProfileMatch(profile_id=profile.id, option_code=option, operator=operator, value=value, position=position))
    session.flush()
    revision = regenerate_profile_candidate(session)
    session.add(AuditEvent(action="profile.create", outcome="success", detail="profile={} revision={}".format(profile.id, revision.id)))
    session.commit()
    return RedirectResponse("/profiles", status_code=303)


@app.post("/profiles/{profile_id}/toggle")
def toggle_profile(profile_id: int, session: Session = Depends(get_session)):
    profile = session.get(ProvisioningProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    enabling = not profile.enabled
    profile.enabled = enabling
    revision = regenerate_profile_candidate(session)
    session.add(AuditEvent(action="profile.toggle", outcome="success", detail="profile={} enabled={} revision={}".format(profile.id, profile.enabled, revision.id)))
    session.commit()
    return RedirectResponse("/profiles", status_code=303)


@app.get("/ztp/{profile_id}/ztp.json")
def serve_generated_ztp(profile_id: int, session: Session = Depends(get_session)):
    profile = session.get(ProvisioningProfile, profile_id)
    if profile is None or profile.stage != "sonic" or not profile.enabled:
        raise HTTPException(status_code=404, detail="ZTP profile not found")
    ids = [item for item in (profile.firmware_artifact_id, profile.configdb_artifact_id, profile.script_artifact_id) if item]
    artifacts = {
        item.id: item for item in session.scalars(
            select(Artifact).where(Artifact.id.in_(ids), Artifact.enabled.is_(True))
        ).all()
    }
    return JSONResponse(generated_ztp(profile, artifacts, settings))


@app.post("/artifacts")
def upload_artifact(file: UploadFile = File(...), comment: str = Form(""), session: Session = Depends(get_session)):
    original_name = Path(file.filename or "artifact.bin").name
    try:
        stored_name, size, sha256 = store_stream(file.file, original_name, settings.artifact_dir, settings.max_upload_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    artifact = Artifact(original_name=original_name, stored_name=stored_name, media_type=file.content_type,
                        size=size, sha256=sha256, comment=comment.strip()[:4000])
    session.add(artifact)
    session.flush()
    session.add(AuditEvent(action="artifact.upload", outcome="success",
                           detail="id={} name={} size={} sha256={}".format(artifact.id, original_name, size, sha256)))
    session.commit()
    return RedirectResponse("/artifacts", status_code=303)


@app.post("/artifacts/{artifact_id}/comment")
def update_artifact_comment(artifact_id: int, comment: str = Form(""), session: Session = Depends(get_session)):
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    artifact.comment = comment.strip()[:4000]
    session.add(AuditEvent(action="artifact.comment", outcome="success", detail="id={}".format(artifact.id)))
    session.commit()
    return RedirectResponse("/artifacts", status_code=303)


@app.get("/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: int, session: Session = Depends(get_session)):
    artifact = session.get(Artifact, artifact_id)
    if artifact is None or not artifact.enabled:
        raise HTTPException(status_code=404, detail="Artifact not found")
    target = settings.artifact_dir / artifact.stored_name
    if not target.is_file():
        raise HTTPException(status_code=410, detail="Artifact file is missing")
    return FileResponse(str(target), media_type=artifact.media_type, filename=artifact.original_name)


@app.get("/files/{artifact_id}/{filename}", name="serve_artifact")
def serve_artifact(artifact_id: int, filename: str, session: Session = Depends(get_session)):
    """Stable versioned URL intended for ONIE, ZTP and DHCP option values."""
    artifact = session.get(Artifact, artifact_id)
    if artifact is None or not artifact.enabled or filename != artifact.original_name:
        raise HTTPException(status_code=404, detail="Artifact not found")
    target = settings.artifact_dir / artifact.stored_name
    if not target.is_file():
        raise HTTPException(status_code=410, detail="Artifact file is missing")
    return FileResponse(str(target), media_type=artifact.media_type)
