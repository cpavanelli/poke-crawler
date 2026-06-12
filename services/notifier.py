"""Discord notification formatting and delivery (FRD §7, §12)."""

from __future__ import annotations

import logging

import httpx

from services.config import AppConfig

logger = logging.getLogger(__name__)


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
