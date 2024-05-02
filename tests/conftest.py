from typing import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(name="enable_bluetooth")
async def mock_enable_bluetooth(
    mock_bleak_scanner_start: MagicMock,
    mock_bluetooth_adapters: None,
) -> AsyncGenerator[None, None]:
    """Fixture to mock starting the bleak scanner."""
    yield


@pytest.fixture(scope="session")
def mock_bluetooth_adapters() -> Generator[None, None, None]:
    """Fixture to mock bluetooth adapters."""
    with (
        patch("bluetooth_auto_recovery.recover_adapter"),
        patch("bluetooth_adapters.systems.platform.system", return_value="Linux"),
        patch("bluetooth_adapters.systems.linux.LinuxAdapters.refresh"),
        patch(
            "bluetooth_adapters.systems.linux.LinuxAdapters.adapters",
            {
                "hci0": {
                    "address": "00:00:00:00:00:01",
                    "hw_version": "usb:v1D6Bp0246d053F",
                    "passive_scan": False,
                    "sw_version": "homeassistant",
                    "manufacturer": "ACME",
                    "product": "Bluetooth Adapter 5.0",
                    "product_id": "aa01",
                    "vendor_id": "cc01",
                },
            },
        ),
    ):
        yield


@pytest.fixture
def mock_bleak_scanner_start() -> Generator[MagicMock, None, None]:
    """Fixture to mock starting the bleak scanner."""
    # Late imports to avoid loading bleak unless we need it

    # pylint: disable-next=import-outside-toplevel
    from habluetooth import scanner as bluetooth_scanner

    bluetooth_scanner.OriginalBleakScanner.stop = AsyncMock()
    with (
        patch.object(
            bluetooth_scanner.OriginalBleakScanner,
            "start",
        ) as mock_bleak_scanner_start,
        patch.object(bluetooth_scanner, "HaScanner"),
    ):
        yield mock_bleak_scanner_start


@pytest.fixture(name="two_adapters")
def two_adapters_fixture():
    """Fixture that mocks two adapters on Linux."""
    with (
        patch(
            "habluetooth.scanner.platform.system",
            return_value="Linux",
        ),
        patch("bluetooth_adapters.systems.platform.system", return_value="Linux"),
        patch("bluetooth_adapters.systems.linux.LinuxAdapters.refresh"),
        patch(
            "bluetooth_adapters.systems.linux.LinuxAdapters.adapters",
            {
                "hci0": {
                    "address": "00:00:00:00:00:01",
                    "hw_version": "usb:v1D6Bp0246d053F",
                    "passive_scan": False,
                    "sw_version": "homeassistant",
                    "manufacturer": "ACME",
                    "product": "Bluetooth Adapter 5.0",
                    "product_id": "aa01",
                    "vendor_id": "cc01",
                    "connection_slots": 1,
                },
                "hci1": {
                    "address": "00:00:00:00:00:02",
                    "hw_version": "usb:v1D6Bp0246d053F",
                    "passive_scan": True,
                    "sw_version": "homeassistant",
                    "manufacturer": "ACME",
                    "product": "Bluetooth Adapter 5.0",
                    "product_id": "aa01",
                    "vendor_id": "cc01",
                    "connection_slots": 2,
                },
            },
        ),
    ):
        yield
