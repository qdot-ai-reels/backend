"""SQLAlchemy database setup and global settings repository."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text, create_engine, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.settings_service import GlobalSettings, SettingsRepository


class Base(DeclarativeBase):
    pass


class GlobalSettingsRow(Base):
    __tablename__ = "global_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    openrouter_script_api_key_encrypted: Mapped[str | None] = mapped_column(String(1024))
    openrouter_tts_api_key_encrypted: Mapped[str | None] = mapped_column(String(1024))
    openrouter_video_api_key_encrypted: Mapped[str | None] = mapped_column(String(1024))
    openrouter_script_model: Mapped[str | None] = mapped_column(String(255))
    openrouter_tts_model: Mapped[str | None] = mapped_column(String(255))
    openrouter_video_model: Mapped[str | None] = mapped_column(String(255))
    openrouter_tts_voice: Mapped[str] = mapped_column(String(255), default="")
    video_min_resolution: Mapped[str] = mapped_column(String(32), default="720p")
    video_max_resolution: Mapped[str] = mapped_column(String(32), default="1080p")
    video_max_duration_seconds: Mapped[int] = mapped_column(Integer, default=15)
    script_generation_retries: Mapped[int] = mapped_column(Integer, default=2)
    video_generation_retries: Mapped[int] = mapped_column(Integer, default=2)
    media_combine_retries: Mapped[int] = mapped_column(Integer, default=3)
    mute_original_audio: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class GenerationJobRow(Base):
    __tablename__ = "generation_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    input_type: Mapped[str] = mapped_column(String(32))
    product_json: Mapped[str | None] = mapped_column(Text)
    script_json: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(String(2048))
    video_job_id: Mapped[str | None] = mapped_column(String(128))
    caption_job_id: Mapped[str | None] = mapped_column(String(128))
    output_path: Mapped[str | None] = mapped_column(String(2048))
    error_message: Mapped[str | None] = mapped_column(Text)
    cost: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
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
        "openrouter_script_api_key_encrypted": ("VARCHAR(1024)", "''"),
        "openrouter_tts_api_key_encrypted": ("VARCHAR(1024)", "''"),
        "openrouter_video_api_key_encrypted": ("VARCHAR(1024)", "''"),
        "openrouter_tts_model": ("VARCHAR(255)", "''"),
        "video_min_resolution": ("VARCHAR(32)", "'720p'"),
        "video_max_resolution": ("VARCHAR(32)", "'1080p'"),
        "script_generation_retries": ("INTEGER", "2"),
        "video_generation_retries": ("INTEGER", "2"),
        "media_combine_retries": ("INTEGER", "3"),
        "openrouter_tts_voice": ("VARCHAR(255)", "''"),
    }

    with engine.begin() as connection:
        if "openrouter_api_key_encrypted" in columns and "openrouter_script_api_key_encrypted" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE global_settings RENAME COLUMN "
                    "openrouter_api_key_encrypted TO openrouter_script_api_key_encrypted"
                )
            )
            columns.remove("openrouter_api_key_encrypted")
            columns.add("openrouter_script_api_key_encrypted")
        if "openrouter_model" in columns and "openrouter_script_model" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE global_settings RENAME COLUMN "
                    "openrouter_model TO openrouter_script_model"
                )
            )
            columns.remove("openrouter_model")
            columns.add("openrouter_script_model")
        if "google_tts_voice_name" in columns and "openrouter_tts_voice" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE global_settings RENAME COLUMN "
                    "google_tts_voice_name TO openrouter_tts_voice"
                )
            )
            columns.remove("google_tts_voice_name")
            columns.add("openrouter_tts_voice")
        for name, (column_type, default) in missing_columns.items():
            if name not in columns:
                connection.execute(
                    text(
                        f"ALTER TABLE global_settings ADD COLUMN {name} "
                        f"{column_type} NOT NULL DEFAULT {default}"
                    )
                )

        # Upgrade the previous default retry count for existing local databases.
        connection.execute(
            text(
                "UPDATE global_settings SET video_generation_retries = 2 "
                "WHERE video_generation_retries = 1"
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
            "openrouter_script_api_key_encrypted",
            "openrouter_tts_api_key_encrypted",
            "openrouter_video_api_key_encrypted",
            "openrouter_script_model",
            "openrouter_tts_model",
            "openrouter_video_model",
            "openrouter_tts_voice",
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
            openrouter_script_api_key_encrypted=row.openrouter_script_api_key_encrypted,
            openrouter_tts_api_key_encrypted=row.openrouter_tts_api_key_encrypted,
            openrouter_video_api_key_encrypted=row.openrouter_video_api_key_encrypted,
            openrouter_script_model=row.openrouter_script_model,
            openrouter_tts_model=row.openrouter_tts_model,
            openrouter_video_model=row.openrouter_video_model,
            openrouter_tts_voice=row.openrouter_tts_voice,
            video_min_resolution=row.video_min_resolution,
            video_max_resolution=row.video_max_resolution,
            video_max_duration_seconds=row.video_max_duration_seconds,
            script_generation_retries=row.script_generation_retries,
            video_generation_retries=row.video_generation_retries,
            media_combine_retries=row.media_combine_retries,
            mute_original_audio=row.mute_original_audio,
        )
