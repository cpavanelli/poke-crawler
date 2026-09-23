"""One-cycle scanner orchestration (FRD §6, §7, §12)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from models.card import Card
from models.price_result import PriceResult
from parsers.amazon import AmazonParser
from parsers.base import CatalogParser, MarketplaceParser
from parsers.copag import CopagParser
from parsers.fourse import FourseParser
from parsers.ligapokemon_parser import LigaPokemonParser, SpriteErrorHandler, SpriteFetcher
from parsers.nuvemshop import NuvemshopParser
from services import storage
from services.catalog import detect_changes
from services.fetcher import CycleStop, FetchError, HttpFetcher, PageNotFound
from services.notifier import DiscordNotifier
from services.pricing import lowest_prices, lowest_sealed_price
from services.storage import local_now_iso

logger = logging.getLogger(__name__)

ParserFactory = Callable[[SpriteFetcher, SpriteErrorHandler], MarketplaceParser]
CatalogParserFactory = Callable[[], CatalogParser]

DEFAULT_PARSERS: tuple[ParserFactory, ...] = (
    lambda fetch, on_err: LigaPokemonParser(
        sprite_fetcher=fetch,
        on_sprite_error=on_err,
    ),
)

# Catalog parsers need neither a sprite fetcher nor a sprite-error handler
# (FRD §21), so they are a separate factory list rather than a widened one.
DEFAULT_CATALOG_PARSERS: tuple[CatalogParserFactory, ...] = (
    NuvemshopParser,
    FourseParser,
    AmazonParser,
    CopagParser,
)


@dataclass(slots=True, frozen=True)
class CardOutcome:
    """Result for one configured entry in the scanner workflow."""

    card_id: str
    results: tuple[PriceResult, ...]
    new_lows: tuple[str, ...]
    initial_baselines: tuple[str, ...]
    error_type: str | None = None
    catalog_changes: int = 0


@dataclass(slots=True, frozen=True)
class ScanSummary:
    """Small run summary for logging and tests."""

    cards_scanned: int
    cards_failed: int
    new_lows: int
    stopped_early: bool
    catalog_updates: int = 0


def _failed(card_id: str, error_type: str) -> CardOutcome:
    """Build the empty outcome used for a logged, non-fatal entry failure."""
    return CardOutcome(
        card_id=card_id,
        results=(),
        new_lows=(),
        initial_baselines=(),
        error_type=error_type,
    )


class Scanner:
    """Coordinate one full pass over configured entries (FRD §6, §21)."""

    def __init__(
        self,
        *,
        fetcher: HttpFetcher,
        notifier: DiscordNotifier,
        conn: sqlite3.Connection,
        parsers: Sequence[ParserFactory] | None = None,
        catalog_parsers: Sequence[CatalogParserFactory] | None = None,
        send_initial_baseline: bool = False,
        clock: Callable[[], str] = local_now_iso,
    ) -> None:
        self._fetcher = fetcher
        self._notifier = notifier
        self._conn = conn
        self._parsers = tuple(parsers) if parsers is not None else DEFAULT_PARSERS
        self._catalog_parsers = (
            tuple(catalog_parsers) if catalog_parsers is not None else DEFAULT_CATALOG_PARSERS
        )
        self._send_initial_baseline = send_initial_baseline
        self._clock = clock

    def run(self, cards: Sequence[Card]) -> ScanSummary:
        """One full pass over the configured entries (FRD §6). Stops early on 403/429."""
        cards_scanned = 0
        cards_failed = 0
        new_lows = 0
        catalog_updates = 0
        stopped_early = False

        for index, card in enumerate(cards):
            try:
                outcome = self.scan_catalog(card) if card.is_catalog else self.scan_card(card)
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
            catalog_updates += outcome.catalog_changes

            if index < len(cards) - 1:
                self._fetcher.wait_between_cards()

        summary = ScanSummary(
            cards_scanned=cards_scanned,
            cards_failed=cards_failed,
            new_lows=new_lows,
            stopped_early=stopped_early,
            catalog_updates=catalog_updates,
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
        except PageNotFound as exc:
            logger.warning("Page not found for %s: %s", card.name, exc)
            storage.insert_scan_error(
                self._conn,
                card_id=card_id,
                url=card.url,
                error_type="not_found",
                error_message=str(exc),
                occurred_at=now,
            )
            return CardOutcome(
                card_id=card_id,
                results=(),
                new_lows=(),
                initial_baselines=(),
                error_type="not_found",
            )
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

        if card.is_sealed:
            sealed_result = lowest_sealed_price(listings)
            results = (sealed_result,) if sealed_result is not None else ()
            if not results:
                logger.info("No sealed listing for %s", card.name)
        else:
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

    def scan_catalog(self, card: Card) -> CardOutcome:
        """Run one New-Product (Catalog) Watch (FRD §21). Raises CycleStop on 403/429."""
        watch_id = card.card_id
        now = self._clock()
        parser = self._select_catalog_parser(card=card, card_id=watch_id, now=now)
        if parser is None:
            return _failed(watch_id, "parse")

        html: str | None = None
        try:
            # Page 1 only: one request per watch, no pagination (FRD §21).
            html = self._fetcher.get_page(card.url)
        except CycleStop:
            raise
        except PageNotFound as exc:
            # A watched product page answers 404 until the store publishes it.
            # Treating that as an empty page seeds the watch now, so the product
            # is reported as new the day it appears (FRD §21). The row keeps a
            # typo'd URL visible instead of silently watching nothing.
            logger.warning("Page not found for %s; treated as empty: %s", card.name, exc)
            storage.insert_scan_error(
                self._conn,
                card_id=watch_id,
                url=card.url,
                error_type="not_found",
                error_message=str(exc),
                occurred_at=now,
            )
        except FetchError as exc:
            logger.error("Fetch failed for %s: %s", card.name, exc)
            storage.insert_scan_error(
                self._conn,
                card_id=watch_id,
                url=card.url,
                error_type="fetch",
                error_message=str(exc),
                occurred_at=now,
            )
            return _failed(watch_id, "fetch")

        try:
            products = parser.parse_catalog(html) if html is not None else []
        except CycleStop:
            raise
        except Exception as exc:
            logger.error("Parse failed for %s: %s", card.name, exc)
            storage.insert_scan_error(
                self._conn,
                card_id=watch_id,
                url=card.url,
                error_type="parse",
                error_message=str(exc),
                occurred_at=now,
            )
            return _failed(watch_id, "parse")

        if not storage.is_watch_seeded(self._conn, watch_id):
            # First scan of this watch: seed the baseline silently (FRD §21).
            # Seeding is keyed on the watch, not on stored products, so an empty
            # collection page still counts as seeded and the next product to
            # appear is reported as new.
            storage.upsert_collection_products(self._conn, watch_id, products, now=now)
            storage.mark_watch_scanned(self._conn, watch_id, now=now)
            logger.info("Seeded %s products for %s", len(products), card.name)
            return CardOutcome(
                card_id=watch_id,
                results=(),
                new_lows=(),
                initial_baselines=(),
            )

        changes = detect_changes(products, storage.get_known_stock(self._conn, watch_id))
        if changes:
            logger.info("Catalog updates for %s: %s", card.name, len(changes))
            # Notify before persisting, as the price path does, so a product is
            # never marked known while its alert was never attempted (FRD §6).
            self._notifier.notify_catalog_updates(watch_name=card.name, changes=changes)
        else:
            logger.info("No catalog updates for %s", card.name)

        storage.upsert_collection_products(self._conn, watch_id, products, now=now)
        storage.mark_watch_scanned(self._conn, watch_id, now=now)
        return CardOutcome(
            card_id=watch_id,
            results=(),
            new_lows=(),
            initial_baselines=(),
            catalog_changes=len(changes),
        )

    def _select_catalog_parser(
        self,
        *,
        card: Card,
        card_id: str,
        now: str,
    ) -> CatalogParser | None:
        for factory in self._catalog_parsers:
            parser = factory()
            if parser.can_handle(card.url):
                return parser

        self._record_no_parser(card=card, card_id=card_id, now=now)
        return None

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

        self._record_no_parser(card=card, card_id=card_id, now=now)
        return None

    def _record_no_parser(self, *, card: Card, card_id: str, now: str) -> None:
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
