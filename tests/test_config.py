"""Tests for configuration loading and validation (FRD §3, §12)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.config import (
    ConfigError,
    load_app_config,
    load_cards,
    load_config,
)

# A path that does not exist, passed as env_path so python-dotenv is a no-op
# and tests rely solely on monkeypatched environment variables.
_NO_ENV = Path("does-not-exist.env")

_REQUIRED_ENV = {
    "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/abc/def",
}


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    # Clear any optional vars that could leak in from the real environment.
    for key in (
        "CARDS_CONFIG_PATH",
        "DATABASE_PATH",
        "REQUEST_DELAY_SECONDS",
        "SPRITE_REQUEST_DELAY_SECONDS",
        "HTTP_TIMEOUT_SECONDS",
        "USER_AGENT",
        "SEND_INITIAL_BASELINE_NOTIFICATION",
        "LOG_MAX_BYTES",
        "LOG_BACKUP_COUNT",
        "DISCORD_WEBHOOK_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**_REQUIRED_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)


def _write_cards(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "cards.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --- AppConfig ------------------------------------------------------------


def test_app_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    app = load_app_config(_NO_ENV)
    assert app.discord_webhook_url == _REQUIRED_ENV["DISCORD_WEBHOOK_URL"]
    assert app.request_delay_seconds == 30
    assert app.sprite_request_delay_seconds == 2
    assert app.http_timeout_seconds == 20
    assert app.user_agent == "PokemonCardWatcher/1.0"
    assert app.send_initial_baseline_notification is False
    assert app.log_max_bytes == 1_048_576
    assert app.log_backup_count == 5


def test_missing_webhook_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL"):
        load_app_config(_NO_ENV)


def test_non_integer_env_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, HTTP_TIMEOUT_SECONDS="soon")
    with pytest.raises(ConfigError, match="HTTP_TIMEOUT_SECONDS"):
        load_app_config(_NO_ENV)


def test_non_positive_env_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, REQUEST_DELAY_SECONDS="0")
    with pytest.raises(ConfigError, match="positive"):
        load_app_config(_NO_ENV)


def test_log_backup_count_allows_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, LOG_BACKUP_COUNT="0")
    assert load_app_config(_NO_ENV).log_backup_count == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("1", True), ("YES", True), ("on", True),
     ("false", False), ("0", False), ("no", False), ("", False)],
)
def test_bool_parsing(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    _set_env(monkeypatch, SEND_INITIAL_BASELINE_NOTIFICATION=raw)
    assert load_app_config(_NO_ENV).send_initial_baseline_notification is expected


# --- Cards ----------------------------------------------------------------


def test_valid_cards_load(tmp_path: Path) -> None:
    path = _write_cards(
        tmp_path,
        [
            {"name": "Mega Gengar", "type": "price", "conditions": ["NM"], "url": "https://x/a"},
            {"name": "Charizard", "type": "price", "conditions": ["nm", "SP"], "url": "https://x/b"},
        ],
    )
    cards = load_cards(path)
    assert [c.name for c in cards] == ["Mega Gengar", "Charizard"]
    # conditions normalised to uppercase
    assert cards[1].conditions == ("NM", "SP")
    assert cards[1].is_sealed is False


def test_missing_conditions_loads_as_sealed_product(tmp_path: Path) -> None:
    path = _write_cards(
        tmp_path,
        [{"name": "ETB Ascended Heroes", "type": "price", "url": "https://x/sealed"}],
    )

    (card,) = load_cards(path)

    assert card.is_sealed is True
    assert card.conditions == ()


def test_empty_conditions_loads_as_sealed_product(tmp_path: Path) -> None:
    path = _write_cards(
        tmp_path,
        [{"name": "ETB Ascended Heroes", "type": "price", "conditions": [], "url": "https://x/sealed"}],
    )

    (card,) = load_cards(path)

    assert card.is_sealed is True
    assert card.conditions == ()


def test_new_product_entry_loads_as_catalog_watch(tmp_path: Path) -> None:
    path = _write_cards(
        tmp_path,
        [
            {
                "name": "Shisui Pré-venda",
                "type": "new_product",
                "url": "https://www.shisuistore.com.br/pre-venda/",
            }
        ],
    )

    (watch,) = load_cards(path)

    assert watch.is_catalog is True
    assert watch.is_sealed is False
    assert watch.conditions == ()


def test_new_product_entry_ignores_conditions(tmp_path: Path) -> None:
    path = _write_cards(
        tmp_path,
        [{"name": "W", "type": "new_product", "conditions": "nonsense", "url": "https://x/w"}],
    )

    (watch,) = load_cards(path)

    assert watch.is_catalog is True
    assert watch.conditions == ()


def test_price_entries_are_not_catalog_watches(tmp_path: Path) -> None:
    path = _write_cards(
        tmp_path,
        [
            {"name": "Card", "type": "price", "conditions": ["NM"], "url": "https://x/a"},
            {"name": "Sealed", "type": "price", "url": "https://x/b"},
        ],
    )

    card, sealed = load_cards(path)

    assert (card.is_catalog, card.is_sealed) == (False, False)
    assert (sealed.is_catalog, sealed.is_sealed) == (False, True)


def test_type_is_normalised(tmp_path: Path) -> None:
    path = _write_cards(
        tmp_path, [{"name": "X", "type": " Price ", "conditions": ["NM"], "url": "https://x/a"}]
    )

    (card,) = load_cards(path)

    assert card.entry_type == "price"


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "X", "conditions": ["NM"], "url": "https://x/a"},
        {"name": "X", "type": "", "conditions": ["NM"], "url": "https://x/a"},
        {"name": "X", "type": "prices", "conditions": ["NM"], "url": "https://x/a"},
        {"name": "X", "type": 123, "conditions": ["NM"], "url": "https://x/a"},
        {"name": "X", "type": None, "conditions": ["NM"], "url": "https://x/a"},
    ],
)
def test_missing_or_unknown_type_aborts(tmp_path: Path, entry: dict[str, object]) -> None:
    path = _write_cards(tmp_path, [entry])
    with pytest.raises(ConfigError, match="'type'"):
        load_cards(path)


def test_unknown_card_keys_ignored(tmp_path: Path) -> None:
    path = _write_cards(
        tmp_path,
        [{"name": "X", "type": "price", "conditions": ["NM"], "url": "https://x/a", "future_field": 99}],
    )
    cards = load_cards(path)
    assert cards[0].name == "X"


def test_missing_cards_file_aborts(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_cards(tmp_path / "nope.json")


def test_malformed_json_aborts(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text("[ not valid json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_cards(path)


def test_non_array_aborts(tmp_path: Path) -> None:
    path = _write_cards(tmp_path, {"name": "X"})
    with pytest.raises(ConfigError, match="must be a JSON array"):
        load_cards(path)


def test_empty_array_aborts(tmp_path: Path) -> None:
    path = _write_cards(tmp_path, [])
    with pytest.raises(ConfigError, match="at least one card"):
        load_cards(path)


def test_missing_url_aborts(tmp_path: Path) -> None:
    path = _write_cards(tmp_path, [{"name": "X", "type": "price", "conditions": ["NM"]}])
    with pytest.raises(ConfigError, match="url"):
        load_cards(path)


def test_non_list_conditions_aborts(tmp_path: Path) -> None:
    path = _write_cards(tmp_path, [{"name": "X", "type": "price", "conditions": "NM", "url": "https://x/a"}])
    with pytest.raises(ConfigError, match="conditions"):
        load_cards(path)


def test_unknown_condition_aborts(tmp_path: Path) -> None:
    path = _write_cards(
        tmp_path, [{"name": "X", "type": "price", "conditions": ["MINT"], "url": "https://x/a"}]
    )
    with pytest.raises(ConfigError, match="unknown condition"):
        load_cards(path)


# --- Combined -------------------------------------------------------------


def test_load_config_end_to_end(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = _write_cards(
        tmp_path, [{"name": "X", "type": "price", "conditions": ["NM"], "url": "https://x/a"}]
    )
    _set_env(monkeypatch, CARDS_CONFIG_PATH=str(path))
    config = load_config(_NO_ENV)
    assert config.app.discord_webhook_url == _REQUIRED_ENV["DISCORD_WEBHOOK_URL"]
    assert len(config.cards) == 1
    assert config.cards[0].card_id  # SHA-256 identity available
