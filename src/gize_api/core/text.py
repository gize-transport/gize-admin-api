"""Rules for text people type: names, areas, descriptions.

Trimmed of surrounding spaces, never empty, and free of control characters (they hide in
names and break exports and SMS). `ShortText` also normalises what looks the same: Unicode
NFC (an accent typed one way or another) and one ordinary space between words (no double
or non-breaking spaces), so look-alike names are equal.
"""

import unicodedata
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator, StringConstraints

SHORT_TEXT_MAX_LENGTH = 100
NOTES_MAX_LENGTH = 1000


def no_control_characters(value: str) -> str:
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("must not contain control characters")
    return value


def same_looking(value: str) -> str:
    """NFC, and single ordinary spaces between words."""
    return " ".join(unicodedata.normalize("NFC", value).split())


def short_enough(value: str) -> str:
    """The length again after normalising: NFC can make text longer (U+0958 becomes two
    characters), and the column holds SHORT_TEXT_MAX_LENGTH."""
    if len(value) > SHORT_TEXT_MAX_LENGTH:
        raise ValueError(f"must be at most {SHORT_TEXT_MAX_LENGTH} characters")
    return value


# A name or short description: an area, a pickup point, a company.
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=SHORT_TEXT_MAX_LENGTH),
    AfterValidator(no_control_characters),  # first: a newline is refused, not collapsed
    AfterValidator(same_looking),
    AfterValidator(short_enough),
]


def _no_control_characters_but_line_breaks(value: str) -> str:
    return "\n".join(
        no_control_characters(line) for line in value.replace("\r\n", "\n").split("\n")
    )


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _notes_short_enough(value: str) -> str:
    if len(value) > NOTES_MAX_LENGTH:
        raise ValueError(f"must be at most {NOTES_MAX_LENGTH} characters")
    return value


# Free text staff write down: line breaks kept, may be empty (the service stores "" as none).
Notes = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=NOTES_MAX_LENGTH),
    AfterValidator(_no_control_characters_but_line_breaks),
    AfterValidator(_nfc),
    AfterValidator(_notes_short_enough),
]


def blank_is_none(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


# A search box: blank means no filter (a picker may send its input box as it is).
SearchText = Annotated[ShortText | None, BeforeValidator(blank_is_none)]
