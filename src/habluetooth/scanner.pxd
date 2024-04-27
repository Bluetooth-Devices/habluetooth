import cython


from .base_scanner cimport BaseHaScanner
from .models cimport BluetoothServiceInfoBleak
from .models cimport BLEDevice
from .models cimport AdvertisementData

cdef object NO_RSSI_VALUE
cdef bint TYPE_CHECKING


cdef class HaScanner(BaseHaScanner):

    cdef public object mac_address
    cdef public object requested_mode
    cdef public object _start_stop_lock
    cdef public object _background_tasks
    cdef public object scanner
    cdef public object _start_future
    cdef public object current_mode

    cpdef void _async_detection_callback(
        self,
        BLEDevice device,
        AdvertisementData advertisement_data
    )
