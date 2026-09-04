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
시작할 수 있습니다. 실제 값이 든 `.env`를 외부로 공유하거나 Git에 추가하지 마세요. 상위
`tools/start_local_stack.sh`는 루트 `.env`만 shell에 source하므로 이 실행 방식에서
사용할 용도별 키와 모델 설정도 모두 루트 `.env`에 둡니다.

```bash
OPENROUTER_API_KEY=
```

필요하면 `OPENROUTER_SCRIPT_API_KEY`, `OPENROUTER_TTS_API_KEY`,
`OPENROUTER_VIDEO_API_KEY`로 용도별 키를 덮어쓸 수 있습니다. `backend/.env`는
Docker Compose가 container에 주입하거나 사용자가 명시적으로 source한 실행에서만
용도별 변수가 process에 들어갑니다. 파일만 만들어 둔 채 direct uvicorn 또는 상위
통합 script가 자동으로 읽는다고 가정하면 안 됩니다. 기본 모델은
`openai/gpt-5.4-mini`, `google/gemini-3.1-flash-tts-preview`,
`bytedance/seedance-2.0`입니다.

### 2. Docker 컨테이너 실행
```bash
cd backend
docker compose --env-file ../.env -p quedot-reels up -d --build
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
docker compose --env-file ../.env -p quedot-reels up -d --build

# 실제 runtime/hyperframes 하위의 job project ID를 지정
export HYPERFRAMES_PROJECT_ID="<actual-project-id>"
test -d "runtime/hyperframes/$HYPERFRAMES_PROJECT_ID"

# 수동 composition 검사
docker compose --env-file ../.env -p quedot-reels run --rm --entrypoint hyperframes \
  hyperframes check "/workspace/$HYPERFRAMES_PROJECT_ID" --json --strict

# 수동 MP4 렌더링
docker compose --env-file ../.env -p quedot-reels run --rm --entrypoint hyperframes \
  hyperframes render "/workspace/$HYPERFRAMES_PROJECT_ID" \
  --output "/workspace/$HYPERFRAMES_PROJECT_ID/output.mp4" \
  --quality high --video-bitrate 10M --fps 30 --strict --workers 1
```

`/workspace` 루트 자체는 composition project가 아니다. 실제 runner도 요청의 `project_id`에 해당하는
`/workspace/<project_id>`만 검사하고 렌더링한다.

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
OPENROUTER_API_KEY=
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
        "time_range_sec": {"start": 0, "end": 4},
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

이 direct `/video` 경로는 DB 설정이 없으면 검증 실패 시 2회 자동 재시도하므로 최초 요청을 포함해
최대 3개의 유료 provider 생성이 발생할 수 있습니다. Studio의 `/generate` 경로는 후보별 자동
유료 재시도를 0으로 고정합니다. 후보 retry도 기존 견적에 비용 포함과 허용 횟수가 명시된 경우만
실행되며, 현재 일반 생성 견적은 `candidate_retry_policy`를 0/비포함으로 반환합니다.

## 광고 상품 카탈로그

Studio의 템플릿 생성은 서버 카탈로그에서 활성 상태로 검수된 상품만 사용합니다.
새 상품은 항상 비활성 상태로 등록되며, 원격 이미지의 HTTPS·공개 IP·형식·크기·
해상도 검증이 끝난 뒤에도 운영자가 상품 의미와 표시 내용의 일치를 별도로 확인해야
활성화할 수 있습니다.

현재 상품 API 자체에는 운영자 인증이 없습니다. 공개 production 전에는 인증 reverse
proxy 또는 서버 권한 계층에서 아래 등록·수정·활성화·비활성화·보관 mutation에 상품
operator RBAC를 강제해야 합니다. UI에서 관리 메뉴를 숨기는 것은 권한 통제가 아닙니다.

```text
GET    /api/v1/reels/products?include_inactive=true
POST   /api/v1/reels/products
PUT    /api/v1/reels/products/{product_id}
POST   /api/v1/reels/products/{product_id}/activate
POST   /api/v1/reels/products/{product_id}/deactivate
DELETE /api/v1/reels/products/{product_id}?expected_revision={revision}
```

등록은 `name`, `image_url`이 필수이고 `product_id`는 생략하면 생성됩니다. 수정과
상태 변경에는 목록에서 받은 `expected_revision`이 필요합니다. 수정된 상품은 자동
비활성화되며, 활성화 요청은 다음처럼 명시적 검수 확인과 메모를 남깁니다.

```json
{
  "expected_revision": 2,
  "asset_review_acknowledged": true,
  "review_note": "대표 상품, 옵션, 라벨과 이미지가 일치함을 확인"
}
```

`DELETE`는 DB 행을 제거하지 않고 보관 처리합니다. 이미 보관된 상품에 같은 요청을 다시
보내면 stale `expected_revision`이어도 멱등 성공으로 현재 상품을 HTTP 200 반환합니다. 그 밖의
stale mutation은 `PRODUCT_REVISION_CONFLICT` 409를 반환합니다. 활성화 API로 재검수 후 복구할
수 있으며 기존 생성 작업은 요청 당시 상품 snapshot을 계속 보존합니다.

## Production 후보 생성 API

`POST /api/v1/reels/generate`는 `candidate_count` 1~4(생략 시 기본 1)를 받아 narration을
한 번 만든 뒤 후보를 각각 생성합니다. 자동 유료 재시도는 하지 않습니다. 실패한
후보의 retry API는 상품 상태와 견적의 유료 재시도 허용량을 다시 확인합니다.

Studio의 `template_id` 생성 요청은 상품 목록 응답의 `raw_product`, `image_url`,
`revision`을 각각 `product`, `image_url`, `product_catalog_revision`으로 전송해야
합니다. 서버는 활성/보관 상태와 revision을 다시 확인하고 카탈로그의 정본 데이터로
prompt 입력을 구성합니다. 요청을 `ACCEPTED`로 저장하는 같은 트랜잭션에서도 상품 행을
잠그고 active/revision CAS를 반복합니다. 변경된 상품은 `PRODUCT_CATALOG_CHANGED`, 비활성 또는
보관 상품은 `PRODUCT_UNAVAILABLE` 409 응답으로 생성 전에 차단됩니다. 기존 script를
직접 제공하는 저수준 호출은 호환성을 위해 카탈로그 적용 대상에서 제외됩니다.

```json
{
  "product": {"name": "상품", "image_url": "https://cdn.example.com/product.jpg"},
  "script": {
    "meta": {"output_format_version": "1.0", "framework": "Hook-CTA", "language": "ko"},
    "summary": {
      "main_target": "육아에 관심 있는 보호자",
      "pain_point": "상품 선택이 어렵다",
      "product_usp": "간편하게 사용할 수 있다",
      "key_message": "상품의 핵심 장점",
      "tone_and_manner": "생활형 광고"
    },
    "scenes": [
      {
        "scene_name": "Hook + CTA",
        "time_range_sec": {"start": 0, "end": 4},
        "visual": "상품을 화면 중앙에 보여준다.",
        "auditory": {"subtitle": "지금 확인하세요", "voiceover": "필요했던 상품, 지금 확인해 보세요."},
        "notes": "상품 형태와 라벨을 유지한다."
      }
    ],
    "compliance_notes": {"avoid": [], "focus": []}
  },
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

Studio 후보 재시도는 기존 견적의 `candidate_retry_policy.cost_included_in_total=true`와
양수 `authorized_paid_retries`가 모두 필요합니다. 허용 횟수는 모든 후보 합계로 원자적으로
차감하며, 현재 기본 견적처럼 비용이 포함되지 않은 경우
`PAID_RETRY_QUOTE_REQUIRED` 409를 반환합니다. 재시도 예약과 worker의 유료 provider 호출
직전에도 상품 active/revision을 확인하며 각각 `PRODUCT_UNAVAILABLE` 또는
`PRODUCT_CATALOG_CHANGED` 409/후보 오류로 종료합니다. `template_id`가 없는 기존 저수준
작업의 retry 동작은 호환성을 위해 그대로 유지됩니다.

최종 후보마다 provider 검수와 caption render 이후 최종 검수를 모두 저장합니다.
최종 검수는 1080x1920 이상, 9:16, 24fps 이상, H.264/HEVC, 2.5Mbps 이상,
길이 오차 0.25초 이하, black-frame 비율 3% 이하를 요구합니다.

## 이미지 입력 보안

서버가 검사하는 모든 원격 이미지는 HTTPS와 공개 IP만 허용합니다. localhost,
사설/link-local/reserved 주소와 안전하지 않은 redirect는 거부하고 다운로드를
15 MiB로 제한합니다. 지원 형식은 JPEG, PNG, WebP이며 카탈로그 등록 이미지는 각 변이
512px 이상, 최대 4:1 비율이어야 합니다. 대표 이미지뿐 아니라 모든 상세 이미지가 이
기준을 통과해야 하며 하나라도 부적합하면 등록 또는 이미지 수정 전체를 거부합니다.
운영에서는 다음처럼 provider-fetch 가능한 CDN 호스트를 명시적으로 제한하세요.

```bash
ALLOWED_IMAGE_HOSTS=cdn.example.com,*.trusted-cdn.example
CORS_ORIGINS=https://reels.example.com
```

로컬에서는 `CORS_ORIGINS=http://localhost:3000` 하나만 지정해도 같은 포트의
`http://127.0.0.1:3000`을 함께 허용합니다. 이 보정은 loopback 호스트에만 적용되며
운영 도메인은 설정한 origin 외로 확장하지 않습니다.

## Worker 경계

Studio의 4/6/8/15초 템플릿, 생성 전 견적, 관리 목록, one-click 생성과
멱등성 계약은 [`docs/studio-workflow-contract.md`](docs/studio-workflow-contract.md)에
정리되어 있습니다.

현재 기본 dispatcher는 단일 호스트용 FastAPI `BackgroundTasks` adapter입니다.
요청 payload와 후보 상태는 DB에 저장되며 worker 함수가 HTTP 계층과 분리되어
있습니다. 다중 인스턴스 운영 전에는 이 adapter를 Celery/RQ/SQS 같은 durable
queue로 교체해야 합니다. 프로세스 종료 시 실행 중 Python task 자체는 복구되지
않으므로 단일 호스트에서도 무중단 배포 전에 실행 중 job을 확인해야 합니다.
