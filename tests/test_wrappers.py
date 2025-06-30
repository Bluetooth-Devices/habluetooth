"""Tests for bluetooth wrappers."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any, Generator
from unittest.mock import patch

import bleak
import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
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
        connector: None,
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
        return None

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
        if "hci1" in ble_device.details["path"]:
            return True
        return False

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
    mock_platform_client_that_fails_to_connect: None,
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

        valid = False

        async def connect(self, *args: Any, **kwargs: Any) -> None:
            """Connect."""
            assert isinstance(self._device, BLEDevice)
            if "/hci0/" in self._device.details["path"]:
                self.valid = False
            else:
                self.valid = True

        @property
        def is_connected(self) -> bool:
            return self.valid

    class FakeBleakClientFailsHCI1Only(BaseFakeBleakClient):
        """Fake bleak client that fails to connect on hci1."""

        valid = False

        async def connect(self, *args: Any, **kwargs: Any) -> None:
            """Connect."""
            assert isinstance(self._device, BLEDevice)
            if "/hci1/" in self._device.details["path"]:
                self.valid = False
            else:
                self.valid = True

        @property
        def is_connected(self) -> bool:
            return self.valid

    with patch(
        "habluetooth.wrappers.get_platform_client_backend_type",
        return_value=FakeBleakClientFailsHCI0Only,
    ):
        # Should try to connect to hci0 first
        await client.connect()
        assert not client.is_connected
        # Should try to connect with hci0 again
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
        assert not client.is_connected
        # Should try to connect with hci0 next
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
        assert await client.connect() is True
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
            return None

        @property
        def is_connected(self) -> bool:
            return True

    with patch(
        "habluetooth.wrappers.get_platform_client_backend_type",
        return_value=FakeBleakClient,
    ):
        assert await client.connect() is True

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
