from .base_scanner cimport BaseHaScanner


cdef object NO_RSSI_VALUE
cdef object BluetoothServiceInfoBleak
cdef object AdvertisementData
cdef object BLEDevice
cdef bint TYPE_CHECKING


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
