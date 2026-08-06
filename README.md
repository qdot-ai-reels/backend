# Backend Local Development Environment

FastAPI + PostgreSQL 로컬 개발용 Docker 환경입니다.

---

## 📁 프로젝트 구조

```text
project-backend/
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
docker compose up -d --build
```

### 3. 서비스 동작 확인
컨테이너가 정상 구동되면 아래 주소로 접속하여 결과를 확인합니다.
* **Health Check:** http://localhost:8000/health
* **Swagger API Docs (API 문서):** http://localhost:8000/docs
* **PostgreSQL DB 접속:** `localhost:5432`