from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.settings import settings


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_database() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    # Small forward-only migration for databases created by the first milestone.
    columns = {column["name"] for column in inspect(engine).get_columns("artifacts")}
    if "comment" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE artifacts ADD COLUMN comment TEXT NOT NULL DEFAULT ''"))
