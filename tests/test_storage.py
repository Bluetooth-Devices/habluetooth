import time
from unittest.mock import ANY

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from habluetooth.storage import (
    DiscoveredDeviceAdvertisementData,
    DiscoveredDeviceAdvertisementDataDict,
    discovered_device_advertisement_data_from_dict,
    discovered_device_advertisement_data_to_dict,
    expire_stale_scanner_discovered_device_advertisement_data,
)


def test_discovered_device_advertisement_data_to_dict():
    """Test discovered device advertisement data to dict."""
    result = discovered_device_advertisement_data_to_dict(
        DiscoveredDeviceAdvertisementData(
            True,
            100,
            {
                "AA:BB:CC:DD:EE:FF": (
                    BLEDevice(
                        address="AA:BB:CC:DD:EE:FF",
                        name="Test Device",
                        details={"details": "test"},
                        rssi=-50,
                    ),
                    AdvertisementData(
                        local_name="Test Device",
                        manufacturer_data={0x004C: b"\x02\x15\xaa\xbb\xcc\xdd\xee\xff"},
                        tx_power=50,
                        service_data={
                            "0000180d-0000-1000-8000-00805f9b34fb": b"\x00\x00\x00\x00"
                        },
                        service_uuids=["0000180d-0000-1000-8000-00805f9b34fb"],
                        platform_data=("Test Device", ""),
                        rssi=-50,
                    ),
                )
            },
            {"AA:BB:CC:DD:EE:FF": 100000},
        )
    )
    assert result == {
        "connectable": True,
        "discovered_device_advertisement_datas": {
            "AA:BB:CC:DD:EE:FF": {
                "advertisement_data": {
                    "local_name": "Test Device",
                    "manufacturer_data": {"76": "0215aabbccddeeff"},
                    "rssi": -50,
                    "service_data": {
                        "0000180d-0000-1000-8000-00805f9b34fb": "00000000"
                    },
                    "service_uuids": ["0000180d-0000-1000-8000-00805f9b34fb"],
                    "tx_power": 50,
                    "platform_data": ["Test Device", ""],
                },
                "device": {
                    "address": "AA:BB:CC:DD:EE:FF",
                    "details": {"details": "test"},
                    "name": "Test Device",
                    "rssi": -50,
                },
            }
        },
        "discovered_device_timestamps": {"AA:BB:CC:DD:EE:FF": ANY},
        "expire_seconds": 100,
    }


def test_discovered_device_advertisement_data_from_dict():
    now = time.time()
    result = discovered_device_advertisement_data_from_dict(
        {
            "connectable": True,
            "discovered_device_advertisement_datas": {
                "AA:BB:CC:DD:EE:FF": {
                    "advertisement_data": {
                        "local_name": "Test Device",
                        "manufacturer_data": {"76": "0215aabbccddeeff"},
                        "rssi": -50,
                        "service_data": {
                            "0000180d-0000-1000-8000-00805f9b34fb": "00000000"
                        },
                        "service_uuids": ["0000180d-0000-1000-8000-00805f9b34fb"],
                        "tx_power": 50,
                        "platform_data": ["Test Device", ""],
                    },
                    "device": {
                        "address": "AA:BB:CC:DD:EE:FF",
                        "details": {"details": "test"},
                        "name": "Test Device",
                        "rssi": -50,
                    },
                }
            },
            "discovered_device_timestamps": {"AA:BB:CC:DD:EE:FF": now},
            "expire_seconds": 100,
        }
    )

    expected_ble_device = BLEDevice(
        address="AA:BB:CC:DD:EE:FF",
        name="Test Device",
        details={"details": "test"},
        rssi=-50,
    )

    expected_advertisement_data = AdvertisementData(
        local_name="Test Device",
        manufacturer_data={0x004C: b"\x02\x15\xaa\xbb\xcc\xdd\xee\xff"},
        tx_power=50,
        service_data={"0000180d-0000-1000-8000-00805f9b34fb": b"\x00\x00\x00\x00"},
        service_uuids=["0000180d-0000-1000-8000-00805f9b34fb"],
        platform_data=("Test Device", ""),
        rssi=-50,
    )
    assert result is not None
    out_ble_device = result.discovered_device_advertisement_datas["AA:BB:CC:DD:EE:FF"][
        0
    ]
    out_advertisement_data = result.discovered_device_advertisement_datas[
        "AA:BB:CC:DD:EE:FF"
    ][1]
    assert out_ble_device.address == expected_ble_device.address
    assert out_ble_device.name == expected_ble_device.name
    assert out_ble_device.details == expected_ble_device.details
    assert out_ble_device.rssi == expected_ble_device.rssi
    assert out_ble_device.metadata == expected_ble_device.metadata
    assert out_advertisement_data == expected_advertisement_data

    assert result == DiscoveredDeviceAdvertisementData(
        connectable=True,
        expire_seconds=100,
        discovered_device_advertisement_datas={
            "AA:BB:CC:DD:EE:FF": (
                ANY,
                expected_advertisement_data,
            )
        },
        discovered_device_timestamps={"AA:BB:CC:DD:EE:FF": ANY},
    )


def test_expire_stale_scanner_discovered_device_advertisement_data():
    """Test expire_stale_scanner_discovered_device_advertisement_data."""
    now = time.time()
    data = {
        "myscanner": DiscoveredDeviceAdvertisementDataDict(
            {
                "connectable": True,
                "discovered_device_advertisement_datas": {
                    "AA:BB:CC:DD:EE:FF": {
                        "advertisement_data": {
                            "local_name": "Test Device",
                            "manufacturer_data": {"76": "0215aabbccddeeff"},
                            "rssi": -50,
                            "service_data": {
                                "0000180d-0000-1000-8000-00805f9b34fb": "00000000"
                            },
                            "service_uuids": ["0000180d-0000-1000-8000-00805f9b34fb"],
                            "tx_power": 50,
                            "platform_data": ["Test Device", ""],
                        },
                        "device": {
                            "address": "AA:BB:CC:DD:EE:FF",
                            "details": {"details": "test"},
                            "name": "Test Device",
                            "rssi": -50,
                        },
                    },
                    "CC:DD:EE:FF:AA:BB": {
                        "advertisement_data": {
                            "local_name": "Test Device Expired",
                            "manufacturer_data": {"76": "0215aabbccddeeff"},
                            "rssi": -50,
                            "service_data": {
                                "0000180d-0000-1000-8000-00805f9b34fb": "00000000"
                            },
                            "service_uuids": ["0000180d-0000-1000-8000-00805f9b34fb"],
                            "tx_power": 50,
                            "platform_data": ["Test Device", ""],
                        },
                        "device": {
                            "address": "CC:DD:EE:FF:AA:BB",
                            "details": {"details": "test"},
                            "name": "Test Device Expired",
                            "rssi": -50,
                        },
                    },
                },
                "discovered_device_timestamps": {
                    "AA:BB:CC:DD:EE:FF": now,
                    "CC:DD:EE:FF:AA:BB": now - 100,
                },
                "expire_seconds": 100,
            }
        ),
        "all_expired": DiscoveredDeviceAdvertisementDataDict(
            {
                "connectable": True,
                "discovered_device_advertisement_datas": {
                    "CC:DD:EE:FF:AA:BB": {
                        "advertisement_data": {
                            "local_name": "Test Device Expired",
                            "manufacturer_data": {"76": "0215aabbccddeeff"},
                            "rssi": -50,
                            "service_data": {
                                "0000180d-0000-1000-8000-00805f9b34fb": "00000000"
                            },
                            "service_uuids": ["0000180d-0000-1000-8000-00805f9b34fb"],
                            "tx_power": 50,
                            "platform_data": ["Test Device", ""],
                        },
                        "device": {
                            "address": "CC:DD:EE:FF:AA:BB",
                            "details": {"details": "test"},
                            "name": "Test Device Expired",
                            "rssi": -50,
                        },
                    }
                },
                "discovered_device_timestamps": {"CC:DD:EE:FF:AA:BB": now - 100},
                "expire_seconds": 100,
            }
        ),
    }
    expire_stale_scanner_discovered_device_advertisement_data(data)
    assert len(data["myscanner"]["discovered_device_advertisement_datas"]) == 1
    assert (
        "CC:DD:EE:FF:AA:BB"
        not in data["myscanner"]["discovered_device_advertisement_datas"]
    )
    assert "all_expired" not in data


def test_expire_future_discovered_device_advertisement_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test test_expire_future_discovered_device_advertisement_data."""
    now = time.time()
    data = {
        "myscanner": DiscoveredDeviceAdvertisementDataDict(
            {
                "connectable": True,
                "discovered_device_advertisement_datas": {
                    "AA:BB:CC:DD:EE:FF": {
                        "advertisement_data": {
                            "local_name": "Test Device",
                            "manufacturer_data": {"76": "0215aabbccddeeff"},
                            "rssi": -50,
                            "service_data": {
                                "0000180d-0000-1000-8000-00805f9b34fb": "00000000"
                            },
                            "service_uuids": ["0000180d-0000-1000-8000-00805f9b34fb"],
                            "tx_power": 50,
                            "platform_data": ["Test Device", ""],
                        },
                        "device": {
                            "address": "AA:BB:CC:DD:EE:FF",
                            "details": {"details": "test"},
                            "name": "Test Device",
                            "rssi": -50,
                        },
                    },
                    "CC:DD:EE:FF:AA:BB": {
                        "advertisement_data": {
                            "local_name": "Test Device Expired",
                            "manufacturer_data": {"76": "0215aabbccddeeff"},
                            "rssi": -50,
                            "service_data": {
                                "0000180d-0000-1000-8000-00805f9b34fb": "00000000"
                            },
                            "service_uuids": ["0000180d-0000-1000-8000-00805f9b34fb"],
                            "tx_power": 50,
                            "platform_data": ["Test Device", ""],
                        },
                        "device": {
                            "address": "CC:DD:EE:FF:AA:BB",
                            "details": {"details": "test"},
                            "name": "Test Device Expired",
                            "rssi": -50,
                        },
                    },
                },
                "discovered_device_timestamps": {
                    "AA:BB:CC:DD:EE:FF": now,
                    "CC:DD:EE:FF:AA:BB": now - 100,
                },
                "expire_seconds": 100,
            }
        ),
        "all_future": DiscoveredDeviceAdvertisementDataDict(
            {
                "connectable": True,
                "discovered_device_advertisement_datas": {
                    "CC:DD:EE:FF:AA:BB": {
                        "advertisement_data": {
                            "local_name": "Test Device Expired",
                            "manufacturer_data": {"76": "0215aabbccddeeff"},
                            "rssi": -50,
                            "service_data": {
                                "0000180d-0000-1000-8000-00805f9b34fb": "00000000"
                            },
                            "service_uuids": ["0000180d-0000-1000-8000-00805f9b34fb"],
                            "tx_power": 50,
                            "platform_data": ["Test Device", ""],
                        },
                        "device": {
                            "address": "CC:DD:EE:FF:AA:BB",
                            "details": {"details": "test"},
                            "name": "Test Device Expired",
                            "rssi": -50,
                        },
                    }
                },
                "discovered_device_timestamps": {"CC:DD:EE:FF:AA:BB": now + 1000000},
                "expire_seconds": 100,
            }
        ),
    }
    expire_stale_scanner_discovered_device_advertisement_data(data)
    assert len(data["myscanner"]["discovered_device_advertisement_datas"]) == 1
    assert (
        "CC:DD:EE:FF:AA:BB"
        not in data["myscanner"]["discovered_device_advertisement_datas"]
    )
    assert "all_future" not in data
    assert (
        "for CC:DD:EE:FF:AA:BB on scanner all_future as it is the future" in caplog.text
    )


def test_discovered_device_advertisement_data_from_dict_corrupt(caplog):
    """Test discovered_device_advertisement_data_from_dict with corrupt data."""
    now = time.time()
    result = discovered_device_advertisement_data_from_dict(
        {
            "connectable": True,
            "discovered_device_advertisement_datas": {
                "AA:BB:CC:DD:EE:FF": {
                    "advertisement_data": {  # type: ignore[typeddict-item]
                        "local_name": "Test Device",
                        "manufacturer_data": {"76": "0215aabbccddeeff"},
                        "rssi": -50,
                        "service_data": {
                            "0000180d-0000-1000-8000-00805f9b34fb": "00000000"
                        },
                        "service_uuids": ["0000180d-0000-1000-8000-00805f9b34fb"],
                    },
                    "device": {  # type: ignore[typeddict-item]
                        "address": "AA:BB:CC:DD:EE:FF",
                        "details": {"details": "test"},
                        "rssi": -50,
                    },
                }
            },
            "discovered_device_timestamps": {"AA:BB:CC:DD:EE:FF": now},
            "expire_seconds": 100,
        }
    )
    assert result is None
    assert "Error deserializing discovered_device_advertisement_data" in caplog.text
