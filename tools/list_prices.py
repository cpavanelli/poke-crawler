"""Print all LigaPokemon listings for one card URL (FRD §11, §19, §4)."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from models.listing import Listing
from parsers.ligapokemon_parser import LigaPokemonParser
from services.fetcher import CycleStop, FetchError, HttpFetcher

CONDITION_ORDER = ("M", "NM", "SP", "MP", "HP", "D")


def sort_listings(listings: list[Listing]) -> list[Listing]:
    """Sort by condition (M, NM, SP, MP, HP, D) then price ascending.

    Unknown conditions sort last.
    """
    rank = {condition: index for index, condition in enumerate(CONDITION_ORDER)}
    return sorted(
        listings,
        key=lambda listing: (
            rank.get(listing.condition, len(CONDITION_ORDER)),
            listing.price,
        ),
    )


def format_listings(listings: list[Listing]) -> str:
    """Format one ``CONDITION PRICE`` line per listing."""
    return "\n".join(
        f"{listing.condition} {listing.price:.2f}" for listing in sort_listings(listings)
    )


def run(
    url: str,
    *,
    fetcher: HttpFetcher,
    on_sprite_error: Callable[[str], None],
) -> list[Listing]:
    """Fetch the page and return every listing.

    ``sprite_fetcher`` is ``fetcher.get_sprite`` directly: the decoder already
    normalises protocol-relative sprite URLs before the fetcher sees them.
    """
    parser = LigaPokemonParser(
        sprite_fetcher=fetcher.get_sprite,
        on_sprite_error=on_sprite_error,
    )
    html = fetcher.get_page(url)
    return parser.parse_listings(html)


def main(argv: Sequence[str] | None = None, *, fetcher: HttpFetcher | None = None) -> int:
    """Run the CLI; ``fetcher`` is an offline test seam."""
    parser = argparse.ArgumentParser(
        description="Print all listing prices from one LigaPokemon card URL.",
    )
    parser.add_argument("url")
    args = parser.parse_args(argv)

    if fetcher is None:
        load_dotenv()
        fetcher = HttpFetcher(
            user_agent=os.getenv("USER_AGENT", "PokemonCardWatcher/1.0"),
            timeout_seconds=int(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
            request_delay_seconds=0,  # The one-URL tool never waits between cards.
            sprite_request_delay_seconds=int(os.getenv("SPRITE_REQUEST_DELAY_SECONDS", "2")),
        )

    def on_sprite_error(message: str) -> None:
        print(f"\u26a0\ufe0f sprite decode failed: {message}", file=sys.stderr)

    try:
        with fetcher:
            listings = run(args.url, fetcher=fetcher, on_sprite_error=on_sprite_error)
    except CycleStop as exc:
        print(
            f"aborted: HTTP {exc.status_code} from source \u2014 stopping (anti-abuse)",
            file=sys.stderr,
        )
        return 2
    except FetchError:
        print(f"fetch failed: {args.url}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"could not parse listings: {exc}", file=sys.stderr)
        return 1

    if not listings:
        print(f"no listings found for {args.url}", file=sys.stderr)
        return 0

    print(format_listings(listings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
