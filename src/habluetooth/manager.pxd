import cython

from .advertisement_tracker cimport AdvertisementTracker
from .base_scanner cimport BaseHaScanner

cdef object NO_RSSI_VALUE
cdef object RSSI_SWITCH_THRESHOLD
cdef object FILTER_UUIDS
cdef object BluetoothServiceInfoBleak
cdef object AdvertisementData
cdef object BLEDevice
cdef bint TYPE_CHECKING
cdef set APPLE_START_BYTES_WANTED

cdef unsigned int APPLE_MFR_ID

@cython.locals(uuids=set)
cdef _dispatch_bleak_callback(
    object callback,
    dict filters,
    object device,
    object advertisement_data
)

cdef class BluetoothManager:

    cdef public object _cancel_unavailable_tracking
    cdef public AdvertisementTracker _advertisement_tracker
    cdef public dict _fallback_intervals
    cdef public dict _intervals
    cdef public dict _unavailable_callbacks
    cdef public dict _connectable_unavailable_callbacks
    cdef public list _bleak_callbacks
    cdef public dict _all_history
    cdef public dict _connectable_history
    cdef public list _non_connectable_scanners
    cdef public list _connectable_scanners
    cdef public dict _adapters
    cdef public dict _sources
    cdef public object _bluetooth_adapters
    cdef public object slot_manager
    cdef public bint _debug
    cdef public bint shutdown
    cdef public object _loop

    cdef bint _prefer_previous_adv_from_different_source(self, object address, object old, object new)

    @cython.locals(source=str, connectable=bint, scanner=BaseHaScanner, connectable_scanner=BaseHaScanner)
    cpdef void scanner_adv_received(self, object service_info)
