import logging
from typing import Any, Callable, Coroutine

from bleak import BleakScanner
from bleak.backends.bluezdbus.manager import DeviceRemovedCallbackAndState
from bleak.backends.bluezdbus.scanner import BleakScannerBlueZDBus
from bleak_retry_connector.bleak_manager import get_global_bluez_manager_with_timeout
from bluetooth_auto_recovery.recover import MGMT_PROTOCOL_TIMEOUT, MGMTBluetoothCtl
from btsocket import btmgmt_protocol, btmgmt_socket

from .exceptions import ManualScannerStartFailed

_LOGGER = logging.getLogger(__name__)


async def _get_adapter(device: str, mac: str) -> MGMTBluetoothCtl:
    """Start or stop scanning for BLE advertisements."""
    interface = int(device.removeprefix("hci"))
    adapter = MGMTBluetoothCtl(interface, mac, MGMT_PROTOCOL_TIMEOUT)
    try:
        await adapter.setup()
    except btmgmt_socket.BluetoothSocketError as ex:
        await adapter.close()
        raise ManualScannerStartFailed(
            f"Getting Bluetooth adapter failed: {ex}"
        ) from ex
    except OSError as ex:
        await adapter.close()
        raise ManualScannerStartFailed(
            f"Getting Bluetooth adapter failed: {ex}"
        ) from ex
    except TimeoutError as ex:
        await adapter.close()
        raise ManualScannerStartFailed(
            "Getting Bluetooth adapter failed due to timeout"
        ) from ex
    except BaseException:
        await adapter.close()
        raise
    return adapter


async def _start_or_stop_scan(adapter: MGMTBluetoothCtl, start: bool) -> None:
    command = "StartDiscovery" if start else "StopDiscovery"
    try:
        response = await adapter.protocol.send(
            command,
            adapter.idx,
            [
                btmgmt_protocol.AddressType.LERandom,
                btmgmt_protocol.AddressType.LEPublic,
            ],
        )
    except Exception as ex:
        await adapter.close()
        raise ManualScannerStartFailed(f"{command} failed: {ex}") from ex
    _LOGGER.debug(
        "%s: %s: response.event_frame.command_opcode = %s, "
        "response.event_frame.status = %s",
        adapter.name,
        response.event_frame.command_opcode,
        response.event_frame.status,
    )
    if response.event_frame.status != btmgmt_protocol.ErrorCodes.Success:
        await adapter.close()
        raise ManualScannerStartFailed(
            f"{command} failed: {response.event_frame.status}"
        )
    return adapter


async def start_manual_scan(
    scanner: BleakScanner, device: str, mac: str
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Start scanning for BLE advertisements."""
    adapter_path = f"/org/bluez/{device}"
    adapter = await _get_adapter(device, mac)

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
        try:
            manager._advertisement_callbacks[adapter_path].remove(
                backend._handle_advertising_data
            )
            manager._device_removed_callbacks.remove(device_removed_callback_and_state)
            await _start_or_stop_scan(adapter, False)
        finally:
            await adapter.close()

    await _start_or_stop_scan(adapter, True)
    return stop
