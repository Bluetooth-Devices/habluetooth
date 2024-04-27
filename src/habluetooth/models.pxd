

cdef object _float
cdef object _int
cdef object _str
cdef object _BluetoothServiceInfoBleakSelfT
cdef object _BluetoothServiceInfoSelfT


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

    cdef public BLEDevice device
    cdef public AdvertisementData advertisement
    cdef public bint connectable
    cdef public double time


cdef class AdvertisementData:

    cdef public str local_name
    cdef public dict manufacturer_data
    cdef public dict service_data
    cdef public list service_uuids
    cdef public object tx_power
    cdef public object rssi
    cdef public tuple platform_data

cdef class BLEDevice:

    cdef public str address
    cdef public str name
    cdef public object details
    cdef public object _rssi
    cdef public object _metadata
