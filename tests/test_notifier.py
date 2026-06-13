"""Tests for Discord notification formatting and delivery."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from services.config import AppConfig
from services.notifier import (
    DiscordNotifier,
    format_all_time_low,
    format_brl,
    format_initial_baseline,
    format_sprite_decode_alert,
)


WEBHOOK_URL = "https://discord.example/webhook"


class QueueHandler:
    def __init__(self, outcomes: list[httpx.Response | Exception]) -> None:
        self._outcomes = outcomes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(12),
    )


def _notifier(handler: QueueHandler) -> DiscordNotifier:
    return DiscordNotifier(WEBHOOK_URL, client=_client(handler))


def _posted_json(request: httpx.Request) -> object:
    return json.loads(request.content.decode("utf-8"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1250.0, "R$1.250,00"),
        (1350.0, "R$1.350,00"),
        (500.0, "R$500,00"),
        (843.0, "R$843,00"),
        (1234567.5, "R$1.234.567,50"),
        (0.0, "R$0,00"),
    ],
)
def test_format_brl_matches_frd_examples(value: float, expected: str) -> None:
    assert format_brl(value) == expected


def test_format_all_time_low_matches_frd_template() -> None:
    assert (
        format_all_time_low(
            card_name="Mega Charizard X",
            condition="NM",
            price=1250.0,
            previous_lowest=1350.0,
            url="https://x",
        )
        == "Mega Charizard X - NM - R$1.250,00 - Previous lowest: R$1.350,00 - https://x"
    )


def test_format_initial_baseline_matches_frd_template() -> None:
    assert (
        format_initial_baseline(
            card_name="Mega Gengar",
            condition="NM",
            price=500.0,
            url="https://x",
        )
        == "Mega Gengar - NM - R$500,00 - Initial baseline - https://x"
    )


def test_format_sprite_decode_alert_matches_frd_template() -> None:
    assert (
        format_sprite_decode_alert(card_name="<card>", url="<url>")
        == "⚠️ Sprite decode failed - <card> - <url> - listing skipped"
    )


def test_notify_all_time_low_posts_expected_content() -> None:
    handler = QueueHandler([httpx.Response(204)])
    notifier = _notifier(handler)

    assert notifier.notify_all_time_low(
        card_name="Mega Charizard X",
        condition="NM",
        price=1250.0,
        previous_lowest=1350.0,
        url="https://x",
    )

    assert len(handler.requests) == 1
    request = handler.requests[0]
    assert str(request.url) == WEBHOOK_URL
    assert request.method == "POST"
    assert _posted_json(request) == {
        "content": (
            "Mega Charizard X - NM - R$1.250,00 - "
            "Previous lowest: R$1.350,00 - https://x"
        )
    }


def test_notify_initial_baseline_posts_expected_content() -> None:
    handler = QueueHandler([httpx.Response(204)])
    notifier = _notifier(handler)

    assert notifier.notify_initial_baseline(
        card_name="Mega Gengar",
        condition="NM",
        price=500.0,
        url="https://x",
    )

    assert len(handler.requests) == 1
    assert _posted_json(handler.requests[0]) == {
        "content": "Mega Gengar - NM - R$500,00 - Initial baseline - https://x"
    }


def test_notify_sprite_decode_failure_posts_expected_content() -> None:
    handler = QueueHandler([httpx.Response(204)])
    notifier = _notifier(handler)

    assert notifier.notify_sprite_decode_failure(card_name="Mega Gengar", url="https://x")

    assert len(handler.requests) == 1
    assert _posted_json(handler.requests[0]) == {
        "content": "⚠️ Sprite decode failed - Mega Gengar - https://x - listing skipped"
    }


def test_non_success_status_is_swallowed_without_retry(caplog: pytest.LogCaptureFixture) -> None:
    handler = QueueHandler([httpx.Response(500)])
    notifier = _notifier(handler)

    with caplog.at_level("WARNING", logger="services.notifier"):
        delivered = notifier.notify_all_time_low(
            card_name="Mega Charizard X",
            condition="NM",
            price=1250.0,
            previous_lowest=1350.0,
            url="https://x",
        )

    assert delivered is False
    assert len(handler.requests) == 1
    assert "HTTP 500" in caplog.text


def test_transport_error_is_swallowed_without_retry(caplog: pytest.LogCaptureFixture) -> None:
    handler = QueueHandler([httpx.ConnectError("boom")])
    notifier = _notifier(handler)

    with caplog.at_level("ERROR", logger="services.notifier"):
        delivered = notifier.notify_initial_baseline(
            card_name="Mega Gengar",
            condition="NM",
            price=500.0,
            url="https://x",
        )

    assert delivered is False
    assert len(handler.requests) == 1
    assert "Discord notification failed" in caplog.text


def test_from_app_config_uses_configured_webhook_url() -> None:
    app = AppConfig(
        discord_webhook_url="https://discord.example/config-webhook",
        cards_config_path=Path("cards.json"),
        database_path=Path("watcher.db"),
        request_delay_seconds=30,
        sprite_request_delay_seconds=2,
        http_timeout_seconds=20,
        user_agent="TestAgent/1.0",
        send_initial_baseline_notification=False,
        log_max_bytes=1000,
        log_backup_count=2,
    )
    handler = QueueHandler([httpx.Response(204)])
    notifier = DiscordNotifier.from_app_config(app, client=_client(handler))

    assert notifier.notify_sprite_decode_failure(card_name="Mega Gengar", url="https://x")

    assert str(handler.requests[0].url) == app.discord_webhook_url


def test_context_manager_closes_client_and_close_is_idempotent() -> None:
    handler = QueueHandler([httpx.Response(204)])
    client = _client(handler)

    with DiscordNotifier(WEBHOOK_URL, client=client) as notifier:
        assert notifier.notify_sprite_decode_failure(card_name="Mega Gengar", url="https://x")

    assert client.is_closed

    notifier.close()
    assert client.is_closed
