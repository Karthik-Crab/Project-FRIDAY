"""Is the owner asking FRIDAY to change a picture, or to talk about it?

"What's in this picture?" goes to the model that reads pictures. "Make the
sky red", "remove the man on the left", "turn it into a painting" go to the
picture editor (Qwen-Image-Edit in ComfyUI), which makes a new picture.
"""

from __future__ import annotations

import re
from pathlib import Path

_LEAD = (
    r"^\W*(?:(?:hey|ok|okay|so|now|and|also|then|friday|please|pls)[\s,.!]+){0,4}"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?(?:please\s+)?(?:just\s+)?"
    r"(?:(?:i\s+(?:want|would\s+like|d\s+like)\s+(?:you\s+)?to)\s+)?"
)
_EDIT_VERB = (
    r"(?:edit|change|make|turn|convert|remove|erase|delete|add|put|place|replace|swap|"
    r"colou?r|recolou?r|paint|repaint|give|dress|transform|restyle|fix|retouch|clean\s+up|"
    r"take\s+(?:out|off|away)|get\s+rid\s+of|brighten|darken|blur|sharpen|zoom|crop|"
    r"extend|expand|undress|show\s+(?:him|her|them|it)|let\s+(?:him|her|them|it))"
)
_EDIT = re.compile(_LEAD + rf"{_EDIT_VERB}\b", re.I)
# "edit this photo", "change the picture so ..." anywhere in the sentence.
_EDIT_PICTURE = re.compile(
    rf"\b{_EDIT_VERB}\b(?:\s+\S+){{0,3}}?\s+(?:this|the|my|that)\s+"
    r"(?:pictures?|images?|photos?|pics?|selfie|drawing|painting)\b",
    re.I,
)
# A question about the picture, not a change to it.
_QUESTION = re.compile(
    r"^\W*(?:what|who|whose|where|when|why|how|which|is|are|was|were|does|do|did|"
    r"describe|explain|tell\s+me|read|translate|count|identify|can\s+you\s+(?:see|tell|read|"
    r"describe|explain|identify))\b",
    re.I,
)


def wants_picture_edit(text: str) -> bool:
    """True when the words ask for a changed copy of the picture."""
    text = text.strip()
    if not text:
        return False
    if _QUESTION.match(text):
        return False
    return bool(_EDIT.match(text) or _EDIT_PICTURE.search(text))


def instruction(text: str) -> str:
    """The words for the editor, without "Friday, could you please"."""
    cleaned = re.sub(_LEAD, "", text.strip(), flags=re.I).strip(" .!?")
    return cleaned or text.strip()


def note() -> str:
    return (
        "Qwen-Image-Edit (an AI on this computer) made a new picture from yours; the "
        "original is unchanged. Small details such as faces and writing can come out "
        "slightly different. Ask again in other words for another try."
    )


NOT_SET_UP_REPLY = (
    "I can't change pictures yet: the picture editor isn't set up on this computer, so "
    "nothing was changed. I can only describe the picture."
)
ATTACH_REPLY = (
    "I don't have a picture in this chat to change. Attach it with the 📎 button and "
    "tell me what to change."
)


def output_name(source: Path, folder: Path) -> Path:
    stem = re.sub(r"[^\w\- ]+", "", source.stem).strip()[:40] or "picture"
    folder.mkdir(parents=True, exist_ok=True)
    number = 1
    while (path := folder / f"{stem} (edited {number}).png").exists():
        number += 1
    return path
