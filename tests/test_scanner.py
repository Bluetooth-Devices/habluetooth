"""Tests for the Bluetooth integration scanners."""

import asyncio
import time
from datetime import timedelta
from typing import Any
from unittest.mock import ANY, MagicMock, Mock, patch

import pytest
from bleak import BleakError
from bleak.backends.scanner import AdvertisementDataCallback
from bleak_retry_connector import BleakSlotManager
from bluetooth_adapters import BluetoothAdapters
from dbus_fast import InvalidMessageError

from habluetooth import (
    SCANNER_WATCHDOG_INTERVAL,
    SCANNER_WATCHDOG_TIMEOUT,
    BluetoothManager,
    BluetoothScanningMode,
    HaScanner,
    ScannerStartError,
    scanner,
    set_manager,
)

from . import (
    async_fire_time_changed,
    generate_advertisement_data,
    generate_ble_device,
    patch_bluetooth_time,
    utcnow,
)

IS_WINDOWS = 'os.name == "nt"'
IS_POSIX = 'os.name == "posix"'
NOT_POSIX = 'os.name != "posix"'
# or_patterns is a workaround for the fact that passive scanning
# needs at least one matcher to be set. The below matcher
# will match all devices.
scanner.PASSIVE_SCANNER_ARGS = Mock()
# If the adapter is in a stuck state the following errors are raised:
NEED_RESET_ERRORS = [
    "org.bluez.Error.Failed",
    "org.bluez.Error.InProgress",
    "org.bluez.Error.NotReady",
    "not found",
]


@pytest.fixture(autouse=True, scope="module")
def manager():
    """Return the BluetoothManager instance."""
    adapters = BluetoothAdapters()
    slot_manager = BleakSlotManager()
    manager = BluetoothManager(adapters, slot_manager)
    set_manager(manager)
    return manager


@pytest.mark.asyncio
async def test_empty_data_no_scanner() -> None:
    """Test we handle empty data."""
    scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
    scanner.async_setup()
    assert scanner.discovered_devices == []
    assert scanner.discovered_devices_and_advertisement_data == {}


@pytest.mark.asyncio
@pytest.mark.skipif("platform.system() != 'Linux'")
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
        pytest.raises(
            ScannerStartError,
            match="DBus service not found; docker config may be missing",
        ),
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        assert mock_stop.called
        await scanner.async_stop()


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
        pytest.raises(
            ScannerStartError,
            match="DBus service not found; make sure the DBus socket is available",
        ),
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        assert mock_stop.called
        await scanner.async_stop()


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
        pytest.raises(ScannerStartError, match="DBus connection broken"),
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        assert mock_stop.called
        await scanner.async_stop()


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
        pytest.raises(ScannerStartError, match="DBus connection broken:"),
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        assert mock_stop.called
        await scanner.async_stop()


@pytest.mark.asyncio
@pytest.mark.skipif(NOT_POSIX)
async def test_invalid_dbus_message(caplog: pytest.LogCaptureFixture) -> None:
    """Test we handle invalid dbus message."""
    with (
        patch(
            "habluetooth.scanner.OriginalBleakScanner.start",
            side_effect=InvalidMessageError,
        ),
        pytest.raises(ScannerStartError, match="Invalid DBus message received"),
    ):
        scanner = HaScanner(BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF")
        scanner.async_setup()
        await scanner.async_start()
        await scanner.async_stop()


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

    class MockBleakScanner:
        async def start(self, *args, **kwargs):
            """Mock Start."""
            nonlocal called_start
            called_start += 1
            if called_start < 3:
                raise BleakError(error)

        async def stop(self, *args, **kwargs):
            """Mock Start."""
            nonlocal called_stop
            called_stop += 1

        @property
        def discovered_devices(self):
            """Mock discovered_devices."""
            nonlocal mock_discovered
            return mock_discovered

        def register_detection_callback(
            self, callback: AdvertisementDataCallback
        ) -> None:
            """Mock Register Detection Callback."""
            nonlocal _callback
            _callback = callback

    mock_scanner = MockBleakScanner()

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

    class MockBleakScanner:
        def __init__(self, detection_callback, *args, **kwargs):
            nonlocal _callback
            _callback = detection_callback

        async def start(self, *args, **kwargs):
            """Mock Start."""
            nonlocal called_start
            called_start += 1

        async def stop(self, *args, **kwargs):
            """Mock Start."""
            nonlocal called_stop
            called_stop += 1

        @property
        def discovered_devices(self):
            """Mock discovered_devices."""
            nonlocal mock_discovered
            return mock_discovered

    with patch(
        "habluetooth.scanner.OriginalBleakScanner",
        MockBleakScanner,
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
            _callback(  # type: ignore
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

    class MockBleakScanner:
        async def start(self, *args, **kwargs):
            """Mock Start."""
            nonlocal called_start
            called_start += 1

        async def stop(self, *args, **kwargs):
            """Mock Start."""
            nonlocal called_stop
            called_stop += 1

        @property
        def discovered_devices(self):
            """Mock discovered_devices."""
            nonlocal mock_discovered
            return mock_discovered

        def register_detection_callback(
            self, callback: AdvertisementDataCallback
        ) -> None:
            """Mock Register Detection Callback."""
            nonlocal _callback
            _callback = callback

    mock_scanner = MockBleakScanner()
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

    class MockBleakScanner:
        async def start(self, *args, **kwargs):
            """Mock Start."""
            nonlocal called_start
            called_start += 1
            if called_start == 1:
                return  # Start ok the first time
            if called_start < 4:
                raise BleakError("Failed to start")

        async def stop(self, *args, **kwargs):
            """Mock Start."""
            nonlocal called_stop
            called_stop += 1

        @property
        def discovered_devices(self):
            """Mock discovered_devices."""
            nonlocal mock_discovered
            return mock_discovered

        def register_detection_callback(
            self, callback: AdvertisementDataCallback
        ) -> None:
            """Mock Register Detection Callback."""
            nonlocal _callback
            _callback = callback

    mock_scanner = MockBleakScanner()
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

    class MockBleakScanner:
        async def start(self, *args, **kwargs):
            """Mock Start."""
            nonlocal called_start
            called_start += 1
            if called_start == 1:
                raise BleakError("org.freedesktop.DBus.Error.UnknownObject")
            if called_start == 2:
                raise BleakError("org.bluez.Error.InProgress")
            if called_start == 3:
                raise BleakError("org.bluez.Error.InProgress")

        async def stop(self, *args, **kwargs):
            """Mock Start."""
            nonlocal called_stop
            called_stop += 1

        @property
        def discovered_devices(self):
            """Mock discovered_devices."""
            nonlocal mock_discovered
            return mock_discovered

        def register_detection_callback(
            self, callback: AdvertisementDataCallback
        ) -> None:
            """Mock Register Detection Callback."""
            nonlocal _callback
            _callback = callback

    mock_scanner = MockBleakScanner()
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

    class MockBleakScanner:
        async def start(self, *args, **kwargs):
            """Mock Start."""
            nonlocal called_start
            called_start += 1
            if called_start == 1:
                return
            await release_start_event.wait()

        async def stop(self, *args, **kwargs):
            """Mock Start."""

        @property
        def discovered_devices(self):
            """Mock discovered_devices."""
            return []

        def register_detection_callback(
            self, callback: AdvertisementDataCallback
        ) -> None:
            """Mock Register Detection Callback."""

    mock_scanner = MockBleakScanner()
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

    class MockBleakScanner:
        def __init__(self, *args, **kwargs):
            """Init the scanner."""
            nonlocal init_kwargs
            init_kwargs = kwargs

        async def start(self, *args, **kwargs):
            """Start the scanner."""

        async def stop(self, *args, **kwargs):
            """Stop the scanner."""

        def register_detection_callback(self, *args, **kwargs):
            """Register a callback."""

    with patch(
        "habluetooth.scanner.OriginalBleakScanner",
        MockBleakScanner,
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

    class MockBleakScanner:
        async def start(self, *args, **kwargs):
            """Mock Start."""
            nonlocal called_start
            called_start += 1
            if called_start == 1:
                raise BleakError("org.freedesktop.DBus.Error.UnknownObject")
            if called_start == 2:
                raise BleakError("org.bluez.Error.InProgress")
            if called_start == 3:
                raise BleakError("org.bluez.Error.InProgress")

        async def stop(self, *args, **kwargs):
            """Mock Start."""
            nonlocal called_stop
            called_stop += 1

        @property
        def discovered_devices(self):
            """Mock discovered_devices."""
            nonlocal mock_discovered
            return mock_discovered

        def register_detection_callback(
            self, callback: AdvertisementDataCallback
        ) -> None:
            """Mock Register Detection Callback."""
            nonlocal _callback
            _callback = callback

        @property
        def discovered_devices_and_advertisement_data(self) -> dict[str, Any]:
            """Mock discovered_devices."""
            return {}

    mock_scanner = MockBleakScanner()
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
