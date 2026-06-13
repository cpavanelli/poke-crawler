"""Tests for the shared HTTP fetch layer."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from services.config import AppConfig
from services.fetcher import (
    MAX_ATTEMPTS,
    RETRY_DELAY_SECONDS,
    CycleStop,
    FetchError,
    HttpFetcher,
)


class QueueHandler:
    def __init__(
        self,
        outcomes: list[httpx.Response | Exception],
        events: list[str] | None = None,
    ) -> None:
        self._outcomes = outcomes
        self.requests: list[httpx.Request] = []
        self.events = events if events is not None else []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.events.append("request")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(12),
    )


def _fetcher(
    handler: QueueHandler,
    sleeps: list[float],
    *,
    client: httpx.Client | None = None,
) -> HttpFetcher:
    return HttpFetcher(
        user_agent="TestAgent/1.0",
        timeout_seconds=12,
        request_delay_seconds=30,
        sprite_request_delay_seconds=2,
        client=client or _client(handler),
        sleep=sleeps.append,
    )


def test_get_page_happy_path_sends_user_agent_and_uses_client_timeout() -> None:
    handler = QueueHandler([httpx.Response(200, text="<html>ok</html>")])
    sleeps: list[float] = []
    client = _client(handler)
    fetcher = _fetcher(handler, sleeps, client=client)

    assert fetcher.get_page("https://example.com/card") == "<html>ok</html>"

    assert len(handler.requests) == 1
    assert sleeps == []
    assert handler.requests[0].headers["User-Agent"] == "TestAgent/1.0"
    assert client.timeout == httpx.Timeout(12)


@pytest.mark.parametrize("status_code", [403, 429])
def test_get_page_stop_status_raises_cycle_stop_without_retry(status_code: int) -> None:
    url = "https://example.com/card"
    handler = QueueHandler([httpx.Response(status_code)])
    sleeps: list[float] = []
    fetcher = _fetcher(handler, sleeps)

    with pytest.raises(CycleStop) as exc_info:
        fetcher.get_page(url)

    assert exc_info.value.status_code == status_code
    assert exc_info.value.url == url
    assert len(handler.requests) == 1
    assert sleeps == []


def test_transient_fail_twice_raises_fetch_error_after_retry() -> None:
    handler = QueueHandler(
        [
            httpx.ConnectTimeout("timeout"),
            httpx.ConnectError("network error"),
        ]
    )
    sleeps: list[float] = []
    fetcher = _fetcher(handler, sleeps)

    with pytest.raises(FetchError):
        fetcher.get_page("https://example.com/card")

    assert len(handler.requests) == MAX_ATTEMPTS
    assert sleeps == [RETRY_DELAY_SECONDS]


def test_transient_then_success_recovers_after_backoff() -> None:
    handler = QueueHandler(
        [
            httpx.ConnectTimeout("timeout"),
            httpx.Response(200, text="<html>ok</html>"),
        ]
    )
    sleeps: list[float] = []
    fetcher = _fetcher(handler, sleeps)

    assert fetcher.get_page("https://example.com/card") == "<html>ok</html>"

    assert len(handler.requests) == 2
    assert sleeps == [RETRY_DELAY_SECONDS]


def test_get_sprite_applies_sprite_delay_before_request() -> None:
    content = b"\xff\xd8sprite"
    events: list[str] = []
    handler = QueueHandler([httpx.Response(200, content=content)], events=events)
    sleeps: list[float] = []

    def sleep(duration: float) -> None:
        sleeps.append(duration)
        events.append(f"sleep:{duration}")

    fetcher = HttpFetcher(
        user_agent="TestAgent/1.0",
        timeout_seconds=12,
        request_delay_seconds=30,
        sprite_request_delay_seconds=2,
        client=_client(handler),
        sleep=sleep,
    )

    assert fetcher.get_sprite("https://example.com/sprite.jpg") == content

    assert sleeps == [2]
    assert events == ["sleep:2", "request"]


@pytest.mark.parametrize("status_code", [403, 429])
def test_get_sprite_stop_status_raises_cycle_stop(status_code: int) -> None:
    handler = QueueHandler([httpx.Response(status_code)])
    sleeps: list[float] = []
    fetcher = _fetcher(handler, sleeps)

    with pytest.raises(CycleStop) as exc_info:
        fetcher.get_sprite("https://example.com/sprite.jpg")

    assert exc_info.value.status_code == status_code
    assert len(handler.requests) == 1
    assert sleeps == [2]


def test_wait_between_cards_sleeps_inter_card_delay_without_http() -> None:
    handler = QueueHandler([])
    sleeps: list[float] = []
    fetcher = _fetcher(handler, sleeps)

    fetcher.wait_between_cards()

    assert sleeps == [30]
    assert handler.requests == []


def test_from_app_config_maps_settings() -> None:
    app = AppConfig(
        discord_webhook_url="https://discord.example/webhook",
        cards_config_path=Path("cards.json"),
        database_path=Path("watcher.db"),
        request_delay_seconds=11,
        sprite_request_delay_seconds=3,
        http_timeout_seconds=7,
        user_agent="ConfigAgent/1.0",
        send_initial_baseline_notification=False,
        log_max_bytes=1000,
        log_backup_count=2,
    )
    handler = QueueHandler([httpx.Response(200, text="ok")])
    sleeps: list[float] = []
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(app.http_timeout_seconds),
    )
    fetcher = HttpFetcher.from_app_config(app, client=client, sleep=sleeps.append)

    assert fetcher.get_page("https://example.com/card") == "ok"
    fetcher.wait_between_cards()

    assert handler.requests[0].headers["User-Agent"] == "ConfigAgent/1.0"
    assert client.timeout == httpx.Timeout(app.http_timeout_seconds)
    assert sleeps == [app.request_delay_seconds]


def test_default_client_is_built_with_configured_timeout() -> None:
    fetcher = HttpFetcher(
        user_agent="TestAgent/1.0",
        timeout_seconds=9,
        request_delay_seconds=30,
        sprite_request_delay_seconds=2,
    )

    try:
        assert fetcher._client.timeout == httpx.Timeout(9)
    finally:
        fetcher.close()


def test_context_manager_closes_client() -> None:
    handler = QueueHandler([httpx.Response(200, text="ok")])
    client = _client(handler)

    with _fetcher(handler, [], client=client) as fetcher:
        assert fetcher.get_page("https://example.com/card") == "ok"

    assert client.is_closed

    fetcher.close()
    assert client.is_closed
