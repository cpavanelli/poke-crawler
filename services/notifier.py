"""Discord notification formatting and delivery (FRD §7, §12)."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx

from services.catalog import CHANGE_NEW, Change
from services.config import AppConfig

logger = logging.getLogger(__name__)

# Catalog alert vocabulary (FRD §21). Discord caps a message at 2000 chars, so
# a very large batch is truncated rather than dropped.
STOCK_IN = "Em estoque"
STOCK_OUT = "Esgotado"
UNKNOWN_PRICE = "—"
MAX_CHANGE_LINES = 20


def format_brl(value: float) -> str:
    """Format a price as Brazilian currency, e.g. 1250.0 -> 'R$1.250,00' (FRD §7).

    Thousands separator is '.', decimal separator is ',', always two decimals.
    """
    grouped = f"{value:,.2f}"
    swapped = grouped.translate(str.maketrans({",": ".", ".": ","}))
    return f"R${swapped}"


def format_all_time_low(
    *,
    card_name: str,
    condition: str,
    price: float,
    previous_lowest: float,
    url: str,
) -> str:
    """Format a new all-time-low Discord message (FRD §7)."""
    return (
        f"{card_name} - {condition} - {format_brl(price)} - "
        f"Previous lowest: {format_brl(previous_lowest)} - {url}"
    )


def format_initial_baseline(
    *,
    card_name: str,
    condition: str,
    price: float,
    url: str,
) -> str:
    """Format an initial-baseline Discord message (FRD §7)."""
    return f"{card_name} - {condition} - {format_brl(price)} - Initial baseline - {url}"


def format_catalog_updates(*, watch_name: str, changes: Sequence[Change]) -> str:
    """Format one consolidated catalog-watch Discord message (FRD §7, §21).

    All new and restocked products of a single scan share one message; at most
    MAX_CHANGE_LINES lines are listed and the rest are summarised.
    """
    header = f"🆕 {watch_name} — {len(changes)} update(s)"
    lines = [
        f"• {'NEW' if change.kind == CHANGE_NEW else 'RESTOCK'}: "
        f"{change.product.name} - "
        f"{UNKNOWN_PRICE if change.product.price is None else format_brl(change.product.price)} - "
        f"{STOCK_IN if change.product.in_stock else STOCK_OUT} - "
        f"{change.product.url}"
        for change in changes[:MAX_CHANGE_LINES]
    ]
    remaining = len(changes) - len(lines)
    if remaining > 0:
        lines.append(f"• … and {remaining} more")
    return "\n".join([header, *lines])


def format_sprite_decode_alert(*, card_name: str, url: str) -> str:
    """Format a sprite-decode failure Discord message (FRD §7, §10)."""
    return f"⚠️ Sprite decode failed - {card_name} - {url} - listing skipped"


class DiscordNotifier:
    """Plain Discord webhook notifier for scan events (FRD §7, §12)."""

    def __init__(
        self,
        webhook_url: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._webhook_url = webhook_url
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_seconds))

    @classmethod
    def from_app_config(
        cls,
        app: AppConfig,
        *,
        client: httpx.Client | None = None,
    ) -> DiscordNotifier:
        """Build straight from the validated AppConfig (uses discord_webhook_url)."""
        return cls(app.discord_webhook_url, client=client)

    def notify_all_time_low(
        self,
        *,
        card_name: str,
        condition: str,
        price: float,
        previous_lowest: float,
        url: str,
    ) -> bool:
        """Send a new all-time-low Discord notification (FRD §7)."""
        return self._send(
            format_all_time_low(
                card_name=card_name,
                condition=condition,
                price=price,
                previous_lowest=previous_lowest,
                url=url,
            )
        )

    def notify_initial_baseline(
        self,
        *,
        card_name: str,
        condition: str,
        price: float,
        url: str,
    ) -> bool:
        """Send an initial-baseline Discord notification (FRD §7)."""
        return self._send(
            format_initial_baseline(
                card_name=card_name,
                condition=condition,
                price=price,
                url=url,
            )
        )

    def notify_catalog_updates(
        self,
        *,
        watch_name: str,
        changes: Sequence[Change],
    ) -> bool:
        """Send one consolidated catalog-watch notification (FRD §21).

        Never called with an empty batch; a scan without changes sends nothing.
        """
        if not changes:
            return False
        return self._send(format_catalog_updates(watch_name=watch_name, changes=changes))

    def notify_sprite_decode_failure(self, *, card_name: str, url: str) -> bool:
        """Send a sprite-decode failure Discord notification (FRD §7, §10)."""
        return self._send(format_sprite_decode_alert(card_name=card_name, url=url))

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> DiscordNotifier:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _send(self, content: str) -> bool:
        """POST to Discord and swallow all webhook failures (FRD §12)."""
        try:
            response = self._client.post(self._webhook_url, json={"content": content})
        except httpx.HTTPError as exc:
            logger.error("Discord notification failed: %s", exc)
            return False

        if response.is_success:
            logger.info("Discord notification sent")
            return True

        logger.warning(
            "Discord notification failed with HTTP %s",
            response.status_code,
        )
        return False
