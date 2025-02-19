import asyncio
from typing import Any, Callable, Coroutine

import aioblescan
from bleak.backends.bluezdbus.manager import DeviceRemovedCallbackAndState
from bleak.backends.bluezdbus.scanner import BleakScannerBlueZDBus
from bleak_retry_connector.bleak_manager import get_global_bluez_manager_with_timeout


async def _start_or_stop_scan(device: str, start: bool) -> None:
    """Start or stop scanning for BLE advertisements."""
    loop = asyncio.get_running_loop()
    bt_sock = aioblescan.create_bt_socket(device)
    conn, btctrl = await loop._create_connection_transport(  # type: ignore[attr-defined]
        bt_sock, aioblescan.BLEScanRequester, None, None
    )
    if start:
        await btctrl.send_scan_request()
    else:
        await btctrl.stop_scan_request()
    conn.close()
    bt_sock.close()


async def start_aioble_scan(
    scanner: BleakScannerBlueZDBus, device: str
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Start scanning for BLE advertisements."""
    adapter_path = f"/org/bluez/{device}"
    manager = await get_global_bluez_manager_with_timeout()
    manager._advertisement_callbacks[adapter_path].append(
        scanner._handle_advertising_data
    )
    device_removed_callback_and_state = DeviceRemovedCallbackAndState(
        scanner._handle_device_removed, adapter_path
    )
    manager._device_removed_callbacks.append(device_removed_callback_and_state)

    async def stop() -> None:
        """Stop scanning for BLE advertisements."""
        manager._advertisement_callbacks[adapter_path].remove(
            scanner._handle_advertising_data
        )
        manager._device_removed_callbacks.remove(device_removed_callback_and_state)
        await _start_or_stop_scan(device, False)

    try:
        await _start_or_stop_scan(device, True)
    except BaseException:
        await stop()
        raise

    return stop
