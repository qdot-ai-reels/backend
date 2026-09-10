from fastapi.testclient import TestClient

from app.main import app
from app.video_generator import VIDEO_CONDITION_PROMPT


def test_prompt_templates_are_exposed_from_generation_sources():
    with TestClient(app) as client:
        response = client.get("/api/v1/reels/prompts")

    assert response.status_code == 200
    payload = response.json()
    assert payload["script_prompt"].startswith("당신은 공동구매 광고 숏폼 스크립트 작성자입니다.")
    assert "Physical-Safe Motion & Continuity" in payload["script_prompt"]
    assert payload["video_prompt"] == VIDEO_CONDITION_PROMPT.strip()
