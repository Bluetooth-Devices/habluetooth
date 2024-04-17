"""Tests for the manager."""

from typing import Any
from unittest.mock import patch

import pytest
from bleak_retry_connector import BleakSlotManager
from bluetooth_adapters import BluetoothAdapters
from bluetooth_adapters.systems.linux import LinuxAdapters

from habluetooth import (
    BluetoothManager,
    set_manager,
)


@pytest.mark.asyncio
async def test_async_recover_failed_adapters() -> None:
    """Return the BluetoothManager instance."""

    class MockLinuxAdapters(LinuxAdapters):
        @property
        def adapters(self) -> dict[str, Any]:
            return {
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
                "hci1": {
                    "address": "00:00:00:00:00:00",
                    "hw_version": "usb:v1D6Bp0246d053F",
                    "passive_scan": False,
                    "sw_version": "homeassistant",
                    "manufacturer": "ACME",
                    "product": "Bluetooth Adapter 5.0",
                    "product_id": "aa01",
                    "vendor_id": "cc01",
                },
                "hci2": {
                    "address": "00:00:00:00:00:00",
                    "hw_version": "usb:v1D6Bp0246d053F",
                    "passive_scan": False,
                    "sw_version": "homeassistant",
                    "manufacturer": "ACME",
                    "product": "Bluetooth Adapter 5.0",
                    "product_id": "aa01",
                    "vendor_id": "cc01",
                },
            }

    with (
        patch("habluetooth.manager.async_reset_adapter") as mock_async_reset_adapter,
    ):
        adapters = MockLinuxAdapters()
        slot_manager = BleakSlotManager()
        manager = BluetoothManager(adapters, slot_manager)
        set_manager(manager)
        await manager.async_recover_failed_adapters()

    assert mock_async_reset_adapter.call_count == 2
    assert mock_async_reset_adapter.call_args_list == [
        (("hci1", "00:00:00:00:00:00"),),
        (("hci2", "00:00:00:00:00:00"),),
    ]


@pytest.mark.asyncio
async def test_create_manager() -> None:
    """Return the BluetoothManager instance."""
    adapters = BluetoothAdapters()
    slot_manager = BleakSlotManager()
    manager = BluetoothManager(adapters, slot_manager)
    set_manager(manager)
    assert manager
