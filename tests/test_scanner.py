"""Tests for the Bluetooth integration scanners."""

import asyncio
import platform
import time
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
    make_bluez_details,
)

from . import (
    async_fire_time_changed,
    generate_advertisement_data,
    generate_ble_device,
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
    # On other platforms, we can use a simple mock
    scanner.PASSIVE_SCANNER_ARGS = Mock()
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
            pass

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
