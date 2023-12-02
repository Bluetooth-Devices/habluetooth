from typing import Any

from habluetooth import BaseHaRemoteScanner, BaseHaScanner, HaBluetoothConnector


class MockBleakClient:
    pass


def test_create_scanner():
    connector = HaBluetoothConnector(MockBleakClient, "any", lambda: True)

    class MockScanner(BaseHaScanner):
        pass

        def discovered_devices_and_advertisement_data(self):
            return []

        def discovered_devices(self):
            return []

    scanner = MockScanner("any", "any", connector)
    assert isinstance(scanner, BaseHaScanner)


def test_create_remote_scanner():
    connector = HaBluetoothConnector(MockBleakClient, "any", lambda: True)

    def callback(data: Any) -> None:
        pass

    scanner = BaseHaRemoteScanner("any", "any", callback, connector, True)
    assert isinstance(scanner, BaseHaRemoteScanner)
