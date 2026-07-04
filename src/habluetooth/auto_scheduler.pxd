import cython

from .models cimport BluetoothServiceInfoBleak

cdef double _AUTO_INITIAL_SWEEP_DELAY
cdef double _AUTO_REDISCOVERY_INTERVAL
cdef double _AUTO_REDISCOVERY_SWEEP_DURATION
cdef double _AUTO_WINDOW_MAX_DURATION
cdef double _AUTO_WINDOW_MIN_DURATION
cdef double _AUTO_CONNECTING_DEFER
cdef double _AUTO_COALESCE_LOOKAHEAD
cdef double _ON_DEMAND_EXTENSION_SLOP
cdef double _RESCUE_SCAN_ACCEPT_SECONDS
cdef int NO_RSSI_VALUE


cdef double _clamp_window_duration(double duration) noexcept


cdef class ActiveScanRequest:

    cdef public str address
    cdef public double scan_interval
    cdef public double scan_duration


cdef class _ScannerWorker:

    cdef public object _scheduler
    cdef public object _scanner
    cdef public object _manager
    cdef public object _wake
    cdef public object _task
    cdef public double _window_end
    cdef public double _sweep_last_completed
    cdef public double _last_window_at
    cdef public double _last_window_duration
    cdef public bint _failed_window
    cdef public bint _warned_no_fallback
    cdef public dict _owned_due_at

    cpdef void start(self, object loop, double initial_offset=*)

    cpdef void stop(self)

    cpdef void wake(self)

    cpdef void _attach_owned(self, str address, dict entries)

    cpdef void _detach_owned(self, str address)

    cpdef void _clear_owned(self)

    cpdef void note_window_dispatched(self, double window_end, double now)

    @cython.locals(window_end=double)
    cpdef void _mark_window_open(self, double now, double duration)

    @cython.locals(
        next_at=double,
        earliest=double,
    )
    cpdef double _next_event_at(self, double now)

    @cython.locals(
        threshold=double,
        any_immediate=bint,
        t=double,
    )
    cpdef tuple _collect_due_buckets(self, double now)

    cpdef void _advance_due(self, list due_buckets, double from_time)


cdef class _ScanSchedule:

    cdef public dict _due_at
    cdef public dict _workers
    cdef public dict _owner_by_address

    cpdef bint seed(self, str address, ActiveScanRequest request, double due_time)

    cpdef void drop(self, str address, ActiveScanRequest request)

    @cython.locals(entries=dict)
    cpdef void advance_to(self, str address, double due_time)

    cpdef void assign(self, str address, str new_source)

    cpdef void unown(self, str address)

    cpdef void clear_source(self, str source)

    cpdef void attach_worker(self, str source)

    cpdef void clear(self)


cdef class AutoScanScheduler:

    cdef public object _manager
    cdef public dict _requests_by_address
    cdef public _ScanSchedule _schedule
    cdef public dict _workers
    cdef public dict _last_window_by_address
    cdef public dict _rescue_accept_after
    cdef public set _rescue_tasks
    cdef public object _loop
    cdef public bint _running
    cdef public object _on_demand_sweep_future
    cdef public double _on_demand_sweep_end

    cpdef void add_request(self, ActiveScanRequest request)

    cpdef void remove_request(self, ActiveScanRequest request)

    cpdef void add_scanner(self, object scanner)

    @cython.locals(
        source=str,
    )
    cpdef void remove_scanner(self, object scanner)

    @cython.locals(
        address=str,
        requests=set,
    )
    cpdef void on_advertisement(self, BluetoothServiceInfoBleak service_info)

    @cython.locals(
        request=ActiveScanRequest,
    )
    cpdef void _seed_requests(
        self, str address, set requests, double now
    )

    cpdef void start(self, object loop)

    cpdef void stop(self)

    @cython.locals(
        now=double,
        coarse_now=double,
        requests=set,
    )
    cpdef void trigger_rescue(
        self, str address, str challenger_source, str owner_source
    )

    cpdef void _record_rescue_accept(self, str address, double accept_after)

    cpdef void _record_window_dispatch(
        self,
        str address,
        double dispatch_now,
        str source,
        double accept_after,
    )

    @cython.locals(address=str)
    cpdef void _mark_delegated_dispatch(
        self,
        str source,
        list addresses,
        double dispatch_now,
        double duration,
        double accept_after,
    )

    @cython.locals(accept_after=double)
    cpdef void _record_rescue_accepts(self, list due_buckets)

    cpdef void _rescue_owner_side(
        self, str address, str owner_source, set requests, double now,
        double coarse_now
    )

    cpdef void _rescue_task_done(
        self, str name, double duration, object task
    )

    @cython.locals(duration=double)
    cpdef void _rescue_challenger_side(
        self,
        str address,
        str challenger_source,
        set requests,
        object loop,
        double now,
        double coarse_now,
    )

    @cython.locals(
        best_rssi=int,
        rssi=int,
        adv_rssi=object,
        scanner=object,
        mode=object,
    )
    cpdef tuple _resolve_fallback_for_address(
        self, str address, str exclude_source
    )
