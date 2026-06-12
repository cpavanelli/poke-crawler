"""Tests for the LigaPokemon listing inspection CLI (FRD §11, §19, §4)."""

from __future__ import annotations

import json
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from models.listing import Listing
from services.fetcher import HttpFetcher, MAX_ATTEMPTS, RETRY_DELAY_SECONDS
from services.notifier import DiscordNotifier
from tools.list_prices import format_listings, main, run, sort_listings

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ligapokemon"
GRENINJA_FIXTURE_PATH = FIXTURE_DIR / "greninja_116_precocss.html"
GRENINJA_SPRITE_PATH = FIXTURE_DIR / "greninja_116_sprite.jpg"
PAGE_URL = "https://www.ligapokemon.com.br/?view=cards/card&card=Mega%20Greninja"


def _fixture_html() -> str:
    return GRENINJA_FIXTURE_PATH.read_text(encoding="utf-8")


def _blank_sprite_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("L", (600, 84), 255).save(buffer, format="JPEG")
    return buffer.getvalue()


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(12),
    )


def _fetcher(
    handler: Callable[[httpx.Request], httpx.Response],
    sleeps: list[float] | None = None,
) -> HttpFetcher:
    return HttpFetcher(
        user_agent="TestAgent/1.0",
        timeout_seconds=12,
        request_delay_seconds=0,
        sprite_request_delay_seconds=2,
        client=_client(handler),
        sleep=(sleeps if sleeps is not None else []).append,
    )


def _fixture_fetcher(
    requests: list[httpx.Request] | None = None,
    sleeps: list[float] | None = None,
) -> HttpFetcher:
    sprite_bytes = GRENINJA_SPRITE_PATH.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        url = str(request.url)
        if "imgnum" in url:
            return httpx.Response(200, content=sprite_bytes)
        return httpx.Response(200, text=_fixture_html())

    return _fetcher(handler, sleeps)


def test_sort_listings_and_format_listings() -> None:
    listings = [
        Listing(condition="ZZ", price=1.0),
        Listing(condition="SP", price=1200.0),
        Listing(condition="NM", price=934.15),
        Listing(condition="NM", price=843.0),
    ]

    assert sort_listings(listings) == [
        Listing(condition="NM", price=843.0),
        Listing(condition="NM", price=934.15),
        Listing(condition="SP", price=1200.0),
        Listing(condition="ZZ", price=1.0),
    ]
    assert format_listings(listings) == "NM 843.00\nNM 934.15\nSP 1200.00\nZZ 1.00"


def test_run_fetches_fixture_and_decodes_preco_css() -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    fetcher = _fixture_fetcher(requests, sleeps)

    listings = run(PAGE_URL, fetcher=fetcher, on_sprite_error=lambda _message: None)
    formatted_lines = format_listings(listings).splitlines()

    assert Listing(condition="NM", price=843.0) in listings
    assert formatted_lines[0] == "NM 843.00"
    assert formatted_lines.index("NM 843.00") < formatted_lines.index("NM 934.15")
    assert len([request for request in requests if "imgnum" in str(request.url)]) == 1
    assert str(requests[1].url).startswith("https://")
    assert sleeps == [2]


def test_main_happy_path_prints_sorted_lines(capsys) -> None:
    fetcher = _fixture_fetcher()

    exit_code = main([PAGE_URL], fetcher=fetcher)

    captured = capsys.readouterr()
    assert exit_code == 0
    stdout_lines = captured.out.splitlines()
    assert stdout_lines[0] == "NM 843.00"
    assert stdout_lines.index("NM 843.00") < stdout_lines.index("NM 934.15")
    assert captured.err == ""


@pytest.mark.parametrize("status_code", [403, 429])
def test_main_stop_status_aborts_cleanly_without_retry(capsys, status_code: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code)

    exit_code = main([PAGE_URL], fetcher=_fetcher(handler, []))

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert (
        captured.err
        == f"aborted: HTTP {status_code} from source \u2014 stopping (anti-abuse)\n"
    )
    assert len(requests) == 1


def test_main_fetch_error_path(capsys) -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("network error", request=request)

    exit_code = main([PAGE_URL], fetcher=_fetcher(handler, sleeps))

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == f"fetch failed: {PAGE_URL}\n"
    assert len(requests) == MAX_ATTEMPTS
    assert sleeps == [RETRY_DELAY_SECONDS]


def test_sprite_decode_warning_continues(capsys) -> None:
    html = """
        <html>
            <script>
                var cards_stock = [
                    {"qualid":"2","precoFinal":"934.15"},
                    {"qualid":"2","precoCss":"digit foo;digit bar;V;digit bar"}
                ];
                var dataQuality = [{"id":2,"acron":"NM","label":"Praticamente Nova (NM)"}];
            </script>
            <style>
                .digit{background-position:0px 0px;}
                .foo{width:7px;float:left;height:15px;}
                .bar{background-image:url(//example.com/imgnum/test.jpg)}
            </style>
        </html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if "imgnum" in str(request.url):
            return httpx.Response(200, content=_blank_sprite_bytes())
        return httpx.Response(200, text=html)

    exit_code = main([PAGE_URL], fetcher=_fetcher(handler, []))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "NM 934.15\n"
    assert captured.err.splitlines() == [
        "\u26a0\ufe0f sprite decode failed: Sprite digit crop did not match a known template"
    ]


def test_main_notify_posts_initial_baseline_per_condition(capsys) -> None:
    fetcher = _fixture_fetcher()
    posts: list[httpx.Request] = []

    def webhook(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(204)

    notifier = DiscordNotifier("https://discord.example/webhook", client=_client(webhook))

    exit_code = main(
        ["--notify", "--name", "Mega Greninja", PAGE_URL],
        fetcher=fetcher,
        notifier=notifier,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.splitlines()[0] == "NM 843.00"
    # The fixture only has NM listings, so exactly one baseline message is sent.
    assert len(posts) == 1
    assert json.loads(posts[0].content.decode("utf-8")) == {
        "content": f"Mega Greninja - NM - R$843,00 - Initial baseline - {PAGE_URL}"
    }
    assert "notified NM R$843,00: sent" in captured.err


def test_main_notify_defaults_card_name_to_url(capsys) -> None:
    fetcher = _fixture_fetcher()
    posts: list[httpx.Request] = []

    def webhook(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(204)

    notifier = DiscordNotifier("https://discord.example/webhook", client=_client(webhook))

    exit_code = main(["--notify", PAGE_URL], fetcher=fetcher, notifier=notifier)

    assert exit_code == 0
    content = json.loads(posts[0].content.decode("utf-8"))["content"]
    assert content == f"{PAGE_URL} - NM - R$843,00 - Initial baseline - {PAGE_URL}"


def test_main_without_notify_sends_nothing(capsys) -> None:
    fetcher = _fixture_fetcher()
    posts: list[httpx.Request] = []

    def webhook(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(204)

    notifier = DiscordNotifier("https://discord.example/webhook", client=_client(webhook))

    # Notifier supplied but --notify absent: the tool must not post anything.
    exit_code = main([PAGE_URL], fetcher=fetcher, notifier=notifier)

    assert exit_code == 0
    assert posts == []


def test_main_notify_without_webhook_url_errors(capsys, monkeypatch) -> None:
    monkeypatch.setattr("tools.list_prices.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    fetcher = _fixture_fetcher()

    exit_code = main(["--notify", PAGE_URL], fetcher=fetcher)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "DISCORD_WEBHOOK_URL is not set" in captured.err


def test_empty_page_prints_note(capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<script>var cards_stock = [];</script>")

    exit_code = main([PAGE_URL], fetcher=_fetcher(handler, []))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert captured.err == f"no listings found for {PAGE_URL}\n"
