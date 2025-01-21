from collections.abc import Iterable
from typing import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from bleak.backends.scanner import AdvertisementData, BLEDevice
from bleak_retry_connector import BleakSlotManager
from bluetooth_adapters import AdapterDetails, BluetoothAdapters

from habluetooth import (
    BaseHaScanner,
    BluetoothManager,
    get_manager,
    set_manager,
)
from habluetooth import scanner as bluetooth_scanner


class FakeBluetoothAdapters(BluetoothAdapters):

    @property
    def adapters(self) -> dict[str, AdapterDetails]:
        return {}


class FakeScannerMixin:
    def get_discovered_device_advertisement_data(
        self, address: str
    ) -> tuple[BLEDevice, AdvertisementData] | None:
        """Return the advertisement data for a discovered device."""
        return self.discovered_devices_and_advertisement_data.get(address)  # type: ignore[attr-defined]

    @property
    def discovered_addresses(self) -> Iterable[str]:
        """Return an iterable of discovered devices."""
        return self.discovered_devices_and_advertisement_data  # type: ignore[attr-defined]


class FakeScanner(FakeScannerMixin, BaseHaScanner):
    """Fake scanner."""

    @property
    def discovered_devices(self) -> list[BLEDevice]:
        """Return a list of discovered devices."""
        return []

    @property
    def discovered_devices_and_advertisement_data(
        self,
    ) -> dict[str, tuple[BLEDevice, AdvertisementData]]:
        """Return a list of discovered devices and their advertisement data."""
        return {}


@pytest.fixture(scope="session", autouse=True)
def manager():
    slot_manager = BleakSlotManager()
    bluetooth_adapters = FakeBluetoothAdapters()
    manager = BluetoothManager(bluetooth_adapters, slot_manager)
    set_manager(manager)
    manager.async_stop()


@pytest_asyncio.fixture(name="enable_bluetooth")
async def mock_enable_bluetooth(
    mock_bleak_scanner_start: MagicMock,
    mock_bluetooth_adapters: None,
) -> AsyncGenerator[None, None]:
    """Fixture to mock starting the bleak scanner."""
    manager = get_manager()
    assert manager._bluetooth_adapters is not None
    await manager.async_setup()
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


@pytest.fixture
def register_hci0_scanner() -> Generator[None, None, None]:
    """Register an hci0 scanner."""
    hci0_scanner = FakeScanner("hci0", "hci0")
    manager = get_manager()
    cancel = manager.async_register_scanner(hci0_scanner)
    yield
    cancel()


@pytest.fixture
def register_hci1_scanner() -> Generator[None, None, None]:
    """Register an hci1 scanner."""
    hci1_scanner = FakeScanner("hci1", "hci1")
    manager = get_manager()
    cancel = manager.async_register_scanner(hci1_scanner)
    yield
    cancel()
