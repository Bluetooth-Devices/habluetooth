import cython

cdef class AdvertisementTracker:

    cdef public dict intervals
    cdef public dict fallback_intervals
    cdef public dict sources
    cdef public dict _timings

    @cython.locals(timings=list)
    cpdef void async_collect(self, object service_info)

    cpdef void async_remove_address(self, object address)
