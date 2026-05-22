import cython

from .models cimport BluetoothServiceInfoBleak


cdef class ActiveScanRequest:

    cdef public str address
    cdef public double scan_interval
    cdef public object scan_duration


cdef class AutoScanScheduler:

    cdef public object _manager
    cdef public dict _requests_by_address
    cdef public dict _needs
    cdef public dict _workers
    cdef public object _sweep_lock
    cdef public object _loop
    cdef public bint _running

    cpdef void add_request(self, ActiveScanRequest request)

    cpdef void remove_request(self, ActiveScanRequest request)

    # scanner is typed as object rather than BaseHaScanner to avoid a
    # three-way cimport cycle: manager.pxd cimports auto_scheduler,
    # auto_scheduler would cimport base_scanner, and base_scanner already
    # cimports manager. Cython handles a 2-way cycle (manager <->
    # base_scanner) via forward declarations but the 3-way variant
    # breaks on macOS at init time with KeyError: '__pyx_vtable__'
    # because base_scanner is only partially initialized when
    # auto_scheduler tries to resolve BaseHaScanner. Object typing on
    # these cold paths costs nothing measurable.
    cpdef void add_scanner(self, object scanner)

    cpdef void remove_scanner(self, object scanner)

    @cython.locals(
        address=str,
        existing=dict,
        requests=set,
        request=ActiveScanRequest,
        added=bint,
    )
    cpdef void on_advertisement(self, BluetoothServiceInfoBleak service_info)

    cpdef void start(self, object loop)

    cpdef void stop(self)
