import cython

cdef object BLEDevice
cdef object AdvertisementData

cdef object _float
cdef object _int
cdef object _str
cdef object _BluetoothServiceInfoBleakSelfT
cdef object _BluetoothServiceInfoSelfT
cdef object NO_RSSI_VALUE

cdef object TUPLE_NEW

cdef int _ADV_DATA_UNKNOWN
cdef int _ADV_DATA_UNCHANGED
cdef int _ADV_DATA_CHANGED

cdef class BluetoothServiceInfo:
    """Prepared info from bluetooth entries."""

    cdef public str name
    cdef public str address
    cdef public int rssi
    cdef public dict manufacturer_data
    cdef public dict service_data
    cdef public list service_uuids
    cdef public str source


cdef class BluetoothServiceInfoBleak(BluetoothServiceInfo):
    """BluetoothServiceInfo with bleak data."""

    cdef public object device
    cdef public object _advertisement
    cdef public bint connectable
    cdef public double time
    cdef public object tx_power
    cdef public bytes raw
    cdef public int _adv_data_changed

    @cython.locals(new_obj=BluetoothServiceInfoBleak)
    cpdef BluetoothServiceInfoBleak _as_connectable(self)

    cdef object _advertisement_internal(self)
