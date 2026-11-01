"""WhatsApp emoji limit: at most one emoji per reply, and none if the previous bot reply had one (so at most every second reply).

The prompt already asks for rare emojis; this is the deterministic backstop so a prompt slip never puts one
on every message. ₹, arrows and box lines are not touched.
"""
import re

_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⭐⭕️‍]+")
_RECENT = 1   # how many earlier bot replies to look at


def strip_emojis(text: str) -> str:
    text = _EMOJI.sub("", text)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


def limit_emojis(reply: str, earlier_bot_replies: list[str]) -> str:
    """`reply` with its emojis removed if a recent bot reply had one, else cut down to the first emoji."""
    if any(_EMOJI.search(t) for t in earlier_bot_replies[-_RECENT:]):
        return strip_emojis(reply)
    found = list(_EMOJI.finditer(reply))
    if len(found) <= 1:
        return reply
    first = found[0]
    kept = reply[: first.end()] + _EMOJI.sub("", reply[first.end():])
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", kept)).strip()
