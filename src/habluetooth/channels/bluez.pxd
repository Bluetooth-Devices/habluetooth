
import cython


cdef bint TYPE_CHECKING

cdef unsigned short DEVICE_FOUND

cdef class BluetoothMGMTProtocol:

    cdef object transport
    cdef object connection_mode_future
    cdef bytes _buffer
    cdef unsigned int _buffer_len
    cdef unsigned int _pos

    @cython.locals(
        original_pos="unsigned int",
        new_pos="unsigned int",
        cstr="const unsigned char *"
    )
    cdef bytes _read(self, int length)

    @cython.locals(bytes_data=bytes)
    cdef void _add_to_buffer(self, object data) except *

    @cython.locals(end_of_frame_pos="unsigned int", cstr="const unsigned char *")
    cdef void _remove_from_buffer(self) except *

    @cython.locals(
        header="const unsigned char *",
        event_code="unsigned short",
        controller_idx="unsigned short",
        param_len="unsigned short"
    )
    cpdef void data_received(self, object data) except *
