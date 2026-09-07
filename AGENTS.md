# Backend Contribution Rules

## Branches

- Keep `main` stable and use it as the release branch.
  - `main`은 안정된 최종 코드와 배포 기준으로 유지합니다.
- Use `develop` for integration and team testing.
  - `develop`은 팀 작업을 통합하고 테스트하는 용도로 사용합니다.
- Create feature branches from `develop`.
  - 기능 브랜치는 `develop`에서 생성합니다.
- Use a task-based prefix: `feat/`, `fix/`, `refactor/`, `docs/`, or `chore/`.
  - 브랜치 이름은 작업 성격에 따라 `feat/`, `fix/`, `refactor/`, `docs/`, `chore/` 중 하나를 사용합니다.
- Examples: `feat/genvid-validator`, `fix/video-duration-check`.
  - 예시: `feat/genvid-validator`, `fix/video-duration-check`

## Commit Messages

- Make small, focused commits.
  - 커밋은 작고 하나의 목적에 집중되도록 나눕니다.
- Use the Conventional Commits prefix followed by a specific description.
  - Conventional Commits 접두사 뒤에 구체적인 작업 내용을 작성합니다.
- Use English for the type and technical terms; Korean is allowed for the explanation when useful.
  - 유형과 기술 용어는 영어를 사용하고, 필요한 설명은 한국어로 작성할 수 있습니다.
- Avoid vague messages such as `feat: work on automation`.
  - `feat: work on automation`처럼 작업 내용을 알기 어려운 메시지는 사용하지 않습니다.
- Examples:
  - `feat: add video metadata validation`
  - `fix: correct duration comparison logic`
  - `test: add video validator tests`
  - `docs: document GENVID-001 validation flow`

## Pull Requests

- Open feature pull requests against `develop`.
  - 기능 브랜치의 Pull Request는 `develop`을 대상으로 생성합니다.
- Open the `develop` to `main` pull request only after integration testing.
  - 통합 테스트가 끝난 후 `develop`에서 `main`으로 Pull Request를 생성합니다.
- Include the scope, test results, known limitations, and follow-up work.
  - PR에는 작업 범위, 테스트 결과, 알려진 제한사항, 후속 작업을 포함합니다.
- Do not claim that an external API was tested if credentials or credits were unavailable.
  - API 키나 크레딧이 없어 외부 API를 실제로 테스트하지 못했다면 테스트했다고 작성하지 않습니다.

## Secrets and Test Artifacts

- Never commit `.env`, API keys, credentials, or other secrets.
  - `.env`, API 키, 인증 정보 등 비밀정보는 절대 커밋하지 않습니다.
- Do not commit local video files or other large test artifacts unless the team explicitly agrees.
  - 팀이 명시적으로 합의하지 않는 한 로컬 영상 파일이나 큰 테스트 파일을 커밋하지 않습니다.
- Keep provider model names and credentials configurable through environment variables.
  - 외부 서비스의 모델명과 인증 정보는 환경변수로 변경할 수 있게 관리합니다.
