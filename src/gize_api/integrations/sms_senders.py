"""Which SMS sender the app uses (GZ-030, GZ-038)."""

from gize_api.core.config import Environment, Settings
from gize_api.core.telemetry import Telemetry
from gize_api.integrations.afromessage import AfroMessageSender
from gize_api.integrations.sms import FakeSmsSender, SmsSender, UnconfiguredSmsSender


def sms_sender_for(settings: Settings, *, telemetry: Telemetry | None) -> SmsSender:
    """Tests: always a fake (never a real SMS). Otherwise AfroMessage when its token is
    set; without it, a fake that prints locally, and no sender on staging and production
    (every send `unavailable`)."""
    if settings.app_env is Environment.TEST:
        return FakeSmsSender()
    if settings.afromessage_token is not None:
        return AfroMessageSender.for_settings(settings, telemetry=telemetry)
    if settings.app_env is Environment.LOCAL:
        return FakeSmsSender(echo=True)
    return UnconfiguredSmsSender()
