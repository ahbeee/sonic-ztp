import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.database import SessionLocal, init_database
from app.models import Artifact, AuditEvent, ConfigRevision
from app.services.artifacts import store_stream
from app.services.kea import KeaProvider
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
    revisions = session.scalars(select(ConfigRevision).order_by(desc(ConfigRevision.id)).limit(10)).all()
    return templates.TemplateResponse(
        "dhcp.html",
        {"request": request, "status": kea.status(), "revision": latest_revision(session),
        "revisions": revisions, "leases": kea.leases(), "settings": settings},
    )


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


@app.post("/dhcp/validate")
def validate_dhcp(session: Session = Depends(get_session)):
    revision = latest_revision(session)
    normalized, semantic_errors = kea.normalize_and_check(revision.content)
    if semantic_errors:
        valid, output = False, "; ".join(semantic_errors)
    else:
        valid, output = kea.binary_validate(normalized)
    revision.is_valid = valid
    revision.validation_output = output
    session.add(AuditEvent(action="dhcp.validate", outcome="success" if valid else "failed", detail=output))
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


@app.post("/artifacts")
def upload_artifact(file: UploadFile = File(...), session: Session = Depends(get_session)):
    original_name = Path(file.filename or "artifact.bin").name
    try:
        stored_name, size, sha256 = store_stream(file.file, original_name, settings.artifact_dir, settings.max_upload_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    artifact = Artifact(original_name=original_name, stored_name=stored_name, media_type=file.content_type,
                        size=size, sha256=sha256)
    session.add(artifact)
    session.flush()
    session.add(AuditEvent(action="artifact.upload", outcome="success",
                           detail="id={} name={} size={} sha256={}".format(artifact.id, original_name, size, sha256)))
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
