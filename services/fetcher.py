"""Shared polite HTTP fetch layer (FRD §4, §12, §13, §17)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

from services.config import AppConfig

MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 5
STOP_STATUSES = frozenset({403, 429})

logger = logging.getLogger(__name__)


class FetchError(Exception):
    """Transient fetch failure that exhausted the retry budget (FRD §12, §13)."""


class CycleStop(Exception):
    """HTTP 403 or 429 from the source; the whole scan cycle must stop.

    Carries the status code and URL for scanner logging (FRD §12, §17).
    """

    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(f"HTTP {status_code} from {url}")


class HttpFetcher:
    """HTTP client wrapper for page and sprite fetches (FRD §4, §13, §17)."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float,
        request_delay_seconds: float,
        sprite_request_delay_seconds: float,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._user_agent = user_agent
        self._timeout_seconds = timeout_seconds
        self._request_delay_seconds = request_delay_seconds
        self._sprite_request_delay_seconds = sprite_request_delay_seconds
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_seconds))
        self._sleep = sleep

    @classmethod
    def from_app_config(
        cls,
        app: AppConfig,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> HttpFetcher:
        """Build a fetcher straight from the validated AppConfig (FRD §3)."""
        return cls(
            user_agent=app.user_agent,
            timeout_seconds=app.http_timeout_seconds,
            request_delay_seconds=app.request_delay_seconds,
            sprite_request_delay_seconds=app.sprite_request_delay_seconds,
            client=client,
            sleep=sleep,
        )

    def get_page(self, url: str) -> str:
        """GET a card page and return response text (FRD §12, §13, §17)."""
        return self._request(url).text

    def get_sprite(self, url: str) -> bytes:
        """Sleep, then GET a digit sprite as in-memory bytes (FRD §4, §13)."""
        return self._request(
            url, delay_before=self._sprite_request_delay_seconds
        ).content

    def wait_between_cards(self) -> None:
        """Sleep REQUEST_DELAY_SECONDS between card page requests (FRD §4)."""
        self._sleep(self._request_delay_seconds)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> HttpFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, url: str, *, delay_before: float = 0.0) -> httpx.Response:
        if delay_before:
            self._sleep(delay_before)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._client.get(
                    url,
                    headers={"User-Agent": self._user_agent},
                )
                if response.status_code in STOP_STATUSES:
                    raise CycleStop(response.status_code, url)
                if response.is_error:
                    response.raise_for_status()
                return response
            except CycleStop:
                raise
            except (
                httpx.TimeoutException,
                httpx.TransportError,
                httpx.HTTPStatusError,
            ) as exc:
                if attempt < MAX_ATTEMPTS:
                    logger.warning(
                        "Fetch attempt %s/%s failed for %s; retrying in %ss",
                        attempt,
                        MAX_ATTEMPTS,
                        url,
                        RETRY_DELAY_SECONDS,
                    )
                    self._sleep(RETRY_DELAY_SECONDS)
                    continue

                logger.error(
                    "Fetch failed after %s attempts for %s",
                    MAX_ATTEMPTS,
                    url,
                )
                raise FetchError(f"Fetch failed after {MAX_ATTEMPTS} attempts: {url}") from exc

        # Unreachable: the final attempt always returns or raises above. Kept as
        # a guard so the function never silently falls through to None.
        raise FetchError(f"Fetch failed after {MAX_ATTEMPTS} attempts: {url}")
