import logging
import mimetypes
from pathlib import Path
from typing import BinaryIO, Optional, Union

import boto3
from botocore.exceptions import ClientError
from app.core.config import settings

logger = logging.getLogger(__name__)


def get_s3_client():
    """S3 공통 클라이언트 인스턴스 반환 (Signature V4 적용)"""
    return boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION,
        endpoint_url=f"https://s3.{settings.AWS_REGION}.amazonaws.com",
        config=boto3.session.Config(signature_version="s3v4"),
    )


def generate_presigned_url(object_key: str = "dummy_reels.mp4", expiration: int = 900) -> str:
    """
    S3 객체 접근(조회/재생)을 위한 Presigned GET URL 발급
    :param object_key: S3 버킷 내 파일 경로 (예: jobs/{job_id}/final.mp4)
    :param expiration: URL 유효 시간 (초 단위, 기본 15분=900초)
    """
    s3_client = get_s3_client()
    try:
        url = s3_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings.S3_BUCKET_NAME,
                "Key": object_key,
            },
            ExpiresIn=expiration,
        )
        return url
    except ClientError as e:
        logger.error(f"S3 Presigned URL 발급 실패 ({object_key}): {e}")
        raise ValueError("S3 URL 생성 중 오류가 발생했습니다.")


def upload_file_to_s3(
    file_path: Union[str, Path],
    object_key: str,
    content_type: Optional[str] = None,
) -> str:
    """
    로컬 디스크의 파일(중간 영상, 오디오, 최종 영상)을 S3에 업로드
    :param file_path: 로컬 파일 경로
    :param object_key: S3 저장 경로 (예: jobs/{job_id}/final.mp4)
    :param content_type: MIME 타입 (미지정 시 확장자 기반 자동 추론)
    :return: S3 Object Key
    """
    s3_client = get_s3_client()
    path_obj = Path(file_path)

    if not path_obj.exists():
        raise FileNotFoundError(f"업로드할 로컬 파일이 존재하지 않습니다: {file_path}")

    # Content-Type 자동 판별 (video/mp4, audio/mpeg 등)
    if not content_type:
        guessed_type, _ = mimetypes.guess_type(str(path_obj))
        content_type = guessed_type or "application/octet-stream"

    try:
        s3_client.upload_file(
            Filename=str(path_obj),
            Bucket=settings.S3_BUCKET_NAME,
            Key=object_key,
            ExtraArgs={"ContentType": content_type},
        )
        logger.info(f"S3 파일 업로드 성공: s3://{settings.S3_BUCKET_NAME}/{object_key}")
        return object_key
    except ClientError as e:
        logger.error(f"S3 파일 업로드 실패 ({file_path} -> {object_key}): {e}")
        raise ValueError(f"S3 파일 업로드 중 오류가 발생했습니다: {e}")


def upload_bytes_to_s3(
    data: bytes,
    object_key: str,
    content_type: str = "application/octet-stream",
) -> str:
    """
    메모리 상의 바이너리 데이터(TTS 생성 바이트 등)를 파일 저장 없이 S3에 직접 업로드
    :param data: 업로드할 바이트 데이터
    :param object_key: S3 저장 경로 (예: jobs/{job_id}/tts/scene_1.mp3)
    :param content_type: MIME 타입 (예: audio/mpeg, video/mp4)
    :return: S3 Object Key
    """
    s3_client = get_s3_client()
    try:
        s3_client.put_object(
            Bucket=settings.S3_BUCKET_NAME,
            Key=object_key,
            Body=data,
            ContentType=content_type,
        )
        logger.info(f"S3 바이트 업로드 성공: s3://{settings.S3_BUCKET_NAME}/{object_key}")
        return object_key
    except ClientError as e:
        logger.error(f"S3 바이트 업로드 실패 ({object_key}): {e}")
        raise ValueError(f"S3 데이터 업로드 중 오류가 발생했습니다: {e}")


