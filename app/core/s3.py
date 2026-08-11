import logging
import boto3
from botocore.exceptions import ClientError
from app.core.config import settings

logger = logging.getLogger(__name__)

def generate_presigned_url(object_key: str = "dummy_reels.mp4", expiration: int = 900) -> str:
    """
    S3 객체 접근을 위한 Presigned URL 발급
    :param object_key: S3 버킷 내 파일 경로
    :param expiration: URL 유효 시간 (초 단위, 기본 15분=900초)
    """
    s3_client = boto3.client(
        's3',
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION,
        config=boto3.session.Config(signature_version='s3v4') # AWS 최신 보안 서명 V4 적용
    )

    try:
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': settings.S3_BUCKET_NAME,
                'Key': object_key
            },
            ExpiresIn=expiration
        )
        return url
    except ClientError as e:
        logger.error(f"S3 Presigned URL 발급 실패: {e}")
        raise ValueError("S3 URL 생성 중 오류가 발생했습니다.")