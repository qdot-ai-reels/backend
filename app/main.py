from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os
# reels 라우터 import
from app.api.v1.reels import router as reels_router
from app.api.v1.script import router as script_router
from app.api.v1.video import router as video_router
from app.api.v1.tts import router as tts_router
from app.api.v1.settings import router as settings_router

app = FastAPI(
    title="Shorts Reels Generator API",
    version="1.0.0"
)

# CORS 설정
cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000,*").split(",")
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

@app.get("/health")
def health_check():
    """서버 및 컨테이너 헬스체크용 엔드포인트"""
    return {
        "status": "healthy",
        "message": "FastAPI service is running smoothly!"
    }
