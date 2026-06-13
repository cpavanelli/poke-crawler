"""Tests for the single-run app entrypoint (FRD sections 15, 16, and 18)."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from app import PidLock, main, pid_alive, setup_logging
from models.card import Card
from services.config import AppConfig, Config, ConfigError
from services.scanner import ScanSummary


@pytest.fixture(autouse=True)
def restore_root_logger() -> None:
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    yield
    for handler in root.handlers:
        handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def _app_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        discord_webhook_url="https://discord.example/webhook",
        cards_config_path=tmp_path / "cards.json",
        database_path=tmp_path / "watcher.db",
        request_delay_seconds=30,
        sprite_request_delay_seconds=2,
        http_timeout_seconds=20,
        user_agent="TestAgent/1.0",
        send_initial_baseline_notification=False,
        log_max_bytes=12345,
        log_backup_count=7,
    )


def _config(tmp_path: Path) -> Config:
    return Config(
        app=_app_config(tmp_path),
        cards=(Card(name="Mega Gengar", conditions=("NM",), url="https://example/card"),),
    )


def _summary() -> ScanSummary:
    return ScanSummary(
        cards_scanned=1,
        cards_failed=0,
        new_lows=0,
        stopped_early=False,
    )


def test_pid_lock_fresh_acquire_writes_pid(tmp_path: Path) -> None:
    path = tmp_path / "watcher.lock"
    lock = PidLock(path, getpid=lambda: 1234, pid_alive=lambda _pid: False)

    assert lock.acquire() is True
    assert path.read_text(encoding="utf-8") == "1234"


def test_pid_lock_live_holder_exits_without_changing_file(tmp_path: Path) -> None:
    path = tmp_path / "watcher.lock"
    path.write_text("4242", encoding="utf-8")
    lock = PidLock(path, getpid=lambda: 1234, pid_alive=lambda _pid: True)

    assert lock.acquire() is False
    assert lock.holder_pid == 4242
    assert path.read_text(encoding="utf-8") == "4242"


def test_pid_lock_stale_holder_is_reclaimed(tmp_path: Path) -> None:
    path = tmp_path / "watcher.lock"
    path.write_text("4242", encoding="utf-8")
    lock = PidLock(path, getpid=lambda: 1234, pid_alive=lambda _pid: False)

    assert lock.acquire() is True
    assert path.read_text(encoding="utf-8") == "1234"


@pytest.mark.parametrize("contents", ["", "not-a-pid"])
def test_pid_lock_corrupt_lock_is_reclaimed(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "watcher.lock"
    path.write_text(contents, encoding="utf-8")
    lock = PidLock(path, getpid=lambda: 1234, pid_alive=lambda _pid: False)

    assert lock.acquire() is True
    assert path.read_text(encoding="utf-8") == "1234"


def test_pid_lock_release_removes_only_when_owned(tmp_path: Path) -> None:
    path = tmp_path / "watcher.lock"
    lock = PidLock(path, getpid=lambda: 1234, pid_alive=lambda _pid: False)
    assert lock.acquire() is True

    lock.release()
    lock.release()

    assert not path.exists()


def test_pid_lock_busy_release_does_not_delete_holder_file(tmp_path: Path) -> None:
    path = tmp_path / "watcher.lock"
    path.write_text("4242", encoding="utf-8")
    lock = PidLock(path, getpid=lambda: 1234, pid_alive=lambda _pid: True)
    assert lock.acquire() is False

    lock.release()

    assert path.read_text(encoding="utf-8") == "4242"


def test_pid_alive_rejects_non_positive_pids() -> None:
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


def test_setup_logging_handlers_and_rotation_params(tmp_path: Path) -> None:
    app = _app_config(tmp_path)

    setup_logging(app, logs_dir=tmp_path)

    root = logging.getLogger()
    file_handlers = [
        handler for handler in root.handlers if isinstance(handler, RotatingFileHandler)
    ]
    stream_handlers = [
        handler
        for handler in root.handlers
        if isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, RotatingFileHandler)
    ]
    assert root.level == logging.INFO
    assert len(file_handlers) == 1
    assert file_handlers[0].maxBytes == app.log_max_bytes
    assert file_handlers[0].backupCount == app.log_backup_count
    assert len(stream_handlers) == 1
    assert stream_handlers[0].level == logging.WARNING
    assert (tmp_path / "watcher.log").exists()


def test_setup_logging_is_idempotent(tmp_path: Path) -> None:
    app = _app_config(tmp_path)

    setup_logging(app, logs_dir=tmp_path)
    count = len(logging.getLogger().handlers)
    setup_logging(app, logs_dir=tmp_path)

    assert len(logging.getLogger().handlers) == count


def test_setup_logging_writes_info_line_to_file(tmp_path: Path) -> None:
    setup_logging(_app_config(tmp_path), logs_dir=tmp_path)

    logging.getLogger(__name__).info("hello")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert "hello" in (tmp_path / "watcher.log").read_text(encoding="utf-8")


def test_main_happy_path_runs_cycle_logs_and_releases_lock(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls: list[Config] = []

    def fake_run_cycle(config_arg: Config) -> ScanSummary:
        calls.append(config_arg)
        return _summary()

    exit_code = main(
        [],
        config_loader=lambda: config,
        run_cycle=fake_run_cycle,
        lock_path=tmp_path / "watcher.lock",
        logs_dir=tmp_path / "logs",
        pid_alive=lambda _pid: False,
        getpid=lambda: 1234,
    )

    assert exit_code == 0
    assert calls == [config]
    assert not (tmp_path / "watcher.lock").exists()
    log_text = (tmp_path / "logs" / "watcher.log").read_text(encoding="utf-8")
    assert "startup" in log_text
    assert "shutdown: ScanSummary" in log_text


def test_main_lock_busy_exits_zero_without_running(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    lock_path = tmp_path / "watcher.lock"
    lock_path.write_text("4242", encoding="utf-8")
    calls: list[Config] = []

    exit_code = main(
        [],
        config_loader=lambda: config,
        run_cycle=lambda config_arg: calls.append(config_arg) or _summary(),
        lock_path=lock_path,
        logs_dir=tmp_path / "logs",
        pid_alive=lambda _pid: True,
        getpid=lambda: 1234,
    )

    assert exit_code == 0
    assert calls == []
    assert lock_path.read_text(encoding="utf-8") == "4242"
    # File logging is configured only after acquiring the lock, so an overlapping
    # run must never open watcher.log (RotatingFileHandler is not multiprocess-safe).
    assert not (tmp_path / "logs").exists()
    assert "another run is in progress (pid=4242); exiting" in capsys.readouterr().err


def test_main_stale_lock_reclaimed_then_runs(tmp_path: Path) -> None:
    config = _config(tmp_path)
    lock_path = tmp_path / "watcher.lock"
    lock_path.write_text("4242", encoding="utf-8")
    calls: list[Config] = []

    exit_code = main(
        [],
        config_loader=lambda: config,
        run_cycle=lambda config_arg: calls.append(config_arg) or _summary(),
        lock_path=lock_path,
        logs_dir=tmp_path / "logs",
        pid_alive=lambda _pid: False,
        getpid=lambda: 1234,
    )

    assert exit_code == 0
    assert calls == [config]
    assert not lock_path.exists()


def test_main_invalid_config_exits_one_without_lock(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lock_path = tmp_path / "watcher.lock"
    calls: list[Config] = []

    exit_code = main(
        [],
        config_loader=lambda: (_ for _ in ()).throw(ConfigError("bad config")),
        run_cycle=lambda config_arg: calls.append(config_arg) or _summary(),
        lock_path=lock_path,
        logs_dir=tmp_path / "logs",
        pid_alive=lambda _pid: False,
        getpid=lambda: 1234,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "configuration error: bad config" in captured.err
    assert not lock_path.exists()
    assert calls == []


def test_main_fatal_error_exits_one_and_releases_lock(tmp_path: Path) -> None:
    config = _config(tmp_path)
    lock_path = tmp_path / "watcher.lock"

    def fail(_config: Config) -> ScanSummary:
        raise RuntimeError("boom")

    exit_code = main(
        [],
        config_loader=lambda: config,
        run_cycle=fail,
        lock_path=lock_path,
        logs_dir=tmp_path / "logs",
        pid_alive=lambda _pid: False,
        getpid=lambda: 1234,
    )

    assert exit_code == 1
    assert not lock_path.exists()
    assert "fatal error during scan" in (tmp_path / "logs" / "watcher.log").read_text(
        encoding="utf-8"
    )
