import asyncio
import logging
from typing import Any, Callable, Coroutine, cast

from aioblescan import aioblescan
from bleak import BleakScanner
from bleak.backends.bluezdbus.manager import DeviceRemovedCallbackAndState
from bleak.backends.bluezdbus.scanner import BleakScannerBlueZDBus
from bleak_retry_connector.bleak_manager import get_global_bluez_manager_with_timeout

_LOGGER = logging.getLogger(__name__)


async def _start_or_stop_scan(device: str, start: bool) -> None:
    """Start or stop scanning for BLE advertisements."""
    interface = int(device.removeprefix("hci"))
    loop = asyncio.get_running_loop()
    bt_sock = aioblescan.create_bt_socket(interface)
    conn, btctrl = await loop._create_connection_transport(  # type: ignore[attr-defined]
        bt_sock, aioblescan.BLEScanRequester, None, None
    )
    proto = cast(aioblescan.BLEScanRequester, btctrl)
    transport = cast(asyncio.Transport, conn)
    if start:
        _LOGGER.debug("Starting BLE scan: %s", device)
        await proto.send_scan_request()
    else:
        _LOGGER.debug("Stopping BLE scan: %s", device)
        await proto.stop_scan_request()
    transport.close()
    bt_sock.close()


async def start_aioble_scan(
    scanner: BleakScanner, device: str
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
        await _start_or_stop_scan(device, False)

    try:
        await _start_or_stop_scan(device, True)
    except BaseException:
        await stop()
        raise

    return stop
