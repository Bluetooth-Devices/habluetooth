"""Tests for the Bluetooth integration scanners."""

import asyncio
import logging
import platform
import time
from collections.abc import Generator
from datetime import timedelta
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, Mock, patch

import pytest
from bleak import BleakError
from bleak.backends.scanner import AdvertisementDataCallback
from bleak_retry_connector import BleakSlotManager

from habluetooth import (
    SCANNER_WATCHDOG_INTERVAL,
    SCANNER_WATCHDOG_TIMEOUT,
    BluetoothManager,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    HaScanner,
    HaScannerType,
    ScannerStartError,
    get_manager,
    scanner,
    set_manager,
)
from habluetooth.channels.bluez import (
    BluetoothMGMTProtocol,
    MGMTBluetoothCtl,
)
from habluetooth.scanner import (
    InvalidMessageError,
    bytes_mac_to_str,
    create_bleak_scanner,
    make_bluez_details,
)

from . import (
    MockBleakScanner,
    async_fire_time_changed,
    generate_advertisement_data,
    generate_ble_device,
    patch_bleak_scanner_factory,
    patch_bluetooth_time,
    utcnow,
)
from .conftest import FakeBluetoothAdapters, MockBluetoothManagerWithCallbacks

DEVICE_FOUND = 0x0012
ADV_MONITOR_DEVICE_FOUND = 0x002F
IS_WINDOWS = 'os.name == "nt"'
IS_POSIX = 'os.name == "posix"'
NOT_POSIX = 'os.name != "posix"'
# or_patterns is a workaround for the fact that passive scanning
# needs at least one matcher to be set. The below matcher
# will match all devices.
if platform.system() == "Linux":
    # On Linux, use the real BlueZScannerArgs to avoid mocking issues
    from bleak.args.bluez import BlueZScannerArgs, OrPattern
    from bleak.assigned_numbers import AdvertisementDataType

    scanner.PASSIVE_SCANNER_ARGS = BlueZScannerArgs(
        or_patterns=[
            OrPattern(0, AdvertisementDataType.FLAGS, b"\x02"),
            OrPattern(0, AdvertisementDataType.FLAGS, b"\x06"),
            OrPattern(0, AdvertisementDataType.FLAGS, b"\x1a"),
        ]
    )
else:
    # On other platforms ``bleak.args.bluez`` may not be importable. Use a
    # non-empty real mapping that mimics the Linux shape so the production
    # code's ``if bluez_args:`` truthy check still adds the ``bluez`` kwarg.
    scanner.PASSIVE_SCANNER_ARGS = {"or_patterns": [(0, 0x01, b"\x06")]}
# If the adapter is in a stuck state the following errors are raised:
NEED_RESET_ERRORS = [
    "org.bluez.Error.Failed",
    "org.bluez.Error.InProgress",
    "org.bluez.Error.NotReady",
    "not found",
]


@pytest.fixture(autouse=True, scope="module")
def disable_stop_discovery():
    """Disable stop discovery."""
    with (
        patch("habluetooth.scanner.stop_discovery"),
        patch("habluetooth.scanner.restore_discoveries"),
    ):
        yield


@pytest.fixture
def force_linux_scanner_mode() -> Generator[None, None, None]:
    """
    Force scanner.IS_LINUX=True / IS_MACOS=False for AUTO-flow tests.

    Lets the active-window toggle path run on any host: the toggle
    is gated on IS_LINUX (BlueZ-only private attribute), and AUTO
    on macOS short-circuits to permanent active.
    """
    with (
        patch("habluetooth.scanner.IS_LINUX", True),
        patch("habluetooth.scanner.IS_MACOS", False),
    ):
        yield


@pytest.fixture(autouse=True, scope="module")
def manager():
    """Return the BluetoothManager instance."""
    adapters = FakeBluetoothAdapters()
    slot_manager = BleakSlotManager()
    manager = BluetoothManager(adapters, slot_manager)
    set_manager(manager)
    return manager


@pytest.fixture
def mock_btmgmt_socket():
    """Mock the btmgmt_socket module."""
    with patch("habluetooth.channels.bluez.btmgmt_socket") as mock_btmgmt:
        mock_socket = Mock()
        # Make the socket look like a real socket with a file descriptor
        mock_socket.fileno.return_value = 99
        mock_btmgmt.open.return_value = mock_socket
        yield mock_btmgmt


def test_bytes_mac_to_str() -> None:
    """Test bytes_mac_to_str."""
    assert bytes_mac_to_str(b"\xff\xee\xdd\xcc\xbb\xaa") == "AA:BB:CC:DD:EE:FF"
    assert bytes_mac_to_str(b"\xff\xee\xdd\xcc\xbb\xaa") == "AA:BB:CC:DD:EE:FF"


def test_make_bluez_details() -> None:
    """Test make_bluez_details."""
    assert make_bluez_details("AA:BB:CC:DD:EE:FF", "hci0") == {
        "path": "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
        "props": {"Adapter": "/org/bluez/hci0"},
    }


def test_create_bleak_scanner_linux_no_adapter_active() -> None:
    """Linux + no adapter + active: ``bluez`` kwarg must be absent."""
    with (
        patch.object(scanner, "IS_LINUX", True),
        patch.object(scanner, "IS_MACOS", False),
        patch("habluetooth.scanner.OriginalBleakScanner") as mock_scanner,
    ):
        create_bleak_scanner(None, BluetoothScanningMode.ACTIVE, None)
    kwargs = mock_scanner.call_args.kwargs
    assert "bluez" not in kwargs
    assert "adapter" not in kwargs


def test_create_bleak_scanner_linux_no_adapter_passive() -> None:
    """Linux + no adapter + passive: ``bluez`` carries passive args only."""
    with (
        patch.object(scanner, "IS_LINUX", True),
        patch.object(scanner, "IS_MACOS", False),
        patch("habluetooth.scanner.OriginalBleakScanner") as mock_scanner,
    ):
        create_bleak_scanner(None, BluetoothScanningMode.PASSIVE, None)
    bluez = mock_scanner.call_args.kwargs["bluez"]
    assert "adapter" not in bluez
    # PASSIVE args are copied in — the production dict must not be mutated.
    assert bluez == dict(scanner.PASSIVE_SCANNER_ARGS)


def test_create_bleak_scanner_linux_adapter_active() -> None:
    """Linux + adapter + active: ``bluez`` carries adapter only."""
    with (
        patch.object(scanner, "IS_LINUX", True),
        patch.object(scanner, "IS_MACOS", False),
        patch("habluetooth.scanner.OriginalBleakScanner") as mock_scanner,
    ):
        create_bleak_scanner(None, BluetoothScanningMode.ACTIVE, "hci2")
    bluez = mock_scanner.call_args.kwargs["bluez"]
    assert bluez == {"adapter": "hci2"}


def test_create_bleak_scanner_linux_adapter_passive() -> None:
    """Linux + adapter + passive: ``bluez`` merges adapter and passive args."""
    with (
        patch.object(scanner, "IS_LINUX", True),
        patch.object(scanner, "IS_MACOS", False),
        patch("habluetooth.scanner.OriginalBleakScanner") as mock_scanner,
    ):
        create_bleak_scanner(None, BluetoothScanningMode.PASSIVE, "hci1")
    bluez = mock_scanner.call_args.kwargs["bluez"]
    assert bluez.get("adapter") == "hci1"
    # The production code must copy PASSIVE_SCANNER_ARGS — assert the source
    # was not mutated by the adapter insertion.
    assert "adapter" not in scanner.PASSIVE_SCANNER_ARGS


@pytest.mark.asyncio
async def test_empty_data_no_scanner() -> None:
    """Test we handle empty data."""
    scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
    scanner.async_setup()
    assert scanner.discovered_devices == []
    assert scanner.discovered_devices_and_advertisement_data == {}


@pytest.mark.asyncio
@pytest.mark.skipif(NOT_POSIX)
async def test_dbus_socket_missing_in_container(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test we handle dbus being missing in the container."""
    with (
        patch("habluetooth.scanner.is_docker_env", return_value=True),
        patch(
            "habluetooth.scanner.OriginalBleakScanner.start",
            side_effect=FileNotFoundError,
        ),
        patch(
            "habluetooth.scanner.OriginalBleakScanner.stop",
        ) as mock_stop,
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        with pytest.raises(
            ScannerStartError,
            match="DBus service not found; docker config may be missing",
        ):
            await scanner.async_start()
        assert mock_stop.called


@pytest.mark.asyncio
@pytest.mark.skipif(NOT_POSIX)
async def test_dbus_socket_missing(caplog: pytest.LogCaptureFixture) -> None:
    """Test we handle dbus being missing."""
    with (
        patch("habluetooth.scanner.is_docker_env", return_value=False),
        patch(
            "habluetooth.scanner.OriginalBleakScanner.start",
            side_effect=FileNotFoundError,
        ),
        patch(
            "habluetooth.scanner.OriginalBleakScanner.stop",
        ) as mock_stop,
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        with pytest.raises(
            ScannerStartError,
            match="DBus service not found; make sure the DBus socket is available",
        ):
            await scanner.async_start()
        assert mock_stop.called


@pytest.mark.asyncio
@pytest.mark.skipif(NOT_POSIX)
async def test_handle_cancellation(caplog: pytest.LogCaptureFixture) -> None:
    """Test cancellation stops."""
    with (
        patch("habluetooth.scanner.is_docker_env", return_value=False),
        patch(
            "habluetooth.scanner.OriginalBleakScanner.start",
            side_effect=asyncio.CancelledError,
        ),
        patch(
            "habluetooth.scanner.OriginalBleakScanner.stop",
        ) as mock_stop,
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        with pytest.raises(asyncio.CancelledError):
            await scanner.async_start()
        assert mock_stop.called


@pytest.mark.asyncio
@pytest.mark.skipif(NOT_POSIX)
async def test_handle_stop_while_starting(caplog: pytest.LogCaptureFixture) -> None:
    """Test stop while starting."""

    async def _start(*args, **kwargs):
        await asyncio.sleep(1000)

    with (
        patch("habluetooth.scanner.is_docker_env", return_value=False),
        patch("habluetooth.scanner.OriginalBleakScanner.start", _start),
        patch(
            "habluetooth.scanner.OriginalBleakScanner.stop",
        ) as mock_stop,
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        task = asyncio.create_task(scanner.async_start())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await scanner.async_stop()
        with pytest.raises(
            ScannerStartError, match="Starting bluetooth scanner aborted"
        ):
            await task
        assert mock_stop.called


@pytest.mark.asyncio
@pytest.mark.skipif(NOT_POSIX)
async def test_dbus_broken_pipe_in_container(caplog: pytest.LogCaptureFixture) -> None:
    """Test we handle dbus broken pipe in the container."""
    with (
        patch("habluetooth.scanner.is_docker_env", return_value=True),
        patch(
            "habluetooth.scanner.OriginalBleakScanner.start",
            side_effect=BrokenPipeError,
        ),
        patch(
            "habluetooth.scanner.OriginalBleakScanner.stop",
        ) as mock_stop,
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        with pytest.raises(ScannerStartError, match="DBus connection broken"):
            await scanner.async_start()
        assert mock_stop.called


@pytest.mark.asyncio
@pytest.mark.skipif(NOT_POSIX)
async def test_dbus_broken_pipe(caplog: pytest.LogCaptureFixture) -> None:
    """Test we handle dbus broken pipe."""
    with (
        patch("habluetooth.scanner.is_docker_env", return_value=False),
        patch(
            "habluetooth.scanner.OriginalBleakScanner.start",
            side_effect=BrokenPipeError,
        ),
        patch(
            "habluetooth.scanner.OriginalBleakScanner.stop",
        ) as mock_stop,
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        with pytest.raises(ScannerStartError, match="DBus connection broken:"):
            await scanner.async_start()
        assert mock_stop.called


@pytest.mark.asyncio
@pytest.mark.skipif(NOT_POSIX)
async def test_invalid_dbus_message(caplog: pytest.LogCaptureFixture) -> None:
    """Test we handle invalid dbus message."""
    with patch(
        "habluetooth.scanner.OriginalBleakScanner.start",
        side_effect=InvalidMessageError,
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        with pytest.raises(ScannerStartError, match="Invalid DBus message received"):
            await scanner.async_start()


@pytest.mark.asyncio
@pytest.mark.skipif(IS_WINDOWS)
@pytest.mark.parametrize("error", NEED_RESET_ERRORS)
async def test_adapter_needs_reset_at_start(
    caplog: pytest.LogCaptureFixture, error: str
) -> None:
    """Test we cycle the adapter when it needs a restart."""
    called_start = 0
    called_stop = 0
    _callback = None
    mock_discovered: list[Any] = []

    class _DBusRecoveryScanner(MockBleakScanner):
        async def start(self, *args: object, **kwargs: object) -> None:
            nonlocal called_start
            called_start += 1
            if called_start < 3:
                raise BleakError(error)

        async def stop(self, *args: object, **kwargs: object) -> None:
            nonlocal called_stop
            called_stop += 1

        @property
        def discovered_devices(self):
            return mock_discovered

        def register_detection_callback(
            self, callback: AdvertisementDataCallback
        ) -> None:
            nonlocal _callback
            _callback = callback

    mock_scanner = _DBusRecoveryScanner()

    with (
        patch("habluetooth.scanner.OriginalBleakScanner", return_value=mock_scanner),
        patch(
            "habluetooth.util.recover_adapter", return_value=True
        ) as mock_recover_adapter,
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()

        assert len(mock_recover_adapter.mock_calls) == 1
        await scanner.async_stop()


@pytest.mark.asyncio
@pytest.mark.skipif(IS_WINDOWS)
async def test_recovery_from_dbus_restart() -> None:
    """Test we can recover when DBus gets restarted out from under us."""
    called_start = 0
    called_stop = 0
    _callback = None
    mock_discovered: list[Any] = []

    class _CallbackCapturingScanner(MockBleakScanner):
        def __init__(
            self,
            detection_callback: AdvertisementDataCallback,
            *args: object,
            **kwargs: object,
        ) -> None:
            super().__init__()
            nonlocal _callback
            _callback = detection_callback

        async def start(self, *args: object, **kwargs: object) -> None:
            nonlocal called_start
            called_start += 1

        async def stop(self, *args: object, **kwargs: object) -> None:
            nonlocal called_stop
            called_stop += 1

        @property
        def discovered_devices(self):
            return mock_discovered

    with patch(
        "habluetooth.scanner.OriginalBleakScanner",
        _CallbackCapturingScanner,
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()

        assert called_start == 1

        start_time_monotonic = time.monotonic()
        mock_discovered = [MagicMock()]

        # Ensure we don't restart the scanner if we don't need to
        with patch_bluetooth_time(
            start_time_monotonic + 10,
        ):
            async_fire_time_changed(utcnow() + SCANNER_WATCHDOG_INTERVAL)

        assert called_start == 1

        # Fire a callback to reset the timer
        with patch_bluetooth_time(
            start_time_monotonic,
        ):
            _callback(  # type: ignore[misc]
                generate_ble_device("44:44:33:11:23:42", "any_name"),
                generate_advertisement_data(local_name="any_name"),
            )

        # Ensure we don't restart the scanner if we don't need to
        with patch_bluetooth_time(
            start_time_monotonic + 20,
        ):
            async_fire_time_changed(utcnow() + SCANNER_WATCHDOG_INTERVAL)
            await asyncio.sleep(0)

        assert called_start == 1

        # We hit the timer, so we restart the scanner
        with patch_bluetooth_time(
            start_time_monotonic + SCANNER_WATCHDOG_TIMEOUT + 20,
        ):
            async_fire_time_changed(
                utcnow() + SCANNER_WATCHDOG_INTERVAL + timedelta(seconds=20)
            )
            await asyncio.sleep(0)

        assert called_start == 2
        await scanner.async_stop()


@pytest.mark.asyncio
@pytest.mark.skipif(IS_WINDOWS)
async def test_adapter_recovery() -> None:
    """Test we can recover when the adapter stops responding."""
    called_start = 0
    called_stop = 0
    _callback = None
    mock_discovered: list[Any] = []

    class _AdapterRecoveryScanner(MockBleakScanner):
        async def start(self, *args: object, **kwargs: object) -> None:
            nonlocal called_start
            called_start += 1

        async def stop(self, *args: object, **kwargs: object) -> None:
            nonlocal called_stop
            called_stop += 1

        @property
        def discovered_devices(self):
            return mock_discovered

        def register_detection_callback(
            self, callback: AdvertisementDataCallback
        ) -> None:
            nonlocal _callback
            _callback = callback

    mock_scanner = _AdapterRecoveryScanner()
    start_time_monotonic = time.monotonic()

    with (
        patch_bluetooth_time(
            start_time_monotonic,
        ),
        patch(
            "habluetooth.scanner.OriginalBleakScanner",
            return_value=mock_scanner,
        ),
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()

        assert called_start == 1

        mock_discovered = [MagicMock()]

        # Ensure we don't restart the scanner if we don't need to
        with patch_bluetooth_time(
            start_time_monotonic + 10,
        ):
            async_fire_time_changed(utcnow() + SCANNER_WATCHDOG_INTERVAL)
            await asyncio.sleep(0)

        assert called_start == 1

        # Ensure we don't restart the scanner if we don't need to
        with patch_bluetooth_time(
            start_time_monotonic + 20,
        ):
            async_fire_time_changed(utcnow() + SCANNER_WATCHDOG_INTERVAL)
            await asyncio.sleep(0)

        assert called_start == 1

        # We hit the timer with no detections, so we
        # reset the adapter and restart the scanner
        with (
            patch_bluetooth_time(
                start_time_monotonic
                + SCANNER_WATCHDOG_TIMEOUT
                + SCANNER_WATCHDOG_INTERVAL.total_seconds(),
            ),
            patch(
                "habluetooth.util.recover_adapter", return_value=True
            ) as mock_recover_adapter,
        ):
            async_fire_time_changed(utcnow() + SCANNER_WATCHDOG_INTERVAL)
            await asyncio.sleep(0)

        assert len(mock_recover_adapter.mock_calls) == 1
        assert mock_recover_adapter.call_args_list[0][0] == (
            0,
            "AA:BB:CC:DD:EE:FF",
            True,
        )

        assert called_start == 2
        await scanner.async_stop()


@pytest.mark.asyncio
@pytest.mark.skipif(IS_WINDOWS)
async def test_adapter_scanner_fails_to_start_first_time() -> None:
    """
    Test we can recover when the adapter stops responding.

    The first recovery fails.
    """
    called_start = 0
    called_stop = 0
    _callback = None
    mock_discovered: list[Any] = []

    class _RestartFailScanner(MockBleakScanner):
        async def start(self, *args: object, **kwargs: object) -> None:
            nonlocal called_start
            called_start += 1
            if called_start == 1:
                return
            if called_start < 4:
                msg = "Failed to start"
                raise BleakError(msg)

        async def stop(self, *args: object, **kwargs: object) -> None:
            nonlocal called_stop
            called_stop += 1

        @property
        def discovered_devices(self):
            return mock_discovered

        def register_detection_callback(
            self, callback: AdvertisementDataCallback
        ) -> None:
            nonlocal _callback
            _callback = callback

    mock_scanner = _RestartFailScanner()
    start_time_monotonic = time.monotonic()

    with (
        patch_bluetooth_time(
            start_time_monotonic,
        ),
        patch(
            "habluetooth.scanner.OriginalBleakScanner",
            return_value=mock_scanner,
        ),
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()

        assert called_start == 1

        mock_discovered = [MagicMock()]

        # Ensure we don't restart the scanner if we don't need to
        with patch_bluetooth_time(
            start_time_monotonic + 10,
        ):
            async_fire_time_changed(utcnow() + SCANNER_WATCHDOG_INTERVAL)
            await asyncio.sleep(0)

        assert called_start == 1

        # Ensure we don't restart the scanner if we don't need to
        with patch_bluetooth_time(
            start_time_monotonic + 20,
        ):
            async_fire_time_changed(utcnow() + SCANNER_WATCHDOG_INTERVAL)
            await asyncio.sleep(0)

        assert called_start == 1

        # We hit the timer with no detections,
        # so we reset the adapter and restart the scanner
        with (
            patch_bluetooth_time(
                start_time_monotonic
                + SCANNER_WATCHDOG_TIMEOUT
                + SCANNER_WATCHDOG_INTERVAL.total_seconds(),
            ),
            patch(
                "habluetooth.util.recover_adapter", return_value=True
            ) as mock_recover_adapter,
        ):
            async_fire_time_changed(utcnow() + SCANNER_WATCHDOG_INTERVAL)
            await asyncio.sleep(0)

        assert len(mock_recover_adapter.mock_calls) == 1
        assert called_start == 4
        assert scanner.scanning is True

        now_monotonic = time.monotonic()
        # We hit the timer again the previous start call failed, make sure
        # we try again
        with (
            patch_bluetooth_time(
                now_monotonic
                + SCANNER_WATCHDOG_TIMEOUT * 2
                + SCANNER_WATCHDOG_INTERVAL.total_seconds(),
            ),
            patch(
                "habluetooth.util.recover_adapter", return_value=True
            ) as mock_recover_adapter,
        ):
            async_fire_time_changed(utcnow() + SCANNER_WATCHDOG_INTERVAL)
            await asyncio.sleep(0)

        assert len(mock_recover_adapter.mock_calls) == 1
        assert called_start == 5
        await scanner.async_stop()


@pytest.mark.asyncio
async def test_adapter_fails_to_start_and_takes_a_bit_to_init(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test we can recover the adapter at startup and we wait for Dbus to init."""
    called_start = 0
    called_stop = 0
    _callback = None
    mock_discovered: list[Any] = []

    class _DBusInProgressScanner(MockBleakScanner):
        async def start(self, *args: object, **kwargs: object) -> None:
            nonlocal called_start
            called_start += 1
            if called_start == 1:
                msg = "org.freedesktop.DBus.Error.UnknownObject"
                raise BleakError(msg)
            if called_start == 2:
                msg = "org.bluez.Error.InProgress"
                raise BleakError(msg)
            if called_start == 3:
                msg = "org.bluez.Error.InProgress"
                raise BleakError(msg)

        async def stop(self, *args: object, **kwargs: object) -> None:
            nonlocal called_stop
            called_stop += 1

        @property
        def discovered_devices(self):
            return mock_discovered

        def register_detection_callback(
            self, callback: AdvertisementDataCallback
        ) -> None:
            nonlocal _callback
            _callback = callback

    mock_scanner = _DBusInProgressScanner()
    start_time_monotonic = time.monotonic()

    with (
        patch(
            "habluetooth.scanner.ADAPTER_INIT_TIME",
            0,
        ),
        patch_bluetooth_time(
            start_time_monotonic,
        ),
        patch(
            "habluetooth.scanner.OriginalBleakScanner",
            return_value=mock_scanner,
        ),
        patch(
            "habluetooth.util.recover_adapter", return_value=True
        ) as mock_recover_adapter,
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()

        assert called_start == 4

        assert len(mock_recover_adapter.mock_calls) == 1
        assert "Waiting for adapter to initialize" in caplog.text
        await scanner.async_stop()


@pytest.mark.asyncio
async def test_restart_takes_longer_than_watchdog_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Test we do not try to recover the adapter again.

    If the restart is still in progress.
    """
    release_start_event = asyncio.Event()
    called_start = 0

    class _ReleaseGatedScanner(MockBleakScanner):
        async def start(self, *args: object, **kwargs: object) -> None:
            nonlocal called_start
            called_start += 1
            if called_start == 1:
                return
            await release_start_event.wait()

    mock_scanner = _ReleaseGatedScanner()
    start_time_monotonic = time.monotonic()

    with (
        patch(
            "habluetooth.scanner.ADAPTER_INIT_TIME",
            0,
        ),
        patch_bluetooth_time(
            start_time_monotonic,
        ),
        patch(
            "habluetooth.scanner.OriginalBleakScanner",
            return_value=mock_scanner,
        ),
        patch("habluetooth.util.recover_adapter", return_value=True),
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()

        assert called_start == 1

        # Now force a recover adapter 2x
        for _ in range(2):
            with patch_bluetooth_time(
                start_time_monotonic
                + SCANNER_WATCHDOG_TIMEOUT
                + SCANNER_WATCHDOG_INTERVAL.total_seconds(),
            ):
                async_fire_time_changed(utcnow() + SCANNER_WATCHDOG_INTERVAL)
                await asyncio.sleep(0)

        # Now release the start event
        release_start_event.set()

        assert "already restarting" in caplog.text
        await scanner.async_stop()


@pytest.mark.asyncio
@pytest.mark.skipif("platform.system() != 'Darwin'")
async def test_setup_and_stop_macos() -> None:
    """Test we enable use_bdaddr on MacOS."""
    init_kwargs = None

    class _KwargsCapturingScanner(MockBleakScanner):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()
            nonlocal init_kwargs
            init_kwargs = kwargs

    with patch(
        "habluetooth.scanner.OriginalBleakScanner",
        _KwargsCapturingScanner,
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()

        assert init_kwargs == {
            "detection_callback": ANY,
            "scanning_mode": "active",
            "cb": {"use_bdaddr": True},
        }
        await scanner.async_stop()


@pytest.mark.asyncio
async def test_adapter_init_fails_fallback_to_passive(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test we fallback to passive when adapter init fails."""
    called_start = 0
    called_stop = 0
    _callback = None
    mock_discovered: list[Any] = []

    class _InProgressFatalScanner(MockBleakScanner):
        async def start(self, *args: object, **kwargs: object) -> None:
            nonlocal called_start
            called_start += 1
            if called_start == 1:
                msg = "org.freedesktop.DBus.Error.UnknownObject"
                raise BleakError(msg)
            if called_start == 2:
                msg = "org.bluez.Error.InProgress"
                raise BleakError(msg)
            if called_start == 3:
                msg = "org.bluez.Error.InProgress"
                raise BleakError(msg)

        async def stop(self, *args: object, **kwargs: object) -> None:
            nonlocal called_stop
            called_stop += 1

        @property
        def discovered_devices(self):
            return mock_discovered

        def register_detection_callback(
            self, callback: AdvertisementDataCallback
        ) -> None:
            nonlocal _callback
            _callback = callback

    mock_scanner = _InProgressFatalScanner()
    start_time_monotonic = time.monotonic()

    with (
        patch(
            "habluetooth.scanner.IS_LINUX",
            True,
        ),
        patch(
            "habluetooth.scanner.ADAPTER_INIT_TIME",
            0,
        ),
        patch_bluetooth_time(
            start_time_monotonic,
        ),
        patch(
            "habluetooth.scanner.OriginalBleakScanner",
            return_value=mock_scanner,
        ),
        patch(
            "habluetooth.util.recover_adapter", return_value=True
        ) as mock_recover_adapter,
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()

        assert called_start == 4

        assert len(mock_recover_adapter.mock_calls) == 1
        assert "Waiting for adapter to initialize" in caplog.text
        assert (
            "Successful fall-back to passive scanning mode after active scanning failed"
            in caplog.text
        )
        assert await scanner.async_diagnostics() == {
            "adapter": "hci0",
            "connect_failures": {},
            "connect_in_progress": {},
            "connect_completed_total": 0,
            "connect_failed_total": 0,
            "last_connect_completed_time": 0.0,
            "connectable": True,
            "current_mode": BluetoothScanningMode.PASSIVE,
            "discovered_devices_and_advertisement_data": [],
            "last_detection": ANY,
            "monotonic_time": ANY,
            "name": "hci0 (AA:BB:CC:DD:EE:FF)",
            "requested_mode": BluetoothScanningMode.ACTIVE,
            "scanning": True,
            "source": "AA:BB:CC:DD:EE:FF",
            "start_time": ANY,
            "type": "HaScanner",
        }
        await scanner.async_stop()
        assert await scanner.async_diagnostics() == {
            "adapter": "hci0",
            "connect_failures": {},
            "connect_in_progress": {},
            "connect_completed_total": 0,
            "connect_failed_total": 0,
            "last_connect_completed_time": 0.0,
            "connectable": True,
            "current_mode": BluetoothScanningMode.PASSIVE,
            "discovered_devices_and_advertisement_data": [],
            "last_detection": ANY,
            "monotonic_time": ANY,
            "name": "hci0 (AA:BB:CC:DD:EE:FF)",
            "requested_mode": BluetoothScanningMode.ACTIVE,
            "scanning": False,
            "source": "AA:BB:CC:DD:EE:FF",
            "start_time": ANY,
            "type": "HaScanner",
        }


@pytest.mark.asyncio
@pytest.mark.skipif(NOT_POSIX)
async def test_scanner_with_bluez_mgmt_side_channel(mock_btmgmt_socket: Mock) -> None:
    """Test scanner receiving advertisements via BlueZ management side channel."""
    # Mock capability check for the entire test
    with patch.object(MGMTBluetoothCtl, "_check_capabilities", return_value=True):
        # Create a custom manager that tracks discovered devices
        class TestBluetoothManager(BluetoothManager):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.discovered_infos = []

            def _discover_service_info(
                self, service_info: BluetoothServiceInfoBleak
            ) -> None:
                """Track discovered service info."""
                self.discovered_infos.append(service_info)

        # Create manager and setup mgmt controller
        adapters = FakeBluetoothAdapters()
        slot_manager = BleakSlotManager()
        manager = TestBluetoothManager(adapters, slot_manager)
        set_manager(manager)

        # Set up the manager first
        await manager.async_setup()

        # Create and setup the mgmt controller with the manager's side channel scanners
        mgmt_ctl = MGMTBluetoothCtl(
            timeout=5.0, scanners=manager._side_channel_scanners
        )

        # Mock the protocol setup
        mock_protocol = Mock(spec=BluetoothMGMTProtocol)
        mock_transport = Mock()
        mock_protocol.transport = mock_transport

        async def mock_setup():
            mgmt_ctl.protocol = mock_protocol
            mgmt_ctl._on_connection_lost_future = (
                asyncio.get_running_loop().create_future()
            )

        mgmt_ctl.setup = mock_setup  # type: ignore[method-assign]

        # Inject mgmt controller into manager
        manager._mgmt_ctl = mgmt_ctl
        manager.has_advertising_side_channel = True

        # Verify get_bluez_mgmt_ctl returns our controller
        assert manager.get_bluez_mgmt_ctl() is mgmt_ctl

        # Register scanner
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        manager.async_register_scanner(scanner, connection_slots=2)

        # Start scanner - should be created without detection callback
        with patch("habluetooth.scanner.OriginalBleakScanner") as mock_scanner_class:
            mock_scanner = Mock()
            mock_scanner.start = AsyncMock()
            mock_scanner.stop = AsyncMock()
            mock_scanner.discovered_devices = []
            mock_scanner_class.return_value = mock_scanner

            await scanner.async_start()

            # Verify scanner was created without detection callback
            # since side channel is available
            mock_scanner_class.assert_called_once()
            call_kwargs = mock_scanner_class.call_args[1]
            assert (
                "detection_callback" not in call_kwargs
                or call_kwargs["detection_callback"] is None
            )

        # Now simulate advertisement data coming through the mgmt protocol
        # The manager should have registered the scanner with mgmt_ctl
        assert 0 in mgmt_ctl.scanners  # hci0 is index 0
        assert mgmt_ctl.scanners[0] is scanner

        # Simulate the protocol calling the scanner's raw advertisement handler
        test_address = b"\xaa\xbb\xcc\xdd\xee\xff"
        test_rssi = -60
        test_flags = 0x06
        # Create valid advertisement data with flags
        # Each AD structure is: length (1 byte), type (1 byte), data
        test_data = (
            b"\x02\x01\x06"  # Length=2, Type=0x01 (Flags), Data=0x06
            # Length=8, Type=0x09 (Complete Local Name), Data="TestDev"
            b"\x08\x09TestDev"
        )

        # Call the method that the protocol would call
        scanner._async_on_raw_bluez_advertisement(
            test_address,
            1,  # address_type: BDADDR_LE_PUBLIC
            test_rssi,
            test_flags,
            test_data,
        )

        # Allow time for processing
        await asyncio.sleep(0)

        # Verify the device was discovered in the base scanner
        assert len(scanner._previous_service_info) == 1
        assert "FF:EE:DD:CC:BB:AA" in scanner._previous_service_info

        service_info = scanner._previous_service_info["FF:EE:DD:CC:BB:AA"]
        assert service_info.address == "FF:EE:DD:CC:BB:AA"
        assert service_info.rssi == test_rssi
        assert service_info.name == "TestDev"

        # Verify the manager also received the advertisement
        assert len(manager.discovered_infos) == 1
        assert manager.discovered_infos[0] is service_info

        await scanner.async_stop()
        manager.async_stop()


@pytest.mark.asyncio
@pytest.mark.skipif(NOT_POSIX)
async def test_scanner_without_bluez_mgmt_side_channel() -> None:
    """Test scanner uses normal detection callback when side channel unavailable."""

    # Create manager without BlueZ mgmt support
    class TestBluetoothManager(BluetoothManager):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.discovered_infos = []

        def _discover_service_info(
            self, service_info: BluetoothServiceInfoBleak
        ) -> None:
            """Track discovered service info."""
            self.discovered_infos.append(service_info)

    adapters = FakeBluetoothAdapters()
    slot_manager = BleakSlotManager()
    manager = TestBluetoothManager(adapters, slot_manager)
    set_manager(manager)

    # Setup without mgmt controller
    await manager.async_setup()
    assert manager.has_advertising_side_channel is False

    # Register scanner
    scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
    scanner.async_setup()
    manager.async_register_scanner(scanner, connection_slots=2)

    # Start scanner - should be created with detection callback
    with patch("habluetooth.scanner.OriginalBleakScanner") as mock_scanner_class:
        mock_scanner = Mock()
        mock_scanner.start = AsyncMock()
        mock_scanner.stop = AsyncMock()
        mock_scanner.discovered_devices = []
        mock_scanner_class.return_value = mock_scanner

        await scanner.async_start()

        # Verify scanner was created with detection callback since no side channel
        mock_scanner_class.assert_called_once()
        call_kwargs = mock_scanner_class.call_args[1]
        assert "detection_callback" in call_kwargs
        assert call_kwargs["detection_callback"] is not None
        assert call_kwargs["detection_callback"] == scanner._async_detection_callback

    await scanner.async_stop()
    manager.async_stop()


@pytest.mark.asyncio
@pytest.mark.skipif(NOT_POSIX)
async def test_bluez_mgmt_protocol_data_flow(mock_btmgmt_socket: Mock) -> None:
    """Test data flow from BlueZ protocol through manager to scanner."""
    # Mock capability check for the entire test
    with patch.object(MGMTBluetoothCtl, "_check_capabilities", return_value=True):
        # Create manager
        class TestBluetoothManager(BluetoothManager):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.discovered_infos = []

            def _discover_service_info(
                self, service_info: BluetoothServiceInfoBleak
            ) -> None:
                """Track discovered service info."""
                self.discovered_infos.append(service_info)

        adapters = FakeBluetoothAdapters()
        slot_manager = BleakSlotManager()
        manager = TestBluetoothManager(adapters, slot_manager)
        set_manager(manager)

        # Set up manager first
        await manager.async_setup()

        # Create mgmt controller with the manager's side channel scanners dictionary
        mgmt_ctl = MGMTBluetoothCtl(
            timeout=5.0, scanners=manager._side_channel_scanners
        )

        # We'll capture the protocol when it's created
        captured_protocol: BluetoothMGMTProtocol | None = None

        async def mock_create_connection(sock, protocol_factory, *args, **kwargs):
            nonlocal captured_protocol
            captured_protocol = protocol_factory()
            mock_transport = Mock()
            captured_protocol.connection_made(mock_transport)
            return mock_transport, captured_protocol

        with patch.object(
            asyncio.get_running_loop(),
            "_create_connection_transport",
            mock_create_connection,
        ):
            await mgmt_ctl.setup()

        # Set mgmt controller on manager
        manager._mgmt_ctl = mgmt_ctl
        manager.has_advertising_side_channel = True

        # Register scanners for hci0 and hci1
        scanner0 = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:00")
        scanner0.async_setup()
        manager.async_register_scanner(scanner0, connection_slots=2)

        scanner1 = HaScanner(BluetoothScanningMode.ACTIVE, "hci1", "AA:BB:CC:DD:EE:01")
        scanner1.async_setup()
        manager.async_register_scanner(scanner1, connection_slots=2)

        # Start scanners
        with patch("habluetooth.scanner.OriginalBleakScanner") as mock_scanner_class:
            mock_scanner = Mock()
            mock_scanner.start = AsyncMock()
            mock_scanner.stop = AsyncMock()
            mock_scanner.discovered_devices = []
            mock_scanner_class.return_value = mock_scanner
            await scanner0.async_start()
            await scanner1.async_start()

        # Verify scanners are registered in mgmt_ctl
        assert 0 in mgmt_ctl.scanners
        assert 1 in mgmt_ctl.scanners
        assert mgmt_ctl.scanners[0] is scanner0
        assert mgmt_ctl.scanners[1] is scanner1

        # Test DEVICE_FOUND event for hci0
        test_address = b"\x11\x22\x33\x44\x55\x66"
        rssi_byte = b"\xc4"  # -60 in signed byte
        event_data = (
            test_address
            + b"\x01"  # address_type
            + rssi_byte
            + b"\x06\x00\x00\x00"  # flags
            + b"\x03\x00"  # data_len
            + b"\x02\x01\x06"  # minimal adv data
        )

        packet = (
            DEVICE_FOUND.to_bytes(2, "little")
            + b"\x00\x00"  # controller_idx 0 (hci0)
            + len(event_data).to_bytes(2, "little")
            + event_data
        )

        # Feed packet to protocol
        assert captured_protocol is not None
        captured_protocol.data_received(packet)

        # Verify device discovered on scanner0 only
        assert len(scanner0._previous_service_info) == 1
        assert "66:55:44:33:22:11" in scanner0._previous_service_info
        assert len(scanner1._previous_service_info) == 0

        # Test ADV_MONITOR_DEVICE_FOUND event for hci1
        test_address2 = b"\xaa\xbb\xcc\xdd\xee\x02"
        monitor_handle = b"\x01\x00"
        rssi_byte2 = b"\xba"  # -70 in signed byte
        event_data2 = (
            monitor_handle
            + test_address2
            + b"\x02"  # address_type (random)
            + rssi_byte2
            + b"\x06\x00\x00\x00"  # flags
            + b"\x03\x00"  # data_len
            + b"\x02\x01\x06"  # minimal adv data
        )

        packet2 = (
            ADV_MONITOR_DEVICE_FOUND.to_bytes(2, "little")
            + b"\x01\x00"  # controller_idx 1 (hci1)
            + len(event_data2).to_bytes(2, "little")
            + event_data2
        )

        assert captured_protocol is not None
        captured_protocol.data_received(packet2)

        # Verify device discovered on scanner1 only
        assert len(scanner0._previous_service_info) == 1  # Still just the first device
        assert len(scanner1._previous_service_info) == 1
        assert "02:EE:DD:CC:BB:AA" in scanner1._previous_service_info

        # Verify RSSI values
        info0 = scanner0._previous_service_info["66:55:44:33:22:11"]
        assert info0.rssi == -60

        info1 = scanner1._previous_service_info["02:EE:DD:CC:BB:AA"]
        assert info1.rssi == -70

        await scanner0.async_stop()
        await scanner1.async_stop()
        manager.async_stop()


@pytest.mark.asyncio
@pytest.mark.skipif(NOT_POSIX)
async def test_mgmt_permission_error_fallback() -> None:
    """Test that permission errors in MGMT setup fall back to BlueZ-only mode."""

    # Create manager
    class TestBluetoothManager(BluetoothManager):
        def _discover_service_info(
            self, service_info: BluetoothServiceInfoBleak
        ) -> None:
            """Track discovered service info."""

    adapters = FakeBluetoothAdapters()
    slot_manager = BleakSlotManager()
    manager = TestBluetoothManager(adapters, slot_manager)

    # Mock MGMTBluetoothCtl setup to raise PermissionError
    with (
        patch("habluetooth.manager.MGMTBluetoothCtl") as mock_mgmt_cls,
        patch("habluetooth.manager.IS_LINUX", True),
    ):
        mock_mgmt = Mock()
        mock_mgmt.setup = AsyncMock(
            side_effect=PermissionError(
                "Missing NET_ADMIN/NET_RAW capabilities for Bluetooth management"
            )
        )
        mock_mgmt_cls.return_value = mock_mgmt

        # Setup should complete without raising the exception
        await manager.async_setup()

        # Verify MGMT was attempted but then set to None
        mock_mgmt.setup.assert_called_once()
        assert manager._mgmt_ctl is None
        assert manager.has_advertising_side_channel is False


def test_usb_scanner_type() -> None:
    """Test that USB adapters get USB scanner type."""
    manager = get_manager()

    # Mock cached adapters with USB adapter
    mock_adapters: dict[str, dict[str, Any]] = {
        "hci0": {
            "address": "00:1A:7D:DA:71:04",
            "adapter_type": "usb",
            "manufacturer": "TestManufacturer",
            "product": "USB Bluetooth Adapter",
        }
    }

    with patch.object(manager, "_adapters", mock_adapters):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "00:1A:7D:DA:71:04")
        assert scanner.details.scanner_type is HaScannerType.USB


def test_uart_scanner_type() -> None:
    """Test that UART adapters get UART scanner type."""
    manager = get_manager()

    # Mock cached adapters with UART adapter
    mock_adapters: dict[str, dict[str, Any]] = {
        "hci0": {
            "address": "00:1A:7D:DA:71:04",
            "adapter_type": "uart",
            "manufacturer": "TestManufacturer",
            "product": "UART Bluetooth Module",
        }
    }

    with patch.object(manager, "_adapters", mock_adapters):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "00:1A:7D:DA:71:04")
        assert scanner.details.scanner_type is HaScannerType.UART


def test_unknown_scanner_type_no_cached_adapters() -> None:
    """Test that scanners get UNKNOWN type when no adapter info is cached."""
    manager = get_manager()

    # No cached adapters
    with patch.object(manager, "_adapters", None):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "00:1A:7D:DA:71:04")
        assert scanner.details.scanner_type is HaScannerType.UNKNOWN


def test_unknown_scanner_type_adapter_not_found() -> None:
    """Test that scanners get UNKNOWN type when adapter is not in cache."""
    manager = get_manager()

    # Cached adapters but not the one we're looking for
    mock_adapters: dict[str, dict[str, Any]] = {
        "hci1": {
            "address": "11:22:33:44:55:66",
            "adapter_type": "usb",
        }
    }

    with patch.object(manager, "_adapters", mock_adapters):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "00:1A:7D:DA:71:04")
        assert scanner.details.scanner_type is HaScannerType.UNKNOWN


def test_unknown_scanner_type_no_adapter_type() -> None:
    """Test that scanners get UNKNOWN type when adapter_type is None."""
    manager = get_manager()

    # Cached adapter without adapter_type field
    mock_adapters: dict[str, dict[str, Any]] = {
        "hci0": {
            "address": "00:1A:7D:DA:71:04",
            "adapter_type": None,
            "manufacturer": "TestManufacturer",
        }
    }

    with patch.object(manager, "_adapters", mock_adapters):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "00:1A:7D:DA:71:04")
        assert scanner.details.scanner_type is HaScannerType.UNKNOWN


@pytest.mark.asyncio
async def test_scanner_type_with_real_adapter_data() -> None:
    """Test scanner type detection with realistic adapter data."""
    # Create a custom manager for this test
    manager = BluetoothManager(bluetooth_adapters=MagicMock())
    set_manager(manager)

    # Simulate real USB adapter data from Linux
    usb_adapter_data: dict[str, dict[str, Any]] = {
        "hci0": {
            "address": "00:1A:7D:DA:71:04",
            "sw_version": "homeassistant",
            "hw_version": "usb:v1D6Bp0246d053F",
            "passive_scan": False,
            "manufacturer": "XTech",
            "product": "Bluetooth 4.0 USB Adapter",
            "vendor_id": "0a12",
            "product_id": "0001",
            "adapter_type": "usb",
        }
    }

    manager._adapters = usb_adapter_data

    # Create USB scanner
    usb_scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "00:1A:7D:DA:71:04")
    assert usb_scanner.details.scanner_type is HaScannerType.USB
    assert usb_scanner.details.adapter == "hci0"

    # Simulate real UART adapter data
    uart_adapter_data: dict[str, dict[str, Any]] = {
        "hci1": {
            "address": "AA:BB:CC:DD:EE:FF",
            "sw_version": "homeassistant",
            "hw_version": "uart:ttyUSB0",
            "passive_scan": False,
            "manufacturer": "cyber-blue(HK)Ltd",
            "product": "Bluetooth 4.0 UART Module",
            "vendor_id": None,
            "product_id": None,
            "adapter_type": "uart",
        }
    }

    manager._adapters = uart_adapter_data

    # Create UART scanner
    uart_scanner = HaScanner(BluetoothScanningMode.PASSIVE, "hci1", "AA:BB:CC:DD:EE:FF")
    assert uart_scanner.details.scanner_type is HaScannerType.UART
    assert uart_scanner.details.adapter == "hci1"

    # Test with macOS/Windows adapter (no adapter_type)
    macos_adapter_data = {
        "Core Bluetooth": {
            "address": "00:00:00:00:00:00",
            "passive_scan": False,
            "sw_version": "18.7.0",
            "manufacturer": "Apple",
            "product": "Unknown MacOS Model",
            "vendor_id": "Unknown",
            "product_id": "Unknown",
            "adapter_type": None,
        }
    }

    manager._adapters = macos_adapter_data

    # Create scanner with unknown adapter type
    macos_scanner = HaScanner(
        BluetoothScanningMode.ACTIVE, "Core Bluetooth", "00:00:00:00:00:00"
    )
    assert macos_scanner.details.scanner_type is HaScannerType.UNKNOWN


@pytest.mark.asyncio
async def test_scanner_type_updates_after_adapter_refresh() -> None:
    """Test scanner type is UNKNOWN initially, determined after adapters load."""
    # Create a custom manager for this test
    manager = BluetoothManager(bluetooth_adapters=MagicMock())
    set_manager(manager)

    # Initially no adapters cached
    manager._adapters = None  # type: ignore[assignment]

    # Create scanner - should be UNKNOWN
    scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "00:1A:7D:DA:71:04")
    assert scanner.details.scanner_type is HaScannerType.UNKNOWN

    # Now simulate adapter data becoming available
    manager._adapters = {
        "hci0": {
            "address": "00:1A:7D:DA:71:04",
            "adapter_type": "usb",
            "manufacturer": "TestManufacturer",
        }
    }

    # Create a new scanner with the same adapter - should now be USB
    scanner2 = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "00:1A:7D:DA:71:04")
    assert scanner2.details.scanner_type is HaScannerType.USB

    # Note: The first scanner still has UNKNOWN since scanner_type is set at init
    assert scanner.details.scanner_type is HaScannerType.UNKNOWN


def test_multiple_scanner_types_simultaneously() -> None:
    """Test that multiple scanners can have different types at the same time."""
    manager = get_manager()

    # Set up adapters with different types
    mock_adapters = {
        "hci0": {
            "address": "00:1A:7D:DA:71:04",
            "adapter_type": "usb",
        },
        "hci1": {
            "address": "AA:BB:CC:DD:EE:FF",
            "adapter_type": "uart",
        },
        "hci2": {
            "address": "11:22:33:44:55:66",
            "adapter_type": None,
        },
    }

    with patch.object(manager, "_adapters", mock_adapters):
        # Create scanners of different types
        usb_scanner = HaScanner(
            BluetoothScanningMode.ACTIVE, "hci0", "00:1A:7D:DA:71:04"
        )
        uart_scanner = HaScanner(
            BluetoothScanningMode.ACTIVE, "hci1", "AA:BB:CC:DD:EE:FF"
        )
        unknown_scanner = HaScanner(
            BluetoothScanningMode.ACTIVE, "hci2", "11:22:33:44:55:66"
        )

        # Verify each has the correct type
        assert usb_scanner.details.scanner_type is HaScannerType.USB
        assert uart_scanner.details.scanner_type is HaScannerType.UART
        assert unknown_scanner.details.scanner_type is HaScannerType.UNKNOWN

        # Verify they all have different types
        types = {
            usb_scanner.details.scanner_type,
            uart_scanner.details.scanner_type,
            unknown_scanner.details.scanner_type,
        }
        assert len(types) == 3  # All different


def test_ha_scanner_get_allocations_no_slot_manager() -> None:
    """Test HaScanner.get_allocations returns None when manager has no slot_manager."""
    scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
    manager = get_manager()

    # Mock slot_manager as None
    with patch.object(manager, "slot_manager", None):
        assert scanner.get_allocations() is None


def test_ha_scanner_get_allocations_with_slot_manager() -> None:
    """Test HaScanner.get_allocations returns allocation info from BleakSlotManager."""
    from bleak_retry_connector import Allocations

    scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
    manager = get_manager()

    # Create mock allocations
    mock_allocations = Allocations(
        adapter="hci0",
        slots=5,
        free=3,
        allocated=["11:22:33:44:55:66", "AA:BB:CC:DD:EE:FF"],
    )

    # Mock slot_manager
    mock_slot_manager = Mock(spec=BleakSlotManager)
    mock_slot_manager.get_allocations.return_value = mock_allocations

    with patch.object(manager, "slot_manager", mock_slot_manager):
        allocations = scanner.get_allocations()

        assert allocations is not None
        assert allocations == mock_allocations
        mock_slot_manager.get_allocations.assert_called_once_with("hci0")


def test_ha_scanner_get_allocations_updates_dynamically() -> None:
    """Test that HaScanner.get_allocations returns current values as they change."""
    from bleak_retry_connector import Allocations

    scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
    manager = get_manager()

    # Mock slot_manager
    mock_slot_manager = Mock(spec=BleakSlotManager)

    # Initial state - 3 free slots
    mock_slot_manager.get_allocations.return_value = Allocations(
        adapter="hci0", slots=3, free=3, allocated=[]
    )

    with patch.object(manager, "slot_manager", mock_slot_manager):
        # Check initial state
        allocations = scanner.get_allocations()
        assert allocations is not None
        assert allocations.free == 3
        assert allocations.allocated == []

        # Update mock to simulate connection made
        mock_slot_manager.get_allocations.return_value = Allocations(
            adapter="hci0", slots=3, free=2, allocated=["11:22:33:44:55:66"]
        )

        # Check updated state
        allocations = scanner.get_allocations()
        assert allocations is not None
        assert allocations.free == 2
        assert allocations.allocated == ["11:22:33:44:55:66"]

        # Update mock to simulate another connection
        mock_slot_manager.get_allocations.return_value = Allocations(
            adapter="hci0",
            slots=3,
            free=1,
            allocated=["11:22:33:44:55:66", "AA:BB:CC:DD:EE:FF"],
        )

        # Check final state
        allocations = scanner.get_allocations()
        assert allocations is not None
        assert allocations.free == 1
        assert len(allocations.allocated) == 2


@pytest.mark.asyncio
async def test_on_scanner_start_callback(
    async_mock_manager_with_scanner_callbacks: MockBluetoothManagerWithCallbacks,
) -> None:
    """Test that on_scanner_start is called when a local scanner starts."""
    manager = async_mock_manager_with_scanner_callbacks

    # Create a local scanner (it will get the manager from get_manager())
    scanner = HaScanner(
        mode=BluetoothScanningMode.ACTIVE,
        adapter="hci0",
        address="00:00:00:00:00:00",
    )

    # Register scanner with manager
    manager.async_register_scanner(scanner)

    # Setup the scanner
    scanner.async_setup()

    # Directly call _on_start_success to test the callback
    # (In real usage, this is called by HaScanner._async_start_attempt
    # after successful start)
    scanner._on_start_success()

    # Verify the callback was called
    assert len(manager.scanner_start_calls) == 1
    assert manager.scanner_start_calls[0] is scanner


@pytest.mark.asyncio
async def test_async_request_active_window_rejected_when_not_auto() -> None:
    """Non-AUTO scanners ignore active-window requests and return False."""
    scanner = HaScanner(BluetoothScanningMode.PASSIVE, "hci0", "AA:BB:CC:DD:EE:FF")
    scanner.async_setup()
    assert await scanner.async_request_active_window(1.0) is False
    assert scanner._scan_mode_override is None


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [float("nan"), float("inf"), -1.0, 0.0])
async def test_async_request_active_window_rejects_invalid_duration(
    duration: float,
) -> None:
    """
    NaN/inf/non-positive durations are refused at the entry point.

    A bad duration would poison ``loop.call_later`` (which raises on
    NaN) and the extension comparison (NaN ordering is always False,
    inf would lock the window open). Guard the public entry so a
    misbehaving subclass / direct caller can't corrupt the scheduler
    state.
    """
    scanner = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
    scanner.async_setup()
    assert await scanner.async_request_active_window(duration) is False
    assert scanner._scan_mode_override is None
    assert scanner._active_window_handle is None


@pytest.mark.usefixtures("force_linux_scanner_mode")
@pytest.mark.asyncio
async def test_async_request_active_window_restarts_scanner_in_active_mode() -> None:
    """An AUTO scanner flips to ACTIVE and schedules a return to the prior mode."""
    starts: list[str] = []

    def _factory(*_args, **kwargs):
        starts.append(kwargs["scanning_mode"])
        return MockBleakScanner()

    with patch("habluetooth.scanner.OriginalBleakScanner", side_effect=_factory):
        scanner = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        # Initial construction: AUTO maps to passive in bleak's
        # scanning_mode. The active-window toggle path reuses
        # this single BleakScanner instance and just mutates
        # _backend._scanning_mode instead of constructing again.
        assert starts == ["passive"]
        backend = scanner.scanner._backend  # type: ignore[union-attr]
        backend._scanning_mode = "passive"

        # Tiny duration so call_later fires on the next loop turn.
        # async_request_active_window rejects 0/NaN/inf at the boundary,
        # so we use the smallest positive value that round-trips through
        # the timer arithmetic.
        assert await scanner.async_request_active_window(1e-9) is True
        # The toggle flipped the existing instance to active.
        assert backend._scanning_mode == "active"
        assert scanner._scan_mode_override is BluetoothScanningMode.ACTIVE
        assert scanner._active_window_handle is not None

        # Let the call_later fire and the background restart task complete.
        for _ in range(6):
            await asyncio.sleep(0)
        # End-of-window toggled the same instance back to passive.
        assert backend._scanning_mode == "passive"
        assert scanner._scan_mode_override is None
        assert scanner._active_window_handle is None  # type: ignore[unreachable]

        await scanner.async_stop()


@pytest.mark.asyncio
async def test_active_window_restart_does_not_log_fallback_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A successful active-window restart on an AUTO scanner must not warn.

    Regression: the start-success log compared current_mode against
    requested_mode. For an AUTO scanner mid-active-window,
    requested_mode is AUTO but current_mode is ACTIVE (because the
    restart was triggered by the scheduler with
    _scan_mode_override=ACTIVE), so the previous code logged a
    spurious "fell back to passive" warning on every active-window
    restart. The check now uses effective_mode (the mode we tried to
    start in) so it only triggers on a real fallback.
    """
    with patch(
        "habluetooth.scanner.OriginalBleakScanner",
        side_effect=lambda *_a, **_kw: MockBleakScanner(),
    ):
        scanner = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert await scanner.async_request_active_window(10.0) is True
        assert not any(
            "fall-back to passive" in record.message for record in caplog.records
        )
        await scanner.async_stop()


@pytest.mark.usefixtures("force_linux_scanner_mode")
@pytest.mark.asyncio
async def test_async_toggle_active_window_mode_returns_false_when_no_scanner() -> None:
    """The toggle helper bails when the scanner instance is gone."""
    scanner_obj = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
    scanner_obj.async_setup()
    assert scanner_obj.scanner is None
    assert await scanner_obj._async_toggle_active_window_mode() is False


@pytest.mark.usefixtures("force_linux_scanner_mode")
@pytest.mark.asyncio
async def test_async_toggle_active_window_mode_returns_false_on_stop_error() -> None:
    """The toggle helper logs and bails when scanner.stop() raises."""

    class StopErrorMockBleakScanner(MockBleakScanner):
        async def stop(self) -> None:
            msg = "simulated stop failure"
            raise BleakError(msg)

    with patch_bleak_scanner_factory(StopErrorMockBleakScanner):
        scanner_obj = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner_obj.async_setup()
        await scanner_obj.async_start()
        scanner_obj._scan_mode_override = BluetoothScanningMode.ACTIVE
        assert scanner_obj.scanning is True
        assert await scanner_obj._async_toggle_active_window_mode() is False
        # scanner.stop() raised so the bleak scanner is in an
        # undefined state; the wrapper must reflect that as not-
        # scanning so the caller's fallback path treats it correctly.
        assert scanner_obj.scanning is False


@pytest.mark.usefixtures("force_linux_scanner_mode")
@pytest.mark.asyncio
async def test_async_toggle_active_window_mode_marks_not_scanning_on_start_error() -> (
    None
):
    """
    Toggle's start-error path also clears self.scanning.

    The stop succeeded but the post-mode-flip start raised, so the
    bleak scanner is stopped. self.scanning must follow.
    """
    starts = 0

    class StartErrorMockBleakScanner(MockBleakScanner):
        async def start(self) -> None:
            nonlocal starts
            starts += 1
            # First start (initial async_start) succeeds; the
            # post-flip start (second call) raises.
            if starts > 1:
                msg = "simulated start failure"
                raise BleakError(msg)

    with patch_bleak_scanner_factory(StartErrorMockBleakScanner):
        scanner_obj = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner_obj.async_setup()
        await scanner_obj.async_start()
        scanner_obj._scan_mode_override = BluetoothScanningMode.ACTIVE
        assert scanner_obj.scanning is True
        assert await scanner_obj._async_toggle_active_window_mode() is False
        assert scanner_obj.scanning is False


@pytest.mark.usefixtures("force_linux_scanner_mode")
@pytest.mark.asyncio
async def test_async_toggle_active_window_mode_attribute_error_marks_not_scanning() -> (
    None
):
    """
    Toggle gracefully handles bleak refactoring away ``_scanning_mode``.

    If a future bleak version drops or renames ``_backend._scanning_mode``,
    the mutation raises AttributeError. The stop has already completed,
    so without a guard the scanner would be left stopped and the caller
    would have no signal to fall back to the full path. The guard logs,
    clears ``self.scanning``, and returns False so the caller can
    recover via the full restart path.
    """

    class MockBackend:
        @property
        def _scanning_mode(self) -> str:
            msg = "simulated bleak refactor — attribute removed"
            raise AttributeError(msg)

        @_scanning_mode.setter
        def _scanning_mode(self, value: str) -> None:
            msg = "simulated bleak refactor — attribute removed"
            raise AttributeError(msg)

    class AttrErrorMockBleakScanner(MockBleakScanner):
        def __init__(self) -> None:
            self._backend = MockBackend()

    with patch_bleak_scanner_factory(AttrErrorMockBleakScanner):
        scanner_obj = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner_obj.async_setup()
        await scanner_obj.async_start()
        scanner_obj._scan_mode_override = BluetoothScanningMode.ACTIVE
        assert scanner_obj.scanning is True
        assert await scanner_obj._async_toggle_active_window_mode() is False
        assert scanner_obj.scanning is False


@pytest.mark.usefixtures("force_linux_scanner_mode")
@pytest.mark.asyncio
async def test_arm_active_window_timer_cancels_existing_handle() -> None:
    """
    _arm_active_window_timer cancels any prior handle before arming.

    Regression for the concurrent-callers race noted in PR review:
    two concurrent ``async_request_active_window`` calls could both
    reach _arm_active_window_timer without the second cancelling the
    first's TimerHandle, leaking a pending timer that would later fire
    an extra _async_end_active_window. Today only the scheduler drives
    the public method (and _tick serializes per worker) so the race
    isn't reachable through normal callers, but the contract on
    _arm_active_window_timer must defend against it.
    """
    with patch(
        "habluetooth.scanner.OriginalBleakScanner",
        side_effect=lambda *_a, **_kw: MockBleakScanner(),
    ):
        scanner = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()

        # Arm a window so there's a handle to potentially leak.
        assert await scanner.async_request_active_window(100.0) is True
        first_handle = scanner._active_window_handle
        assert first_handle is not None
        # Directly call _arm again (simulating the race-path second
        # caller). The first handle must be cancelled, not leaked.
        scanner._arm_active_window_timer(50.0)
        assert first_handle.cancelled()
        assert scanner._active_window_handle is not first_handle

        await scanner.async_stop()


@pytest.mark.usefixtures("force_linux_scanner_mode")
@pytest.mark.asyncio
async def test_async_request_active_window_extends_existing_window() -> None:
    """A second request inside an active window extends the timer in place."""
    starts: list[str] = []

    def _factory(*_args, **kwargs):
        starts.append(kwargs["scanning_mode"])
        return MockBleakScanner()

    with patch("habluetooth.scanner.OriginalBleakScanner", side_effect=_factory):
        scanner = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()

        assert await scanner.async_request_active_window(100.0) is True
        first_handle = scanner._active_window_handle
        first_end = scanner._active_window_end
        # A longer request extends the existing window without a second restart.
        assert await scanner.async_request_active_window(200.0) is True
        assert scanner._active_window_handle is not first_handle
        assert scanner._active_window_end > first_end
        # Only one BleakScanner construction happened (the initial
        # passive one). The active-window flip toggles the existing
        # instance's _backend._scanning_mode instead of creating a
        # new scanner.
        assert starts == ["passive"]
        assert scanner.current_mode is BluetoothScanningMode.ACTIVE
        # A shorter follow-up is a no-op on the timer.
        kept_end = scanner._active_window_end
        assert await scanner.async_request_active_window(0.001) is True
        assert scanner._active_window_end == kept_end

        await scanner.async_stop()


@pytest.mark.usefixtures("force_linux_scanner_mode")
@pytest.mark.asyncio
async def test_async_request_active_window_end_time_matches_real_timer() -> None:
    """
    _active_window_end reflects the post-restart loop.time() + duration.

    Regression: a slow stop/restart cycle previously left
    ``_active_window_end`` set to ``loop.time() + duration`` captured
    *before* the restart, so it lagged the real ``call_later`` fire
    time by the restart duration. The fix moved the
    ``_active_window_end`` computation inside
    ``_arm_active_window_timer`` so it always matches when the timer
    will actually fire.

    Uses an asyncio.Event to gate the restart-in-progress
    deterministically rather than relying on asyncio.sleep precision,
    which can fire slightly early on busy CI runners.
    """
    duration = 10.0
    restart_started = asyncio.Event()
    gate = asyncio.Event()

    class GatedMockBleakScanner(MockBleakScanner):
        _first_start_done = False

        async def start(self) -> None:
            if not type(self)._first_start_done:
                type(self)._first_start_done = True
                return
            restart_started.set()
            await gate.wait()

    with patch_bleak_scanner_factory(GatedMockBleakScanner):
        scanner = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()

        loop = asyncio.get_running_loop()
        before = loop.time()
        task = asyncio.create_task(scanner.async_request_active_window(duration))
        await restart_started.wait()
        # Provably advance loop.time() past `before` before the restart
        # completes; the exact amount doesn't matter for the assertion
        # below as long as loop.time() has visibly moved.
        await asyncio.sleep(0.05)
        elapsed = loop.time() - before
        gate.set()
        assert await task is True

        # Contract: _active_window_end matches loop.time() + duration
        # measured AFTER the restart, not before. Pre-fix it would be
        # before + duration. Allow generous tolerance for the small
        # gap between arming and reading.
        now = loop.time()
        assert scanner._active_window_end == pytest.approx(now + duration, abs=0.1)
        # Reject pre-fix value (before + duration) explicitly with a
        # margin well above asyncio scheduling jitter: the stored end
        # is at least ``elapsed`` ahead of before + duration.
        assert scanner._active_window_end - before - duration >= elapsed / 2
        first_handle = scanner._active_window_handle

        # A follow-up whose new_end lands between the pre-fix stored
        # end and the real fire time must NOT be treated as an
        # extension. With the fix this is rejected; without it the
        # live timer would be cancelled and armed shorter.
        target_new_end = before + duration + elapsed / 2
        shorter_duration = target_new_end - loop.time()
        assert await scanner.async_request_active_window(shorter_duration) is True
        assert scanner._active_window_handle is first_handle

        await scanner.async_stop()


@pytest.mark.usefixtures("force_linux_scanner_mode")
@pytest.mark.asyncio
async def test_async_request_active_window_skips_restart_if_still_active() -> None:
    """
    Re-arm the timer instead of restarting if the scanner is still ACTIVE.

    A new request arriving after the end-of-window timer fires but
    before the bg task runs reuses the in-flight ACTIVE mode and just
    arms a new timer.
    """
    starts: list[str] = []

    def _factory(*_args, **kwargs):
        starts.append(kwargs["scanning_mode"])
        return MockBleakScanner()

    with patch("habluetooth.scanner.OriginalBleakScanner", side_effect=_factory):
        scanner = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        # Single construction (passive); toggle reuses the instance.
        assert starts == ["passive"]
        backend = scanner.scanner._backend  # type: ignore[union-attr]
        backend._scanning_mode = "passive"

        assert await scanner.async_request_active_window(100.0) is True
        # Toggle flipped the existing instance to active.
        assert backend._scanning_mode == "active"
        # Simulate the timer firing but the end-window task not having
        # run yet: clear the handle (like _schedule_end_active_window
        # does) but leave _scan_mode_override / current_mode == ACTIVE.
        handle = scanner._active_window_handle
        assert handle is not None
        handle.cancel()
        scanner._active_window_handle = None

        # Scanner is still ACTIVE; a longer follow-up re-arms the
        # timer without flipping the radio again. A shorter follow-up
        # would no-op the timer (covered by
        # test_async_request_active_window_still_active_does_not_shrink).
        assert await scanner.async_request_active_window(200.0) is True
        assert scanner._active_window_handle is not None
        # Mode unchanged: no toggle happened on the still-ACTIVE path.
        assert backend._scanning_mode == "active"  # type: ignore[unreachable]
        # Still only one BleakScanner construction.
        assert starts == ["passive"]

        await scanner.async_stop()


@pytest.mark.usefixtures("force_linux_scanner_mode")
@pytest.mark.asyncio
async def test_async_request_active_window_still_active_does_not_shrink() -> None:
    """
    Concurrent shorter caller into the still-ACTIVE locked branch is a no-op.

    Regression: the locked early-return at the top of
    ``async_request_active_window``'s lock block re-armed the timer
    unconditionally when ``current_mode is ACTIVE``. A second caller
    with a shorter duration could shrink an in-flight window someone
    else asked for. Guarded with the same
    ``loop.time() + duration > _active_window_end`` check the
    lockless fast-path uses.
    """
    with patch(
        "habluetooth.scanner.OriginalBleakScanner",
        side_effect=lambda *_, **__: MockBleakScanner(),
    ):
        scanner = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        # Open a long window so _active_window_handle is set and
        # current_mode is ACTIVE.
        assert await scanner.async_request_active_window(100.0) is True
        long_end = scanner._active_window_end
        long_handle = scanner._active_window_handle
        # Simulate the timer firing without _async_end_active_window
        # running yet: clear the handle so the locked branch is
        # reachable (lockless fast path needs handle is not None).
        assert long_handle is not None
        long_handle.cancel()
        scanner._active_window_handle = None
        # Concurrent shorter caller now hits the locked
        # current_mode-is-ACTIVE branch. Pre-fix this would re-arm
        # at end = now + 5 (shrinking the live window); post-fix the
        # stored end-time stays put and the timer isn't re-armed.
        assert await scanner.async_request_active_window(5.0) is True
        assert scanner._active_window_end == long_end

        await scanner.async_stop()


@pytest.mark.usefixtures("force_linux_scanner_mode")
@pytest.mark.asyncio
async def test_async_stop_clears_active_window_state() -> None:
    """Stopping mid-window cancels the timer and clears the override."""
    with patch(
        "habluetooth.scanner.OriginalBleakScanner",
        side_effect=lambda *_, **__: MockBleakScanner(),
    ):
        scanner = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        await scanner.async_request_active_window(100.0)
        assert scanner._active_window_handle is not None
        await scanner.async_stop()
        assert scanner._active_window_handle is None
        assert scanner._scan_mode_override is None  # type: ignore[unreachable]
        assert scanner._active_window_end == 0.0


@pytest.mark.usefixtures("force_linux_scanner_mode")
@pytest.mark.asyncio
async def test_async_request_active_window_recovers_on_start_failure() -> None:
    """If the ACTIVE restart raises, recovery brings the scanner back up."""
    call_count = 0
    fail_until = 0

    class _CountingFailScanner(MockBleakScanner):
        async def start(self) -> None:
            nonlocal call_count
            call_count += 1
            if call_count <= fail_until:
                msg = "simulated start failure"
                raise BleakError(msg)

    with patch_bleak_scanner_factory(_CountingFailScanner):
        scanner = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        before = call_count
        # Fail the next 4 start attempts so the ACTIVE swap raises;
        # then succeed so the recovery restart can come back up.
        fail_until = call_count + 4
        result = await scanner.async_request_active_window(1.0)
        assert result is False
        assert scanner._scan_mode_override is None
        # Recovery restart happened after the failure path.
        assert call_count > before + 4
        await scanner.async_stop()


@pytest.mark.usefixtures("force_linux_scanner_mode")
@pytest.mark.asyncio
async def test_async_request_active_window_clears_override_on_unexpected_error() -> (
    None
):
    """
    An unexpected exception from the restart clears _scan_mode_override.

    Regression: only ScannerStartError was caught explicitly, so any
    other exception propagating from _async_stop_then_start_under_lock
    would leave _scan_mode_override = ACTIVE. The next
    _async_start_attempt would then see effective_mode = ACTIVE
    instead of AUTO, poisoning subsequent starts.
    """
    start_count = 0

    class _UnexpectedErrorAfterFirstScanner(MockBleakScanner):
        async def start(self) -> None:
            nonlocal start_count
            start_count += 1
            # First start (initial async_start) succeeds; second start
            # (the ACTIVE restart from async_request_active_window)
            # raises a non-ScannerStartError so we exercise the
            # broad-except cleanup path.
            if start_count > 1:
                msg = "simulated unexpected error"
                raise RuntimeError(msg)

    with patch_bleak_scanner_factory(_UnexpectedErrorAfterFirstScanner):
        scanner = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        with pytest.raises(RuntimeError, match="simulated unexpected error"):
            await scanner.async_request_active_window(1.0)
        # The override must be cleared even though the exception
        # wasn't a ScannerStartError.
        assert scanner._scan_mode_override is None
        await scanner.async_stop()


@pytest.mark.asyncio
async def test_base_scanner_default_active_window_is_noop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BaseHaScanner.async_request_active_window default returns False."""
    from collections.abc import Iterable

    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData

    from habluetooth import BaseHaScanner

    class _PlainScanner(BaseHaScanner):
        @property
        def discovered_devices(self) -> list[BLEDevice]:
            return []

        @property
        def discovered_devices_and_advertisement_data(
            self,
        ) -> dict[str, tuple[BLEDevice, AdvertisementData]]:
            return {}

        def get_discovered_device_advertisement_data(
            self, address: str
        ) -> tuple[BLEDevice, AdvertisementData] | None:
            return None

        @property
        def discovered_addresses(self) -> Iterable[str]:
            return ()

    scanner = _PlainScanner("AA:BB:CC:DD:EE:FF", "plain")
    with caplog.at_level(logging.DEBUG, logger="habluetooth"):
        result = await scanner.async_request_active_window(1.0)
    assert result is False
    assert any(
        "does not support on-demand active windows" in record.message
        for record in caplog.records
    )


@pytest.mark.usefixtures("force_linux_scanner_mode")
@pytest.mark.asyncio
async def test_async_end_active_window_defers_to_new_window() -> None:
    """If a new window armed the timer, the end-window task returns early."""
    with patch(
        "habluetooth.scanner.OriginalBleakScanner",
        side_effect=lambda *_, **__: MockBleakScanner(),
    ):
        scanner = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        await scanner.async_request_active_window(3600.0)
        # Simulate a new window taking over by leaving the handle in place
        # and call _async_end_active_window directly; it must short-circuit.
        assert scanner._active_window_handle is not None
        await scanner._async_end_active_window()
        # Override and handle untouched because we deferred to the new window.
        assert scanner._scan_mode_override == BluetoothScanningMode.ACTIVE
        assert scanner._active_window_handle is not None
        await scanner.async_stop()


@pytest.mark.asyncio
async def test_async_end_active_window_skips_when_not_scanning() -> None:
    """If the scanner was stopped during the window the restart is skipped."""
    with patch(
        "habluetooth.scanner.OriginalBleakScanner",
        side_effect=lambda *_, **__: MockBleakScanner(),
    ):
        scanner = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        await scanner.async_request_active_window(3600.0)
        # Pretend the end-window timer just fired (handle cleared) and the
        # scanner was stopped in the meantime.
        scanner._active_window_handle = None
        scanner.scanning = False
        # Should be a quick no-op: clears override, sees not scanning, returns.
        await scanner._async_end_active_window()
        assert scanner._scan_mode_override is None
        scanner.scanning = True
        await scanner.async_stop()


@pytest.mark.usefixtures("force_linux_scanner_mode")
@pytest.mark.asyncio
async def test_async_request_active_window_passive_fallback_on_linux() -> None:
    """If the swap restart falls back to PASSIVE on Linux, request returns False."""
    starts = 0

    class _PassiveFallbackScanner(MockBleakScanner):
        async def start(self) -> None:
            nonlocal starts
            starts += 1
            # Fail the first three attempts so the 4th-attempt PASSIVE
            # fallback inside _async_start_attempt kicks in.
            if 2 <= starts <= 4:
                msg = "simulated active failure"
                raise BleakError(msg)

    with (
        patch("habluetooth.scanner.IS_LINUX", True),
        patch_bleak_scanner_factory(_PassiveFallbackScanner),
        patch("habluetooth.scanner.async_reset_adapter", AsyncMock()),
        patch("habluetooth.scanner.ADAPTER_INIT_TIME", 0),
    ):
        scanner = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        result = await scanner.async_request_active_window(1.0)
        # The swap ran through to the 4th attempt and fell back to PASSIVE;
        # the request reports False because the scanner is not ACTIVE.
        assert result is False
        assert scanner._scan_mode_override is None
        await scanner.async_stop()


@pytest.mark.usefixtures("force_linux_scanner_mode")
@pytest.mark.asyncio
async def test_async_end_active_window_handles_start_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ScannerStartError during the end-of-window restart logs a warning."""
    starts = 0
    fail_until = 0

    class _FailUntilThresholdScanner(MockBleakScanner):
        async def start(self) -> None:
            nonlocal starts
            starts += 1
            if starts <= fail_until:
                msg = "simulated end-window failure"
                raise BleakError(msg)

    with patch_bleak_scanner_factory(_FailUntilThresholdScanner):
        scanner = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        try:
            # Open a long active window then drive end-of-window
            # with the bleak start mocked to fail.
            await scanner.async_request_active_window(3600.0)
            assert scanner._active_window_handle is not None
            # Fail enough start() calls that BOTH the toggle attempt
            # and every retry in the fallback _async_start cycle
            # raise, so we exercise the "Failed to restart scanner
            # after active window" warning.
            fail_until = starts + 100
            scanner._active_window_handle.cancel()
            scanner._active_window_handle = None
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                await scanner._async_end_active_window()
            assert any(
                "Failed to restart scanner after active window" in record.message
                for record in caplog.records
            )
        finally:
            # Allow the fallback restart to succeed for teardown,
            # then stop the scanner so we don't leak the watchdog
            # timer / background tasks into later tests.
            fail_until = 0
            await scanner.async_stop()


@pytest.mark.parametrize("exc", [FileNotFoundError("no dbus"), BleakError("nope")])
def test_create_bleak_scanner_wraps_init_error(exc: Exception) -> None:
    """``create_bleak_scanner`` wraps FileNotFoundError/BleakError as RuntimeError."""
    with (
        patch.object(scanner, "IS_LINUX", True),
        patch.object(scanner, "IS_MACOS", False),
        patch(
            "habluetooth.scanner.OriginalBleakScanner",
            side_effect=exc,
        ),
        pytest.raises(RuntimeError, match="Failed to initialize Bluetooth"),
    ):
        create_bleak_scanner(None, BluetoothScanningMode.ACTIVE, "hci0")


@pytest.mark.asyncio
@pytest.mark.skipif(NOT_POSIX)
@pytest.mark.parametrize("exc", [TimeoutError("slow"), BleakError("nope")])
async def test_async_stop_scanner_logs_when_scanner_stop_raises(
    caplog: pytest.LogCaptureFixture, exc: Exception
) -> None:
    """``_async_stop_scanner`` logs and clears the scanner when ``.stop()`` raises."""
    mock_scanner = MagicMock()
    mock_scanner.start = AsyncMock()
    mock_scanner.stop = AsyncMock(side_effect=exc)
    with patch("habluetooth.scanner.OriginalBleakScanner", return_value=mock_scanner):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        caplog.clear()
        await scanner.async_stop()
    assert "Error stopping scanner" in caplog.text
    assert scanner.scanner is None


@pytest.mark.asyncio
@pytest.mark.skipif(NOT_POSIX)
async def test_async_force_stop_discovery_logs_on_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Force-stop logs an error when ``stop_discovery`` times out."""
    ha_scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
    ha_scanner.async_setup()
    with patch("habluetooth.scanner.stop_discovery", side_effect=TimeoutError("slow")):
        await ha_scanner._async_force_stop_discovery()
    assert "Timeout force stopping scanner" in caplog.text
    await ha_scanner.async_stop()


@pytest.mark.asyncio
@pytest.mark.skipif(NOT_POSIX)
async def test_async_force_stop_discovery_logs_on_unexpected_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Force-stop logs an error when ``stop_discovery`` raises an unexpected error."""
    ha_scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
    ha_scanner.async_setup()
    with patch("habluetooth.scanner.stop_discovery", side_effect=BleakError("boom")):
        await ha_scanner._async_force_stop_discovery()
    assert "Failed to force stop scanner" in caplog.text
    await ha_scanner.async_stop()


@pytest.mark.asyncio
async def test_get_allocations_returns_none_without_slot_manager() -> None:
    """``HaScanner.get_allocations`` returns None when manager has no slot manager."""
    ha_scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
    ha_scanner.async_setup()
    with patch.object(get_manager(), "slot_manager", None):
        assert ha_scanner.get_allocations() is None
    await ha_scanner.async_stop()


@pytest.mark.asyncio
async def test_discovered_properties_delegate_when_scanner_attached() -> None:
    """Discovered* delegate to the underlying bleak scanner when one is attached."""
    device = generate_ble_device("AA:BB:CC:DD:EE:01", "x")
    adv = generate_advertisement_data(local_name="x")

    class _DiscoveredScanner(MockBleakScanner):
        @property
        def discovered_devices(self):
            return [device]

        @property
        def discovered_devices_and_advertisement_data(self):
            return {device.address: (device, adv)}

    with patch_bleak_scanner_factory(_DiscoveredScanner):
        ha_scanner = HaScanner(
            BluetoothScanningMode.PASSIVE, "hci0", "AA:BB:CC:DD:EE:FF"
        )
        ha_scanner.async_setup()
        await ha_scanner.async_start()
        try:
            assert ha_scanner.discovered_devices == [device]
            assert ha_scanner.discovered_devices_and_advertisement_data == {
                device.address: (device, adv)
            }
            assert ha_scanner.get_discovered_device_advertisement_data(
                device.address
            ) == (device, adv)
            assert device.address in list(ha_scanner.discovered_addresses)
        finally:
            await ha_scanner.async_stop()


@pytest.mark.asyncio
async def test_detection_callback_coerces_non_str_name_and_non_int_tx_power() -> None:
    """
    Defensive coercion: bleak occasionally returns non-str names / non-int tx_power.

    The advertisement path normalizes both so downstream code can rely
    on plain Python str / int rather than bytes-likes or numpy ints.
    Inspects ``manager._all_history`` (populated by
    ``_scanner_adv_received``) since the cython method itself isn't
    monkey-patchable.
    """
    ha_scanner = HaScanner(BluetoothScanningMode.PASSIVE, "hci0", "AA:BB:CC:DD:EE:FF")
    ha_scanner.async_setup()

    class _StrSubclass(str):
        """str subclass — `type() is not str` so coercion fires."""

        __slots__ = ()

    class _IntLike:
        """Non-int that ``int()`` accepts (e.g. numpy.int64 stand-in)."""

        def __int__(self) -> int:
            return -7

    address = "AA:BB:CC:DD:EE:F0"
    device = generate_ble_device(address, None)
    adv = generate_advertisement_data(
        local_name=_StrSubclass("weird"),
        tx_power=_IntLike(),
    )
    ha_scanner._async_detection_callback(device, adv)
    info = get_manager()._all_history[address]
    # Both fields were coerced to the canonical Python type.
    assert type(info.name) is str
    assert info.name == "weird"
    assert type(info.tx_power) is int
    assert info.tx_power == -7
    await ha_scanner.async_stop()


@pytest.mark.asyncio
async def test_start_attempt_timeout_resets_then_raises_on_exhaustion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Persistent start TimeoutError: attempt 2 resets the adapter, attempt 4 raises.

    Covers the in-between `attempt < START_ATTEMPTS` return-False branch
    (logged as a retry warning) and the `attempt == START_ATTEMPTS` raise.
    Subclasses HaScanner because the cython type is immutable so a
    bare patch.object can't override its methods.
    """
    reset_calls: list[bool] = []

    class _RecordingResetScanner(HaScanner):
        async def _async_reset_adapter(self, gone_silent: bool) -> None:
            reset_calls.append(gone_silent)

    class TimeoutMockBleakScanner(MockBleakScanner):
        async def start(self) -> None:
            msg = "simulated start timeout"
            raise TimeoutError(msg)

    with patch_bleak_scanner_factory(TimeoutMockBleakScanner):
        ha_scanner = _RecordingResetScanner(
            BluetoothScanningMode.PASSIVE, "hci0", "AA:BB:CC:DD:EE:FF"
        )
        ha_scanner.async_setup()
        with (
            caplog.at_level(logging.DEBUG, logger="habluetooth.scanner"),
            pytest.raises(ScannerStartError, match="Timed out starting Bluetooth"),
        ):
            await ha_scanner.async_start()
    # Attempt 2 (gone_silent=False) triggered exactly one adapter reset.
    assert reset_calls == [False]
    await ha_scanner.async_stop()


@pytest.mark.asyncio
async def test_async_restart_scanner_logs_when_start_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Watchdog restart swallows + logs ``ScannerStartError`` from ``_async_start``.

    Covers the ``except ScannerStartError`` branch in
    ``_async_restart_scanner`` so a single restart failure doesn't
    propagate out of the background task. Uses a subclass to override
    methods because cython types are immutable.
    """

    class _RaisingRestartScanner(HaScanner):
        async def _async_start(self) -> None:
            msg = "simulated restart failure"
            raise ScannerStartError(msg)

        async def _async_stop_scanner(self) -> None:
            pass

        async def _async_reset_adapter(self, gone_silent: bool) -> None:
            pass

    ha_scanner = _RaisingRestartScanner(
        BluetoothScanningMode.PASSIVE, "hci0", "AA:BB:CC:DD:EE:FF"
    )
    ha_scanner.async_setup()
    with caplog.at_level(logging.ERROR, logger="habluetooth.scanner"):
        await ha_scanner._async_restart_scanner()
    assert "Failed to restart Bluetooth scanner" in caplog.text
    assert "simulated restart failure" in caplog.text


@pytest.fixture
def force_non_linux_non_macos_scanner_mode() -> Generator[None, None, None]:
    """Force the non-Linux, non-macOS branch of the active-window entry."""
    with (
        patch("habluetooth.scanner.IS_LINUX", False),
        patch("habluetooth.scanner.IS_MACOS", False),
    ):
        yield


@pytest.mark.usefixtures("force_non_linux_non_macos_scanner_mode")
@pytest.mark.asyncio
async def test_async_request_active_window_restart_path_happy() -> None:
    """Non-Linux active-window entry: full stop+restart leaves scanner in ACTIVE."""
    with patch_bleak_scanner_factory(MockBleakScanner):
        ha_scanner = HaScanner(BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:F1")
        ha_scanner.async_setup()
        await ha_scanner.async_start()
        try:
            assert await ha_scanner.async_request_active_window(0.5) is True
            assert ha_scanner.current_mode is BluetoothScanningMode.ACTIVE
            assert ha_scanner._scan_mode_override is BluetoothScanningMode.ACTIVE
            assert ha_scanner._active_window_handle is not None
        finally:
            await ha_scanner.async_stop()


@pytest.mark.usefixtures("force_non_linux_non_macos_scanner_mode")
@pytest.mark.asyncio
async def test_async_request_active_window_restart_path_scanner_start_error() -> None:
    """
    Non-Linux entry: a ``ScannerStartError`` triggers the abort recovery path.

    ``_async_begin_active_window_via_restart`` catches the error and
    routes through ``_async_abort_active_window`` so the scanner comes
    back up in its underlying mode instead of being left stopped.
    """
    starts = 0

    class AlwaysFailAfterFirstMockBleakScanner(MockBleakScanner):
        async def start(self) -> None:
            nonlocal starts
            starts += 1
            # First start (initial async_start) succeeds; every
            # subsequent start raises so all 4 retries of the
            # restart attempt exhaust, raising ScannerStartError,
            # which the abort path then suppresses.
            if starts > 1:
                msg = "simulated start failure"
                raise BleakError(msg)

    class _NoResetScanner(HaScanner):
        async def _async_reset_adapter(self, gone_silent: bool) -> None:
            pass

    with patch_bleak_scanner_factory(AlwaysFailAfterFirstMockBleakScanner):
        ha_scanner = _NoResetScanner(
            BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:F2"
        )
        ha_scanner.async_setup()
        await ha_scanner.async_start()
        try:
            assert await ha_scanner.async_request_active_window(0.5) is False
            # Abort cleared the override; no end-of-window timer armed.
            assert ha_scanner._scan_mode_override is None
            assert ha_scanner._active_window_handle is None
        finally:
            await ha_scanner.async_stop()


@pytest.mark.usefixtures("force_non_linux_non_macos_scanner_mode")
@pytest.mark.asyncio
async def test_async_request_active_window_restart_path_unexpected_error() -> None:
    """
    Non-Linux entry: unexpected exceptions clear the override and re-raise.

    Mirrors the toggle-path test for the restart-path
    ``except BaseException`` branch in
    ``_async_begin_active_window_via_restart``: a non-ScannerStartError
    must not poison ``_scan_mode_override`` for the next start.
    """
    raise_on_restart = False

    class _MaybeRaiseRestartScanner(HaScanner):
        async def _async_stop_then_start_under_lock(self) -> None:
            if raise_on_restart:
                msg = "simulated unexpected error"
                raise RuntimeError(msg)

    with patch_bleak_scanner_factory(MockBleakScanner):
        ha_scanner = _MaybeRaiseRestartScanner(
            BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:F4"
        )
        ha_scanner.async_setup()
        await ha_scanner.async_start()
        try:
            raise_on_restart = True
            with pytest.raises(RuntimeError, match="simulated unexpected error"):
                await ha_scanner.async_request_active_window(0.5)
            assert ha_scanner._scan_mode_override is None
        finally:
            raise_on_restart = False
            await ha_scanner.async_stop()


@pytest.mark.asyncio
async def test_detection_callback_skips_last_detection_for_empty_advertisement() -> (
    None
):
    """
    Empty advertisements don't bump ``_last_detection``.

    Bleak occasionally hands us a callback with no name / data /
    service info, which we treat as a heartbeat from a failing
    adapter rather than a real ping.
    """
    ha_scanner = HaScanner(BluetoothScanningMode.PASSIVE, "hci0", "AA:BB:CC:DD:EE:FF")
    ha_scanner.async_setup()
    ha_scanner._last_detection = -1.0
    device = generate_ble_device("AA:BB:CC:DD:EE:F5", None)
    # All four fields explicitly empty so the truthy-check skips the
    # _last_detection bump (generate_advertisement_data defaults
    # local_name to "Unknown", which would otherwise trip it).
    adv = generate_advertisement_data(
        local_name=None,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
    )
    ha_scanner._async_detection_callback(device, adv)
    assert ha_scanner._last_detection == -1.0
    await ha_scanner.async_stop()


@pytest.mark.usefixtures("force_non_linux_non_macos_scanner_mode")
@pytest.mark.asyncio
async def test_async_request_active_window_restart_path_mode_mismatch() -> None:
    """
    Non-Linux entry: a restart that doesn't land in ACTIVE clears the override.

    Simulates a backend that ignores ``_scan_mode_override`` by patching
    ``_async_stop_then_start_under_lock`` at the class level so it
    leaves ``current_mode`` unchanged at PASSIVE. The branch must clear
    the override and return False so the public method can report
    failure to the scheduler.
    """
    noop_restart = False

    class _NoopRestartScanner(HaScanner):
        async def _async_stop_then_start_under_lock(self) -> None:
            if not noop_restart:
                await HaScanner._async_stop_then_start_under_lock(self)

    with patch_bleak_scanner_factory(MockBleakScanner):
        ha_scanner = _NoopRestartScanner(
            BluetoothScanningMode.AUTO, "hci0", "AA:BB:CC:DD:EE:F3"
        )
        ha_scanner.async_setup()
        await ha_scanner.async_start()
        # Pretend a previous start left current_mode at PASSIVE.
        ha_scanner.set_current_mode(BluetoothScanningMode.PASSIVE)
        try:
            noop_restart = True
            assert await ha_scanner.async_request_active_window(0.5) is False
            assert ha_scanner._scan_mode_override is None
            assert ha_scanner._active_window_handle is None
        finally:
            noop_restart = False
            await ha_scanner.async_stop()
