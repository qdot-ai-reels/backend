# Quedot Production Reels Backend

FastAPI, PostgreSQL, OpenRouter, FFmpeg, HyperFrames로 여러 개의 1080x1920
릴스 후보를 생성·검수·저장하는 백엔드입니다.

---

## 📁 프로젝트 구조

```text
backend/
├── app/
│   ├── __init__.py
│   └── main.py
├── tests/
│   └── __init__.py
├── .dockerignore
├── .env.example
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

---

## 시작 가이드

### 1. 환경변수

workspace 루트(`reels-george/.env`)의 공용 키 한 개만으로 세 provider client를
시작할 수 있습니다. 파일을 복사하거나 Git에 추가하지 마세요.

```bash
OPENROUTER_API_KEY=...
```

필요하면 `backend/.env`의 `OPENROUTER_SCRIPT_API_KEY`,
`OPENROUTER_TTS_API_KEY`, `OPENROUTER_VIDEO_API_KEY`로 용도별 키를 덮어쓸 수
있습니다. 기본 모델은 `openai/gpt-5.4-mini`, `google/gemini-3.1-flash-tts-preview`,
`bytedance/seedance-2.0`입니다.

### 2. Docker 컨테이너 실행
```bash
cd backend
docker compose -p quedot-reels up -d --build
```

기본적으로 funded API와 PostgreSQL host port는 각각
`127.0.0.1:${BACKEND_HOST_PORT:-8000}`과
`127.0.0.1:${POSTGRES_HOST_PORT:-55432}`에만 공개됩니다. 외부 공개가
필요한 운영 환경에서는 인증과 요청별 예산 제한을 갖춘 reverse proxy 뒤에 두세요.

### HyperFrames 컨테이너

HyperFrames 0.8.27은 Node.js 22, Chromium, FFmpeg가 포함된 전용 컨테이너에서
실행됩니다. 컨테이너 내부 runner가 FastAPI의 렌더링 요청을 받아 HyperFrames
CLI를 실행합니다. 이미지 재현성을 위해 runtime 자동 업데이트는 비활성화합니다.

FastAPI와 HyperFrames는 `runtime/hyperframes` 디렉터리를 공유합니다.
FastAPI가 입력 HTML과 영상을 이 경로에 저장하면 HyperFrames 컨테이너가
`/workspace`에서 읽고 최종 MP4를 같은 경로에 출력할 수 있습니다.

```bash
# 이미지 빌드 및 전체 서비스 실행
docker compose -p quedot-reels up -d --build

# composition 검사
# 수동 composition 검사
docker compose -p quedot-reels run --rm --entrypoint hyperframes hyperframes check /workspace --json

# MP4 렌더링
# 수동 MP4 렌더링
docker compose -p quedot-reels run --rm --entrypoint hyperframes hyperframes render /workspace \
  --output /workspace/output.mp4 --quality high --video-bitrate 10M --fps 30 --strict --workers 1
```

FastAPI의 `POST /api/v1/reels/caption`에 스크립트와 공유 작업 디렉터리 내의
결합 MP4 파일명을 전달하면, runner가 자막이 포함된 최종 MP4를 생성합니다.

### 3. 서비스 동작 확인
컨테이너가 정상 구동되면 아래 주소로 접속하여 결과를 확인합니다.
* **Health Check:** `http://localhost:${BACKEND_HOST_PORT:-8000}/health`
* **Swagger API Docs (API 문서):** `http://localhost:${BACKEND_HOST_PORT:-8000}/docs`
* **PostgreSQL DB 접속:** `127.0.0.1:${POSTGRES_HOST_PORT:-55432}`

## OpenRouter 스크립트 생성

### 설정 암호화 키

설정 화면에서 저장하는 OpenRouter API Key는 DB에 암호화하여 저장합니다.
암호화 키 자체는 DB나 Git에 저장하지 않고 환경변수로 관리합니다.

```bash
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

출력된 값을 `.env`의 `SETTINGS_ENCRYPTION_KEY`에 저장한 뒤 서버를 시작합니다.
이 값은 운영 환경에서도 동일하게 유지해야 기존에 저장한 API Key를 복호화할 수 있습니다.

`app/script_generator.py`의 `OpenRouterClient`는 공구 상품 정보를 받아
권예빈 문서 형식의 구조화된 광고 스크립트 JSON을 생성합니다.

필요한 환경변수:

```bash
OPENROUTER_API_KEY=공용_키
OPENROUTER_SCRIPT_MODEL=openai/gpt-5.4-mini
OPENROUTER_TTS_MODEL=google/gemini-3.1-flash-tts-preview
OPENROUTER_TTS_VOICE=Aoede
OPENROUTER_VIDEO_MODEL=bytedance/seedance-2.0
```

사용 예시:

```python
from app.script_generator import OpenRouterClient, ScriptGenerationRequest

request = ScriptGenerationRequest(
    product={
        "brand_name": "프랭클린",
        "product_name": "아기 주방세제",
        "price": 22900,
        "discount_rate": 49,
        "selling_points": ["EWG 그린등급", "비건 인증"],
        "image_url": "https://example.com/product.jpg",
    }
)
script = OpenRouterClient.from_env().generate_script(request)
```

FastAPI가 실행 중이면 Postman이나 Swagger에서 다음 API로도 호출할 수 있습니다.

```text
POST /api/v1/reels/script
Content-Type: application/json
```

요청 본문 예시:

```json
{
  "product": {
    "brand_name": "프랭클린",
    "product_name": "아기 주방세제",
    "price": 22900,
    "discount_rate": 49,
    "selling_points": ["EWG 그린등급", "비건 인증"],
    "image_url": "https://example.com/product.jpg"
  },
  "max_duration_seconds": 30,
  "channel": "Instagram Reels",
  "target_audience": "육아에 관심 있는 보호자"
}
```

클라이언트는 모델 응답에서 JSON을 추출한 뒤 `scenes`, 장면 시간 범위,
화면 설명, 자막을 검증합니다. 외부 API 호출은 네트워크와 계정의 모델별
요청 제한 영향을 받으므로 자동화 테스트에서는 실제 API를 호출하지 않습니다.

## OpenRouter 영상 생성

스크립트 JSON과 상품 이미지 URL을 보내면 OpenRouter 영상 생성 작업을 요청하고,
완료될 때까지 상태를 확인한 뒤 결과 영상 URL과 비용을 반환합니다.

```text
POST /api/v1/reels/video
Content-Type: application/json
```

요청 본문 예시:

```json
{
  "script": {
    "meta": {
      "output_format_version": "1.0",
      "framework": "Hook-Body-CTA",
      "language": "ko"
    },
    "summary": {
      "main_target": "육아에 관심 있는 보호자",
      "pain_point": "상품 선택이 어려움",
      "product_usp": "상품의 핵심 장점",
      "key_message": "상품의 핵심 메시지",
      "tone_and_manner": "생활형 광고"
    },
    "scenes": [
      {
        "scene_name": "Hook",
        "time_range_sec": {"start": 0, "end": 3},
        "visual": "상품을 화면 중앙에 보여준다.",
        "auditory": {
          "subtitle": "상품 소개",
          "voiceover": "상품을 소개합니다."
        },
        "notes": "상품을 먼저 보여준다."
      }
    ],
    "compliance_notes": {
      "avoid": [],
      "focus": []
    }
  },
  "image_url": "https://example.com/product.jpg",
  "resolution": "1080p",
  "aspect_ratio": "9:16",
  "generate_audio": false
}
```

기본 영상 모델은 `OPENROUTER_VIDEO_MODEL` 환경변수로 변경할 수 있습니다.
영상 길이는 요청 본문에서 따로 입력하지 않습니다. 백엔드가 스크립트의 마지막 장면
종료 시간을 읽어 OpenRouter의 `duration` 값으로 전달합니다. 스크립트 형식이
올바르지 않으면 외부 API를 호출하지 않습니다.
기본 모델이 지원하는 길이는 `4~15초`입니다. 지원하지 않는 길이의
스크립트는 API 호출 전에 차단하며, 긴 영상은 장면 분할·결합 또는 해당 길이를
지원하는 다른 모델이 필요합니다.

## Production 후보 생성 API

`POST /api/v1/reels/generate`는 `candidate_count` 1~4(기본 3)를 받아 narration을
한 번 만든 뒤 후보를 각각 생성합니다. 자동 유료 재시도는 하지 않으며 실패한
후보는 명시적인 retry API로만 다시 생성합니다.

```json
{
  "product": {"name": "상품", "image_url": "https://cdn.example.com/product.jpg"},
  "script": {"meta": {}, "summary": {}, "scenes": []},
  "candidate_count": 3,
  "influencer_image_urls": ["https://cdn.example.com/person-portrait.jpg"]
}
```

인플루언서 레퍼런스는 생략할 수 있습니다. 전달할 때에는 한 사람만 포함된
세로/정사각 이미지 최대 2장이어야 합니다. 가로형 콘택트시트는 거부됩니다.
요청값이 없으면 선택적으로 `INFLUENCER_REFERENCE_URLS`를 사용합니다.

- 상태: `GET /api/v1/reels/generate/{job_id}`
- 후보 파일: `GET /api/v1/reels/generate/{job_id}/candidates/{candidate_id}/file`
- 후보 재시도: `POST /api/v1/reels/generate/{job_id}/candidates/{candidate_id}/retry`

최종 후보마다 provider 검수와 caption render 이후 최종 검수를 모두 저장합니다.
최종 검수는 1080x1920 이상, 9:16, 24fps 이상, H.264/HEVC, 2.5Mbps 이상,
길이 오차 0.25초 이하, black-frame 비율 3% 이하를 요구합니다.

## 이미지 입력 보안

서버가 검사하는 모든 원격 이미지는 HTTPS와 공개 IP만 허용합니다. localhost,
사설/link-local/reserved 주소와 안전하지 않은 redirect는 거부하고 다운로드를
25MB로 제한합니다. 운영에서는 다음처럼 provider-fetch 가능한 CDN 호스트를
명시적으로 제한하세요.

```bash
ALLOWED_IMAGE_HOSTS=cdn.example.com,*.trusted-cdn.example
CORS_ORIGINS=https://reels.example.com
```

## Worker 경계

Studio의 4/6/8/15초 템플릿, 생성 전 견적, 관리 목록, one-click 생성과
멱등성 계약은 [`docs/studio-workflow-contract.md`](docs/studio-workflow-contract.md)에
정리되어 있습니다.

현재 기본 dispatcher는 단일 호스트용 FastAPI `BackgroundTasks` adapter입니다.
요청 payload와 후보 상태는 DB에 저장되며 worker 함수가 HTTP 계층과 분리되어
있습니다. 다중 인스턴스 운영 전에는 이 adapter를 Celery/RQ/SQS 같은 durable
queue로 교체해야 합니다. 프로세스 종료 시 실행 중 Python task 자체는 복구되지
않으므로 단일 호스트에서도 무중단 배포 전에 실행 중 job을 확인해야 합니다.
