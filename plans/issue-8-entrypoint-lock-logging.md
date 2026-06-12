# Plan: Issue #8 — Entrypoint, lock file & logging (`app.py`) · FRD §15, §16

The single-run **entrypoint**. Each cron invocation runs `python app.py` once
(§18): it sets up logging, guards against overlapping runs with a PID lock,
builds the scanner's collaborators, runs exactly one scan cycle (issue #7's
`Scanner.run`), and tears everything down cleanly. This is the seam the #7 plan
deliberately left open ("wiring notes (future `app.py`)").

Source of truth: `FRD.md` §15 (lock file + PID stale detection, verbatim), §16
(rotating logs, local-time timestamps), plus §18 (cron / single-run model) and
§3/§12 (invalid config aborts startup).

**Depends on (all merged):** #7 scanner (`services/scanner.py`), and transitively
everything it ties together — config (`services/config.py`), storage
(`services/storage.py`), fetcher (`services/fetcher.py`), notifier
(`services/notifier.py`).

Match the repo conventions (read `services/scanner.py`, `services/fetcher.py`,
`tools/list_prices.py` first): `from __future__ import annotations`; FRD refs in
docstrings; typed signatures; keyword-only args for multi-field calls;
dependency injection so tests run **offline and hermetic**; module logger via
`logging.getLogger(__name__)`.

---

## Decisions (confirmed)

- **Lock-busy exit code = 0.** A second run starting while the first still holds
  a *live* lock is the normal cron-overlap case, not a failure. Exit `0` (cron
  stays quiet) and log it at **WARNING** so it's still visible. (Invalid config
  and unexpected fatal errors are the non-zero cases — see "Exit codes".)
- **Two log sinks:** the §16 rotating **file** handler at all levels (INFO+),
  **plus** a **stderr** handler at **WARNING+** so cron can surface failures by
  email. Healthy runs stay silent on stderr. Config errors that occur *before*
  logging is initialised print to stderr unconditionally.
- **Lock is acquired BEFORE file logging is configured.** `RotatingFileHandler`
  is not multiprocess-safe, so an overlapping cron run must never open
  `watcher.log`. Consequence: the busy-exit and stale-reclaim messages go to
  **stderr** (cron mail), and the file log begins at "startup" only once this
  process owns the lock. This is a deliberate reorder from a naive
  log-first-then-lock entrypoint.
- **Everything lives in `app.py`.** FRD §19's layout lists only `app.py` for this
  concern (no `services/locking.py` or `logging_setup.py`). The lock helper, the
  logging setup, and the wiring stay in `app.py` as small, individually testable
  functions/classes. Faithful to the documented structure; trivially extractable
  later if a second entrypoint ever needs them.

---

## FRD mapping

| Concern | FRD | Implementation |
|---|---|---|
| Single-run, cron | §18 | `main()` runs one cycle and returns an exit code; no daemon, no loop. |
| Load + validate config | §3, §12 | `load_config()`; `ConfigError` → stderr + exit 1 (abort startup). |
| Rotating logs | §16 | `RotatingFileHandler(logs/watcher.log, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)`. |
| Local-time timestamps | §16, §18 | default `logging` `asctime` uses `time.localtime` — already local; no UTC override. |
| PID lock + stale detect | §15 | `PidLock.acquire()` / `release()`; live PID → exit, dead/corrupt PID → reclaim. |
| Run the cycle | §6 | builds DB + fetcher + notifier, calls `Scanner.run(cards)`. |
| Clean shutdown | §15, §16 | log shutdown summary; release lock; close DB/clients in `finally`. |

---

## Startup → shutdown sequence (`main`)

```
1. config = load_config()              # §3 — ConfigError → stderr, return 1
2. lock = PidLock(lock_path)
   if not lock.acquire():              # §15 — live holder (file logging NOT up yet)
       print("another run is in progress (pid=%s); exiting", holder_pid → stderr)
       return 0                        # cron-overlap is normal; never opens watcher.log
   try:
3.     setup_logging(config.app, ...)  # §16 — file(INFO+) + stderr(WARNING+); we own the lock
4.     logger.info("startup")          # §16
5.     summary = run_cycle(config)     # build DB+fetcher+notifier, Scanner.run(cards)
6.     logger.info("shutdown: %s", summary)
       return 0
   except Exception:                   # unexpected fatal — should be rare
       logger.exception("fatal error during scan")
       return 1
   finally:
7.     lock.release()                  # §15 — remove on normal shutdown (only if owned)
```

The lock is taken before `setup_logging` precisely so an overlapping run exits
on stderr without touching the (multiprocess-unsafe) rotating file. The
stale-reclaim warning emitted inside `acquire()` therefore reaches stderr via
logging's last-resort handler — acceptable, since a reclaim is a crash-recovery
event the operator wants surfaced by cron mail anyway.

Notes:
- `Scanner.run` already contains the per-card error handling and the §17
  stop-cycle (it swallows `CycleStop` internally and returns
  `ScanSummary(stopped_early=True)`), so `app.py` does **not** see `CycleStop`
  or per-card faults — it only logs the returned summary. The broad
  `except Exception` is a backstop for genuinely unexpected failures (e.g. DB
  open error), not normal flow.
- The lock is released **only if we acquired it** (the busy path returns before
  the `try`, so the other run's lock is never touched). `release()` is
  idempotent and tolerates an already-removed file.

---

## `setup_logging(app, *, logs_dir=Path("logs"), log_name="watcher.log")`

```python
def setup_logging(app: AppConfig, *, logs_dir=Path("logs"), log_name="watcher.log") -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)          # one-time; SD-friendly
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    file_handler = RotatingFileHandler(
        logs_dir / log_name,
        maxBytes=app.log_max_bytes,
        backupCount=app.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(fmt)

    stderr_handler = logging.StreamHandler()             # defaults to sys.stderr
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(fmt)

    root.handlers.clear()    # idempotent: never stack handlers across calls/tests
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)
```

- **Local time** — Python's `logging` formats `%(asctime)s` with `time.localtime`
  by default, so timestamps are host-local (§16, §18) with **no** extra code.
  Consistent with `storage.local_now_iso`. (No `converter = time.gmtime`.)
- **`root.handlers.clear()`** keeps the function idempotent — a second call (or a
  test) replaces handlers instead of duplicating every log line. Matters because
  `pytest` may invoke `main`/`setup_logging` repeatedly in one process.
- `logs_dir` is a keyword arg so tests point it at `tmp_path` and never write into
  the real `logs/`. `logs/` and `*.log` are already gitignored.
- Levels: file = INFO+ (the §16 event list — startup, scans, baseline creation,
  new lows, notification results, errors, retries — flows from the modules'
  existing `logger.info/warning/error` calls, which the root now captures);
  stderr = WARNING+ (failures only → cron email).

---

## `PidLock` — FRD §15, the core of this issue

```python
class PidLock:
    def __init__(
        self,
        path: Path | str = Path("watcher.lock"),
        *,
        getpid: Callable[[], int] = os.getpid,
        pid_alive: Callable[[int], bool] = pid_alive,
    ) -> None: ...

    def acquire(self) -> bool:
        """Try to take the lock. True = acquired (we own it); False = a live run
        already holds it (caller should exit). Stale/corrupt locks are reclaimed
        and return True (§15)."""

    def release(self) -> None:
        """Remove the lock iff we own it. Idempotent; tolerates a missing file."""

    @property
    def holder_pid(self) -> int | None:
        """PID last read from an existing lock (for the 'exiting' log line)."""
```

### `acquire()` logic (read the §15 rules literally)

```
pid = self._getpid()
try:
    fd = os.open(path, O_CREAT | O_EXCL | O_WRONLY)   # atomic "create if absent"
except FileExistsError:
    holder = _read_pid(path)        # int, or None if empty/garbage/unreadable
    if holder is not None and self._pid_alive(holder):
        self._holder_pid = holder
        return False                # live run in progress → caller exits
    # stale (dead PID) OR corrupt lock left by a crash/power-loss → reclaim
    logger.warning("reclaiming stale lock (pid=%s)", holder)
    _write_pid(path, pid)           # overwrite with our PID
    self._owned = True
    return True
else:
    with os.fdopen(fd, "w") as f:   # we created it atomically
        f.write(str(pid))
    self._owned = True
    return True
```

- **`O_CREAT | O_EXCL`** makes the happy-path create atomic, closing the classic
  two-processes-both-create race without extra machinery.
- **Corrupt/empty lock = stale.** A lock file with non-integer or empty contents
  was left mid-write by a crash; treat it as reclaimable (`_read_pid` returns
  `None` → reclaim), don't wedge forever. This is exactly the §15 rationale
  ("prevents a killed run from permanently wedging all future cron invocations").
- The reclaim *overwrite* is not itself atomic against a second simultaneous
  reclaimer. At cron's 15-minute cadence that window is negligible; documented as
  an accepted limitation, not worth a lockf/flock dance on the target.

### `release()`

```
if not self._owned: return
try: os.unlink(self._path)
except FileNotFoundError: pass      # already gone — fine
self._owned = False
```

Only removes the file if **this** instance acquired it, so the busy-exit path can
never delete the other run's lock.

### `pid_alive(pid)` — process liveness (injectable seam)

```python
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)             # signal 0 = existence/permission probe
    except ProcessLookupError:
        return False                # no such process
    except PermissionError:
        return True                 # exists, owned by another user → alive
    except OSError:
        return False
    return True
```

- The deployment target is the Raspberry Pi (Linux), where `os.kill(pid, 0)` is
  the canonical liveness probe. **Flagged caveat:** on Windows (dev box only)
  `os.kill(pid, 0)`'s behaviour differs and may not be reliable — that's fine
  because (a) production is Linux and (b) every test injects a fake `pid_alive`,
  so the suite is hermetic and OS-independent. Do **not** add a Windows-specific
  branch unless the owner asks; keep the default POSIX and injectable.

---

## `run_cycle(config) -> ScanSummary` — the wiring (from the #7 "wiring notes")

```python
def run_cycle(config: Config) -> ScanSummary:
    conn = storage.connect(config.app.database_path)     # §8 WAL/NORMAL pragmas
    try:
        storage.init_db(conn)
        with HttpFetcher.from_app_config(config.app) as fetcher, \
             DiscordNotifier.from_app_config(config.app) as notifier:
            scanner = Scanner(
                fetcher=fetcher,
                notifier=notifier,
                conn=conn,
                send_initial_baseline=config.app.send_initial_baseline_notification,
            )
            return scanner.run(config.cards)
    finally:
        conn.close()
```

Kept as a **separate, injectable function** so `main`'s lock/logging logic (the
acceptance focus) is tested without any real DB/network: tests pass a fake
`run_cycle` that records it was called and returns a canned `ScanSummary`.

---

## `main(...)` — signature with test seams

```python
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
```

- No CLI args are required by the FRD (cron just runs `python app.py`); accept
  `argv` for parity with `tools/list_prices.py` and future flags, but the body
  needs no options yet. (A bare `argparse` with only `-h` is fine, or skip it.)
- Every external dependency (config load, the cycle, lock path/behaviour, logs
  dir, pid liveness, getpid) is injectable → the acceptance tests are fully
  offline and deterministic.
- `if __name__ == "__main__": raise SystemExit(main(sys.argv[1:]))`.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Normal completion **or** lock-busy (another live run) — cron-overlap is not a failure. |
| `1` | Invalid configuration (abort startup, §3/§12) **or** an unexpected fatal error during the run. |

No other codes. `Scanner.run` handling 403/429 internally means a stop-cycle is a
*successful* `main` (exit 0) with `summary.stopped_early=True` logged — it is not
an app-level error.

---

## Tests: `tests/test_app.py` (offline, hermetic — `tmp_path`, injected seams)

**`PidLock` (the §15 acceptance core):**

1. **Fresh acquire** — no file at `tmp_path/watcher.lock`; `acquire()` returns
   `True`, file now contains our injected PID.
2. **Live holder → exit** — pre-write the file with PID `4242`,
   `pid_alive=lambda p: True`; `acquire()` returns `False`, **file unchanged**
   (still `4242`), `holder_pid == 4242`. *(Acceptance: "a second run exits while
   the first holds the lock.")*
3. **Stale (dead) holder → reclaim** — pre-write `4242`,
   `pid_alive=lambda p: False`; `acquire()` returns `True`, file now contains our
   PID. *(Acceptance: "a stale (dead-PID) lock is reclaimed.")*
4. **Corrupt/empty lock → reclaim** — pre-write `""` / `"not-a-pid"`; `acquire()`
   returns `True`, file overwritten with our PID (no crash).
5. **release removes only when owned** — after a successful `acquire()`,
   `release()` deletes the file; calling `release()` again is a no-op (idempotent,
   no error).
6. **busy path never deletes the holder's lock** — on the case-2 instance (got
   `False`), `release()` does **not** remove the file (still `4242`).
7. **`pid_alive` default** — `pid_alive(getpid())` is `True`;
   `pid_alive(some-unused-pid)` / `pid_alive(-1)` / `pid_alive(0)` is `False`.
   *(Marked POSIX; if the dev box is Windows and this is flaky, assert only the
   `<= 0` and current-pid cases and rely on the injected fakes elsewhere.)*

**`setup_logging`:**

8. **Handlers + rotation params** — `setup_logging(app, logs_dir=tmp_path)`
   attaches a `RotatingFileHandler` with `maxBytes==app.log_max_bytes` and
   `backupCount==app.log_backup_count`, plus a `StreamHandler` at `WARNING`;
   `tmp_path` now contains the log file (or is created). Root level is `INFO`.
9. **Idempotent** — calling it twice does **not** accumulate handlers (count is
   stable; `root.handlers.clear()` works).
10. **A logged line lands in the file** — emit `logger.info("hello")` after setup;
    `tmp_path/watcher.log` contains `hello`. (Sanity that wiring is live.)

**`main` integration (all seams injected; no real DB/network):**

11. **Happy path** — fake `config_loader` returns a minimal `Config`; fake
    `run_cycle` records the call and returns a canned `ScanSummary`; `lock_path`
    and `logs_dir` under `tmp_path`. Assert: returns `0`, `run_cycle` called once,
    lock file **created then removed** (gone after `main`), and the log file
    contains a startup line and the shutdown/summary line.
12. **Lock-busy → exit 0 without running** — pre-write a live-PID lock
    (`pid_alive=True`); assert `main` returns `0`, `run_cycle` was **not** called,
    and the lock file is **untouched** (the holder's PID is preserved).
13. **Stale lock reclaimed → runs** — pre-write a dead-PID lock
    (`pid_alive=False`); assert `main` returns `0`, `run_cycle` **was** called,
    lock removed on exit.
14. **Invalid config → exit 1, no lock** — `config_loader` raises `ConfigError`;
    assert `main` returns `1`, **no** lock file created, and (optionally) the
    message went to stderr (`capsys`). `run_cycle` not called.
15. **Fatal error in run_cycle → exit 1, lock released** — fake `run_cycle`
    raises `RuntimeError`; assert `main` returns `1` and the lock file was still
    **removed** (released in `finally`), so the next cron run isn't wedged.

All hermetic: `tmp_path` for lock + logs, injected `pid_alive`/`getpid`/
`config_loader`/`run_cycle`. No sockets, no real `watcher.db`, no real sleeps.
Reset root-logger handlers in a fixture (add then `clear()`) so tests don't leak
handlers into each other.

---

## Operational note (for the README / deploy, not code)

The cron line from §18 already `cd`s into the project dir, so the relative
`watcher.lock`, `logs/`, and `DATABASE_PATH` all resolve against the project root
— matches how `watcher.lock` / `logs/` are gitignored. No absolute paths baked
in. (Mentioning here so the implementer doesn't "helpfully" absolutise them.)

---

## Dependencies

None new. `logging.handlers.RotatingFileHandler`, `os`, `pathlib`, `sqlite3` are
stdlib; `httpx`/scanner/storage already present. No `psutil` for liveness —
`os.kill(pid, 0)` is sufficient on the Linux target and keeps the dependency
surface flat.

---

## Acceptance (from issue #8)

- ✅ **A second run exits while the first holds the lock** — test 2 (`PidLock`) and
  test 12 (`main` returns 0, `run_cycle` not called, holder lock untouched).
- ✅ **A stale (dead-PID) lock is reclaimed** — test 3 (`PidLock`) and test 13
  (`main` runs after reclaiming).
- ✅ Rotating logs with `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT`, local-time stamps —
  tests 8–10.
- ✅ Startup/shutdown lifecycle, config-abort, clean lock release on error —
  tests 11, 14, 15.

---

## Out of scope (later / not this issue)

- **Scan logic itself** — owned by #7; `app.py` only wires and runs it.
- **Cron installation / systemd units / deploy scripts** — deployment docs (§18),
  not app code.
- **`.env.example` / README deploy section** — useful follow-ups, but this issue
  is the entrypoint + lock + logging. Flag if the owner wants them bundled.
- **Health checks, daily summaries, Telegram** — §21 future enhancements.
- **Extracting `PidLock`/`setup_logging` into `services/`** — kept in `app.py`
  per §19; revisit only if a second entrypoint appears.

## Suggested PR

Single PR — `app.py` + `tests/test_app.py`. `pytest` green, fully offline (no DB,
no network, `tmp_path` for lock/logs). Commit references `closes #8`. No changes
to existing modules (`app.py` only consumes them), so the current 119 tests stay
green.
