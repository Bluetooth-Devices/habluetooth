import cython

from .manager cimport BluetoothManager
from .models cimport BluetoothServiceInfoBleak

# auto_scheduler intentionally cimports BluetoothManager even though the
# attribute is stored untyped: the mutual cimport (manager <-> auto_scheduler)
# is what lets Cython's deferred-resolution path settle the init order on
# macOS. The one-way variant produced KeyError: '__pyx_vtable__' on the
# partially-initialized peer because the deferral only kicks in when both
# sides advertise the dependency at compile time.


cdef class ActiveScanRequest:

    cdef public str address
    cdef public double scan_interval
    cdef public object scan_duration


cdef class AutoScanScheduler:

    # _manager is typed as object rather than BluetoothManager to keep the
    # attribute access through Python protocol; promoting to a typed cdef
    # would require base_scanner to also cimport auto_scheduler and the
    # resulting three-way cycle (manager <-> base_scanner, base_scanner <->
    # auto_scheduler, auto_scheduler <-> manager) breaks macOS Cython init
    # with KeyError: '__pyx_vtable__' on whichever module is partial when
    # the chain comes back around.
    cdef public object _manager
    cdef public dict _requests_by_address
    cdef public dict _needs
    cdef public dict _workers
    cdef public object _sweep_lock
    cdef public object _loop
    cdef public bint _running

    cpdef void add_request(self, ActiveScanRequest request)

    cpdef void remove_request(self, ActiveScanRequest request)

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
