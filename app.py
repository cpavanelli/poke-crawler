"""Single-run application entrypoint (FRD sections 15, 16, and 18)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path

from services import storage
from services.config import AppConfig, Config, ConfigError, load_config
from services.fetcher import HttpFetcher
from services.notifier import DiscordNotifier
from services.scanner import ScanSummary, Scanner

logger = logging.getLogger(__name__)


def pid_alive(pid: int) -> bool:
    """Return whether ``pid`` appears to be alive (FRD section 15)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class PidLock:
    """PID-file lock for one cron-driven scan process (FRD section 15)."""

    def __init__(
        self,
        path: Path | str = Path("watcher.lock"),
        *,
        getpid: Callable[[], int] = os.getpid,
        pid_alive: Callable[[int], bool] = pid_alive,
    ) -> None:
        self._path = Path(path)
        self._getpid = getpid
        self._pid_alive = pid_alive
        self._owned = False
        self._holder_pid: int | None = None

    @property
    def holder_pid(self) -> int | None:
        """PID last read from an existing live lock."""
        return self._holder_pid

    def acquire(self) -> bool:
        """Acquire the lock, reclaiming stale or corrupt lock files (FRD section 15)."""
        pid = self._getpid()
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = _read_pid(self._path)
            if holder is not None and self._pid_alive(holder):
                self._holder_pid = holder
                return False

            logger.warning("reclaiming stale lock (pid=%s)", holder)
            _write_pid(self._path, pid)
            self._owned = True
            return True

        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(str(pid))
        self._owned = True
        return True

    def release(self) -> None:
        """Remove the lock if this instance owns it; tolerate repeated calls."""
        if not self._owned:
            return
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass
        self._owned = False


def setup_logging(
    app: AppConfig,
    *,
    logs_dir: Path = Path("logs"),
    log_name: str = "watcher.log",
) -> None:
    """Configure rotating file logs and warning+ stderr logs (FRD section 16)."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    file_handler = RotatingFileHandler(
        logs_dir / log_name,
        maxBytes=app.log_max_bytes,
        backupCount=app.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(formatter)

    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)


def run_cycle(config: Config) -> ScanSummary:
    """Wire scanner collaborators and run one scan cycle (FRD sections 6 and 18)."""
    conn = storage.connect(config.app.database_path)
    try:
        storage.init_db(conn)
        with (
            HttpFetcher.from_app_config(config.app) as fetcher,
            DiscordNotifier.from_app_config(config.app) as notifier,
        ):
            scanner = Scanner(
                fetcher=fetcher,
                notifier=notifier,
                conn=conn,
                send_initial_baseline=config.app.send_initial_baseline_notification,
            )
            return scanner.run(config.cards)
    finally:
        conn.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    config_loader: Callable[[], Config] = load_config,
    run_cycle: Callable[[Config], ScanSummary] = run_cycle,
    lock_path: Path = Path("watcher.lock"),
    logs_dir: Path = Path("logs"),
    pid_alive: Callable[[int], bool] = pid_alive,
    getpid: Callable[[], int] = os.getpid,
) -> int:
    """Run one cron invocation and return a shell exit code."""
    parser = argparse.ArgumentParser(description="Run one Pokemon card watcher scan.")
    parser.parse_args(argv)

    try:
        config = config_loader()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    # Acquire the lock BEFORE configuring file logging: RotatingFileHandler is
    # not multiprocess-safe, so an overlapping run must never open watcher.log.
    # The busy/reclaim lines therefore go to stderr (cron mail), and the file
    # log begins at "startup" only once this process owns the lock (FRD §15-16).
    lock = PidLock(lock_path, getpid=getpid, pid_alive=pid_alive)
    if not lock.acquire():
        print(
            f"another run is in progress (pid={lock.holder_pid}); exiting",
            file=sys.stderr,
        )
        return 0

    try:
        setup_logging(config.app, logs_dir=logs_dir)
        logger.info("startup")
        summary = run_cycle(config)
        logger.info("shutdown: %s", summary)
        return 0
    except Exception:
        logger.exception("fatal error during scan")
        return 1
    finally:
        lock.release()


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _write_pid(path: Path, pid: int) -> None:
    path.write_text(str(pid), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
