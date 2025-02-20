import logging
from typing import Any, Callable, Coroutine

from bleak import BleakScanner
from bleak.backends.bluezdbus.manager import DeviceRemovedCallbackAndState
from bleak.backends.bluezdbus.scanner import BleakScannerBlueZDBus
from bleak_retry_connector.bleak_manager import get_global_bluez_manager_with_timeout
from bluetooth_auto_recovery.recover import _get_adapter
from btsocket import btmgmt_protocol

_LOGGER = logging.getLogger(__name__)


async def _start_or_stop_scan(device: str, mac: str, start: bool) -> None:
    """Start or stop scanning for BLE advertisements."""
    interface = int(device.removeprefix("hci"))
    async with _get_adapter(interface, mac) as adapter:
        if not adapter:
            raise OSError(0, f"{device} not found")
        adapter.set_powered(True)
        command = "StartDiscovery" if start else "StopDiscovery"
        response = await adapter.protocol.send(
            command,
            adapter.idx,
            [
                btmgmt_protocol.AddressType.LERandom,
                btmgmt_protocol.AddressType.LEPublic,
            ],
        )
        _LOGGER.warning(
            "response.event_frame.command_opcode = %s, "
            "response.event_frame.status = %s",
            response.event_frame.command_opcode,
            response.event_frame.status,
        )
        if response.event_frame.status != btmgmt_protocol.ErrorCodes.SUCCESS:
            raise OSError(0, f"{command} failed: {response.event_frame.status}")


async def start_manual_scan(
    scanner: BleakScanner, device: str, mac: str
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Start scanning for BLE advertisements."""
    adapter_path = f"/org/bluez/{device}"
    backend: BleakScannerBlueZDBus = scanner._backend
    manager = await get_global_bluez_manager_with_timeout()
    manager._advertisement_callbacks[adapter_path].append(
        backend._handle_advertising_data
    )
    device_removed_callback_and_state = DeviceRemovedCallbackAndState(
        backend._handle_device_removed, adapter_path
    )
    manager._device_removed_callbacks.append(device_removed_callback_and_state)

    async def stop() -> None:
        """Stop scanning for BLE advertisements."""
        manager._advertisement_callbacks[adapter_path].remove(
            backend._handle_advertising_data
        )
        manager._device_removed_callbacks.remove(device_removed_callback_and_state)
        await _start_or_stop_scan(device, mac, False)

    try:
        await _start_or_stop_scan(device, mac, True)
    except BaseException:
        await stop()
        raise

    return stop
