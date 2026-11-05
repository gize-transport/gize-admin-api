"""AfroMessage, the SMS gateway (GZ-038): https://www.afromessage.com/developers.

`POST https://api.afromessage.com/api/send` with `Authorization: Bearer <token>` and a JSON
body: `to` (E.164), `message`, and the approved `sender` name and `from` identifier when
set (AfroMessage's defaults otherwise). It answers `{"acknowledge": "success", "response":
{"message_id": ...}}` (accepted for sending, not yet delivered) or `{"acknowledge":
"error", "response": {"errors": [...]}}`; its documentation gives no HTTP statuses, so the
answer's body decides. Mapped to GZ-030's outcomes:

- accepted: its `message_id`;
- refused (an error answer, or a 400/422): `rejected`, the reasons logged (numbers masked);
- the token refused (401/403), not found, rate limited (429), down (5xx), unreachable:
  `unavailable`: nothing was sent;
- no answer in time, the answer lost after sending, or a 2xx we can't read: `timeout`:
  it may have been sent.

Sent once through the guarded outbound client (its POSTs are never retried); the token,
the phone and the text never reach a log or an error.
"""

from typing import Any, Self

import structlog
from pydantic import SecretStr

from gize_api.core.config import Settings
from gize_api.core.http import (
    OutboundAnswerLostError,
    OutboundClient,
    OutboundError,
    OutboundResponse,
    OutboundTimeoutError,
)
from gize_api.core.scrub import mask_phone_numbers
from gize_api.core.telemetry import Telemetry
from gize_api.integrations.sms import SmsSendError
from gize_api.sms.models import PROVIDER_ID_MAX_LENGTH

AFROMESSAGE_HOST = "api.afromessage.com"
SEND_URL = f"https://{AFROMESSAGE_HOST}/api/send"
MAX_LOGGED_REASONS = 5
MAX_REASON_LENGTH = 200

logger = structlog.get_logger("gize_api.sms.afromessage")


class AfroMessageSender:
    def __init__(
        self,
        *,
        token: SecretStr,
        sender_name: str | None,
        identifier_id: str | None,
        client: OutboundClient,
    ) -> None:
        self._token = token
        self._sender_name = sender_name
        self._identifier_id = identifier_id
        self._client = client

    @classmethod
    def for_settings(
        cls,
        settings: Settings,
        *,
        telemetry: Telemetry | None,
        outbound: OutboundClient | None = None,
    ) -> Self:
        if settings.afromessage_token is None:
            raise ValueError("set AFROMESSAGE_TOKEN to send SMS through AfroMessage")
        return cls(
            token=settings.afromessage_token,
            sender_name=settings.afromessage_sender_name,
            identifier_id=settings.afromessage_identifier_id,
            client=outbound
            or OutboundClient(allowed_hosts={AFROMESSAGE_HOST}, telemetry=telemetry),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, to: str, text: str) -> str:
        body: dict[str, str] = {"to": to, "message": text}
        if self._sender_name is not None:
            body["sender"] = self._sender_name
        if self._identifier_id is not None:
            body["from"] = self._identifier_id
        try:
            response = await self._client.request(
                "POST",
                SEND_URL,
                json=body,
                headers={"Authorization": f"Bearer {self._token.get_secret_value()}"},
            )
        except (OutboundTimeoutError, OutboundAnswerLostError):
            raise SmsSendError("timeout") from None  # it may have been sent
        except OutboundError:
            raise SmsSendError("unavailable") from None
        return _outcome(response)


def _outcome(response: OutboundResponse) -> str:
    answer = _body(response)
    acknowledge = answer.get("acknowledge") if isinstance(answer, dict) else None
    if response.status in {400, 422} or (200 <= response.status < 300 and acknowledge == "error"):
        logger.warning("afromessage_refused", status=response.status, errors=_reasons(answer))
        raise SmsSendError("rejected")
    if not 200 <= response.status < 300:
        logger.warning("afromessage_unavailable", status=response.status)
        raise SmsSendError("unavailable")
    details = answer.get("response") if isinstance(answer, dict) else None
    message_id = details.get("message_id") if isinstance(details, dict) else None
    if (
        acknowledge != "success"
        or not isinstance(message_id, str)
        or not 0 < len(message_id) <= PROVIDER_ID_MAX_LENGTH
    ):
        # Accepted, perhaps sent: as uncertain as a timeout.
        logger.warning("afromessage_unreadable", status=response.status)
        raise SmsSendError("timeout")
    return message_id


def _body(response: OutboundResponse) -> Any:  # noqa: ANN401 - JSON
    try:
        return response.json()
    except ValueError:
        return None


def _reasons(answer: Any) -> list[str]:  # noqa: ANN401 - JSON
    """AfroMessage's reasons, bounded, numbers masked (they may name the recipient)."""
    details = answer.get("response") if isinstance(answer, dict) else None
    errors = details.get("errors") if isinstance(details, dict) else None
    if not isinstance(errors, list):
        return []
    return [
        mask_phone_numbers(str(error))[:MAX_REASON_LENGTH] for error in errors[:MAX_LOGGED_REASONS]
    ]
