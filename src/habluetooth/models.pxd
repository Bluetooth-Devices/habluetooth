
cdef object BLEDevice
cdef object AdvertisementData

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

    cdef public object device
    cdef public object _advertisement
    cdef public bint connectable
    cdef public double time
    cdef public int tx_power

    cdef _cython_init(
        self,
        str name,
        str address,
        int rssi,
        dict manufacturer_data,
        dict service_data,
        list service_uuids,
        str source,
        object device,
        object _advertisement,
        bint connectable,
        double time,
        int tx_power
    )
