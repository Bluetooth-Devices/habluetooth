import cython

from .models cimport BluetoothServiceInfoBleak

cdef double _AUTO_INITIAL_SWEEP_DELAY
cdef double _AUTO_REDISCOVERY_INTERVAL
cdef double _AUTO_REDISCOVERY_SWEEP_DURATION
cdef double _AUTO_WINDOW_MAX_DURATION
cdef double _AUTO_WINDOW_MIN_DURATION


cdef class ActiveScanRequest:

    cdef public str address
    cdef public double scan_interval
    cdef public object scan_duration


cdef class _ScannerWorker:

    cdef public object _scheduler
    cdef public object _scanner
    cdef public object _manager
    cdef public object _wake
    cdef public object _task
    cdef public double _window_end
    cdef public double _sweep_last_completed

    cpdef void start(self, object loop, double initial_offset=*)

    cpdef void stop(self)

    cpdef void wake(self)

    cpdef double _next_event_at(self, double now)

    @cython.locals(
        source=str,
        needs=dict,
        address=str,
        entries=dict,
        due=list,
        due_buckets=list,
        all_due=list,
    )
    cpdef tuple _collect_due_buckets(self, double now)

    @cython.locals(
        entries=dict,
        due=list,
        request=ActiveScanRequest,
    )
    cpdef void _advance_due(self, list due_buckets, double from_time)


cdef class AutoScanScheduler:

    cdef public object _manager
    cdef public dict _requests_by_address
    cdef public dict _needs
    cdef public dict _workers
    cdef public object _loop
    cdef public bint _running

    @cython.locals(
        existing=dict,
        added=bint,
    )
    cpdef void add_request(self, ActiveScanRequest request)

    cpdef void remove_request(self, ActiveScanRequest request)

    cpdef void add_scanner(self, object scanner)

    cpdef void remove_scanner(self, object scanner)

    @cython.locals(
        address=str,
        existing=dict,
        requests=set,
        request=ActiveScanRequest,
    )
    cpdef void on_advertisement(self, BluetoothServiceInfoBleak service_info)

    cpdef void start(self, object loop)

    cpdef void stop(self)
