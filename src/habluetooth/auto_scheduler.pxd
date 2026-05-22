import cython

from .base_scanner cimport BaseHaScanner
from .manager cimport BleakCallback
from .models cimport BluetoothServiceInfoBleak


cdef bint _matches(BleakCallback callback, BluetoothServiceInfoBleak service_info)


cdef class AutoScanScheduler:

    cdef public object _manager
    cdef public dict _needs
    cdef public dict _scanner_windows
    cdef public dict _sweep_last_completed
    cdef public object _sweep_in_flight
    cdef public object _tick_handle
    cdef public object _loop
    cdef public bint _running
    cdef public set _pending_tasks
    cdef public set _interval_callbacks

    cpdef void add_callback(self, BleakCallback callback)

    cpdef void remove_callback(self, BleakCallback callback)

    cpdef void add_scanner(self, BaseHaScanner scanner)

    cpdef void remove_scanner(self, BaseHaScanner scanner)

    @cython.locals(
        address=str,
        existing=dict,
        callback=BleakCallback,
        interval=object,
    )
    cpdef void on_advertisement(self, BluetoothServiceInfoBleak service_info)

    cpdef void start(self, object loop)

    cpdef void stop(self)
