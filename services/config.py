"""Application and card configuration loading and validation (FRD §3, §12).

Invalid configuration must abort startup, so every loader raises
:class:`ConfigError` with a clear, actionable message rather than returning a
partially-valid result. Unknown JSON keys in the card config are ignored for
forward compatibility (FRD §3).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from models.card import Card

# Condition acronyms recognised by the LigaPokemon parser (FRD §10).
VALID_CONDITIONS = frozenset({"M", "NM", "SP", "MP", "HP", "D"})

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class ConfigError(Exception):
    """Raised when application or card configuration is invalid."""


@dataclass(slots=True, frozen=True)
class AppConfig:
    """Environment-derived application settings (FRD §3)."""

    discord_webhook_url: str
    cards_config_path: Path
    database_path: Path
    request_delay_seconds: int
    sprite_request_delay_seconds: int
    http_timeout_seconds: int
    user_agent: str
    send_initial_baseline_notification: bool
    log_max_bytes: int
    log_backup_count: int


@dataclass(slots=True, frozen=True)
class Config:
    """The fully-resolved configuration: app settings plus the card list."""

    app: AppConfig
    cards: tuple[Card, ...]


def load_config(env_path: str | os.PathLike[str] | None = None) -> Config:
    """Load and validate the full application configuration.

    Args:
        env_path: Optional explicit path to a ``.env`` file. When ``None``,
            python-dotenv searches for a ``.env`` in the working directory.

    Returns:
        A validated :class:`Config`.

    Raises:
        ConfigError: If any environment value or the card config is invalid.
    """
    app = load_app_config(env_path)
    cards = load_cards(app.cards_config_path)
    return Config(app=app, cards=cards)


def load_app_config(env_path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load and validate application settings from the environment.

    ``.env`` values do not override variables already set in the real
    environment (python-dotenv default), which keeps tests hermetic.
    """
    load_dotenv(env_path)
    return AppConfig(
        discord_webhook_url=_require_str("DISCORD_WEBHOOK_URL"),
        cards_config_path=Path(os.getenv("CARDS_CONFIG_PATH", "cards.json")),
        database_path=Path(os.getenv("DATABASE_PATH", "watcher.db")),
        request_delay_seconds=_positive_int("REQUEST_DELAY_SECONDS", 30),
        sprite_request_delay_seconds=_positive_int("SPRITE_REQUEST_DELAY_SECONDS", 2),
        http_timeout_seconds=_positive_int("HTTP_TIMEOUT_SECONDS", 20),
        user_agent=os.getenv("USER_AGENT", "PokemonCardWatcher/1.0"),
        send_initial_baseline_notification=_bool(
            "SEND_INITIAL_BASELINE_NOTIFICATION", default=False
        ),
        log_max_bytes=_positive_int("LOG_MAX_BYTES", 1_048_576),
        log_backup_count=_non_negative_int("LOG_BACKUP_COUNT", 5),
    )


def load_cards(path: Path) -> tuple[Card, ...]:
    """Load and validate the card list from a JSON file (FRD §3).

    Unknown keys on each card object are ignored for forward compatibility.
    Entries without ``conditions`` or with an empty ``conditions`` array are
    sealed products and track one SEALED price.

    Raises:
        ConfigError: If the file is missing, not valid JSON, not a non-empty
            array, or any card entry is malformed.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"Cards config not found: {path}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Cards config is not valid JSON ({path}): {exc}") from exc

    if not isinstance(data, list):
        raise ConfigError(f"Cards config must be a JSON array, got {type(data).__name__}")
    if not data:
        raise ConfigError("Cards config must contain at least one card")

    return tuple(_parse_card(entry, index) for index, entry in enumerate(data))


def _parse_card(entry: object, index: int) -> Card:
    """Validate one raw card object and build a :class:`Card` (FRD §3)."""
    if not isinstance(entry, dict):
        raise ConfigError(f"Card at index {index} must be a JSON object")

    name = entry.get("name")
    url = entry.get("url")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"Card at index {index} is missing a non-empty 'name'")
    if not isinstance(url, str) or not url.strip():
        raise ConfigError(f"Card {name!r} is missing a non-empty 'url'")

    if "conditions" not in entry:
        return Card(name=name.strip(), conditions=(), url=url.strip(), is_sealed=True)

    conditions = entry["conditions"]
    if not isinstance(conditions, list):
        raise ConfigError(f"Card {name!r} must have a 'conditions' array")
    if not conditions:
        return Card(name=name.strip(), conditions=(), url=url.strip(), is_sealed=True)

    normalized: list[str] = []
    for condition in conditions:
        if not isinstance(condition, str):
            raise ConfigError(f"Card {name!r} has a non-string condition: {condition!r}")
        acronym = condition.strip().upper()
        if acronym not in VALID_CONDITIONS:
            valid = ", ".join(sorted(VALID_CONDITIONS))
            raise ConfigError(
                f"Card {name!r} has unknown condition {condition!r}; valid: {valid}"
            )
        normalized.append(acronym)

    # Only known fields are read, so any extra JSON keys are ignored (FRD §3).
    return Card(name=name.strip(), conditions=tuple(normalized), url=url.strip())


def _require_str(name: str) -> str:
    """Return a required, non-empty environment variable."""
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _positive_int(name: str, default: int) -> int:
    """Return an environment integer that must be > 0, or the default if unset."""
    value = _optional_int(name, default)
    if value <= 0:
        raise ConfigError(f"{name} must be a positive integer, got {value}")
    return value


def _non_negative_int(name: str, default: int) -> int:
    """Return an environment integer that must be >= 0, or the default if unset."""
    value = _optional_int(name, default)
    if value < 0:
        raise ConfigError(f"{name} must be a non-negative integer, got {value}")
    return value


def _optional_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _bool(name: str, *, default: bool) -> bool:
    """Return an environment boolean (truthy: 1/true/yes/on), or the default."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUTHY
