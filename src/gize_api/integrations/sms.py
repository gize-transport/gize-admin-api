"""SMS senders (GZ-030): the interface the rest of the app sends through.

No provider is chosen yet (an Ethiopian SMS gateway, product brief), so:
- local and test runs use `FakeSmsSender`, which keeps what it sent; locally it also prints
  each message, the number masked as in logs, so a developer can read a sign-up code;
- staging and production use `UnconfiguredSmsSender`: every send fails as `unavailable`.
  A code is never silently dropped into a fake where nobody can read it.

The gateway adapter comes with the provider: it will call the gateway through
`core/http.py` (allowlisted host, bounded time, no retried POST) and map its answers to
`SmsSendError` kinds. Senders never log or store the text: it may hold a code.
"""

import sys
import uuid
from collections import deque
from typing import Literal, Protocol, get_args

from gize_api.core.config import Environment, Settings
from gize_api.core.scrub import mask_phone_numbers

# Why a send failed, for the record and the caller's answer:
# - unavailable: no sender, or the gateway is down or refused us (our side or theirs);
# - rejected: the gateway refused this message (the number, or the text);
# - timeout: no answer in time: the SMS may or may not have gone out.
SmsFailure = Literal["unavailable", "rejected", "timeout"]
FAKE_SENT_KEPT = 100


class SmsSendError(Exception):
    def __init__(self, kind: SmsFailure) -> None:
        if kind not in get_args(SmsFailure):
            raise ValueError(f"unknown SMS failure kind: {kind!r}")
        super().__init__(kind)
        self.kind: SmsFailure = kind


class SmsSender(Protocol):
    async def send(self, to: str, text: str) -> str:
        """Send `text` to `to` (E.164) and return the provider's message ID, or raise
        SmsSendError. Called at most once per message: never retried on its own."""
        ...


class FakeSmsSender:
    """Keeps the latest messages in `sent` (tests read them); with `echo`, prints each one
    to stderr with the number masked (local development only: the text holds codes)."""

    def __init__(self, *, echo: bool = False) -> None:
        self.echo = echo
        self._sent: deque[tuple[str, str]] = deque(maxlen=FAKE_SENT_KEPT)

    @property
    def sent(self) -> list[tuple[str, str]]:
        return list(self._sent)

    async def send(self, to: str, text: str) -> str:
        self._sent.append((to, text))
        if self.echo:
            print(f"[fake SMS to {mask_phone_numbers(to)}] {text}", file=sys.stderr)  # noqa: T201
        return f"fake-{uuid.uuid4()}"


class UnconfiguredSmsSender:
    """No provider yet: nothing can be sent."""

    async def send(self, to: str, text: str) -> str:
        raise SmsSendError("unavailable")


def sms_sender_for(settings: Settings) -> SmsSender:
    if settings.app_env is Environment.LOCAL:
        return FakeSmsSender(echo=True)
    if settings.app_env is Environment.TEST:
        return FakeSmsSender()
    return UnconfiguredSmsSender()
