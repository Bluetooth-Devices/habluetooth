

cdef object NO_RSSI_VALUE
cdef object BluetoothServiceInfoBleak
cdef object AdvertisementData
cdef object BLEDevice

cdef class BaseHaScanner:

    cdef public str adapter
    cdef public object connectable
    cdef public str source
    cdef public object connector
    cdef public unsigned int _connecting
    cdef public str name
    cdef public bint scanning
    cdef public object _last_detection
    cdef public object _start_time
    cdef public object _cancel_watchdog
    cdef public object _loop


cdef class BaseHaRemoteScanner(BaseHaScanner):

    cdef public object _new_info_callback
    cdef public dict _discovered_device_advertisement_datas
    cdef public dict _discovered_device_timestamps
    cdef public dict _details
    cdef public object _expire_seconds
    cdef public object _cancel_track

    @cython.locals(
        prev_service_uuids=list,
        prev_service_data=dict,
        prev_manufacturer_data=dict,
        prev_name=str,
        prev_discovery=tuple
    )
    cpdef void _async_on_advertisement(
        self,
        object address,
        object rssi,
        object local_name,
        list service_uuids,
        dict service_data,
        dict manufacturer_data,
        object tx_power,
        dict details,
        object advertisement_monotonic_time
    )
