# Backend Local Development Environment

FastAPI + PostgreSQL 로컬 개발용 Docker 환경입니다.

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

### 1. 환경변수 파일 생성
`.env.example` 파일을 복사하여 `.env` 파일을 생성하고 필요한 설정을 채워 넣습니다.
```bash
cp .env.example .env
```

### 2. Docker 컨테이너 실행
```bash
docker compose -p quedot-reels up -d --build
```

### HyperFrames 컨테이너

HyperFrames는 Node.js 22, Chromium, FFmpeg가 포함된 전용 컨테이너에서
실행됩니다. 컨테이너 내부 runner가 FastAPI의 렌더링 요청을 받아 HyperFrames
CLI를 실행합니다.

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
  --output /workspace/output.mp4 --quality draft --workers 1
```

FastAPI의 `POST /api/v1/reels/caption`에 스크립트와 공유 작업 디렉터리 내의
결합 MP4 파일명을 전달하면, runner가 자막이 포함된 최종 MP4를 생성합니다.

### 3. 서비스 동작 확인
컨테이너가 정상 구동되면 아래 주소로 접속하여 결과를 확인합니다.
* **Health Check:** http://localhost:8000/health
* **Swagger API Docs (API 문서):** http://localhost:8000/docs
* **PostgreSQL DB 접속:** `localhost:5432`

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
OPENROUTER_SCRIPT_API_KEY=스크립트_생성용_키
OPENROUTER_TTS_API_KEY=TTS_생성용_키
OPENROUTER_VIDEO_API_KEY=영상_생성용_키
OPENROUTER_SCRIPT_MODEL=openai/gpt-oss-20b:free
OPENROUTER_TTS_MODEL=fish-audio/s2.1-pro-free:free
OPENROUTER_TTS_VOICE=
OPENROUTER_VIDEO_MODEL=google/veo-3.1-lite
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
기본 모델이 지원하는 길이는 `4초`, `6초`, `8초`입니다. 지원하지 않는 길이의
스크립트는 API 호출 전에 차단하며, 긴 영상은 장면 분할·결합 또는 해당 길이를
지원하는 다른 모델이 필요합니다.
