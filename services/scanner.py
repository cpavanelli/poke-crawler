"""One-cycle scanner orchestration (FRD §6, §7, §12)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from models.card import Card
from models.price_result import PriceResult
from parsers.base import MarketplaceParser
from parsers.ligapokemon_parser import LigaPokemonParser, SpriteErrorHandler, SpriteFetcher
from services import storage
from services.fetcher import CycleStop, FetchError, HttpFetcher
from services.notifier import DiscordNotifier
from services.pricing import lowest_prices
from services.storage import local_now_iso

logger = logging.getLogger(__name__)

ParserFactory = Callable[[SpriteFetcher, SpriteErrorHandler], MarketplaceParser]

DEFAULT_PARSERS: tuple[ParserFactory, ...] = (
    lambda fetch, on_err: LigaPokemonParser(
        sprite_fetcher=fetch,
        on_sprite_error=on_err,
    ),
)


@dataclass(slots=True, frozen=True)
class CardOutcome:
    """Result for one configured card in the scanner workflow."""

    card_id: str
    results: tuple[PriceResult, ...]
    new_lows: tuple[str, ...]
    initial_baselines: tuple[str, ...]
    error_type: str | None = None


@dataclass(slots=True, frozen=True)
class ScanSummary:
    """Small run summary for logging and tests."""

    cards_scanned: int
    cards_failed: int
    new_lows: int
    stopped_early: bool


class Scanner:
    """Coordinate one full pass over configured cards (FRD §6)."""

    def __init__(
        self,
        *,
        fetcher: HttpFetcher,
        notifier: DiscordNotifier,
        conn: sqlite3.Connection,
        parsers: Sequence[ParserFactory] | None = None,
        send_initial_baseline: bool = False,
        clock: Callable[[], str] = local_now_iso,
    ) -> None:
        self._fetcher = fetcher
        self._notifier = notifier
        self._conn = conn
        self._parsers = tuple(parsers) if parsers is not None else DEFAULT_PARSERS
        self._send_initial_baseline = send_initial_baseline
        self._clock = clock

    def run(self, cards: Sequence[Card]) -> ScanSummary:
        """One full pass over the card list (FRD §6). Stops early on 403/429."""
        cards_scanned = 0
        cards_failed = 0
        new_lows = 0
        stopped_early = False

        for index, card in enumerate(cards):
            try:
                outcome = self.scan_card(card)
            except CycleStop as exc:
                logger.warning("Stopping cycle: HTTP %s from %s", exc.status_code, exc.url)
                storage.insert_scan_error(
                    self._conn,
                    card_id=card.card_id,
                    url=exc.url,
                    error_type=f"http_{exc.status_code}",
                    error_message=str(exc),
                    occurred_at=self._clock(),
                )
                stopped_early = True
                break

            if outcome.error_type is None:
                cards_scanned += 1
            else:
                cards_failed += 1
            new_lows += len(outcome.new_lows)

            if index < len(cards) - 1:
                self._fetcher.wait_between_cards()

        summary = ScanSummary(
            cards_scanned=cards_scanned,
            cards_failed=cards_failed,
            new_lows=new_lows,
            stopped_early=stopped_early,
        )
        logger.info("Scan complete: %s", summary)
        return summary

    def scan_card(self, card: Card) -> CardOutcome:
        """Run FRD §6 steps 1-9 for one card. Raises CycleStop on 403/429."""
        card_id = card.card_id
        now = self._clock()
        parser = self._select_parser(card=card, card_id=card_id, now=now)
        if parser is None:
            return CardOutcome(
                card_id=card_id,
                results=(),
                new_lows=(),
                initial_baselines=(),
                error_type="parse",
            )

        try:
            html = self._fetcher.get_page(card.url)
        except CycleStop:
            raise
        except FetchError as exc:
            logger.error("Fetch failed for %s: %s", card.name, exc)
            storage.insert_scan_error(
                self._conn,
                card_id=card_id,
                url=card.url,
                error_type="fetch",
                error_message=str(exc),
                occurred_at=now,
            )
            return CardOutcome(
                card_id=card_id,
                results=(),
                new_lows=(),
                initial_baselines=(),
                error_type="fetch",
            )

        try:
            listings = parser.parse_listings(html)
        except CycleStop:
            raise
        except Exception as exc:
            logger.error("Parse failed for %s: %s", card.name, exc)
            storage.insert_scan_error(
                self._conn,
                card_id=card_id,
                url=card.url,
                error_type="parse",
                error_message=str(exc),
                occurred_at=now,
            )
            return CardOutcome(
                card_id=card_id,
                results=(),
                new_lows=(),
                initial_baselines=(),
                error_type="parse",
            )

        results = tuple(lowest_prices(listings, card.conditions))
        result_conditions = {result.condition for result in results}
        missing = [
            condition for condition in card.conditions if condition not in result_conditions
        ]
        if missing:
            logger.info("No listings for %s conditions %s", card.name, missing)

        new_lows: list[str] = []
        initial_baselines: list[str] = []
        for result in results:
            outcome = self._record_and_compare(
                card=card,
                card_id=card_id,
                result=result,
                now=now,
            )
            if outcome == "new_low":
                new_lows.append(result.condition)
            elif outcome == "initial":
                initial_baselines.append(result.condition)

        return CardOutcome(
            card_id=card_id,
            results=results,
            new_lows=tuple(new_lows),
            initial_baselines=tuple(initial_baselines),
        )

    def _select_parser(
        self,
        *,
        card: Card,
        card_id: str,
        now: str,
    ) -> MarketplaceParser | None:
        def on_sprite_error(message: str) -> None:
            storage.insert_scan_error(
                self._conn,
                card_id=card_id,
                url=card.url,
                error_type="sprite_decode",
                error_message=message,
                occurred_at=now,
            )
            self._notifier.notify_sprite_decode_failure(card_name=card.name, url=card.url)
            logger.warning("Sprite decode failed for %s: %s", card.name, message)

        for factory in self._parsers:
            parser = factory(self._fetcher.get_sprite, on_sprite_error)
            if parser.can_handle(card.url):
                return parser

        message = "no parser for url"
        logger.error("Parse failed for %s: %s", card.name, message)
        storage.insert_scan_error(
            self._conn,
            card_id=card_id,
            url=card.url,
            error_type="parse",
            error_message=message,
            occurred_at=now,
        )
        return None

    def _record_and_compare(
        self,
        *,
        card: Card,
        card_id: str,
        result: PriceResult,
        now: str,
    ) -> str | None:
        storage.insert_scan_result(
            self._conn,
            card_id,
            card.name,
            card.url,
            result.condition,
            result.lowest_price,
            scanned_at=now,
        )

        baseline = storage.get_baseline(self._conn, card_id, result.condition)
        current = result.lowest_price
        if baseline is None:
            storage.upsert_baseline(
                self._conn,
                card_id,
                card.name,
                card.url,
                result.condition,
                current,
                now=now,
            )
            logger.info("Baseline created: %s %s = %s", card.name, result.condition, current)
            if self._send_initial_baseline:
                self._notifier.notify_initial_baseline(
                    card_name=card.name,
                    condition=result.condition,
                    price=current,
                    url=card.url,
                )
            return "initial"

        if current < baseline.lowest_price:
            previous_lowest = baseline.lowest_price
            logger.info(
                "New all-time-low: %s %s %s -> %s",
                card.name,
                result.condition,
                previous_lowest,
                current,
            )
            self._notifier.notify_all_time_low(
                card_name=card.name,
                condition=result.condition,
                price=current,
                previous_lowest=previous_lowest,
                url=card.url,
            )
            storage.upsert_baseline(
                self._conn,
                card_id,
                card.name,
                card.url,
                result.condition,
                current,
                now=now,
            )
            return "new_low"

        return None
