from fastapi import APIRouter

from app.script_generator import get_default_script_prompt_preview
from app.video_generator import VIDEO_CONDITION_PROMPT


router = APIRouter()


@router.get("/prompts", summary="기본 스크립트·영상 프롬프트 조회")
def get_prompt_templates() -> dict[str, str]:
    """Expose the same prompt sources used by generation for frontend preview."""
    return {
        "script_prompt": get_default_script_prompt_preview(),
        "video_prompt": VIDEO_CONDITION_PROMPT.strip(),
    }
