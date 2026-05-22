import cython

from .base_scanner cimport BaseHaScanner
from .models cimport BluetoothServiceInfoBleak


cdef class ActiveScanRequest:

    cdef public str address
    cdef public double scan_interval
    cdef public object scan_duration


cdef class AutoScanScheduler:

    cdef public object _manager
    cdef public dict _requests_by_address
    cdef public dict _needs
    cdef public dict _scanner_windows
    cdef public dict _sweep_last_completed
    cdef public object _sweep_in_flight
    cdef public object _tick_handle
    cdef public object _loop
    cdef public bint _running
    cdef public set _pending_tasks

    cpdef void add_request(self, ActiveScanRequest request)

    cpdef void remove_request(self, ActiveScanRequest request)

    cpdef void add_scanner(self, BaseHaScanner scanner)

    cpdef void remove_scanner(self, BaseHaScanner scanner)

    @cython.locals(
        address=str,
        existing=dict,
        requests=set,
        request=ActiveScanRequest,
    )
    cpdef void on_advertisement(self, BluetoothServiceInfoBleak service_info)

    cpdef void start(self, object loop)

    cpdef void stop(self)
