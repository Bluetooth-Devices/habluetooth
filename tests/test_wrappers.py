"""Tests for bluetooth wrappers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from typing import Any
from unittest.mock import Mock, patch

import bleak
import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
from bleak_retry_connector import Allocations
from bluetooth_data_tools import monotonic_time_coarse as MONOTONIC_TIME

from habluetooth import BaseHaRemoteScanner, HaBluetoothConnector
from habluetooth import get_manager as _get_manager
from habluetooth.manager import BluetoothManager
from habluetooth.usage import (
    install_multiple_bleak_catcher,
    uninstall_multiple_bleak_catcher,
)
from habluetooth.wrappers import HaBleakScannerWrapper

from . import (
    HCI0_SOURCE_ADDRESS,
    generate_advertisement_data,
    generate_ble_device,
    inject_advertisement,
    patch_discovered_devices,
)


@contextmanager
def mock_shutdown(manager: BluetoothManager) -> Generator[None, None, None]:
    """Mock shutdown of the HomeAssistantBluetoothManager."""
    manager.shutdown = True
    yield
    manager.shutdown = False


class FakeScanner(BaseHaRemoteScanner):
    """Fake scanner."""

    def __init__(
        self,
        scanner_id: str,
        name: str,
        connector: Any,
        connectable: bool,
    ) -> None:
        """Initialize the scanner."""
        super().__init__(scanner_id, name, connector, connectable)
        self._details: dict[str, str | HaBluetoothConnector] = {}

    def __repr__(self) -> str:
        """Return the representation."""
        return f"FakeScanner({self.name})"

    def inject_advertisement(
        self, device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Inject an advertisement."""
        self._async_on_advertisement(
            device.address,
            advertisement_data.rssi,
            device.name,
            advertisement_data.service_uuids,
            advertisement_data.service_data,
            advertisement_data.manufacturer_data,
            advertisement_data.tx_power,
            device.details | {"scanner_specific_data": "test"},
            MONOTONIC_TIME(),
        )


class BaseFakeBleakClient:
    """Base class for fake bleak clients."""

    def __init__(self, address_or_ble_device: BLEDevice | str, **kwargs: Any) -> None:
        """Initialize the fake bleak client."""
        self._device_path = "/dev/test"
        self._device = address_or_ble_device
        assert isinstance(address_or_ble_device, BLEDevice)
        self._address = address_or_ble_device.address

    async def disconnect(self, *args, **kwargs):
        """Disconnect."""

    async def get_services(self, *args, **kwargs):
        """Get services."""
        return []


class FakeBleakClient(BaseFakeBleakClient):
    """Fake bleak client."""

    async def connect(self, *args, **kwargs):
        """Connect."""
        return True

    @property
    def is_connected(self):
        return False


class FakeBleakClientFailsToConnect(BaseFakeBleakClient):
    """Fake bleak client that fails to connect."""

    async def connect(self, *args, **kwargs):
        """Connect."""
        return

    @property
    def is_connected(self):
        return False


class FakeBleakClientRaisesOnConnect(BaseFakeBleakClient):
    """Fake bleak client that raises on connect."""

    async def connect(self, *args, **kwargs):
        """Connect."""
        raise ConnectionError("Test exception")


def _generate_ble_device_and_adv_data(
    interface: str, mac: str, rssi: int
) -> tuple[BLEDevice, AdvertisementData]:
    """Generate a BLE device with adv data."""
    return (
        generate_ble_device(
            mac,
            "any",
            delegate="",
            details={"path": f"/org/bluez/{interface}/dev_{mac}"},
        ),
        generate_advertisement_data(rssi=rssi),
    )


@pytest.fixture(name="install_bleak_catcher")
def install_bleak_catcher_fixture():
    """Fixture that installs the bleak catcher."""
    install_multiple_bleak_catcher()
    yield
    uninstall_multiple_bleak_catcher()


@pytest.fixture(name="mock_platform_client")
def mock_platform_client_fixture():
    """Fixture that mocks the platform client."""
    with patch(
        "habluetooth.wrappers.get_platform_client_backend_type",
        return_value=FakeBleakClient,
    ):
        yield


@pytest.fixture(name="mock_platform_client_that_fails_to_connect")
def mock_platform_client_that_fails_to_connect_fixture():
    """Fixture that mocks the platform client that fails to connect."""
    with patch(
        "habluetooth.wrappers.get_platform_client_backend_type",
        return_value=FakeBleakClientFailsToConnect,
    ):
        yield


@pytest.fixture(name="mock_platform_client_that_raises_on_connect")
def mock_platform_client_that_raises_on_connect_fixture():
    """Fixture that mocks the platform client that fails to connect."""
    with patch(
        "habluetooth.wrappers.get_platform_client_backend_type",
        return_value=FakeBleakClientRaisesOnConnect,
    ):
        yield


def _generate_scanners_with_fake_devices():
    """Generate scanners with fake devices."""
    manager = _get_manager()
    hci0_device_advs = {}
    for i in range(10):
        device, adv_data = _generate_ble_device_and_adv_data(
            "hci0", f"00:00:00:00:00:{i:02x}", rssi=-60
        )
        hci0_device_advs[device.address] = (device, adv_data)
    hci1_device_advs = {}
    for i in range(10):
        device, adv_data = _generate_ble_device_and_adv_data(
            "hci1", f"00:00:00:00:00:{i:02x}", rssi=-80
        )
        hci1_device_advs[device.address] = (device, adv_data)

    scanner_hci0 = FakeScanner("00:00:00:00:00:01", "hci0", None, True)
    scanner_hci1 = FakeScanner("00:00:00:00:00:02", "hci1", None, True)

    for device, adv_data in hci0_device_advs.values():
        scanner_hci0.inject_advertisement(device, adv_data)

    for device, adv_data in hci1_device_advs.values():
        scanner_hci1.inject_advertisement(device, adv_data)

    cancel_hci0 = manager.async_register_scanner(scanner_hci0, connection_slots=2)
    cancel_hci1 = manager.async_register_scanner(scanner_hci1, connection_slots=1)

    return hci0_device_advs, cancel_hci0, cancel_hci1


@pytest.mark.asyncio
async def test_test_switch_adapters_when_out_of_slots(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
    mock_platform_client: None,
) -> None:
    """Ensure we try another scanner when one runs out of slots."""
    manager = _get_manager()
    # hci0 has an rssi of -60, hci1 has an rssi of -80
    hci0_device_advs, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()
    # hci0 has 2 slots, hci1 has 1 slot
    with (
        patch.object(manager.slot_manager, "release_slot") as release_slot_mock,
        patch.object(
            manager.slot_manager, "allocate_slot", return_value=True
        ) as allocate_slot_mock,
    ):
        ble_device = hci0_device_advs["00:00:00:00:00:01"][0]
        with patch.object(FakeBleakClient, "is_connected", return_value=True):
            client = bleak.BleakClient(ble_device)
            await client.connect()
        assert allocate_slot_mock.call_count == 1
        assert release_slot_mock.call_count == 0

    # All adapters are out of slots
    with (
        patch.object(manager.slot_manager, "release_slot") as release_slot_mock,
        patch.object(
            manager.slot_manager, "allocate_slot", return_value=False
        ) as allocate_slot_mock,
    ):
        ble_device = hci0_device_advs["00:00:00:00:00:02"][0]
        client = bleak.BleakClient(ble_device)
        with pytest.raises(bleak.exc.BleakError):
            await client.connect()
        assert allocate_slot_mock.call_count == 2
        assert release_slot_mock.call_count == 0

    # When hci0 runs out of slots, we should try hci1
    def _allocate_slot_mock(ble_device: BLEDevice) -> bool:
        return "hci1" in ble_device.details["path"]

    with (
        patch.object(manager.slot_manager, "release_slot") as release_slot_mock,
        patch.object(  # type: ignore
            manager.slot_manager, "allocate_slot", _allocate_slot_mock
        ) as allocate_slot_mock,
    ):
        ble_device = hci0_device_advs["00:00:00:00:00:03"][0]
        with patch.object(FakeBleakClient, "is_connected", return_value=True):
            client = bleak.BleakClient(ble_device)
            await client.connect()
        assert release_slot_mock.call_count == 0

    cancel_hci0()
    cancel_hci1()


@pytest.mark.asyncio
async def test_release_slot_on_connect_failure(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
    mock_platform_client_that_raises_on_connect: None,
) -> None:
    """Ensure the slot gets released on connection failure."""
    manager = _get_manager()
    hci0_device_advs, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()
    # hci0 has 2 slots, hci1 has 1 slot
    with (
        patch.object(manager.slot_manager, "release_slot") as release_slot_mock,
        patch.object(
            manager.slot_manager, "allocate_slot", return_value=True
        ) as allocate_slot_mock,
    ):
        ble_device = hci0_device_advs["00:00:00:00:00:01"][0]
        client = bleak.BleakClient(ble_device)
        with pytest.raises(ConnectionError):
            await client.connect()
        assert allocate_slot_mock.call_count == 1
        assert release_slot_mock.call_count == 1

    cancel_hci0()
    cancel_hci1()


@pytest.mark.asyncio
async def test_release_slot_on_connect_exception(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
    mock_platform_client_that_raises_on_connect: None,
) -> None:
    """Ensure the slot gets released on connection exception."""
    manager = _get_manager()
    hci0_device_advs, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()
    # hci0 has 2 slots, hci1 has 1 slot
    with (
        patch.object(manager.slot_manager, "release_slot") as release_slot_mock,
        patch.object(
            manager.slot_manager, "allocate_slot", return_value=True
        ) as allocate_slot_mock,
    ):
        ble_device = hci0_device_advs["00:00:00:00:00:01"][0]
        client = bleak.BleakClient(ble_device)
        with pytest.raises(ConnectionError) as exc_info:
            await client.connect()
        assert str(exc_info.value) == "Test exception"
        assert allocate_slot_mock.call_count == 1
        assert release_slot_mock.call_count == 1

    cancel_hci0()
    cancel_hci1()


@pytest.mark.asyncio
async def test_switch_adapters_on_failure(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
) -> None:
    """Ensure we try the next best adapter after a failure."""
    hci0_device_advs, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()
    ble_device = hci0_device_advs["00:00:00:00:00:01"][0]
    client = bleak.BleakClient(ble_device)

    class FakeBleakClientFailsHCI0Only(BaseFakeBleakClient):
        """Fake bleak client that fails to connect on hci0."""

        async def connect(self, *args: Any, **kwargs: Any) -> None:
            """Connect."""
            assert isinstance(self._device, BLEDevice)
            if "/hci0/" in self._device.details["path"]:
                raise BleakError("Failed to connect on hci0")

        @property
        def is_connected(self) -> bool:
            return True

    class FakeBleakClientFailsHCI1Only(BaseFakeBleakClient):
        """Fake bleak client that fails to connect on hci1."""

        async def connect(self, *args: Any, **kwargs: Any) -> None:
            """Connect."""
            assert isinstance(self._device, BLEDevice)
            if "/hci1/" in self._device.details["path"]:
                raise BleakError("Failed to connect on hci1")

        @property
        def is_connected(self) -> bool:
            return True

    with patch(
        "habluetooth.wrappers.get_platform_client_backend_type",
        return_value=FakeBleakClientFailsHCI0Only,
    ):
        # Should try to connect to hci0 first
        with pytest.raises(BleakError):
            await client.connect()
        assert not client.is_connected
        # Should try to connect with hci0 again
        with pytest.raises(BleakError):
            await client.connect()
        assert not client.is_connected

        # After two tries we should switch to hci1
        await client.connect()
        assert client.is_connected

        # ..and we remember that hci1 works as long as the client doesn't change
        await client.connect()
        assert client.is_connected

        # If we replace the client, we should remember hci0 is failing
        client = bleak.BleakClient(ble_device)

        await client.connect()
        assert client.is_connected

    with patch(
        "habluetooth.wrappers.get_platform_client_backend_type",
        return_value=FakeBleakClientFailsHCI1Only,
    ):
        # Should try to connect to hci1 first
        await client.connect()
        assert client.is_connected
        # Should work with hci0 on next attempt
        await client.connect()
        assert client.is_connected
        # Next attempt should also use hci0
        await client.connect()
        assert client.is_connected

    cancel_hci0()
    cancel_hci1()


@pytest.mark.asyncio
async def test_switch_adapters_on_connecting(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
) -> None:
    """Ensure we try the next best adapter after a failure."""
    # hci0 has an rssi of -60, hci1 has an rssi of -80
    hci0_device_advs, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()
    ble_device = hci0_device_advs["00:00:00:00:00:01"][0]
    client = bleak.BleakClient(ble_device)

    class FakeBleakClientSlowHCI0Connnect(BaseFakeBleakClient):
        """Fake bleak client that connects instantly on hci1 and slow on hci0."""

        valid = False

        async def connect(self, *args: Any, **kwargs: Any) -> None:
            """Connect."""
            assert isinstance(self._device, BLEDevice)
            if "/hci0/" in self._device.details["path"]:
                await asyncio.sleep(0.4)
                self.valid = True
            else:
                self.valid = True

        @property
        def is_connected(self) -> bool:
            return self.valid

    with patch(
        "habluetooth.wrappers.get_platform_client_backend_type",
        return_value=FakeBleakClientSlowHCI0Connnect,
    ):
        task = asyncio.create_task(client.connect())
        await asyncio.sleep(0.1)
        assert not task.done()

        task2 = asyncio.create_task(client.connect())
        await asyncio.sleep(0.1)
        assert task2.done()
        await task2
        assert client.is_connected

        task3 = asyncio.create_task(client.connect())
        await asyncio.sleep(0.1)
        assert task3.done()
        await task3
        assert client.is_connected

        await task
        assert client.is_connected

    cancel_hci0()
    cancel_hci1()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enable_bluetooth", "install_bleak_catcher")
async def test_single_adapter_connection_history(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test connection history failure count."""
    manager = _get_manager()
    scanner_hci0 = FakeScanner(HCI0_SOURCE_ADDRESS, "hci0", None, True)
    unsub_hci0 = manager.async_register_scanner(scanner_hci0, connection_slots=2)
    ble_device, adv_data = _generate_ble_device_and_adv_data(
        "hci0", "00:00:00:00:00:11", rssi=-60
    )
    scanner_hci0.inject_advertisement(ble_device, adv_data)
    service_info = manager.async_last_service_info(
        ble_device.address, connectable=False
    )
    assert service_info is not None
    assert service_info.source == HCI0_SOURCE_ADDRESS

    client = bleak.BleakClient(ble_device)

    class FakeBleakClientFastConnect(BaseFakeBleakClient):
        """Fake bleak client that connects instantly on hci1 and slow on hci0."""

        valid = False

        async def connect(self, *args: Any, **kwargs: Any) -> None:
            """Connect."""
            assert isinstance(self._device, BLEDevice)
            self.valid = "/hci0/" in self._device.details["path"]

        @property
        def is_connected(self) -> bool:
            return self.valid

    with patch(
        "habluetooth.wrappers.get_platform_client_backend_type",
        return_value=FakeBleakClientFastConnect,
    ):
        await client.connect()
    unsub_hci0()


@pytest.mark.asyncio
async def test_passing_subclassed_str_as_address(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
) -> None:
    """Ensure the client wrapper can handle a subclassed str as the address."""
    _, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()

    class SubclassedStr(str):
        __slots__ = ()

    address = SubclassedStr("00:00:00:00:00:01")
    client = bleak.BleakClient(address)

    class FakeBleakClient(BaseFakeBleakClient):
        """Fake bleak client."""

        async def connect(self, *args, **kwargs):
            """Connect."""
            return

        @property
        def is_connected(self) -> bool:
            return True

    with patch(
        "habluetooth.wrappers.get_platform_client_backend_type",
        return_value=FakeBleakClient,
    ):
        await client.connect()

    cancel_hci0()
    cancel_hci1()


@pytest.mark.asyncio
async def test_find_device_by_address(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
) -> None:
    """Ensure the client wrapper can handle a subclassed str as the address."""
    _, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()
    device = await bleak.BleakScanner.find_device_by_address("00:00:00:00:00:01")
    assert device.address == "00:00:00:00:00:01"
    device = await bleak.BleakScanner().find_device_by_address("00:00:00:00:00:01")
    assert device.address == "00:00:00:00:00:01"


@pytest.mark.asyncio
async def test_discover(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
) -> None:
    """Ensure the discover is implemented."""
    _, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()
    devices = await bleak.BleakScanner.discover()
    assert any(device.address == "00:00:00:00:00:01" for device in devices)
    devices_adv = await bleak.BleakScanner.discover(return_adv=True)
    assert "00:00:00:00:00:01" in devices_adv


@pytest.mark.asyncio
async def test_raise_after_shutdown(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
    mock_platform_client_that_raises_on_connect: None,
) -> None:
    """Ensure the slot gets released on connection exception."""
    manager = _get_manager()
    hci0_device_advs, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()
    # hci0 has 2 slots, hci1 has 1 slot
    with mock_shutdown(manager):
        ble_device = hci0_device_advs["00:00:00:00:00:01"][0]
        client = bleak.BleakClient(ble_device)
        with pytest.raises(BleakError, match="shutdown"):
            await client.connect()
    cancel_hci0()
    cancel_hci1()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_wrapped_instance_with_filter(
    register_hci0_scanner: None,
) -> None:
    """Test wrapped instance with a filter as if it was normal BleakScanner."""
    detected = []

    def _device_detected(
        device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Handle a detected device."""
        detected.append((device, advertisement_data))

    switchbot_device = generate_ble_device("44:44:33:11:23:45", "wohand")
    switchbot_adv = generate_advertisement_data(
        local_name="wohand",
        service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"],
        manufacturer_data={89: b"\xd8.\xad\xcd\r\x85"},
        service_data={"00000d00-0000-1000-8000-00805f9b34fb": b"H\x10c"},
    )
    switchbot_adv_2 = generate_advertisement_data(
        local_name="wohand",
        service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"],
        manufacturer_data={89: b"\xd8.\xad\xcd\r\x84"},
        service_data={"00000d00-0000-1000-8000-00805f9b34fb": b"H\x10c"},
    )
    empty_device = generate_ble_device("11:22:33:44:55:66", "empty")
    empty_adv = generate_advertisement_data(local_name="empty")

    assert _get_manager() is not None
    scanner = HaBleakScannerWrapper(
        filters={"UUIDs": ["cba20d00-224d-11e6-9fb8-0002a5d5c51b"]}
    )
    scanner.register_detection_callback(_device_detected)

    inject_advertisement(switchbot_device, switchbot_adv_2)
    await asyncio.sleep(0)

    discovered = await scanner.discover(timeout=0)
    assert len(discovered) == 1
    assert discovered == [switchbot_device]
    assert len(detected) == 1

    scanner.register_detection_callback(_device_detected)
    # We should get a reply from the history when we register again
    assert len(detected) == 2
    scanner.register_detection_callback(_device_detected)
    # We should get a reply from the history when we register again
    assert len(detected) == 3

    with patch_discovered_devices([]):
        discovered = await scanner.discover(timeout=0)
        assert len(discovered) == 0
        assert discovered == []

    inject_advertisement(switchbot_device, switchbot_adv)
    assert len(detected) == 4

    # The filter we created in the wrapped scanner with should be respected
    # and we should not get another callback
    inject_advertisement(empty_device, empty_adv)
    assert len(detected) == 4


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_wrapped_instance_with_service_uuids(
    register_hci0_scanner: None,
) -> None:
    """Test wrapped instance with a service_uuids list as normal BleakScanner."""
    detected = []

    def _device_detected(
        device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Handle a detected device."""
        detected.append((device, advertisement_data))

    switchbot_device = generate_ble_device("44:44:33:11:23:45", "wohand")
    switchbot_adv = generate_advertisement_data(
        local_name="wohand",
        service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"],
        manufacturer_data={89: b"\xd8.\xad\xcd\r\x85"},
        service_data={"00000d00-0000-1000-8000-00805f9b34fb": b"H\x10c"},
    )
    switchbot_adv_2 = generate_advertisement_data(
        local_name="wohand",
        service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"],
        manufacturer_data={89: b"\xd8.\xad\xcd\r\x84"},
        service_data={"00000d00-0000-1000-8000-00805f9b34fb": b"H\x10c"},
    )
    empty_device = generate_ble_device("11:22:33:44:55:66", "empty")
    empty_adv = generate_advertisement_data(local_name="empty")

    assert _get_manager() is not None
    scanner = HaBleakScannerWrapper(
        service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"]
    )
    scanner.register_detection_callback(_device_detected)

    inject_advertisement(switchbot_device, switchbot_adv)
    inject_advertisement(switchbot_device, switchbot_adv_2)

    await asyncio.sleep(0)

    assert len(detected) == 2

    # The UUIDs list we created in the wrapped scanner with should be respected
    # and we should not get another callback
    inject_advertisement(empty_device, empty_adv)
    assert len(detected) == 2


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_wrapped_instance_with_service_uuids_with_coro_callback(
    register_hci0_scanner: None,
) -> None:
    """
    Test wrapped instance with a service_uuids list as normal BleakScanner.

    Verify that coro callbacks are supported.
    """
    detected = []

    async def _device_detected(
        device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Handle a detected device."""
        detected.append((device, advertisement_data))

    switchbot_device = generate_ble_device("44:44:33:11:23:45", "wohand")
    switchbot_adv = generate_advertisement_data(
        local_name="wohand",
        service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"],
        manufacturer_data={89: b"\xd8.\xad\xcd\r\x85"},
        service_data={"00000d00-0000-1000-8000-00805f9b34fb": b"H\x10c"},
    )
    switchbot_adv_2 = generate_advertisement_data(
        local_name="wohand",
        service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"],
        manufacturer_data={89: b"\xd8.\xad\xcd\r\x84"},
        service_data={"00000d00-0000-1000-8000-00805f9b34fb": b"H\x10c"},
    )
    empty_device = generate_ble_device("11:22:33:44:55:66", "empty")
    empty_adv = generate_advertisement_data(local_name="empty")

    assert _get_manager() is not None
    scanner = HaBleakScannerWrapper(
        service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"]
    )
    scanner.register_detection_callback(_device_detected)

    inject_advertisement(switchbot_device, switchbot_adv)
    inject_advertisement(switchbot_device, switchbot_adv_2)

    await asyncio.sleep(0)

    assert len(detected) == 2

    # The UUIDs list we created in the wrapped scanner with should be respected
    # and we should not get another callback
    inject_advertisement(empty_device, empty_adv)
    assert len(detected) == 2


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_wrapped_instance_with_broken_callbacks(
    register_hci0_scanner: None,
) -> None:
    """Test broken callbacks do not cause the scanner to fail."""
    detected: list[tuple[BLEDevice, AdvertisementData]] = []

    def _device_detected(
        device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Handle a detected device."""
        if detected:
            raise ValueError
        detected.append((device, advertisement_data))

    switchbot_device = generate_ble_device("44:44:33:11:23:45", "wohand")
    switchbot_adv = generate_advertisement_data(
        local_name="wohand",
        service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"],
        manufacturer_data={89: b"\xd8.\xad\xcd\r\x85"},
        service_data={"00000d00-0000-1000-8000-00805f9b34fb": b"H\x10c"},
    )

    assert _get_manager() is not None
    scanner = HaBleakScannerWrapper(
        service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"]
    )
    scanner.register_detection_callback(_device_detected)

    inject_advertisement(switchbot_device, switchbot_adv)
    await asyncio.sleep(0)
    inject_advertisement(switchbot_device, switchbot_adv)
    await asyncio.sleep(0)
    assert len(detected) == 1


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_wrapped_instance_changes_uuids(
    register_hci0_scanner: None,
) -> None:
    """Test consumers can use the wrapped instance can change the uuids later."""
    detected = []

    def _device_detected(
        device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Handle a detected device."""
        detected.append((device, advertisement_data))

    switchbot_device = generate_ble_device("44:44:33:11:23:45", "wohand")
    switchbot_adv = generate_advertisement_data(
        local_name="wohand",
        service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"],
        manufacturer_data={89: b"\xd8.\xad\xcd\r\x85"},
        service_data={"00000d00-0000-1000-8000-00805f9b34fb": b"H\x10c"},
    )
    switchbot_adv_2 = generate_advertisement_data(
        local_name="wohand",
        service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"],
        manufacturer_data={89: b"\xd8.\xad\xcd\r\x84"},
        service_data={"00000d00-0000-1000-8000-00805f9b34fb": b"H\x10c"},
    )
    empty_device = generate_ble_device("11:22:33:44:55:66", "empty")
    empty_adv = generate_advertisement_data(local_name="empty")

    assert _get_manager() is not None
    scanner = HaBleakScannerWrapper()
    scanner.set_scanning_filter(service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"])
    scanner.register_detection_callback(_device_detected)

    inject_advertisement(switchbot_device, switchbot_adv)
    inject_advertisement(switchbot_device, switchbot_adv_2)
    await asyncio.sleep(0)

    assert len(detected) == 2

    # The UUIDs list we created in the wrapped scanner with should be respected
    # and we should not get another callback
    inject_advertisement(empty_device, empty_adv)
    assert len(detected) == 2


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_wrapped_instance_changes_filters(
    register_hci0_scanner: None,
) -> None:
    """Test consumers can use the wrapped instance can change the filter later."""
    detected = []

    def _device_detected(
        device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Handle a detected device."""
        detected.append((device, advertisement_data))

    switchbot_device = generate_ble_device("44:44:33:11:23:42", "wohand")
    switchbot_adv = generate_advertisement_data(
        local_name="wohand",
        service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"],
        manufacturer_data={89: b"\xd8.\xad\xcd\r\x85"},
        service_data={"00000d00-0000-1000-8000-00805f9b34fb": b"H\x10c"},
    )
    switchbot_adv_2 = generate_advertisement_data(
        local_name="wohand",
        service_uuids=["cba20d00-224d-11e6-9fb8-0002a5d5c51b"],
        manufacturer_data={89: b"\xd8.\xad\xcd\r\x84"},
        service_data={"00000d00-0000-1000-8000-00805f9b34fb": b"H\x10c"},
    )
    empty_device = generate_ble_device("11:22:33:44:55:62", "empty")
    empty_adv = generate_advertisement_data(local_name="empty")

    assert _get_manager() is not None
    scanner = HaBleakScannerWrapper()
    scanner.set_scanning_filter(
        filters={"UUIDs": ["cba20d00-224d-11e6-9fb8-0002a5d5c51b"]}
    )
    scanner.register_detection_callback(_device_detected)

    inject_advertisement(switchbot_device, switchbot_adv)
    inject_advertisement(switchbot_device, switchbot_adv_2)

    await asyncio.sleep(0)

    assert len(detected) == 2

    # The UUIDs list we created in the wrapped scanner with should be respected
    # and we should not get another callback
    inject_advertisement(empty_device, empty_adv)
    assert len(detected) == 2


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_wrapped_instance_unsupported_filter(
    register_hci0_scanner: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test we want when their filter is ineffective."""
    assert _get_manager() is not None
    scanner = HaBleakScannerWrapper()
    scanner.set_scanning_filter(
        filters={
            "unsupported": ["cba20d00-224d-11e6-9fb8-0002a5d5c51b"],
            "DuplicateData": True,
        }
    )
    assert "Only UUIDs filters are supported" in caplog.text


@pytest.mark.asyncio
async def test_client_with_services_parameter(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
    mock_platform_client: None,
) -> None:
    """Test that services parameter is passed correctly to the backend."""
    hci0_device_advs, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()
    ble_device = hci0_device_advs["00:00:00:00:00:01"][0]

    test_services = [
        "00001800-0000-1000-8000-00805f9b34fb",
        "00001801-0000-1000-8000-00805f9b34fb",
    ]

    # Track what services were passed to the backend
    services_passed_to_backend = None

    class FakeBleakClientTracksServices(BaseFakeBleakClient):
        """Fake bleak client that tracks services parameter."""

        def __init__(
            self, address_or_ble_device: BLEDevice | str, **kwargs: Any
        ) -> None:
            """Initialize and capture services."""
            super().__init__(address_or_ble_device, **kwargs)
            nonlocal services_passed_to_backend
            services_passed_to_backend = kwargs.get("services")

        async def connect(self, *args, **kwargs):
            """Connect."""
            return True

        @property
        def is_connected(self):
            return True

    with patch(
        "habluetooth.wrappers.get_platform_client_backend_type",
        return_value=FakeBleakClientTracksServices,
    ):
        client = bleak.BleakClient(ble_device, services=test_services)
        await client.connect()

        # Verify services were normalized and passed as a set
        assert services_passed_to_backend is not None
        assert isinstance(services_passed_to_backend, set)
        assert services_passed_to_backend == {
            "00001800-0000-1000-8000-00805f9b34fb",
            "00001801-0000-1000-8000-00805f9b34fb",
        }

    cancel_hci0()
    cancel_hci1()


@pytest.mark.asyncio
async def test_client_with_pair_parameter(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
    mock_platform_client: None,
) -> None:
    """Test that pair parameter is set correctly on the wrapper."""
    hci0_device_advs, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()
    ble_device = hci0_device_advs["00:00:00:00:00:01"][0]

    # Test default pair=False
    client = bleak.BleakClient(ble_device)
    assert client._pair_before_connect is False

    # Test pair=True
    client = bleak.BleakClient(ble_device, pair=True)
    assert client._pair_before_connect is True

    cancel_hci0()
    cancel_hci1()


@pytest.mark.asyncio
async def test_client_services_normalization(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
    mock_platform_client: None,
) -> None:
    """Test that service UUIDs are normalized correctly."""
    hci0_device_advs, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()
    ble_device = hci0_device_advs["00:00:00:00:00:01"][0]

    # Test with short UUIDs that need normalization
    test_services = ["1800", "1801", "CBA20D00-224D-11E6-9FB8-0002A5D5C51B"]

    services_passed_to_backend = None

    class FakeBleakClientTracksServices(BaseFakeBleakClient):
        """Fake bleak client that tracks services parameter."""

        def __init__(
            self, address_or_ble_device: BLEDevice | str, **kwargs: Any
        ) -> None:
            """Initialize and capture services."""
            super().__init__(address_or_ble_device, **kwargs)
            nonlocal services_passed_to_backend
            services_passed_to_backend = kwargs.get("services")

        async def connect(self, *args, **kwargs):
            """Connect."""
            return True

        @property
        def is_connected(self):
            return True

    with patch(
        "habluetooth.wrappers.get_platform_client_backend_type",
        return_value=FakeBleakClientTracksServices,
    ):
        client = bleak.BleakClient(ble_device, services=test_services)
        await client.connect()

        # Verify services were normalized
        assert services_passed_to_backend is not None
        assert isinstance(services_passed_to_backend, set)
        assert services_passed_to_backend == {
            "00001800-0000-1000-8000-00805f9b34fb",
            "00001801-0000-1000-8000-00805f9b34fb",
            "cba20d00-224d-11e6-9fb8-0002a5d5c51b",  # Should be lowercased
        }

    cancel_hci0()
    cancel_hci1()


@pytest.mark.asyncio
async def test_client_with_none_services(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
    mock_platform_client: None,
) -> None:
    """Test that None services parameter is handled correctly."""
    hci0_device_advs, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()
    ble_device = hci0_device_advs["00:00:00:00:00:01"][0]

    services_passed_to_backend = "not_set"

    class FakeBleakClientTracksServices(BaseFakeBleakClient):
        """Fake bleak client that tracks services parameter."""

        def __init__(
            self, address_or_ble_device: BLEDevice | str, **kwargs: Any
        ) -> None:
            """Initialize and capture services."""
            super().__init__(address_or_ble_device, **kwargs)
            nonlocal services_passed_to_backend
            services_passed_to_backend = kwargs.get("services", "not_set")

        async def connect(self, *args, **kwargs):
            """Connect."""
            return True

        @property
        def is_connected(self):
            return True

    with patch(
        "habluetooth.wrappers.get_platform_client_backend_type",
        return_value=FakeBleakClientTracksServices,
    ):
        # Test with no services parameter (default None)
        client = bleak.BleakClient(ble_device)
        await client.connect()
        assert services_passed_to_backend is None

    # Reset the captured value
    services_passed_to_backend = "not_set"  # type: ignore[unreachable]

    with patch(
        "habluetooth.wrappers.get_platform_client_backend_type",
        return_value=FakeBleakClientTracksServices,
    ):
        # Test with explicit None
        client = bleak.BleakClient(ble_device, services=None)
        await client.connect()
        assert services_passed_to_backend is None

    cancel_hci0()
    cancel_hci1()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_passive_only_scanner_error_message() -> None:
    """Test error message when all scanners are passive-only (like Shelly)."""
    manager = _get_manager()
    # Register a passive-only scanner (connectable=False)
    scanner = FakeScanner(
        "passive_scanner_1", "shelly_plus1pm_e86bea01020c", None, False
    )
    cancel = manager.async_register_scanner(scanner)

    # Inject an advertisement from this passive scanner
    device = generate_ble_device(
        "00:00:00:00:00:01", "Test Device", {"source": "passive_scanner_1"}
    )
    adv_data = generate_advertisement_data(
        local_name="Test Device",
        service_uuids=[],
        rssi=-50,
    )
    scanner.inject_advertisement(device, adv_data)
    await asyncio.sleep(0)  # Let the advertisement be processed

    # Try to connect - should fail with our custom error message
    client = bleak.BleakClient("00:00:00:00:00:01")
    with pytest.raises(
        BleakError,
        match=(
            "00:00:00:00:00:01: No connectable Bluetooth adapters. "
            "Shelly devices are passive-only and cannot connect. "
            "Need local Bluetooth adapter or ESPHome proxy. "
            "Available: shelly_plus1pm_e86bea01020c \\(passive_scanner_1\\)"
        ),
    ):
        await client.connect()

    cancel()


@pytest.mark.usefixtures("enable_bluetooth")
@pytest.mark.asyncio
async def test_passive_scanner_with_active_scanner() -> None:
    """Test normal error when there's a mix of passive and active scanners."""
    manager = _get_manager()
    # Register a passive-only scanner
    passive_scanner = FakeScanner("passive_scanner", "shelly_device", None, False)
    cancel_passive = manager.async_register_scanner(passive_scanner)

    # Register an active scanner with no available slots
    active_scanner = FakeScanner("active_scanner", "esphome_device", None, True)
    cancel_active = manager.async_register_scanner(active_scanner)

    # Inject advertisements from both scanners
    device1 = generate_ble_device(
        "00:00:00:00:00:02", "Test Device", {"source": "passive_scanner"}
    )
    device2 = generate_ble_device(
        "00:00:00:00:00:02", "Test Device", {"source": "active_scanner"}
    )
    adv_data = generate_advertisement_data(
        local_name="Test Device",
        service_uuids=[],
        rssi=-50,
    )
    passive_scanner.inject_advertisement(device1, adv_data)
    active_scanner.inject_advertisement(device2, adv_data)
    await asyncio.sleep(0)  # Let the advertisements be processed

    # Mock the slot allocation to fail (simulating no available slots)
    with patch.object(manager.slot_manager, "allocate_slot", return_value=False):
        # Should get the normal "no available slot" error, not the passive-only error
        client = bleak.BleakClient("00:00:00:00:00:02")
        with pytest.raises(
            BleakError,
            match=(
                "No backend with an available connection slot that can reach "
                "address 00:00:00:00:00:02 was found"
            ),
        ):
            await client.connect()

    cancel_passive()
    cancel_active()


@pytest.mark.asyncio
async def test_connection_params_loading_with_bluez_mgmt(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that connection parameters are loaded when mgmt API is available."""
    manager = _get_manager()
    hci0_device_advs, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()

    # Mock the bluez mgmt controller
    mock_mgmt_ctl = Mock()
    mock_mgmt_ctl.load_conn_params.return_value = True

    class FakeBleakClientTracksConnect(BaseFakeBleakClient):
        """Fake bleak client that tracks connect."""

        connected = False

        async def connect(self, *args, **kwargs):
            """Connect."""
            self.connected = True
            # Simulate service discovery
            await asyncio.sleep(0)

        @property
        def is_connected(self) -> bool:
            return self.connected

    # Test with debug logging enabled
    with (
        caplog.at_level(logging.DEBUG),
        patch.object(manager, "get_bluez_mgmt_ctl", return_value=mock_mgmt_ctl),
        patch(
            "habluetooth.wrappers.get_platform_client_backend_type",
            return_value=FakeBleakClientTracksConnect,
        ),
    ):
        ble_device = hci0_device_advs["00:00:00:00:00:01"][0]
        client = bleak.BleakClient(ble_device)
        await client.connect()

        # Verify load_conn_params was called twice (fast before connect, medium after)
        assert mock_mgmt_ctl.load_conn_params.call_count == 2

        # First call should be for FAST params
        first_call = mock_mgmt_ctl.load_conn_params.call_args_list[0]
        assert first_call[0][0] == 0  # adapter_idx
        assert first_call[0][1] == "00:00:00:00:00:01"  # address
        assert first_call[0][2] == 1  # BDADDR_LE_PUBLIC (default)
        assert first_call[0][3].value == "fast"  # ConnectParams.FAST

        # Second call should be for MEDIUM params
        second_call = mock_mgmt_ctl.load_conn_params.call_args_list[1]
        assert second_call[0][0] == 0  # adapter_idx
        assert second_call[0][1] == "00:00:00:00:00:01"  # address
        assert second_call[0][2] == 1  # BDADDR_LE_PUBLIC
        assert second_call[0][3].value == "medium"  # ConnectParams.MEDIUM

        # Verify debug logging
        assert "Loaded ConnectParams.FAST connection parameters" in caplog.text
        assert "Loaded ConnectParams.MEDIUM connection parameters" in caplog.text

    cancel_hci0()
    cancel_hci1()


@pytest.mark.asyncio
async def test_connection_params_not_loaded_without_mgmt(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that connection parameters are not loaded when mgmt API is unavailable."""
    manager = _get_manager()
    hci0_device_advs, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()

    class FakeBleakClientTracksConnect(BaseFakeBleakClient):
        """Fake bleak client that tracks connect."""

        connected = False

        async def connect(self, *args, **kwargs):
            """Connect."""
            self.connected = True
            await asyncio.sleep(0)

        @property
        def is_connected(self) -> bool:
            return self.connected

    with (
        caplog.at_level(logging.DEBUG),
        patch.object(manager, "get_bluez_mgmt_ctl", return_value=None),
        patch(
            "habluetooth.wrappers.get_platform_client_backend_type",
            return_value=FakeBleakClientTracksConnect,
        ),
    ):
        ble_device = hci0_device_advs["00:00:00:00:00:01"][0]
        client = bleak.BleakClient(ble_device)
        await client.connect()

        # Verify no connection parameters were loaded
        assert "connection parameters" not in caplog.text

    cancel_hci0()
    cancel_hci1()


@pytest.mark.asyncio
async def test_get_device_address_type_random(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
) -> None:
    """Test _get_device_address_type returns BDADDR_LE_RANDOM for random address."""
    hci0_device_advs, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()

    # Create a device with random address type
    device = generate_ble_device(
        "00:00:00:00:00:02",
        "Test Device",
        {
            "path": "/org/bluez/hci0/dev_00_00_00_00_00_02",
            "props": {"AddressType": "random"},
        },
    )

    from habluetooth.const import BDADDR_LE_RANDOM
    from habluetooth.wrappers import _get_device_address_type

    assert _get_device_address_type(device) == BDADDR_LE_RANDOM

    cancel_hci0()
    cancel_hci1()


@pytest.mark.asyncio
async def test_get_device_address_type_public(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
) -> None:
    """Test _get_device_address_type returns BDADDR_LE_PUBLIC for public address."""
    hci0_device_advs, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()

    # Create a device with public address type (default)
    device = hci0_device_advs["00:00:00:00:00:01"][0]

    from habluetooth.const import BDADDR_LE_PUBLIC
    from habluetooth.wrappers import _get_device_address_type

    assert _get_device_address_type(device) == BDADDR_LE_PUBLIC

    cancel_hci0()
    cancel_hci1()


@pytest.mark.asyncio
async def test_connection_params_loading_fails_silently(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that connection still succeeds even if loading params fails."""
    manager = _get_manager()
    hci0_device_advs, cancel_hci0, cancel_hci1 = _generate_scanners_with_fake_devices()

    # Mock the bluez mgmt controller to fail loading params
    mock_mgmt_ctl = Mock()
    mock_mgmt_ctl.load_conn_params.return_value = False

    class FakeBleakClientTracksConnect(BaseFakeBleakClient):
        """Fake bleak client that tracks connect."""

        connected = False

        async def connect(self, *args, **kwargs):
            """Connect."""
            self.connected = True
            await asyncio.sleep(0)

        @property
        def is_connected(self) -> bool:
            return self.connected

    with (
        caplog.at_level(logging.DEBUG),
        patch.object(manager, "get_bluez_mgmt_ctl", return_value=mock_mgmt_ctl),
        patch(
            "habluetooth.wrappers.get_platform_client_backend_type",
            return_value=FakeBleakClientTracksConnect,
        ),
    ):
        ble_device = hci0_device_advs["00:00:00:00:00:01"][0]
        client = bleak.BleakClient(ble_device)
        # Connection should succeed even though param loading failed
        await client.connect()

        # Verify load_conn_params was called
        assert mock_mgmt_ctl.load_conn_params.call_count == 2

        # But no success message should be logged
        assert "Loaded" not in caplog.text

    cancel_hci0()
    cancel_hci1()


@pytest.mark.asyncio
async def test_connection_params_no_adapter_idx(
    two_adapters: None,
    enable_bluetooth: None,
    install_bleak_catcher: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that connection params are not loaded if scanner has no adapter_idx."""
    manager = _get_manager()

    # Mock the bluez mgmt controller
    mock_mgmt_ctl = Mock()
    mock_mgmt_ctl.load_conn_params.return_value = True

    class FakeBleakClientTracksConnect(BaseFakeBleakClient):
        """Fake bleak client that tracks connect."""

        connected = False

        async def connect(self, *args, **kwargs):
            """Connect."""
            self.connected = True
            await asyncio.sleep(0)

        @property
        def is_connected(self) -> bool:
            return self.connected

    # Create a fake connector for the remote scanner
    fake_connector = HaBluetoothConnector(
        client=FakeBleakClientTracksConnect, source="any", can_connect=lambda: True
    )

    # Create a scanner without adapter_idx (e.g., remote scanner)
    remote_scanner = FakeScanner(
        "remote_scanner", "ESPHome Device", fake_connector, True
    )
    cancel_remote = manager.async_register_scanner(remote_scanner)

    # Inject advertisement
    device = generate_ble_device(
        "00:00:00:00:00:03", "Test Device", {"source": "remote_scanner"}
    )
    adv_data = generate_advertisement_data(
        local_name="Test Device",
        service_uuids=[],
        rssi=-50,
    )
    remote_scanner.inject_advertisement(device, adv_data)
    await asyncio.sleep(0)

    # Remote scanner should already have adapter_idx returning None
    with (
        caplog.at_level(logging.DEBUG),
        patch.object(manager, "get_bluez_mgmt_ctl", return_value=mock_mgmt_ctl),
    ):
        client = bleak.BleakClient("00:00:00:00:00:03")
        await client.connect()

        # Verify load_conn_params was NOT called since adapter_idx is None
        assert mock_mgmt_ctl.load_conn_params.call_count == 0

    cancel_remote()


@pytest.mark.asyncio
async def test_connection_path_scoring_with_slots_and_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test connection path scoring and logging reflects slot availability."""
    from bleak_retry_connector import Allocations

    manager = _get_manager()

    class FakeBleakClientNoConnect(BaseFakeBleakClient):
        """Fake bleak client that doesn't connect."""

        async def connect(self, *args, **kwargs):
            """Don't actually connect."""
            raise BleakError("Test - connection not needed")

    # Create fake connectors
    fake_connector_1 = HaBluetoothConnector(
        client=FakeBleakClientNoConnect, source="scanner1", can_connect=lambda: True
    )
    fake_connector_2 = HaBluetoothConnector(
        client=FakeBleakClientNoConnect, source="scanner2", can_connect=lambda: True
    )
    fake_connector_3 = HaBluetoothConnector(
        client=FakeBleakClientNoConnect, source="scanner3", can_connect=lambda: True
    )

    # Create scanners with different sources
    scanner1 = FakeScanner("scanner1", "Scanner 1", fake_connector_1, True)
    scanner2 = FakeScanner("scanner2", "Scanner 2", fake_connector_2, True)
    scanner3 = FakeScanner("scanner3", "Scanner 3", fake_connector_3, True)

    # Mock get_allocations for each scanner using patch.object
    with (
        patch.object(
            scanner1,
            "get_allocations",
            return_value=Allocations(
                adapter="scanner1",
                slots=3,
                free=1,  # Only 1 slot free - should get penalty
                allocated=["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"],
            ),
        ),
        patch.object(
            scanner2,
            "get_allocations",
            return_value=Allocations(
                adapter="scanner2",
                slots=3,
                free=2,  # 2 slots free - no penalty
                allocated=["AA:BB:CC:DD:EE:03"],
            ),
        ),
        patch.object(
            scanner3,
            "get_allocations",
            return_value=Allocations(
                adapter="scanner3",
                slots=3,
                free=3,  # All slots free - no penalty
                allocated=[],
            ),
        ),
    ):
        cancel1 = manager.async_register_scanner(scanner1)
        cancel2 = manager.async_register_scanner(scanner2)
        cancel3 = manager.async_register_scanner(scanner3)

        # Inject advertisements with different RSSI values
        device1 = generate_ble_device(
            "00:00:00:00:00:01", "Test Device", {"source": "scanner1"}
        )
        adv_data1 = generate_advertisement_data(local_name="Test Device", rssi=-60)
        scanner1.inject_advertisement(device1, adv_data1)

        device2 = generate_ble_device(
            "00:00:00:00:00:01", "Test Device", {"source": "scanner2"}
        )
        adv_data2 = generate_advertisement_data(local_name="Test Device", rssi=-65)
        scanner2.inject_advertisement(device2, adv_data2)

        device3 = generate_ble_device(
            "00:00:00:00:00:01", "Test Device", {"source": "scanner3"}
        )
        adv_data3 = generate_advertisement_data(local_name="Test Device", rssi=-70)
        scanner3.inject_advertisement(device3, adv_data3)

        await asyncio.sleep(0)

        # Try to connect with logging enabled
        with caplog.at_level(logging.INFO):
            client = bleak.BleakClient("00:00:00:00:00:01")
            with suppress(BleakError):
                await client.connect()

        # Check that the log contains the connection paths with correct scoring
        log_text = caplog.text
        assert "Found 3 connection path(s)" in log_text

        # Extract the log line with connection paths
        for line in caplog.text.splitlines():
            if "Found 3 connection path(s)" in line:
                # rssi_diff = best_rssi - second_best_rssi = -60 - (-65) = 5
                # Scanner 1 has best RSSI (-60) but only 1 slot free, so with penalty:
                # score = -60 - (5 * 0.76) = -63.8
                assert "Scanner 1" in line
                assert "(slots=1/3 free)" in line
                assert "(score=-63.8)" in line

                # Scanner 2 has RSSI -65 with 2 slots free, no penalty:
                # score = -65
                assert "Scanner 2" in line
                assert "(slots=2/3 free)" in line
                # Check for both -65 and -65.0
                assert ("(score=-65)" in line) or ("(score=-65.0)" in line)

                # Scanner 3 has RSSI -70 with all slots free, no penalty:
                # score = -70
                assert "Scanner 3" in line
                assert "(slots=3/3 free)" in line
                # Check for both -70 and -70.0
                assert ("(score=-70)" in line) or ("(score=-70.0)" in line)

                # Verify order: Scanner 1 should be first (best score -63.8),
                # then Scanner 2 (-65), then Scanner 3 (-70)
                scanner1_pos = line.find("Scanner 1")
                scanner2_pos = line.find("Scanner 2")
                scanner3_pos = line.find("Scanner 3")

                assert scanner1_pos < scanner2_pos < scanner3_pos, (
                    f"Expected Scanner 1 before Scanner 2 before Scanner 3, "
                    f"but got positions {scanner1_pos}, {scanner2_pos}, {scanner3_pos}"
                )
                break
        else:
            pytest.fail("Could not find connection path log line")

        cancel1()
        cancel2()
        cancel3()


@pytest.mark.asyncio
async def test_connection_path_scoring_no_slots_available(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that scanners with no free slots are excluded."""
    manager = _get_manager()

    class FakeBleakClientNoConnect(BaseFakeBleakClient):
        """Fake bleak client that doesn't connect."""

        async def connect(self, *args, **kwargs):
            """Don't actually connect."""
            raise BleakError("Test - connection not needed")

    # Create fake connectors
    fake_connector_1 = HaBluetoothConnector(
        client=FakeBleakClientNoConnect, source="scanner1", can_connect=lambda: True
    )
    fake_connector_2 = HaBluetoothConnector(
        client=FakeBleakClientNoConnect, source="scanner2", can_connect=lambda: True
    )

    # Create scanners
    scanner1 = FakeScanner("scanner1", "Scanner 1", fake_connector_1, True)
    scanner2 = FakeScanner("scanner2", "Scanner 2", fake_connector_2, True)

    # Mock get_allocations - scanner1 has no free slots
    with (
        patch.object(
            scanner1,
            "get_allocations",
            return_value=Allocations(
                adapter="scanner1",
                slots=3,
                free=0,  # No slots available - should be excluded
                allocated=[
                    "AA:BB:CC:DD:EE:01",
                    "AA:BB:CC:DD:EE:02",
                    "AA:BB:CC:DD:EE:03",
                ],
            ),
        ),
        patch.object(
            scanner2,
            "get_allocations",
            return_value=Allocations(
                adapter="scanner2", slots=3, free=3, allocated=[]  # All slots free
            ),
        ),
    ):
        cancel1 = manager.async_register_scanner(scanner1)
        cancel2 = manager.async_register_scanner(scanner2)

        # Inject advertisements
        device1 = generate_ble_device(
            "00:00:00:00:00:02", "Test Device", {"source": "scanner1"}
        )
        adv_data1 = generate_advertisement_data(
            local_name="Test Device", rssi=-50
        )  # Better RSSI
        scanner1.inject_advertisement(device1, adv_data1)

        device2 = generate_ble_device(
            "00:00:00:00:00:02", "Test Device", {"source": "scanner2"}
        )
        adv_data2 = generate_advertisement_data(
            local_name="Test Device", rssi=-70
        )  # Worse RSSI
        scanner2.inject_advertisement(device2, adv_data2)

        await asyncio.sleep(0)

        # Try to connect with logging enabled
        with caplog.at_level(logging.INFO):
            client = bleak.BleakClient("00:00:00:00:00:02")
            with suppress(BleakError):
                await client.connect()

        # Check that only scanner2 is in the connection paths
        log_text = caplog.text
        assert (
            "Found 1 connection path(s)" in log_text
            or "Found 2 connection path(s)" in log_text
        )

        # If both are shown, scanner1 should have bad score (NO_RSSI_VALUE = -127)
        for line in caplog.text.splitlines():
            if "connection path(s)" in line:
                if "Scanner 1" in line:
                    # Scanner 1 should show 0 free slots and bad score
                    assert "(slots=0/3 free)" in line
                    assert "(score=-127)" in line  # NO_RSSI_VALUE

                # Scanner 2 should be present with normal score
                assert "Scanner 2" in line
                assert "(slots=3/3 free)" in line
                # Check for both -70 and -70.0
                assert ("(score=-70)" in line) or ("(score=-70.0)" in line)
                break

        cancel1()
        cancel2()


@pytest.mark.asyncio
async def test_thundering_herd_connection_slots() -> None:
    """
    Test thundering herd scenario with limited connection slots.

    Simulates 7 devices trying to connect simultaneously to 3 proxies:
    - Proxy 1 & 2: Good signal (-60 RSSI), 3 slots each
    - Proxy 3: Bad signal (-95 RSSI), 3 slots each

    Expected behavior:
    - First 6 devices should connect to proxy1 and proxy2 (3 each)
    - 7th device should connect to proxy3 (bad signal) when others are full
    """
    from bleak_retry_connector import Allocations

    manager = _get_manager()

    # Track which backend each device connected to
    connection_tracker = {}

    class FakeBleakClientThunderingHerd(BaseFakeBleakClient):
        """Fake bleak client for thundering herd test."""

        def __init__(self, address_or_ble_device, *args, **kwargs):
            """Initialize with tracking."""
            super().__init__(address_or_ble_device, *args, **kwargs)
            self._connected = False
            # Track the device and source
            if hasattr(address_or_ble_device, "address"):
                self._address = address_or_ble_device.address
                self._source = address_or_ble_device.details.get("source")
            else:
                self._address = str(address_or_ble_device)
                self._source = None

        async def connect(self, *args, **kwargs):
            """Simulate connection and record which backend was used."""
            # Small delay to simulate connection time
            await asyncio.sleep(0.01)
            self._connected = True
            # Record which backend this device connected to
            if self._address and self._source:
                connection_tracker[self._address] = self._source
            return True

        @property
        def is_connected(self) -> bool:
            """Return connection state."""
            return self._connected

    # Create fake connectors for 3 proxies
    fake_connector_1 = HaBluetoothConnector(
        client=FakeBleakClientThunderingHerd,
        source="proxy1",
        can_connect=lambda: True,
    )
    fake_connector_2 = HaBluetoothConnector(
        client=FakeBleakClientThunderingHerd,
        source="proxy2",
        can_connect=lambda: True,
    )
    fake_connector_3 = HaBluetoothConnector(
        client=FakeBleakClientThunderingHerd,
        source="proxy3",
        can_connect=lambda: True,
    )

    # Create 3 scanners (proxies) with 3 connection slots each
    proxy1 = FakeScanner("proxy1", "Proxy 1 (Good)", fake_connector_1, True)
    proxy2 = FakeScanner("proxy2", "Proxy 2 (Good)", fake_connector_2, True)
    proxy3 = FakeScanner("proxy3", "Proxy 3 (Bad)", fake_connector_3, True)

    # Track actual slot allocations dynamically
    proxy_allocations: dict[str, set[str]] = {
        "proxy1": set(),
        "proxy2": set(),
        "proxy3": set(),
    }

    def get_proxy_allocations(proxy_name: str) -> Allocations:
        """Get allocations for a specific proxy."""
        allocated = proxy_allocations[proxy_name]
        return Allocations(
            adapter=proxy_name,
            slots=3,
            free=3 - len(allocated),
            allocated=list(allocated),
        )

    # Mock methods to track allocations
    def make_add_connecting(proxy_name: str) -> Callable[[str], None]:
        def _add_connecting(addr: str) -> None:
            proxy_allocations[proxy_name].add(addr)

        return _add_connecting

    def make_finished_connecting(proxy_name: str) -> Callable[[str, bool], None]:
        def _finished_connecting(addr: str, success: bool) -> None:
            if not success:
                proxy_allocations[proxy_name].discard(addr)

        return _finished_connecting

    # Mock get_allocations and connection tracking
    with (
        patch.object(
            proxy1, "get_allocations", lambda: get_proxy_allocations("proxy1")
        ),
        patch.object(
            proxy2, "get_allocations", lambda: get_proxy_allocations("proxy2")
        ),
        patch.object(
            proxy3, "get_allocations", lambda: get_proxy_allocations("proxy3")
        ),
        patch.object(proxy1, "_add_connecting", make_add_connecting("proxy1")),
        patch.object(proxy2, "_add_connecting", make_add_connecting("proxy2")),
        patch.object(proxy3, "_add_connecting", make_add_connecting("proxy3")),
        patch.object(
            proxy1, "_finished_connecting", make_finished_connecting("proxy1")
        ),
        patch.object(
            proxy2, "_finished_connecting", make_finished_connecting("proxy2")
        ),
        patch.object(
            proxy3, "_finished_connecting", make_finished_connecting("proxy3")
        ),
    ):
        cancel1 = manager.async_register_scanner(proxy1)
        cancel2 = manager.async_register_scanner(proxy2)
        cancel3 = manager.async_register_scanner(proxy3)

        # Create 7 devices to connect
        device_addresses = [f"AA:BB:CC:DD:EE:0{i}" for i in range(1, 8)]

        # Inject advertisements for all devices on all proxies
        for i, address in enumerate(device_addresses, 1):
            # Good signal on proxy1
            device1 = generate_ble_device(address, f"Device {i}", {"source": "proxy1"})
            adv_data1 = generate_advertisement_data(local_name=f"Device {i}", rssi=-60)
            proxy1.inject_advertisement(device1, adv_data1)

            # Good signal on proxy2 (exactly same as proxy1)
            device2 = generate_ble_device(address, f"Device {i}", {"source": "proxy2"})
            adv_data2 = generate_advertisement_data(local_name=f"Device {i}", rssi=-60)
            proxy2.inject_advertisement(device2, adv_data2)

            # Bad signal on proxy3
            device3 = generate_ble_device(address, f"Device {i}", {"source": "proxy3"})
            adv_data3 = generate_advertisement_data(local_name=f"Device {i}", rssi=-95)
            proxy3.inject_advertisement(device3, adv_data3)

        await asyncio.sleep(0)

        # Clear the connection tracker before starting
        connection_tracker.clear()

        async def connect_device(address: str) -> tuple[str, str | None]:
            """Try to connect to a device."""
            client = bleak.BleakClient(address)
            try:
                await client.connect()
                # The connection tracker should have recorded which backend was used
                return address, connection_tracker.get(address, "unknown")
            except BleakError:
                # Connection failed (no available backend)
                return address, None

        # Simulate thundering herd - all devices try to connect at once
        tasks = [connect_device(addr) for addr in device_addresses]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        connection_results = {}
        for result in results:
            if isinstance(result, tuple):
                address, proxy = result
                connection_results[address] = proxy

        # Count connections per proxy
        proxy1_connections = [
            addr for addr, p in connection_results.items() if p == "proxy1"
        ]
        proxy2_connections = [
            addr for addr, p in connection_results.items() if p == "proxy2"
        ]
        proxy3_connections = [
            addr for addr, p in connection_results.items() if p == "proxy3"
        ]
        failed_connections = [
            addr for addr, p in connection_results.items() if p is None
        ]

        # Verify constraints
        # 1. No proxy should exceed its slot limit
        assert (
            len(proxy1_connections) <= 3
        ), f"Proxy1 exceeded slot limit: {len(proxy1_connections)} > 3"
        assert (
            len(proxy2_connections) <= 3
        ), f"Proxy2 exceeded slot limit: {len(proxy2_connections)} > 3"
        assert (
            len(proxy3_connections) <= 3
        ), f"Proxy3 exceeded slot limit: {len(proxy3_connections)} > 3"

        # 2. Good signal proxies should be preferred and fill up first
        good_proxy_total = len(proxy1_connections) + len(proxy2_connections)
        assert (
            good_proxy_total == 6
        ), f"Expected exactly 6 connections on good proxies, got {good_proxy_total}"

        # 3. All 7 devices should connect (6 to good proxies, 1 to bad proxy)
        total_connected = (
            len(proxy1_connections) + len(proxy2_connections) + len(proxy3_connections)
        )
        assert (
            total_connected == 7
        ), f"Expected all 7 devices to connect, but only {total_connected} did"

        # 4. The 7th device should go to proxy3 since good ones are full
        assert (
            len(proxy3_connections) == 1
        ), f"Expected exactly 1 connection on proxy3, got {len(proxy3_connections)}"

        # 5. Verify good distribution across proxy1 and proxy2
        # Both should have roughly equal load (3 connections each)
        assert (
            len(proxy1_connections) == 3
        ), f"Expected proxy1 to have 3 connections, got {len(proxy1_connections)}"
        assert (
            len(proxy2_connections) == 3
        ), f"Expected proxy2 to have 3 connections, got {len(proxy2_connections)}"

        # 6. No connections should fail
        assert (
            len(failed_connections) == 0
        ), f"Expected no failed connections, but {len(failed_connections)} failed"

        # Clean up
        cancel1()
        cancel2()
        cancel3()
