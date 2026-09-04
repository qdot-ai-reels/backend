from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os
# reels 라우터 import
from app.api.v1.reels import router as reels_router
from app.api.v1.script import router as script_router
from app.api.v1.video import router as video_router
from app.api.v1.tts import router as tts_router
from app.api.v1.settings import router as settings_router
from app.api.v1.caption import router as caption_router
from app.api.v1.combine import combine_router
from app.api.v1.prompt_versions import router as prompt_versions_router
from app.api.v1.products import router as products_router
from app.db import init_db


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Create known tables once when the application starts."""
    init_db()
    yield

app = FastAPI(
    title="Shorts Reels Generator API",
    version="1.0.0",
    lifespan=lifespan,
)

def _parse_cors_origins(raw_origins: str) -> list[str]:
    """Normalize configured origins and keep local browser aliases usable."""
    origins: list[str] = []

    def append(origin: str) -> None:
        if origin and origin != "*" and origin not in origins:
            origins.append(origin)

    for raw_origin in raw_origins.split(","):
        origin = raw_origin.strip().rstrip("/")
        append(origin)

        try:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.hostname not in {"localhost", "127.0.0.1"}
                or parsed.port is None
            ):
                continue
        except ValueError:
            continue

        alias_host = "127.0.0.1" if parsed.hostname == "localhost" else "localhost"
        append(urlunsplit((parsed.scheme, f"{alias_host}:{parsed.port}", "", "", "")))

    return origins


# CORS 설정
cors_origins = _parse_cors_origins(
    os.getenv("CORS_ORIGINS", "http://localhost:3000")
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Reels 라우터 등록
app.include_router(reels_router, prefix="/api/v1/reels", tags=["reels"])
app.include_router(script_router, prefix="/api/v1/reels", tags=["reels"])
app.include_router(video_router, prefix="/api/v1/reels", tags=["reels"])
app.include_router(tts_router, prefix="/api/v1/reels", tags=["reels"])
app.include_router(settings_router, prefix="/api/v1", tags=["settings"])
app.include_router(caption_router, prefix="/api/v1/reels", tags=["reels"])
app.include_router(combine_router, prefix="/api/v1/reels", tags=["reels"])
app.include_router(
    prompt_versions_router,
    prefix="/api/v1/reels",
    tags=["prompt-settings"],
)
app.include_router(
    products_router,
    prefix="/api/v1/reels",
    tags=["products"],
)

@app.get("/health")
def health_check():
    """서버 및 컨테이너 헬스체크용 엔드포인트"""
    return {
        "status": "healthy",
        "message": "FastAPI service is running smoothly!"
    }
