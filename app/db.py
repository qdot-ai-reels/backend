"""SQLAlchemy database setup and global settings repository."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from dotenv import load_dotenv

# .env 파일 즉시 로드
load_dotenv()

from sqlalchemy import DateTime, Integer, String, create_engine, inspect, select, text
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
    video_min_resolution: Mapped[str] = mapped_column(String(32), default="720p")
    video_max_resolution: Mapped[str] = mapped_column(String(32), default="1080p")
    video_max_duration_seconds: Mapped[int] = mapped_column(Integer, default=15)
    script_generation_retries: Mapped[int] = mapped_column(Integer, default=2)
    video_generation_retries: Mapped[int] = mapped_column(Integer, default=1)
    media_combine_retries: Mapped[int] = mapped_column(Integer, default=3)
    mute_original_audio: Mapped[bool] = mapped_column(default=True)
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
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("global_settings")}
    missing_columns = {
        "video_min_resolution": ("VARCHAR(32)", "'720p'"),
        "video_max_resolution": ("VARCHAR(32)", "'1080p'"),
        "script_generation_retries": ("INTEGER", "2"),
        "video_generation_retries": ("INTEGER", "1"),
        "media_combine_retries": ("INTEGER", "3"),
    }

    with engine.begin() as connection:
        for name, (column_type, default) in missing_columns.items():
            if name not in columns:
                connection.execute(
                    text(
                        f"ALTER TABLE global_settings ADD COLUMN {name} "
                        f"{column_type} NOT NULL DEFAULT {default}"
                    )
                )

        if "video_resolution" in columns and "video_max_resolution" not in columns:
            connection.execute(
                text(
                    "UPDATE global_settings "
                    "SET video_max_resolution = video_resolution "
                    "WHERE video_resolution IS NOT NULL"
                )
            )
        if "max_retries" in columns and "script_generation_retries" not in columns:
            connection.execute(
                text(
                    "UPDATE global_settings "
                    "SET script_generation_retries = max_retries "
                    "WHERE max_retries IS NOT NULL"
                )
            )


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
            "video_min_resolution",
            "video_max_resolution",
            "video_max_duration_seconds",
            "script_generation_retries",
            "video_generation_retries",
            "media_combine_retries",
            "mute_original_audio",
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
            video_min_resolution=row.video_min_resolution,
            video_max_resolution=row.video_max_resolution,
            video_max_duration_seconds=row.video_max_duration_seconds,
            script_generation_retries=row.script_generation_retries,
            video_generation_retries=row.video_generation_retries,
            media_combine_retries=row.media_combine_retries,
            mute_original_audio=row.mute_original_audio,
        )
