"""SQLAlchemy database setup and global settings repository."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.settings_service import GlobalSettings, SettingsRepository


class Base(DeclarativeBase):
    pass


class GlobalSettingsRow(Base):
    __tablename__ = "global_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    openrouter_api_key_encrypted: Mapped[str | None] = mapped_column(String(1024))
    openrouter_model: Mapped[str | None] = mapped_column(String(255))
    openrouter_video_model: Mapped[str | None] = mapped_column(String(255))
    google_tts_voice_name: Mapped[str] = mapped_column(String(255), default="ko-KR-Standard-A")
    video_resolution: Mapped[str] = mapped_column(String(32), default="720p")
    video_max_duration_seconds: Mapped[int] = mapped_column(Integer, default=30)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


def get_engine():
    from app.core.config import settings

    database_url = settings.DATABASE_URL or "sqlite:///./quedot.local.db"
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)


engine = get_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


class SQLAlchemySettingsRepository(SettingsRepository):
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self) -> GlobalSettings:
        row = self.session.scalar(select(GlobalSettingsRow).where(GlobalSettingsRow.id == 1))
        if row is None:
            row = GlobalSettingsRow(id=1)
            self.session.add(row)
            self.session.commit()
            self.session.refresh(row)
        return self._to_domain(row)

    def save(self, settings: GlobalSettings) -> GlobalSettings:
        row = self.session.scalar(select(GlobalSettingsRow).where(GlobalSettingsRow.id == 1))
        if row is None:
            row = GlobalSettingsRow(id=1)
            self.session.add(row)
        for field in (
            "openrouter_api_key_encrypted",
            "openrouter_model",
            "openrouter_video_model",
            "google_tts_voice_name",
            "video_resolution",
            "video_max_duration_seconds",
            "max_retries",
        ):
            setattr(row, field, getattr(settings, field))
        self.session.commit()
        return self._to_domain(row)

    @staticmethod
    def _to_domain(row: GlobalSettingsRow) -> GlobalSettings:
        return GlobalSettings(
            openrouter_api_key_encrypted=row.openrouter_api_key_encrypted,
            openrouter_model=row.openrouter_model,
            openrouter_video_model=row.openrouter_video_model,
            google_tts_voice_name=row.google_tts_voice_name,
            video_resolution=row.video_resolution,
            video_max_duration_seconds=row.video_max_duration_seconds,
            max_retries=row.max_retries,
        )
