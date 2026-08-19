from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ConfigRevision(Base):
    __tablename__ = "config_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    content: Mapped[str] = mapped_column(Text)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    validation_output: Mapped[str] = mapped_column(Text, default="Not validated")
    applied: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    outcome: Mapped[str] = mapped_column(String(20), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    original_name: Mapped[str] = mapped_column(String(255))
    stored_name: Mapped[str] = mapped_column(String(80), unique=True)
    media_type: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ProvisioningProfile(Base):
    __tablename__ = "provisioning_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    stage: Mapped[str] = mapped_column(String(20), index=True)
    artifact_id: Mapped[int] = mapped_column(Integer, index=True)
    match_option: Mapped[int] = mapped_column(Integer, default=60)
    match_operator: Mapped[str] = mapped_column(String(20), default="starts_with")
    match_value: Mapped[str] = mapped_column(String(255), default="onie_vendor")
    firmware_artifact_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    configdb_artifact_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    script_artifact_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ProfileMatch(Base):
    __tablename__ = "profile_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("provisioning_profiles.id"), index=True)
    option_code: Mapped[int] = mapped_column(Integer)
    operator: Mapped[str] = mapped_column(String(20))
    value: Mapped[str] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(Integer, default=0)


class DhcpScope(Base):
    __tablename__ = "dhcp_scopes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    subnet: Mapped[str] = mapped_column(String(64))
    pool_start: Mapped[str] = mapped_column(String(45))
    pool_end: Mapped[str] = mapped_column(String(45))
    gateway: Mapped[str] = mapped_column(String(45), default="")
    dns_servers: Mapped[str] = mapped_column(String(255), default="")
    lease_time: Mapped[int] = mapped_column(Integer, default=600)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class DhcpReservation(Base):
    __tablename__ = "dhcp_reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hw_address: Mapped[str] = mapped_column(String(17), unique=True, index=True)
    ip_address: Mapped[str] = mapped_column(String(45), unique=True, index=True)
    hostname: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
