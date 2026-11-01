"""Photo/video asks: never "I can't send", give the page link, promise more, hand off to a human."""
import asyncio
import json

import pytest

from app import llm as llm_module
from app.handoff import detect_handoff
from app.schemas import ChatMessage


@pytest.mark.parametrize("text", [
    "Can you share me the photos or videos ?", "send pics please", "show me some pictures", "video bhej do",
    "photos dikhao", "can I see the videos", "any photos?", "Pictures",
])
def test_media_asks_are_handed_off(text):
    assert detect_handoff(text) == "other"


@pytest.mark.parametrize("text", [
    "is the video included?", "12 km with video price", "video ke saath kitna hai", "what is the price for 26 km ?",
    "do you have a photo package?", "rafting with GoPro video",
])
def test_media_mentions_in_normal_questions_are_not(text):
    assert detect_handoff(text) is None


def test_prompt_has_the_media_rule_only_where_handoff_is_enabled():
    def system(handoff):
        msgs = [ChatMessage(role="user", content="photos?")]
        built = asyncio.run(llm_module.build_messages(msgs, "s-3", channel="whatsapp", handoff_enabled=handoff))
        return json.dumps(built[0]["content"])
    assert "never say you cannot send them" in system(True)
    assert "you will share the photos after some time" in system(True)
    assert "never say you cannot send them" not in system(False)
