from .base_scanner cimport BaseHaScanner


cdef object NO_RSSI_VALUE
cdef object BluetoothServiceInfoBleak
cdef object AdvertisementData
cdef object BLEDevice
cdef bint TYPE_CHECKING


class HaScanner(BaseHaScanner):
    """
    Operate and automatically recover a BleakScanner.

    Multiple BleakScanner can be used at the same time
    if there are multiple adapters. This is only useful
    if the adapters are not located physically next to each other.

    Example use cases are usbip, a long extension cable, usb to bluetooth
    over ethernet, usb over ethernet, etc.
    """

    __slots__ = (
        "mac_address",
        "mode",
        "_start_stop_lock",
        "_new_info_callback",
        "scanning",
        "_background_tasks",
        "scanner",
    )

cdef class HaScanner(BaseHaScanner):

    cdef public object mac_address
    cdef public object mode
    cdef public object _start_stop_lock
    cdef public object _new_info_callback
    cdef public object _background_tasks
    cdef public object scanner

    cpdef void _async_detection_callback(
        self,
        object device,
        object advertisement_data
    )
