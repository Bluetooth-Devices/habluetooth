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
                    "rssi": -50,  # Now included for backward compatibility
                },
            }
        },
        "discovered_device_timestamps": {"AA:BB:CC:DD:EE:FF": ANY},
        "expire_seconds": 100,
        "discovered_device_raw": {},
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
                    },  # type: ignore[typeddict-item]
                }
            },
            "discovered_device_timestamps": {"AA:BB:CC:DD:EE:FF": now},
            "expire_seconds": 100,
            "discovered_device_raw": {
                "AA:BB:CC:DD:EE:FF": "0215aabbccddeeff",
            },
        }
    )

    expected_ble_device = BLEDevice(
        address="AA:BB:CC:DD:EE:FF",
        name="Test Device",
        details={"details": "test"},
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
    # BLEDevice no longer has rssi attribute in bleak 1.0+
    # rssi is only available in AdvertisementData
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
        discovered_device_raw={
            "AA:BB:CC:DD:EE:FF": b"\x02\x15\xaa\xbb\xcc\xdd\xee\xff"
        },
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
                        },  # type: ignore[typeddict-item]
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
                        },  # type: ignore[typeddict-item]
                    },
                },
                "discovered_device_raw": {},
                "discovered_device_timestamps": {
                    "AA:BB:CC:DD:EE:FF": now,
                    "CC:DD:EE:FF:AA:BB": now - 101,
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
                        },  # type: ignore[typeddict-item]
                    }
                },
                "discovered_device_raw": {},
                "discovered_device_timestamps": {"CC:DD:EE:FF:AA:BB": now - 101},
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
                        },  # type: ignore[typeddict-item]
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
                        },  # type: ignore[typeddict-item]
                    },
                },
                "discovered_device_timestamps": {
                    "AA:BB:CC:DD:EE:FF": now,
                    "CC:DD:EE:FF:AA:BB": now - 101,
                },
                "discovered_device_raw": {},
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
                        },  # type: ignore[typeddict-item]
                    }
                },
                "discovered_device_timestamps": {"CC:DD:EE:FF:AA:BB": now + 1000000},
                "discovered_device_raw": {},
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


def test_expire_stale_scanner_with_divergent_dicts():
    """
    Expire a timestamp with no matching ad/raw entry without raising KeyError.

    The timestamps dict drives expiry; if a corrupt blob has an address there
    that is missing from the companion dicts, expiry must drop the entry rather
    than abort the whole load.
    """
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
                        },  # type: ignore[typeddict-item]
                    },
                },
                "discovered_device_raw": {},
                # "CC:DD:EE:FF:AA:BB" is stale and present only in timestamps —
                # the ad-datas dict above has no matching entry.
                "discovered_device_timestamps": {
                    "AA:BB:CC:DD:EE:FF": now,
                    "CC:DD:EE:FF:AA:BB": now - 101,
                },
                "expire_seconds": 100,
            }
        ),
    }
    # Must not raise KeyError despite the divergent dicts.
    expire_stale_scanner_discovered_device_advertisement_data(data)
    assert "myscanner" in data
    timestamps = data["myscanner"]["discovered_device_timestamps"]
    assert "CC:DD:EE:FF:AA:BB" not in timestamps
    assert "AA:BB:CC:DD:EE:FF" in timestamps
    assert len(data["myscanner"]["discovered_device_advertisement_datas"]) == 1


def test_expire_stale_scanner_with_missing_keys(caplog):
    """
    A scanner blob missing required top-level keys is dropped, not fatal.

    ``expire_stale...`` runs across every scanner before the cache is handed
    to ``..._from_dict``. A single corrupt/partial scanner blob (missing
    ``expire_seconds``/``timestamps``/``ad-datas``) must not raise ``KeyError``
    and abort expiry for the healthy scanners; the bad scanner is discarded
    and the good one survives.
    """
    now = time.time()
    good_scanner = DiscoveredDeviceAdvertisementDataDict(
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
                    },  # type: ignore[typeddict-item]
                },
            },
            "discovered_device_raw": {},
            "discovered_device_timestamps": {"AA:BB:CC:DD:EE:FF": now},
            "expire_seconds": 100,
        }
    )
    data: dict[str, DiscoveredDeviceAdvertisementDataDict] = {
        # Missing "discovered_device_timestamps" (and others) entirely.
        "badscanner": {"connectable": True},  # type: ignore[typeddict-item]
        "goodscanner": good_scanner,
    }
    # Must not raise despite the malformed scanner blob.
    expire_stale_scanner_discovered_device_advertisement_data(data)
    # The malformed scanner is dropped, the healthy one is preserved intact.
    assert "badscanner" not in data
    assert "goodscanner" in data
    assert "AA:BB:CC:DD:EE:FF" in data["goodscanner"]["discovered_device_timestamps"]
    assert "Discarding malformed discovery cache for scanner badscanner" in caplog.text


def test_discovered_device_advertisement_data_from_dict_corrupt(caplog):
    """Shape mismatches log a WARNING and discard the cache without a traceback."""
    now = time.time()
    result = discovered_device_advertisement_data_from_dict(
        {
            "connectable": True,
            "discovered_device_advertisement_datas": {
                "AA:BB:CC:DD:EE:FF": {
                    "advertisement_data": {
                        "local_name": "Test Device",
                        "manufacturer_data": {"76": "0215aabbccddeeff"},
                        "service_data": {
                            "0000180d-0000-1000-8000-00805f9b34fb": "00000000"
                        },
                        "service_uuids": ["0000180d-0000-1000-8000-00805f9b34fb"],
                    },
                    "device": {  # type: ignore[typeddict-item]
                        "address": "AA:BB:CC:DD:EE:FF",
                        "details": {"details": "test"},
                    },
                }
            },
            "discovered_device_timestamps": {"AA:BB:CC:DD:EE:FF": now},
            "expire_seconds": 100,
        }
    )
    assert result is None
    assert "Discovery cache shape mismatch" in caplog.text
    # The shape-mismatch path is logged at WARNING without a traceback so
    # operators can distinguish it from genuinely unexpected failures.
    records = [
        r for r in caplog.records if "Discovery cache shape mismatch" in r.getMessage()
    ]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert records[0].exc_info is None


def test_discovered_device_advertisement_data_from_dict_unexpected_error(
    caplog, monkeypatch
):
    """Unexpected errors keep the full traceback and are logged at ERROR."""

    def boom(_data):
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        "habluetooth.storage._deserialize_discovered_device_advertisement_datas",
        boom,
    )
    result = discovered_device_advertisement_data_from_dict(
        {
            "connectable": True,
            "discovered_device_advertisement_datas": {},
            "discovered_device_timestamps": {},
            "expire_seconds": 100,
            "discovered_device_raw": {},
        }
    )
    assert result is None
    records = [
        r for r in caplog.records if "Unexpected error deserializing" in r.getMessage()
    ]
    assert len(records) == 1
    assert records[0].levelname == "ERROR"
    assert records[0].exc_info is not None


def test_backward_compatibility_rssi_in_device_dict():
    """Test that devices with RSSI in storage can still be loaded."""
    now = time.time()
    # Simulate old storage format where RSSI was stored in the device dict
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
                        "rssi": -50,  # Old format included RSSI here
                    },
                }
            },
            "discovered_device_timestamps": {"AA:BB:CC:DD:EE:FF": now},
            "expire_seconds": 100,
            "discovered_device_raw": {},
        }
    )

    # Should successfully deserialize without errors
    assert result is not None
    assert result.connectable is True
    assert result.expire_seconds == 100

    # Check that the device was properly created
    ble_device, adv_data = result.discovered_device_advertisement_datas[
        "AA:BB:CC:DD:EE:FF"
    ]
    assert ble_device.address == "AA:BB:CC:DD:EE:FF"
    assert ble_device.name == "Test Device"
    assert adv_data.rssi == -50
