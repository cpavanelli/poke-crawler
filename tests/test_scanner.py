"""Tests for scanner orchestration (FRD §6, §7, §12)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import httpx

from models.card import Card
from services import storage
from services.fetcher import RETRY_DELAY_SECONDS, HttpFetcher
from services.notifier import DiscordNotifier
from services.scanner import Scanner


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ligapokemon"
GENGAR_HTML = (FIXTURE_DIR / "mega_gengar_284.html").read_text(encoding="utf-8")
GRENINJA_HTML = (FIXTURE_DIR / "greninja_116_precocss.html").read_text(encoding="utf-8")
ETB_HTML = (FIXTURE_DIR / "etb_ascended_heroes_prod.html").read_text(encoding="utf-8")
GRENINJA_SPRITE = (FIXTURE_DIR / "greninja_116_sprite.jpg").read_bytes()
GRENINJA_SPRITE_URL = (
    "https://repositorio.sbrauble.com/arquivos/up/comp/imgnum/files/img/"
    "260422lT92f3zskqjd04i6zfa6z2q78n23hf.jpg"
)
FIXED_NOW = "2026-06-12T09:10:11-03:00"
LIGA_BASE = "https://www.ligapokemon.com.br/card"


class RouteHandler:
    def __init__(self, routes: dict[str, list[httpx.Response | Exception]]) -> None:
        self._routes = routes
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        outcomes = self._routes[url]
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class SpyNotifier:
    def __init__(self) -> None:
        self.all_time_lows: list[dict[str, object]] = []
        self.initial_baselines: list[dict[str, object]] = []
        self.sprite_failures: list[dict[str, object]] = []

    def notify_all_time_low(
        self,
        *,
        card_name: str,
        condition: str,
        price: float,
        previous_lowest: float,
        url: str,
    ) -> bool:
        self.all_time_lows.append(
            {
                "card_name": card_name,
                "condition": condition,
                "price": price,
                "previous_lowest": previous_lowest,
                "url": url,
            }
        )
        return True

    def notify_initial_baseline(
        self,
        *,
        card_name: str,
        condition: str,
        price: float,
        url: str,
    ) -> bool:
        self.initial_baselines.append(
            {
                "card_name": card_name,
                "condition": condition,
                "price": price,
                "url": url,
            }
        )
        return True

    def notify_sprite_decode_failure(self, *, card_name: str, url: str) -> bool:
        self.sprite_failures.append({"card_name": card_name, "url": url})
        return True


def _conn() -> sqlite3.Connection:
    conn = storage.connect(":memory:")
    storage.init_db(conn)
    return conn


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(12),
    )


def _fetcher(handler: RouteHandler, sleeps: list[float]) -> HttpFetcher:
    return HttpFetcher(
        user_agent="TestAgent/1.0",
        timeout_seconds=12,
        request_delay_seconds=30,
        sprite_request_delay_seconds=2,
        client=_client(handler),
        sleep=sleeps.append,
    )


def _scanner(
    conn: sqlite3.Connection,
    handler: RouteHandler,
    notifier: object,
    sleeps: list[float] | None = None,
    *,
    send_initial_baseline: bool = False,
    clock: Callable[[], str] = lambda: FIXED_NOW,
) -> Scanner:
    return Scanner(
        fetcher=_fetcher(handler, sleeps if sleeps is not None else []),
        notifier=notifier,  # type: ignore[arg-type]
        conn=conn,
        send_initial_baseline=send_initial_baseline,
        clock=clock,
    )


def _card(
    suffix: str = "gengar",
    *,
    name: str = "Mega Gengar",
    conditions: tuple[str, ...] = ("NM",),
    is_sealed: bool = False,
) -> Card:
    return Card(
        name=name,
        conditions=conditions,
        url=f"{LIGA_BASE}/{suffix}",
        is_sealed=is_sealed,
    )


def _sealed_card(suffix: str = "etb") -> Card:
    return _card(
        suffix,
        name="ETB Ascended Heroes",
        conditions=(),
        is_sealed=True,
    )


def _page_routes(*cards: Card, html: str = GENGAR_HTML) -> dict[str, list[httpx.Response]]:
    return {card.url: [httpx.Response(200, text=html)] for card in cards}


def _baseline(
    conn: sqlite3.Connection,
    card: Card,
    condition: str,
    price: float,
    *,
    now: str = "2026-06-12T08:00:00-03:00",
) -> None:
    storage.upsert_baseline(
        conn,
        card.card_id,
        card.name,
        card.url,
        condition,
        price,
        now=now,
    )


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _scan_results(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT condition, lowest_price, scanned_at FROM scan_results ORDER BY id"
    ).fetchall()


def _scan_errors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT card_id, url, error_type, error_message, occurred_at FROM scan_errors ORDER BY id"
    ).fetchall()


def _broken_sprite_html() -> str:
    return """
        <html>
            <script>
                var cards_stock = [
                    {"qualid":"2","precoFinal":"934.15"},
                    {"qualid":"2","precoCss":"digit foo;V;digit foo"}
                ];
                var dataQuality = [{"id":2,"acron":"NM","label":"Praticamente Nova (NM)"}];
            </script>
            <style>
                .digit{background-position:0px 0px;}
                .foo{background-image:url(//example.com/imgnum/test.jpg)}
            </style>
        </html>
    """


def _sprite_stop_html() -> str:
    return """
        <html>
            <script>
                var cards_stock = [{"qualid":"2","precoCss":"digit foo;V;digit foo"}];
                var dataQuality = [{"id":2,"acron":"NM","label":"Praticamente Nova (NM)"}];
            </script>
            <style>
                .digit{background-position:0px 0px;}
                .foo{background-image:url(//example.com/imgnum/stop.jpg)}
            </style>
        </html>
    """


def _sealed_prod_html(stock: list[dict[str, object]]) -> str:
    return f"""
        <html>
            <script>
                var prod_stock = {json.dumps(stock)};
                var dataQuality = [
                    {{"id":1,"acron":"L","label":"Lacrado (L)"}},
                    {{"id":2,"acron":"N","label":"Novo (N)"}},
                    {{"id":3,"acron":"NEA","label":"Novo aberto (NEA)"}},
                    {{"id":4,"acron":"NSA","label":"Novo sem acessorios (NSA)"}},
                    {{"id":5,"acron":"U","label":"Usado (U)"}},
                    {{"id":6,"acron":"D","label":"Defeituoso (D)"}}
                ];
            </script>
        </html>
    """


def test_first_sight_creates_baseline_without_all_time_low_notification() -> None:
    conn = _conn()
    try:
        card = _card()
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(_page_routes(card)), notifier)

        outcome = scanner.scan_card(card)

        baseline = storage.get_baseline(conn, card.card_id, "NM")
        assert baseline is not None
        assert baseline.lowest_price == 2670.0
        assert outcome.initial_baselines == ("NM",)
        assert outcome.new_lows == ()
        assert notifier.all_time_lows == []
        assert notifier.initial_baselines == []
        assert [(row["condition"], row["lowest_price"]) for row in _scan_results(conn)] == [
            ("NM", 2670.0)
        ]
    finally:
        conn.close()


def test_first_sight_with_initial_baseline_flag_notifies_once_per_condition() -> None:
    conn = _conn()
    try:
        card = _card(conditions=("NM", "SP"))
        notifier = SpyNotifier()
        scanner = _scanner(
            conn,
            RouteHandler(_page_routes(card)),
            notifier,
            send_initial_baseline=True,
        )

        outcome = scanner.scan_card(card)

        assert outcome.initial_baselines == ("NM", "SP")
        assert [call["condition"] for call in notifier.initial_baselines] == ["NM", "SP"]
        assert notifier.all_time_lows == []
        assert storage.get_baseline(conn, card.card_id, "NM").lowest_price == 2670.0
        assert storage.get_baseline(conn, card.card_id, "SP").lowest_price == 2350.0
    finally:
        conn.close()


def test_new_all_time_low_notifies_with_previous_lowest_and_updates_baseline() -> None:
    conn = _conn()
    try:
        card = _card()
        _baseline(conn, card, "NM", 2700.0)
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(_page_routes(card)), notifier)

        outcome = scanner.scan_card(card)

        assert outcome.new_lows == ("NM",)
        assert notifier.all_time_lows == [
            {
                "card_name": "Mega Gengar",
                "condition": "NM",
                "price": 2670.0,
                "previous_lowest": 2700.0,
                "url": card.url,
            }
        ]
        assert storage.get_baseline(conn, card.card_id, "NM").lowest_price == 2670.0
        assert _count(conn, "scan_results") == 1
    finally:
        conn.close()


def test_no_new_low_above_baseline_keeps_baseline_but_appends_history() -> None:
    conn = _conn()
    try:
        card = _card()
        _baseline(conn, card, "NM", 2600.0)
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(_page_routes(card)), notifier)

        outcome = scanner.scan_card(card)

        assert outcome.new_lows == ()
        assert notifier.all_time_lows == []
        assert storage.get_baseline(conn, card.card_id, "NM").lowest_price == 2600.0
        assert [(row["condition"], row["lowest_price"]) for row in _scan_results(conn)] == [
            ("NM", 2670.0)
        ]
    finally:
        conn.close()


def test_equal_to_baseline_is_not_a_new_low_and_does_not_update() -> None:
    conn = _conn()
    try:
        card = _card()
        original_now = "2026-06-12T08:00:00-03:00"
        _baseline(conn, card, "NM", 2670.0, now=original_now)
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(_page_routes(card)), notifier)

        outcome = scanner.scan_card(card)

        baseline = storage.get_baseline(conn, card.card_id, "NM")
        assert outcome.new_lows == ()
        assert notifier.all_time_lows == []
        assert baseline.lowest_price == 2670.0
        assert baseline.updated_at == original_now
    finally:
        conn.close()


def test_multi_condition_baselines_are_independent() -> None:
    conn = _conn()
    try:
        card = _card(conditions=("NM", "SP"))
        _baseline(conn, card, "NM", 2700.0)
        _baseline(conn, card, "SP", 2300.0)
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(_page_routes(card)), notifier)

        outcome = scanner.scan_card(card)

        assert outcome.new_lows == ("NM",)
        assert [call["condition"] for call in notifier.all_time_lows] == ["NM"]
        assert storage.get_baseline(conn, card.card_id, "NM").lowest_price == 2670.0
        assert storage.get_baseline(conn, card.card_id, "SP").lowest_price == 2300.0
        assert [(row["condition"], row["lowest_price"]) for row in _scan_results(conn)] == [
            ("NM", 2670.0),
            ("SP", 2350.0),
        ]
    finally:
        conn.close()


def test_no_matching_condition_is_logged_as_noop_without_scan_error() -> None:
    conn = _conn()
    try:
        card = _card(conditions=("NM", "HP"))
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(_page_routes(card)), notifier)

        outcome = scanner.scan_card(card)

        assert [result.condition for result in outcome.results] == ["NM"]
        assert storage.get_baseline(conn, card.card_id, "NM").lowest_price == 2670.0
        assert storage.get_baseline(conn, card.card_id, "HP") is None
        assert _count(conn, "scan_results") == 1
        assert _count(conn, "scan_errors") == 0
        assert notifier.all_time_lows == []
    finally:
        conn.close()


def test_sealed_first_sight_creates_sealed_baseline_without_all_time_low_notification() -> None:
    conn = _conn()
    try:
        card = _sealed_card()
        routes: dict[str, list[httpx.Response | Exception]] = {
            card.url: [httpx.Response(200, text=ETB_HTML)],
            GRENINJA_SPRITE_URL: [httpx.Response(200, content=GRENINJA_SPRITE)],
        }
        sleeps: list[float] = []
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(routes), notifier, sleeps)

        outcome = scanner.scan_card(card)

        baseline = storage.get_baseline(conn, card.card_id, "SEALED")
        assert baseline is not None
        assert baseline.lowest_price == 843.0
        assert outcome.initial_baselines == ("SEALED",)
        assert outcome.new_lows == ()
        assert notifier.all_time_lows == []
        assert [(row["condition"], row["lowest_price"]) for row in _scan_results(conn)] == [
            ("SEALED", 843.0)
        ]
        assert sleeps == [2]
    finally:
        conn.close()


def test_sealed_lower_scan_notifies_with_sealed_label_and_updates_baseline() -> None:
    conn = _conn()
    try:
        card = _sealed_card()
        first_html = _sealed_prod_html([{"qualid": "1", "precoFinal": "900.00"}])
        second_html = _sealed_prod_html([{"qualid": "1", "precoFinal": "700.00"}])
        routes = {
            card.url: [
                httpx.Response(200, text=first_html),
                httpx.Response(200, text=second_html),
            ],
        }
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(routes), notifier)

        scanner.scan_card(card)
        outcome = scanner.scan_card(card)

        assert outcome.new_lows == ("SEALED",)
        assert notifier.all_time_lows == [
            {
                "card_name": "ETB Ascended Heroes",
                "condition": "SEALED",
                "price": 700.0,
                "previous_lowest": 900.0,
                "url": card.url,
            }
        ]
        assert storage.get_baseline(conn, card.card_id, "SEALED").lowest_price == 700.0
        assert [(row["condition"], row["lowest_price"]) for row in _scan_results(conn)] == [
            ("SEALED", 900.0),
            ("SEALED", 700.0),
        ]
    finally:
        conn.close()


def test_sealed_tracks_l_price_when_non_sealed_listing_is_cheaper() -> None:
    conn = _conn()
    try:
        card = _sealed_card()
        html = _sealed_prod_html(
            [
                {"qualid": "6", "precoFinal": "10.00"},
                {"qualid": "5", "precoFinal": "20.00"},
                {"qualid": "1", "precoFinal": "80.00"},
            ]
        )
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(_page_routes(card, html=html)), notifier)

        outcome = scanner.scan_card(card)

        assert [(result.condition, result.lowest_price) for result in outcome.results] == [
            ("SEALED", 80.0)
        ]
        assert storage.get_baseline(conn, card.card_id, "SEALED").lowest_price == 80.0
    finally:
        conn.close()


def test_sealed_page_without_l_listing_is_noop_without_scan_error() -> None:
    conn = _conn()
    try:
        card = _sealed_card()
        html = _sealed_prod_html(
            [
                {"qualid": "6", "precoFinal": "10.00"},
                {"qualid": "5", "precoFinal": "20.00"},
            ]
        )
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(_page_routes(card, html=html)), notifier)

        outcome = scanner.scan_card(card)

        assert outcome.results == ()
        assert _count(conn, "scan_results") == 0
        assert _count(conn, "price_baselines") == 0
        assert _count(conn, "scan_errors") == 0
        assert notifier.all_time_lows == []
    finally:
        conn.close()


def test_mixed_card_and_sealed_list_processes_both_modes() -> None:
    conn = _conn()
    try:
        card_mode = _card("gengar")
        sealed = _sealed_card("etb")
        sealed_html = _sealed_prod_html([{"qualid": "1", "precoFinal": "100.00"}])
        routes = {
            card_mode.url: [httpx.Response(200, text=GENGAR_HTML)],
            sealed.url: [httpx.Response(200, text=sealed_html)],
        }
        sleeps: list[float] = []
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(routes), notifier, sleeps)

        summary = scanner.run([card_mode, sealed])

        assert storage.get_baseline(conn, card_mode.card_id, "NM").lowest_price == 2670.0
        assert storage.get_baseline(conn, sealed.card_id, "SEALED").lowest_price == 100.0
        assert summary.cards_scanned == 2
        assert summary.cards_failed == 0
        assert sleeps == [30]
    finally:
        conn.close()


def test_fetch_error_is_logged_and_cycle_continues_with_between_card_delay() -> None:
    conn = _conn()
    try:
        first = _card("first")
        second = _card("second")
        routes: dict[str, list[httpx.Response | Exception]] = {
            first.url: [httpx.Response(500), httpx.Response(500)],
            second.url: [httpx.Response(200, text=GENGAR_HTML)],
        }
        sleeps: list[float] = []
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(routes), notifier, sleeps)

        summary = scanner.run([first, second])

        errors = _scan_errors(conn)
        assert [row["error_type"] for row in errors] == ["fetch"]
        assert errors[0]["card_id"] == first.card_id
        assert storage.get_baseline(conn, second.card_id, "NM").lowest_price == 2670.0
        assert sleeps == [RETRY_DELAY_SECONDS, 30]
        assert summary.cards_scanned == 1
        assert summary.cards_failed == 1
        assert summary.stopped_early is False
    finally:
        conn.close()


def test_parser_failure_is_logged_and_cycle_continues() -> None:
    conn = _conn()
    try:
        first = _card("bad")
        second = _card("good")
        routes = {
            first.url: [httpx.Response(200, text="<html></html>")],
            second.url: [httpx.Response(200, text=GENGAR_HTML)],
        }
        sleeps: list[float] = []
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(routes), notifier, sleeps)

        summary = scanner.run([first, second])

        assert [row["error_type"] for row in _scan_errors(conn)] == ["parse"]
        assert storage.get_baseline(conn, second.card_id, "NM").lowest_price == 2670.0
        assert sleeps == [30]
        assert summary.cards_scanned == 1
        assert summary.cards_failed == 1
    finally:
        conn.close()


def test_sprite_decode_failure_records_alert_and_surviving_listing_continues() -> None:
    conn = _conn()
    try:
        card = _card("sprite-broken")
        routes: dict[str, list[httpx.Response | Exception]] = {
            card.url: [httpx.Response(200, text=_broken_sprite_html())],
            "https://example.com/imgnum/test.jpg": [httpx.Response(200, content=b"not-image")],
        }
        sleeps: list[float] = []
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(routes), notifier, sleeps)

        outcome = scanner.scan_card(card)

        assert [(result.condition, result.lowest_price) for result in outcome.results] == [
            ("NM", 934.15)
        ]
        assert [row["error_type"] for row in _scan_errors(conn)] == ["sprite_decode"]
        assert notifier.sprite_failures == [{"card_name": "Mega Gengar", "url": card.url}]
        assert storage.get_baseline(conn, card.card_id, "NM").lowest_price == 934.15
        assert sleeps == [2]
    finally:
        conn.close()


def test_cycle_stop_stops_whole_cycle_and_skips_delay_after_stop() -> None:
    conn = _conn()
    try:
        first = _card("first")
        second = _card("second")
        third = _card("third")
        handler = RouteHandler(
            {
                first.url: [httpx.Response(200, text=GENGAR_HTML)],
                second.url: [httpx.Response(429)],
                third.url: [httpx.Response(200, text=GENGAR_HTML)],
            }
        )
        sleeps: list[float] = []
        notifier = SpyNotifier()
        scanner = _scanner(conn, handler, notifier, sleeps)

        summary = scanner.run([first, second, third])

        assert storage.get_baseline(conn, first.card_id, "NM").lowest_price == 2670.0
        assert storage.get_baseline(conn, third.card_id, "NM") is None
        assert [row["error_type"] for row in _scan_errors(conn)] == ["http_429"]
        assert third.url not in handler.requests
        assert sleeps == [30]
        assert summary.stopped_early is True
        assert summary.cards_scanned == 1
        assert summary.cards_failed == 0
    finally:
        conn.close()


def test_cycle_stop_inside_sprite_fetch_stops_whole_cycle() -> None:
    conn = _conn()
    try:
        first = _card("sprite-stop")
        second = _card("second")
        handler = RouteHandler(
            {
                first.url: [httpx.Response(200, text=_sprite_stop_html())],
                "https://example.com/imgnum/stop.jpg": [httpx.Response(403)],
                second.url: [httpx.Response(200, text=GENGAR_HTML)],
            }
        )
        sleeps: list[float] = []
        notifier = SpyNotifier()
        scanner = _scanner(conn, handler, notifier, sleeps)

        summary = scanner.run([first, second])

        assert [row["error_type"] for row in _scan_errors(conn)] == ["http_403"]
        assert second.url not in handler.requests
        assert sleeps == [2]
        assert summary.stopped_early is True
        assert summary.cards_scanned == 0
    finally:
        conn.close()


def test_between_card_delay_runs_exactly_between_cards() -> None:
    conn = _conn()
    try:
        cards = [_card("one"), _card("two"), _card("three")]
        handler = RouteHandler(_page_routes(*cards))
        sleeps: list[float] = []
        notifier = SpyNotifier()
        scanner = _scanner(conn, handler, notifier, sleeps)

        summary = scanner.run(cards)

        assert sleeps == [30, 30]
        assert summary.cards_scanned == 3
    finally:
        conn.close()


def test_scan_summary_counts_mixed_run() -> None:
    conn = _conn()
    try:
        first = _card("new-low")
        second = _card("fetch-fail")
        third = _card("normal")
        _baseline(conn, first, "NM", 2700.0)
        routes: dict[str, list[httpx.Response | Exception]] = {
            first.url: [httpx.Response(200, text=GENGAR_HTML)],
            second.url: [httpx.Response(500), httpx.Response(500)],
            third.url: [httpx.Response(200, text=GENGAR_HTML)],
        }
        sleeps: list[float] = []
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(routes), notifier, sleeps)

        summary = scanner.run([first, second, third])

        assert summary.cards_scanned == 2
        assert summary.cards_failed == 1
        assert summary.new_lows == 1
        assert summary.stopped_early is False
    finally:
        conn.close()


def test_injected_clock_is_used_for_all_rows_for_one_card() -> None:
    conn = _conn()
    try:
        card = _card(conditions=("NM", "SP"))
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(_page_routes(card)), notifier)

        scanner.scan_card(card)

        assert [row["scanned_at"] for row in _scan_results(conn)] == [FIXED_NOW, FIXED_NOW]
        baselines = conn.execute(
            "SELECT created_at, updated_at FROM price_baselines ORDER BY condition"
        ).fetchall()
        assert [(row["created_at"], row["updated_at"]) for row in baselines] == [
            (FIXED_NOW, FIXED_NOW),
            (FIXED_NOW, FIXED_NOW),
        ]
    finally:
        conn.close()


def test_real_discord_notifier_can_be_wired_through_scanner() -> None:
    conn = _conn()
    try:
        card = _card()
        page_handler = RouteHandler(_page_routes(card))
        sleeps: list[float] = []
        posted: list[httpx.Request] = []

        def discord_handler(request: httpx.Request) -> httpx.Response:
            posted.append(request)
            return httpx.Response(204)

        notifier = DiscordNotifier(
            "https://discord.example/webhook",
            client=_client(discord_handler),
        )
        scanner = Scanner(
            fetcher=_fetcher(page_handler, sleeps),
            notifier=notifier,
            conn=conn,
            send_initial_baseline=True,
            clock=lambda: FIXED_NOW,
        )

        scanner.scan_card(card)

        assert len(posted) == 1
        assert json.loads(posted[0].content.decode("utf-8")) == {
            "content": (
                "Mega Gengar - NM - R$2.670,00 - Initial baseline - "
                "https://www.ligapokemon.com.br/card/gengar"
            )
        }
    finally:
        conn.close()


def test_precocss_fixture_decodes_through_fetcher_and_real_parser() -> None:
    conn = _conn()
    try:
        card = _card("greninja", name="Greninja", conditions=("NM",))
        routes: dict[str, list[httpx.Response | Exception]] = {
            card.url: [httpx.Response(200, text=GRENINJA_HTML)],
            GRENINJA_SPRITE_URL: [httpx.Response(200, content=GRENINJA_SPRITE)],
        }
        sleeps: list[float] = []
        notifier = SpyNotifier()
        scanner = _scanner(conn, RouteHandler(routes), notifier, sleeps)

        outcome = scanner.scan_card(card)

        assert [(result.condition, result.lowest_price) for result in outcome.results] == [
            ("NM", 843.0)
        ]
        assert storage.get_baseline(conn, card.card_id, "NM").lowest_price == 843.0
        assert sleeps == [2]
    finally:
        conn.close()
