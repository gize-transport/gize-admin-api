"""Contact details people type: email addresses and Ethiopian mobile numbers.

- `Email`: trimmed and lower-case, ASCII, the HTML form rule with a dot in the domain.
- `EthiopianMobile`: typed any usual way (`0911 23 45 67`, `+251-911-234567`, `911234567`)
  and stored E.164, `+2519…` (Ethio Telecom) or `+2517…` (Safaricom): one number has one
  spelling, so it can be compared, rate-limited and texted.
"""

import re
from typing import Annotated

from pydantic import AfterValidator, StringConstraints

EMAIL_MAX_LENGTH = 320  # RFC 5321 path limit
EMAIL_LOCAL_PART_MAX_LENGTH = 64  # RFC 5321
# Punctuation escaped: browsers compile `<input pattern>` with the `v` flag, which refuses
# unescaped `/ { | } -` in a class (tests/staff/test_invite.py runs it under node).
_LOCAL_PART = r"[A-Za-z0-9.!#$%&'*+\/=?^_`\{\|\}~\-]"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
# The HTML form rule for email addresses (what Supabase accepts too), ASCII only, with a dot
# in the domain: no spaces, control or invisible characters.
EMAIL_PATTERN = rf"^{_LOCAL_PART}{{1,{EMAIL_LOCAL_PART_MAX_LENGTH}}}@{_LABEL}(?:\.{_LABEL})+$"

Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, to_lower=True, max_length=EMAIL_MAX_LENGTH, pattern=EMAIL_PATTERN
    ),
]

MOBILE_TYPED_MAX_LENGTH = 20
MOBILE_E164_LENGTH = 13  # +251 and nine digits
# What the database checks too.
MOBILE_E164_PATTERN = r"^\+251[79][0-9]{8}$"
_SEPARATORS = re.compile(r"[\s().-]")
_MOBILE = re.compile(r"(?:\+?251|0)?([79][0-9]{8})")


def ethiopian_mobile(typed: str) -> str:
    """The number in E.164 (`+2519…`/`+2517…`), or ValueError."""
    match = _MOBILE.fullmatch(_SEPARATORS.sub("", typed))
    if match is None:
        raise ValueError("must be an Ethiopian mobile number (09… or 07…)")
    return f"+251{match.group(1)}"


EthiopianMobile = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=MOBILE_TYPED_MAX_LENGTH),
    AfterValidator(ethiopian_mobile),
]
