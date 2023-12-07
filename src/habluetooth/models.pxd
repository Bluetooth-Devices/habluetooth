
cdef object BLEDevice
cdef object AdvertisementData

cdef object _float
cdef object _int
cdef object _str
cdef object _BluetoothServiceInfoBleakSelfT
cdef object _BluetoothServiceInfoSelfT

cdef class BaseServiceInfo:
    """Base class for discovery ServiceInfo."""


cdef class BluetoothServiceInfo(BaseServiceInfo):
    """Prepared info from bluetooth entries."""

    cdef public object name
    cdef public object address
    cdef public object rssi
    cdef public object manufacturer_data
    cdef public object service_data
    cdef public object service_uuids
    cdef public object source


cdef class BluetoothServiceInfoBleak(BluetoothServiceInfo):
    """BluetoothServiceInfo with bleak data."""

    cdef public object device
    cdef public object advertisement
    cdef public object connectable
    cdef public object time
