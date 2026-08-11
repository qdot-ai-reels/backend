from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from app.core.s3 import generate_presigned_url

router = APIRouter()

# 응답 스키마 정의 (응답 포맷 고정)
class ReelsGenerateResponse(BaseModel):
    status: str
    message: str
    video_url: str

@router.post(
    "/generate", 
    response_model=ReelsGenerateResponse,
    status_code=status.HTTP_200_OK,
    summary="숏폼 영상 생성 및 S3 URL 반환"
)
def generate_reels():
    try:
        # 더미 영상 Presigned URL 발급 (유효시간 15분)
        presigned_url = generate_presigned_url(object_key="dummy_reels.mp4", expiration=900)
        
        return ReelsGenerateResponse(
            status="success",
            message="영상 생성 성공 (Dummy Data)",
            video_url=presigned_url
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )