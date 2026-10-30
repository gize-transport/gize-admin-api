"""Email addresses and Ethiopian mobile numbers as people type them."""

import re

import pytest
from pydantic import TypeAdapter, ValidationError

from gize_api.core.contact import MOBILE_E164_PATTERN, EthiopianMobile, ethiopian_mobile

MOBILE = TypeAdapter(EthiopianMobile)


@pytest.mark.parametrize(
    "typed",
    [
        "0911234567",
        "911234567",
        "+251911234567",
        "251911234567",
        " 0911 23 45 67 ",
        "+251-911-234-567",
        "(0911) 234.567",
    ],
)
def test_usual_spellings_become_one_number(typed: str) -> None:
    assert MOBILE.validate_python(typed) == "+251911234567"


def test_safaricom_numbers_are_mobiles_too() -> None:
    assert ethiopian_mobile("0712345678") == "+251712345678"


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "0111234567",  # a landline (Addis Ababa)
        "091123456",  # too short
        "09112345678",  # too long
        "+254711234567",  # Kenya
        "0911a34567",
        "0911_234567",
        "+251 911 234 567 8",
        "0" * 21,  # past the typed limit
    ],
)
def test_other_text_is_refused(typed: str) -> None:
    with pytest.raises(ValidationError):
        MOBILE.validate_python(typed)


def test_the_database_pattern_matches_what_is_stored() -> None:
    assert re.fullmatch(MOBILE_E164_PATTERN, ethiopian_mobile("0911234567"))
