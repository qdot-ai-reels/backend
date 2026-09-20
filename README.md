# Quedot AI Reels Backend

FastAPI 기반의 공구 상품 숏폼(Reels) 생성 백엔드입니다. 상품 정보와 이미지를 입력받아 스크립트, TTS 내레이션, 영상, 자막을 순서대로 생성하고 최종 MP4를 제공합니다.

## 현재 구현 범위

- OpenRouter를 이용한 광고 스크립트 생성 및 JSON 검증
- OpenRouter TTS를 이용한 장면별 음성 생성·길이 검증·MP3 결합
- OpenRouter 영상 생성 및 `ffprobe` 기반 해상도·비율·길이 검증
- 검증 실패 시 영상 재생성, TTS 길이 초과 시 스크립트 재생성
- FFmpeg를 이용한 영상·음성 결합 및 원본 영상 오디오 제거
- HyperFrames 컨테이너를 이용한 자막 애니메이션 렌더링
- PostgreSQL/SQLite를 이용한 전역 설정과 생성 작업 상태 저장
- 설정 API에서 OpenRouter API Key를 Fernet으로 암호화하여 저장
- 로컬 파일 기반 영상 결과 제공

현재 최종 영상과 중간 산출물은 S3가 아니라 `runtime/` 아래에 저장됩니다. `app/core/s3.py`에는 S3 업로드와 presigned URL 유틸리티가 있지만, 현재 생성 파이프라인에 연결된 저장소는 로컬입니다.

## 전체 처리 흐름

```text
POST /api/v1/reels/generate
  → generation_jobs에 PENDING 작업 저장
  → TTS 생성 및 장면별 음성 길이 검증
  → TTS 길이 초과 시 스크립트 재생성 후 재시도
  → OpenRouter 영상 생성·폴링
  → 영상 메타데이터 검증 및 필요 시 재생성
  → 영상과 내레이션 결합
  → HyperFrames 자막 렌더링
  → runtime/final/{job_id}/final.mp4 저장
  → GET 상태 API에서 완료 결과와 파일 URL 제공
```

스크립트 생성 API도 별도 비동기 작업으로 동작합니다. 반면 `/video`, `/tts`, `/combine`, `/caption`은 단계별 테스트·재사용을 위한 독립 API입니다.

## 기술 스택

- Python 3.12
- FastAPI, Pydantic Settings, SQLAlchemy
- PostgreSQL 16 (Docker Compose) 또는 기본 SQLite 로컬 DB
- FFmpeg/ffprobe
- Node.js 22, Chromium, HyperFrames 0.8.12
- OpenRouter Script / TTS / Video API
- AWS S3 유틸리티(Boto3; 현재 메인 생성 흐름에서는 미사용)

## 디렉터리 구조

```text
backend/
├── app/
│   ├── main.py                    # FastAPI 앱, CORS, 라우터, DB 초기화
│   ├── api/v1/                    # HTTP 엔드포인트
│   │   ├── script.py              # 비동기 스크립트 생성
│   │   ├── video.py               # 영상 생성·검증·로컬 파일 제공
│   │   ├── tts.py                 # MP3 내레이션 생성
│   │   ├── combine.py             # MP4 + MP3 결합
│   │   ├── caption.py             # HyperFrames 자막 렌더링
│   │   ├── final_generation.py    # 전체 릴스 파이프라인
│   │   ├── settings.py            # 설정 및 provider catalog API
│   │   └── prompts.py             # 기본 프롬프트 미리보기
│   ├── core/
│   │   ├── config.py              # 환경변수 설정
│   │   └── s3.py                  # S3 업로드·presigned URL 유틸리티
│   ├── db.py                      # SQLAlchemy 모델·초기화·설정 repository
│   ├── generation_jobs.py         # 생성 작업 CRUD 및 상태 메시지
│   ├── script_generator.py        # 스크립트 프롬프트·OpenRouter·검증
│   ├── video_generator.py         # 영상 provider 호출·폴링
│   ├── video_validation_pipeline.py
│   ├── tts_generator.py
│   ├── media_combiner.py
│   ├── hyperframes_caption.py
│   ├── hyperframes_client.py
│   ├── image_metadata.py
│   └── video_metadata.py
├── tests/                         # 외부 API를 mock한 단위/API 테스트
├── scripts/                       # 배포 API fixture 테스트 스크립트
├── runtime/                       # 로컬 생성 산출물; Git에 커밋하지 않음
├── Dockerfile                     # FastAPI + FFmpeg
├── Dockerfile.hyperframes         # Node + Chromium + HyperFrames
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## 실행 전 준비

Docker Desktop이 필요합니다. 애플리케이션 설정 객체가 시작 시 AWS/S3 환경변수를 읽으므로, 현재 로컬 생성 흐름에서 S3를 사용하지 않더라도 아래 AWS 변수는 설정해야 합니다.

`backend/.env`를 직접 만들고 다음 값을 환경에 맞게 입력합니다. `.env`는 커밋하지 않습니다.

```env
# Compose의 db 서비스에 연결하려면 host는 db여야 합니다.
DATABASE_URL=postgresql+psycopg2://postgres:postgres1234@db:5432/app_db

# 현재는 설정 객체 초기화에 필요하며, 메인 생성 파이프라인은 로컬 저장을 사용합니다.
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
AWS_REGION=ap-northeast-2
S3_BUCKET_NAME=your-bucket

# 설정 API에서 API Key를 DB에 암호화해 저장하려는 경우 필수입니다.
# Fernet 키 생성:
# python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
SETTINGS_ENCRYPTION_KEY=your-fernet-key

# 설정 API를 사용하지 않고 환경변수만 사용할 때의 provider 설정
OPENROUTER_SCRIPT_API_KEY=your-script-key
OPENROUTER_TTS_API_KEY=your-tts-key
OPENROUTER_VIDEO_API_KEY=your-video-key
OPENROUTER_SCRIPT_MODEL=google/gemini-3.8-flash
OPENROUTER_TTS_MODEL=your-tts-model
OPENROUTER_TTS_VOICE=your-voice
OPENROUTER_VIDEO_MODEL=bytedance/seedance-2.0-mini

# 선택값
CORS_ORIGINS=http://localhost:3000
# OPENROUTER_API_URL=https://openrouter.ai/api/v1/chat/completions
# OPENROUTER_VIDEO_API_URL=https://openrouter.ai/api/v1/videos
# OPENROUTER_FALLBACK_MODEL=google/gemini-3.8-flash
# OPENROUTER_VIDEO_SUPPORTED_DURATIONS=4,5,6,7,8,9,10,11,12,13,14,15
```

`SETTINGS_ENCRYPTION_KEY`를 설정하면 설정 API가 DB 설정을 사용하고 API Key를 암호화해 저장합니다. 이 값을 바꾸면 기존에 저장된 Key를 복호화할 수 없습니다. 키를 비워 두면 provider API Key·모델은 환경변수 fallback을 사용하며, 설정 API는 사용할 수 없습니다.

`DATABASE_URL`을 생략하면 애플리케이션은 `sqlite:///./quedot.local.db`를 사용합니다. Docker Compose의 PostgreSQL을 사용할 때는 반드시 위처럼 `@db:5432` 주소를 지정합니다.

## Docker 실행

`backend/`에서 실행합니다.

```bash
docker compose -p quedot-reels up -d --build
docker compose -p quedot-reels ps
docker compose -p quedot-reels logs -f web
```

접속 주소:

- Health: <http://localhost:8000/health>
- Swagger: <http://localhost:8000/docs>
- OpenAPI JSON: <http://localhost:8000/openapi.json>
- PostgreSQL: `localhost:5432`

HyperFrames는 `web`과 `hyperframes`가 `runtime/hyperframes`를 공유합니다. 직접 검증하거나 렌더링할 때는 다음 명령을 사용할 수 있습니다.

```bash
docker compose -p quedot-reels run --rm --entrypoint hyperframes hyperframes check /workspace --json

docker compose -p quedot-reels run --rm --entrypoint hyperframes hyperframes render /workspace \
  --output /workspace/output.mp4 --quality draft --workers 1
```

서비스를 중지할 때는 다음을 사용합니다. `-v`를 붙이면 PostgreSQL 볼륨까지 삭제될 수 있으므로 주의합니다.

```bash
docker compose -p quedot-reels down
```

## API 목록

모든 릴스 API의 prefix는 `/api/v1/reels`입니다.

| Method | Path | 설명 |
| --- | --- | --- |
| GET | `/health` | FastAPI 프로세스 상태 확인. DB 연결까지 확인하지는 않음 |
| POST | `/reels/script` | 스크립트 생성 작업 시작, `202` 반환 |
| GET | `/reels/script/{job_id}` | 스크립트 작업 상태·결과 조회 |
| POST | `/reels/video` | 영상 생성·검증·로컬 저장. 완료까지 요청이 유지됨 |
| GET | `/reels/video/{job_id}/url` | 로컬 영상 URL 메타데이터 조회 |
| GET | `/reels/video/{job_id}/file` | 검증 완료 영상 재생 또는 다운로드 |
| POST | `/reels/tts` | 스크립트 전체 내레이션 MP3 반환 |
| POST | `/reels/combine` | `video` MP4와 `audio` MP3 multipart 결합 |
| GET | `/reels/combine/{job_id}/file` | 결합 영상 재생 또는 다운로드 |
| POST | `/reels/caption` | 공유 작업 디렉터리의 MP4에 HyperFrames 자막 적용 |
| GET | `/reels/caption/{job_id}/file` | 자막 영상 재생 또는 다운로드 |
| POST | `/reels/generate` | TTS→영상→결합→자막 전체 작업 시작, `202` 반환 |
| GET | `/reels/generate/{job_id}` | 전체 작업 상태·결과 조회 |
| GET | `/reels/generate/{job_id}/file` | 완료된 최종 MP4 재생 또는 다운로드 |
| GET | `/settings` | 저장된 설정의 공개 정보 조회(API Key 원문 미반환) |
| PUT | `/settings` | 모델·음성·재시도·API Key 설정 저장 |
| GET | `/settings/openrouter/models` | OpenRouter 텍스트 모델 목록 |
| GET | `/settings/openrouter/video-models` | 영상 모델 capability 목록 |
| GET | `/settings/openrouter-tts/models` | TTS 모델 목록 |
| GET | `/settings/openrouter-tts/voices` | TTS voice 목록 |
| GET | `/reels/prompts` | 실제 생성에 사용하는 기본 프롬프트 미리보기 |

### 전체 릴스 생성 요청 예시

`POST /api/v1/reels/generate`는 이미 생성된 `script`와 상품 `product`를 함께 받습니다. `image_url`은 product 안에 있거나 최상위에 있어야 하고, `influencer_image_url`은 필수입니다.

```json
{
  "product": {
    "product_id": "product-001",
    "name": "아기 주방세제",
    "image_url": "https://example.com/product.jpg",
    "selling_points": ["EWG 그린등급", "비건 인증"]
  },
  "script": {
    "meta": {"output_format_version": "1.0", "language": "ko"},
    "product": {"usp": "아기 식기에 사용할 수 있는 순한 세정 성분"},
    "customer": {"main_target": "육아 보호자", "pain_point": "아기 식기 세정 성분이 걱정됨"},
    "ads": {
      "goal": "상품 판매",
      "cta_action": "링크 확인",
      "channel_platform": "Instagram Reels",
      "ad_planner": {"persona": "생활용품 광고 기획자"},
      "speaker": {"persona": "친근한 육아 보호자", "tone": "친근하고 명확한 말투"}
    },
    "video": {"video_duration": 6},
    "etc": {"additional_information": null, "video_ads_methodology": null},
    "scenes": [
      {
        "section": "Hook",
        "time_range_sec": {"start": 0, "end": 3},
        "visual": "상품을 화면 중앙에 보여준다.",
        "auditory": {
          "subtitle": "아기 식기, 아무거나 쓰고 계신가요?",
          "voiceover": "아기 식기, 아무거나 쓰고 계신가요?"
        },
        "intent": "시청자의 문제의식을 빠르게 환기한다.",
        "exclusion_list_about_physical_motions": "손과 상품이 갑자기 사라지거나 형태가 변하지 않도록 한다.",
        "notes": null
      },
      {
        "section": "Body",
        "time_range_sec": {"start": 3, "end": 6},
        "visual": "상품 패키지와 사용 장면을 세로 화면에 보여준다.",
        "auditory": {
          "subtitle": "순한 성분으로 매일 안심하고 사용하세요.",
          "voiceover": "순한 성분으로 매일 안심하고 사용하세요."
        },
        "intent": "상품의 핵심 장점을 전달한다.",
        "exclusion_list_about_physical_motions": "제품 라벨과 용기가 왜곡되거나 손가락이 비정상적으로 움직이지 않도록 한다.",
        "notes": null
      }
    ]
  },
  "image_url": "https://example.com/product.jpg",
  "influencer_image_url": "https://example.com/influencer.jpg",
  "max_duration_seconds": 6,
  "allow_script_regeneration": true
}
```

응답은 먼저 작업 ID만 반환합니다.

```json
{
  "job_id": "generated-job-id",
  "status": "PENDING",
  "status_url": "/api/v1/reels/generate/generated-job-id"
}
```

상태 조회 결과에는 `status`, `stage`, `error`, `error_code`, `retryable`, `cost`, `elapsed_seconds`가 포함될 수 있습니다. 완료 시 `video_url`과 `download_url`이 추가됩니다.

주요 상태와 단계는 다음과 같습니다.

```text
status: PENDING → PROCESSING → COMPLETED 또는 FAILED
stage: QUEUED
       SCRIPT_GENERATION / SCRIPT_REGENERATION
       TTS_GENERATION / TTS_VALIDATION
       VIDEO_GENERATION
       AUDIO_MERGE
       CAPTION_RENDER
       COMPLETED
```

### 단계별 요청 예시

스크립트 생성:

```json
{
  "product": {"name": "아기 주방세제", "image_url": "https://example.com/product.jpg"},
  "image_url": "https://example.com/product.jpg",
  "max_duration_seconds": 6,
  "channel": "Instagram Reels",
  "target_audience": "육아에 관심 있는 보호자",
  "use_default_prompt": true
}
```

영상 생성은 `script`, `image_url`, `influencer_image_url`가 필요하며 기본 화면 비율은 `9:16`입니다. TTS는 `{ "script": { ... } }`를 받아 `audio/mpeg` 응답을 반환합니다. 결합 API는 multipart form-data의 `video`와 `audio` 필드를 사용합니다. 자막 API는 `{ "script": { ... }, "video_filename": "job-id/combined.mp4" }`를 사용하며, 파일은 HyperFrames 공유 workspace 안에 있어야 합니다.

## 데이터베이스

애플리케이션 시작 시 `Base.metadata.create_all()`을 호출해 다음 테이블을 준비합니다. 별도의 Alembic 마이그레이션은 현재 사용하지 않습니다.

- `global_settings`: provider API Key 암호문, 모델, 음성, 해상도, 최대 길이, 재시도 횟수, 원본 오디오 음소거 설정
- `generation_jobs`: 작업 ID, 상태, 단계, 상품/스크립트 JSON, provider 작업 ID, 출력 경로, 오류, 비용, 생성·수정 시각

`init_db()`에는 이전 컬럼명과 누락 컬럼을 보정하는 간단한 로컬 DB upgrade 로직도 포함되어 있습니다. 운영 환경에서는 schema migration 도구를 별도로 도입하는 것이 좋습니다.

## 로컬 산출물

```text
runtime/
├── tts/{job_id}/narration.mp3
├── videos/{provider_job_id}/final.mp4
├── combined/{job_id}/combined.mp4 또는 final.mp4
├── final/{job_id}/final.mp4
└── hyperframes/{job_id}/combined.mp4, index.html, final.mp4
```

`runtime/`은 `.gitignore`에 등록되어 있습니다. 생성 영상·음성·HyperFrames 입력물을 Git에 커밋하지 마세요.

## 테스트

외부 OpenRouter 호출은 mock으로 대체한 20개 unittest 파일이 있습니다. FFmpeg가 필요한 미디어 테스트가 포함되어 있으므로 로컬 실행 시 FFmpeg/ffprobe가 PATH에 있어야 합니다.

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

배포된 API를 대상으로 하는 fixture 스크립트도 있습니다. 스크립트 fixture는 외부 호출 비용이 발생할 수 있고, 영상 fixture는 스크립트와 영상 생성 비용이 모두 발생하므로 소량만 실행합니다.

```bash
python scripts/run_script_fixture_test.py --base-url http://localhost:8000 --count 1
python scripts/run_video_fixture_test.py --base-url http://localhost:8000
```

fixture 스크립트의 기본 상품 파일 경로는 이 복사본에 포함되어 있지 않을 수 있으므로, 필요하면 `--source`로 별도 JSON을 지정합니다. 실제 provider API가 호출되지 않은 경우 테스트 성공으로 외부 API가 검증되었다고 간주하지 않습니다.

## 보안 및 운영 주의사항

- `.env`, AWS credential, OpenRouter API Key, `SETTINGS_ENCRYPTION_KEY`는 커밋하지 않습니다.
- `SETTINGS_ENCRYPTION_KEY`는 DB에 저장된 API Key 복호화에 필요하므로 운영 환경에서 고정합니다.
- 현재 영상 파일은 로컬 디스크에 저장되며 만료·삭제 정책이 없습니다. 운영 배포 전 S3 업로드, 접근 제어, 보존 기간을 연결해야 합니다.
- `/health`는 프로세스 상태만 확인하며 PostgreSQL·OpenRouter·HyperFrames 연결 상태를 종합 검사하지 않습니다.
- `/video`와 `/generate`는 외부 영상 생성 요청을 발생시킬 수 있습니다. 개발 중에는 실제 provider 호출과 비용을 확인한 뒤 실행합니다.
