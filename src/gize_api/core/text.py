"""Rules for text people type: names, areas, descriptions.

Trimmed of surrounding spaces, never empty, and free of control characters (they hide in
names and break exports and SMS).
"""

import unicodedata
from typing import Annotated

from pydantic import AfterValidator, StringConstraints

SHORT_TEXT_MAX_LENGTH = 100


def no_control_characters(value: str) -> str:
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("must not contain control characters")
    return value


# A name or short description: an area, a pickup point, a company.
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=SHORT_TEXT_MAX_LENGTH),
    AfterValidator(no_control_characters),
]
