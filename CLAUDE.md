# CLAUDE.md — habluetooth

Guide for AI assistants working in this repo. Skim it before editing.

## What this is

`habluetooth` is the Bluetooth core library used by Home Assistant. It wraps
[`bleak`](https://github.com/hbldh/bleak) with multi-scanner orchestration,
advertisement tracking, a per-scanner discovery cache, and Cython-compiled hot
paths. The package is published to PyPI and consumed by Home Assistant plus
related glue libraries (`bleak-retry-connector`, `bleak-esphome`,
`bluetooth-adapters`, etc.).

It is **not** a daemon. It runs in-process inside HA's event loop and
coordinates multiple `BaseHaScanner` instances (local USB/UART adapters via
BlueZ, ESPHome remote scanners, etc.).

## Layout

```
src/habluetooth/
  manager.py            BluetoothManager — central orchestrator, dispatch, scoring
  base_scanner.py       BaseHaScanner / BaseHaRemoteScanner — shared scanner logic
  scanner.py            HaScanner — local bleak scanner (BlueZ / CoreBluetooth)
  scanner_device.py     BluetoothScannerDevice dataclass
  advertisement_tracker.py  Per-device advertising interval estimator
  models.py             BluetoothServiceInfo(Bleak), HaScannerDetails, enums
  storage.py            Discovery-cache (de)serialization — TypedDict ↔ dataclass
  wrappers.py           HaBleakClientWrapper / HaBleakScannerWrapper (public API)
  channels/bluez.py     Low-level BlueZ raw-advertisement channel (Cython)
  central_manager.py    Module-level singleton holder (get_manager / set_manager)
  const.py              Timeouts, thresholds, connection-parameter presets
  usage.py, util.py     Misc helpers
tests/                  pytest suite (asyncio + freezegun + codspeed)
docs/                   Sphinx documentation (readthedocs)
build_ext.py            Cython build script invoked by Poetry
```

Each "hot" module has a paired `.pxd` declaring its Cython attributes. See
[Cython rules](#cython) below.

## Core concepts

- **BluetoothManager** (`manager.py`) is the single in-process orchestrator.
  It is held by `central_manager.CentralBluetoothManager.manager` and accessed
  via `get_manager()` / `set_manager()`. There is no DI: tests typically set
  it directly.
- **Scanners** subclass `BaseHaScanner` (with the local-vs-remote split in
  `BaseHaRemoteScanner`). Each scanner reports advertisements to the manager
  via the `_async_on_advertisement` path; the manager dedupes, scores, and
  fans out to registered Bleak callbacks.
- **Advertisement tracker** (`advertisement_tracker.py`) learns each device's
  advertising interval and feeds expiry decisions. Until it has
  `ADVERTISING_TIMES_NEEDED` samples it uses a fallback timeout.
- **Wrappers** (`wrappers.py`) — `HaBleakClientWrapper` /
  `HaBleakScannerWrapper` are the _public_ Bleak-compatible facade. External
  callers (HA integrations) talk to these, not to scanners directly.
- **Allocations** — for proxy scanners (ESPHome) the manager tracks per-source
  slot allocations via `async_on_allocation_changed`. This state is push-only
  and trusted; habluetooth does **not** independently verify slot counts. See
  [Allocations are unverified](#allocations) below.

## Storage / "schema"

There is no SQL. The persistence layer is `storage.py`:

- HA's `Store` writes a JSON blob (`DiscoveryStorageType`) to disk.
- Round-trip: in-memory timestamps are `time.monotonic()`; on serialize they
  are converted to wall-time via
  `_get_monotonic_time_diff = time.time() - time.monotonic()`, then inverted
  on load.
- There is **no `version` field** as of 6.1.0. Backward compatibility is
  shape-based (`data.get(KEY, default)` + strip-on-read for removed
  `BLEDevice` fields). If you touch the schema in a load-bearing way,
  consider proposing a version key.
- `discovered_device_advertisement_data_from_dict` / `..._to_dict` are the
  only legitimate entry points — don't hand-roll the format.

## Cython

`build_ext.py` cythonizes these modules:

```
advertisement_tracker.py
base_scanner.py
manager.py
models.py
scanner.py
channels/bluez.py
```

**Rules:**

1. When changing the attributes of a class declared `cdef class` in a `.pxd`,
   update the matching `.pxd` or the build breaks.
2. Type annotations in `.py` are advisory; the `.pxd` is authoritative for
   Cython. `cdef public object foo` in `.pxd` corresponds to an instance
   attribute on the Python side.
3. Set `SKIP_CYTHON=1` to install without compilation (faster local dev,
   matches one half of the CI matrix). Set `REQUIRE_CYTHON=1` to fail if
   compilation fails (matches the other half).
4. Avoid implicit Cython type narrowing for objects that must remain Python
   types. `models.py` declares `_float = float`, `_str = str`, `_int = int`
   for exactly this reason — use the underscore aliases when you need to
   guarantee a Python object.

## Development workflow

Setup:

```bash
poetry install                  # cython build
SKIP_CYTHON=1 poetry install    # pure-python (faster)
```

Run tests:

```bash
poetry run pytest                          # full suite
poetry run pytest tests/test_manager.py    # single file
poetry run pytest -k allocation            # by keyword
```

Lint / format (pre-commit covers ruff, ruff-format, mypy, codespell, prettier,
poetry-check):

```bash
pre-commit run -a
```

Type checking (strict mypy — see `pyproject.toml`):

```bash
poetry run mypy src
```

Tests use `pytest-asyncio` (no auto-mode — mark coroutines explicitly) and
`freezegun` for time control. `pytest-codspeed` powers the benchmark file
(`tests/test_benchmark_base_scanner.py`).

## Coding conventions

- **Python ≥ 3.11**, target `py311` for ruff/black. Code may use 3.11+
  features (PEP 604 unions, `Self`, etc.).
- `from __future__ import annotations` at the top of every module.
- Imports sorted by isort (ruff `I`); first-party = `habluetooth`, `tests`.
- Black formatting, line length 88.
- Public API is exported from `habluetooth/__init__.py` — when adding a
  symbol, also add it to `__all__`.
- Docstrings: ruff enforces `D` rules with a small ignore list (see
  `pyproject.toml`). Module/package/`__init__` docstrings are not required;
  public function/class docstrings are.
- `mypy` is strict (`disallow_untyped_defs`, `disallow_any_generics`,
  `warn_unreachable`, `warn_unused_ignores`). Tests are exempted via override.
- Logging: module-level `_LOGGER = logging.getLogger(__name__)`. Never print.
- No `assert` in production code (ruff `S101`). Tests are exempted.

## Commit / PR conventions

- **Conventional Commits PR title, lowercase subject.** PRs are
  squash-merged, so the **PR title** becomes the commit on `main` and is the
  only string that has to parse as a Conventional Commit. The repo enforces
  this via the `pr-title` CI job in `ci.yml` using
  `amannn/action-semantic-pull-request`. Accepted types: `feat`, `fix`,
  `chore`, `ci`, `docs`, `refactor`, `test`, `perf`, `build`, etc. The
  subject (text after `type(scope):`) must start lowercase (enforced by
  `subjectPattern: ^(?![A-Z]).+$`). Per-commit messages on the PR branch are
  **not** linted; they get collapsed at squash-merge.
- Releases are fully automated by `python-semantic-release` from the commit
  log. Anything that should land in the changelog must use `feat:` or `fix:`
  (or a breaking-change footer). `chore*` and `ci*` are excluded.
- The version lives in three places, kept in sync by semantic-release:
  `pyproject.toml`, `src/habluetooth/__init__.py:__version__`,
  `docs/conf.py:release`. **Do not bump versions by hand.**
- PRs target `main`. CI runs the matrix `{3.11, 3.12, 3.13, 3.14, 3.14t} ×
{linux, macos, windows} × {skip_cython, use_cython}` — flaky breakage in
  one cell usually means a Cython annotation got too aggressive.

## Gotchas <a id="allocations"></a>

- **Allocations are unverified.** `_allocations[source]` is updated solely
  from `async_on_allocation_changed` (called by external glue when a proxy
  reports slot state). habluetooth has no parallel "currently-connected"
  counter and does not cross-check. When a proxy gets stuck, the only
  observable symptom is the per-source score collapsing to `NO_RSSI_VALUE`
  in `wrappers.py`.
- **`_connect_in_progress` is the only per-scanner "busy" signal.** There is
  no counter of active or completed connections — only "does this scanner
  have a connect attempt in flight right now".
- **bleak 3.0 deprecations:**
  - `BleakScanner(adapter="hciN")` is gone; use `BleakScanner(bluez={"adapter":
"hciN"})`. When also passing `PASSIVE_SCANNER_ARGS` (itself a
    `BlueZScannerArgs`), merge — don't overwrite.
  - `BLEDevice(..., rssi=-NN)` is gone; bleak 3.0 only accepts
    `(address, name, details)`. RSSI lives on `AdvertisementData` only.
- **Test deprecation hygiene:**
  ```bash
  pytest tests/ -W "error::DeprecationWarning" \
                -W "ignore::DeprecationWarning:asyncio"
  ```
  turns each deprecation into a failure (while ignoring asyncio's internal
  ones).
- **Time source.** Everything in hot paths uses
  `bluetooth_data_tools.monotonic_time_coarse()` — do not mix with
  `time.time()` or `time.monotonic()` except at storage boundaries.

## When in doubt

- Public API contracts live in `__init__.py`'s `__all__` and in `wrappers.py`.
  Breaking those is a major version bump.
- Internal refactors are fine as long as the public surface, the storage
  schema, and the scanner-callback signatures don't move.
- Tests are the source of truth for expected behavior — if a test is awkward
  to write, the API probably needs to change, not the test.
