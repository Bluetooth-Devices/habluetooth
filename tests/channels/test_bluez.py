"""Tests for the BlueZ management API module."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import Mock

import pytest

from habluetooth.channels.bluez import BluetoothMGMTProtocol
from habluetooth.scanner import HaScanner


class MockHaScanner(HaScanner):
    """Mock HaScanner for testing with Cython."""

    def __init__(self):
        """Initialize without calling parent __init__ to avoid BleakScanner setup."""
        self.source = "test"
        self.connectable = True
        # Mock the method that will be called
        self._async_on_raw_bluez_advertisement: Any = Mock()


@pytest.fixture
def event_loop():
    """Create and manage event loop for tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_scanner() -> MockHaScanner:
    """Create a mock scanner for testing."""
    return MockHaScanner()


@pytest.fixture
def mock_transport() -> Mock:
    """Create a mock transport."""
    transport = Mock()
    transport.write = Mock()
    return transport


def test_connection_made(
    event_loop: asyncio.AbstractEventLoop, mock_transport: Mock
) -> None:
    """Test connection_made sets up the protocol correctly."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )
    protocol.connection_made(mock_transport)

    assert protocol.transport is mock_transport
    assert future.done()
    assert future.result() is None


def test_connection_lost(
    event_loop: asyncio.AbstractEventLoop, mock_transport: Mock
) -> None:
    """Test connection_lost handles disconnection."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )
    protocol.connection_made(mock_transport)

    # Test with exception
    protocol.connection_lost(Exception("Test error"))
    assert protocol.transport is None
    on_connection_lost.assert_called_once()


def test_connection_lost_no_exception(
    event_loop: asyncio.AbstractEventLoop,
    mock_transport: Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test connection_lost without exception."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )
    protocol.connection_made(mock_transport)

    # Test without exception
    protocol.connection_lost(None)
    assert "Bluetooth management socket connection closed" in caplog.text


def test_data_received_device_found(
    event_loop: asyncio.AbstractEventLoop, mock_scanner: MockHaScanner
) -> None:
    """Test data_received handles DEVICE_FOUND event."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {0: mock_scanner}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Create a DEVICE_FOUND event (event_code 0x0012)
    # Header: event_code (2), controller_idx (2), param_len (2)
    # Params: address (6), address_type (1), rssi (1), flags (4),
    # ad_data_len (2), ad_data
    ad_data = b"\x02\x01\x06"  # Simple advertisement data
    param_len = 6 + 1 + 1 + 4 + 2 + len(ad_data)

    header = b"\x12\x00"  # DEVICE_FOUND
    header += b"\x00\x00"  # controller_idx = 0
    header += param_len.to_bytes(2, "little")

    params = b"\xaa\xbb\xcc\xdd\xee\xff"  # address (reversed)
    params += b"\x01"  # address_type
    params += b"\xc8"  # rssi = -56 (200 - 256)
    params += b"\x00\x00\x00\x00"  # flags
    params += len(ad_data).to_bytes(2, "little")  # ad_data_len
    params += ad_data

    protocol.data_received(header + params)

    mock_scanner._async_on_raw_bluez_advertisement.assert_called_once_with(
        b"\xaa\xbb\xcc\xdd\xee\xff",
        1,
        -56,
        0,
        ad_data,
    )


def test_data_received_adv_monitor_device_found(
    event_loop: asyncio.AbstractEventLoop, mock_scanner: MockHaScanner
) -> None:
    """Test data_received handles ADV_MONITOR_DEVICE_FOUND event."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {0: mock_scanner}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Create an ADV_MONITOR_DEVICE_FOUND event (event_code 0x002F)
    # Has 2 extra bytes at the beginning of params
    ad_data = b"\x02\x01\x06"
    param_len = 2 + 6 + 1 + 1 + 4 + 2 + len(ad_data)

    header = b"\x2f\x00"  # ADV_MONITOR_DEVICE_FOUND
    header += b"\x00\x00"  # controller_idx = 0
    header += param_len.to_bytes(2, "little")

    params = b"\x00\x00"  # 2 extra bytes
    params += b"\xaa\xbb\xcc\xdd\xee\xff"  # address
    params += b"\x02"  # address_type
    params += b"\x64"  # rssi = 100 (positive, no conversion needed)
    params += b"\x00\x00\x00\x00"  # flags
    params += len(ad_data).to_bytes(2, "little")
    params += ad_data

    protocol.data_received(header + params)

    mock_scanner._async_on_raw_bluez_advertisement.assert_called_once_with(
        b"\xaa\xbb\xcc\xdd\xee\xff",
        2,
        100,
        0,
        ad_data,
    )


def test_data_received_cmd_complete_success(
    event_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test data_received handles successful MGMT_EV_CMD_COMPLETE."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Create a CMD_COMPLETE event for LOAD_CONN_PARAM
    header = b"\x01\x00"  # MGMT_EV_CMD_COMPLETE
    header += b"\x00\x00"  # controller_idx = 0
    header += b"\x03\x00"  # param_len = 3

    params = b"\x35\x00"  # opcode = MGMT_OP_LOAD_CONN_PARAM
    params += b"\x00"  # status = 0 (success)

    protocol.data_received(header + params)

    assert "Connection parameters loaded successfully" in caplog.text


def test_data_received_cmd_complete_failure(
    event_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test data_received handles failed MGMT_EV_CMD_COMPLETE."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Create a CMD_COMPLETE event with failure
    header = b"\x01\x00"  # MGMT_EV_CMD_COMPLETE
    header += b"\x01\x00"  # controller_idx = 1
    header += b"\x03\x00"  # param_len = 3

    params = b"\x35\x00"  # opcode = MGMT_OP_LOAD_CONN_PARAM
    params += b"\x0c"  # status = 12 (Not Supported)

    protocol.data_received(header + params)

    assert "Failed to load conn params: status=12" in caplog.text


def test_data_received_cmd_status(
    event_loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
) -> None:
    """Test data_received handles MGMT_EV_CMD_STATUS."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Create a CMD_STATUS event
    header = b"\x02\x00"  # MGMT_EV_CMD_STATUS
    header += b"\x00\x00"  # controller_idx = 0
    header += b"\x03\x00"  # param_len = 3

    params = b"\x35\x00"  # opcode = MGMT_OP_LOAD_CONN_PARAM
    params += b"\x01"  # status = 1 (Unknown Command)

    protocol.data_received(header + params)

    assert "Failed to load conn params: status=1" in caplog.text


def test_data_received_partial_data(
    event_loop: asyncio.AbstractEventLoop, mock_scanner: MockHaScanner
) -> None:
    """Test data_received handles partial data correctly."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {0: mock_scanner}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Create a DEVICE_FOUND event but send it in chunks
    ad_data = b"\x02\x01\x06"
    param_len = 6 + 1 + 1 + 4 + 2 + len(ad_data)

    full_data = b"\x12\x00\x00\x00" + param_len.to_bytes(2, "little")
    full_data += b"\xaa\xbb\xcc\xdd\xee\xff\x01\xc8\x00\x00\x00\x00"
    full_data += len(ad_data).to_bytes(2, "little") + ad_data

    # Send header first
    protocol.data_received(full_data[:6])
    mock_scanner._async_on_raw_bluez_advertisement.assert_not_called()

    # Send rest of data
    protocol.data_received(full_data[6:])
    mock_scanner._async_on_raw_bluez_advertisement.assert_called_once()


def test_data_received_partial_data_split_in_params(
    event_loop: asyncio.AbstractEventLoop, mock_scanner: MockHaScanner
) -> None:
    """Test data_received handles data split in the middle of params."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {0: mock_scanner}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Create a DEVICE_FOUND event
    ad_data = b"\x02\x01\x06\x03\xff\x00\x01"  # Longer ad data
    param_len = 6 + 1 + 1 + 4 + 2 + len(ad_data)

    full_data = b"\x12\x00\x00\x00" + param_len.to_bytes(2, "little")
    full_data += b"\xaa\xbb\xcc\xdd\xee\xff\x01\xc8\x00\x00\x00\x00"
    full_data += len(ad_data).to_bytes(2, "little") + ad_data

    # Split in the middle of the address
    protocol.data_received(full_data[:10])  # Header + part of address
    mock_scanner._async_on_raw_bluez_advertisement.assert_not_called()

    # Send rest of data
    protocol.data_received(full_data[10:])
    mock_scanner._async_on_raw_bluez_advertisement.assert_called_once_with(
        b"\xaa\xbb\xcc\xdd\xee\xff",
        1,
        -56,
        0,
        ad_data,
    )


def test_data_received_multiple_small_chunks(
    event_loop: asyncio.AbstractEventLoop, mock_scanner: MockHaScanner
) -> None:
    """Test data_received handles data sent in many small chunks."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {0: mock_scanner}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Create a DEVICE_FOUND event
    ad_data = b"\x02\x01\x06"
    param_len = 6 + 1 + 1 + 4 + 2 + len(ad_data)

    full_data = b"\x12\x00\x00\x00" + param_len.to_bytes(2, "little")
    full_data += b"\xaa\xbb\xcc\xdd\xee\xff\x01\xc8\x00\x00\x00\x00"
    full_data += len(ad_data).to_bytes(2, "little") + ad_data

    # Send data byte by byte
    for i in range(len(full_data)):
        protocol.data_received(full_data[i : i + 1])
        if i < len(full_data) - 1:
            mock_scanner._async_on_raw_bluez_advertisement.assert_not_called()

    # After all bytes are sent, callback should be called once
    mock_scanner._async_on_raw_bluez_advertisement.assert_called_once()


def test_data_received_multiple_events_in_one_chunk(
    event_loop: asyncio.AbstractEventLoop,
    mock_scanner: Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test data_received handles multiple events in one data chunk."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {0: mock_scanner}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Create two events: a DEVICE_FOUND and a CMD_COMPLETE
    ad_data = b"\x02\x01\x06"
    param_len1 = 6 + 1 + 1 + 4 + 2 + len(ad_data)

    event1 = b"\x12\x00\x00\x00" + param_len1.to_bytes(2, "little")
    event1 += b"\xaa\xbb\xcc\xdd\xee\xff\x01\xc8\x00\x00\x00\x00"
    event1 += len(ad_data).to_bytes(2, "little") + ad_data

    event2 = b"\x01\x00\x00\x00\x03\x00"  # CMD_COMPLETE header
    event2 += b"\x35\x00\x00"  # LOAD_CONN_PARAM success

    # Send both events in one chunk
    protocol.data_received(event1 + event2)

    # Both events should be processed
    mock_scanner._async_on_raw_bluez_advertisement.assert_called_once()
    assert "Connection parameters loaded successfully" in caplog.text


def test_data_received_partial_then_multiple_events(
    event_loop: asyncio.AbstractEventLoop, mock_scanner: MockHaScanner
) -> None:
    """Test partial data followed by multiple complete events."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {0: mock_scanner}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # First event (DEVICE_FOUND)
    ad_data1 = b"\x02\x01\x06"
    param_len1 = 6 + 1 + 1 + 4 + 2 + len(ad_data1)
    event1 = b"\x12\x00\x00\x00" + param_len1.to_bytes(2, "little")
    event1 += b"\x11\x22\x33\x44\x55\x66\x01\xc8\x00\x00\x00\x00"
    event1 += len(ad_data1).to_bytes(2, "little") + ad_data1

    # Second event (ADV_MONITOR_DEVICE_FOUND)
    ad_data2 = b"\x03\xff\x00\x01"
    param_len2 = 2 + 6 + 1 + 1 + 4 + 2 + len(ad_data2)
    event2 = b"\x2f\x00\x00\x00" + param_len2.to_bytes(2, "little")
    event2 += b"\x00\x00"  # Extra 2 bytes
    event2 += b"\x77\x88\x99\xaa\xbb\xcc\x02\x64\x00\x00\x00\x00"
    event2 += len(ad_data2).to_bytes(2, "little") + ad_data2

    # Send partial first event
    protocol.data_received(event1[:15])
    mock_scanner._async_on_raw_bluez_advertisement.assert_not_called()

    # Send rest of first event + second event
    protocol.data_received(event1[15:] + event2)

    # Both callbacks should be called
    assert mock_scanner._async_on_raw_bluez_advertisement.call_count == 2
    calls = mock_scanner._async_on_raw_bluez_advertisement.call_args_list

    # First call
    assert calls[0][0] == (
        b"\x11\x22\x33\x44\x55\x66",
        1,
        -56,
        0,
        ad_data1,
    )

    # Second call
    assert calls[1][0] == (
        b"\x77\x88\x99\xaa\xbb\xcc",
        2,
        100,
        0,
        ad_data2,
    )


def test_data_received_cmd_complete_different_opcode(
    event_loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
) -> None:
    """Test data_received handles CMD_COMPLETE for different opcodes."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Create a CMD_COMPLETE event for a different opcode (e.g., 0x0004 - Add UUID)
    header = b"\x01\x00"  # MGMT_EV_CMD_COMPLETE
    header += b"\x00\x00"  # controller_idx = 0
    header += b"\x03\x00"  # param_len = 3

    params = b"\x04\x00"  # opcode = 0x0004 (not MGMT_OP_LOAD_CONN_PARAM)
    params += b"\x00"  # status = 0 (success)

    protocol.data_received(header + params)

    # Should not log anything about connection parameters
    assert "Connection parameters" not in caplog.text


def test_data_received_cmd_status_different_opcode(
    event_loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
) -> None:
    """Test data_received handles CMD_STATUS for different opcodes."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Create a CMD_STATUS event for a different opcode
    header = b"\x02\x00"  # MGMT_EV_CMD_STATUS
    header += b"\x00\x00"  # controller_idx = 0
    header += b"\x03\x00"  # param_len = 3

    params = b"\x05\x00"  # opcode = 0x0005 (not MGMT_OP_LOAD_CONN_PARAM)
    params += b"\x01"  # status = 1 (failure)

    protocol.data_received(header + params)

    # Should not log anything about connection parameters
    assert "conn params" not in caplog.text


def test_data_received_cmd_complete_short_params(
    event_loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
) -> None:
    """Test data_received handles CMD_COMPLETE with param_len < 3."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Create a CMD_COMPLETE event with param_len < 3
    header = b"\x01\x00"  # MGMT_EV_CMD_COMPLETE
    header += b"\x00\x00"  # controller_idx = 0
    header += b"\x02\x00"  # param_len = 2 (too short to contain opcode + status)

    params = b"\x00\x00"  # Just 2 bytes

    protocol.data_received(header + params)

    # Should not log anything (no opcode to check)
    assert "conn params" not in caplog.text


def test_data_received_cmd_status_param_len_1(
    event_loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
) -> None:
    """Test data_received handles CMD_STATUS with param_len = 1."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Create a CMD_STATUS event with param_len = 1
    header = b"\x02\x00"  # MGMT_EV_CMD_STATUS
    header += b"\x00\x00"  # controller_idx = 0
    header += b"\x01\x00"  # param_len = 1 (too short)

    params = b"\x00"  # Just 1 byte

    protocol.data_received(header + params)

    # Should not log anything (no opcode to check)
    assert "conn params" not in caplog.text


def test_data_received_cmd_complete_param_len_0(
    event_loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
) -> None:
    """Test data_received handles CMD_COMPLETE with param_len = 0."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Create a CMD_COMPLETE event with param_len = 0
    header = b"\x01\x00"  # MGMT_EV_CMD_COMPLETE
    header += b"\x00\x00"  # controller_idx = 0
    header += b"\x00\x00"  # param_len = 0 (no params at all)

    protocol.data_received(header)

    # Should not log anything (no opcode to check)
    assert "conn params" not in caplog.text


def test_data_received_unknown_event(event_loop: asyncio.AbstractEventLoop) -> None:
    """Test data_received ignores unknown events."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Create an unknown event
    header = b"\xff\x00"  # Unknown event code
    header += b"\x00\x00"  # controller_idx = 0
    header += b"\x04\x00"  # param_len = 4
    params = b"\x00\x00\x00\x00"

    # Should not raise any exception
    protocol.data_received(header + params)


def test_data_received_no_scanner_for_controller(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """Test data_received handles missing scanner gracefully."""
    future = event_loop.create_future()
    scanners: dict[int, HaScanner] = {}  # No scanner for controller 0
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Create a DEVICE_FOUND event for controller 0
    ad_data = b"\x02\x01\x06"
    param_len = 6 + 1 + 1 + 4 + 2 + len(ad_data)

    header = b"\x12\x00\x00\x00" + param_len.to_bytes(2, "little")
    params = b"\xaa\xbb\xcc\xdd\xee\xff\x01\xc8\x00\x00\x00\x00"
    params += len(ad_data).to_bytes(2, "little") + ad_data

    # Should not raise any exception
    protocol.data_received(header + params)


@pytest.mark.asyncio
async def test_setup_command_response() -> None:
    """Test the setup_command_response and cleanup_command_response methods."""
    future = asyncio.get_running_loop().create_future()
    future.set_result(None)  # Mark connection as made
    scanners: dict[int, HaScanner] = {}
    on_connection_lost = Mock()
    is_shutting_down = Mock(return_value=False)

    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    # Test successful command setup
    opcode = 0x0015  # MGMT_OP_GET_CONNECTIONS

    # Setup command response
    response_future = protocol.setup_command_response(opcode)
    assert response_future is not None
    assert isinstance(response_future, asyncio.Future)

    # Simulate receiving a response
    response_data = (
        b"\x01\x00"  # MGMT_EV_CMD_COMPLETE
        + b"\x00\x00"  # controller index
        + b"\x03\x00"  # param_len (3 bytes: opcode=2 + status=1)
        + opcode.to_bytes(2, "little")  # opcode
        + b"\x00"  # status (success)
    )

    protocol.data_received(response_data)

    # Get the result
    status, data = await response_future
    assert status == 0  # Success

    # Cleanup
    protocol.cleanup_command_response(opcode)


@pytest.mark.asyncio
async def test_setup_command_response_cleanup_on_exception() -> None:
    """Test that cleanup_command_response works properly."""
    future = asyncio.get_running_loop().create_future()
    scanners: dict[int, HaScanner] = {}
    on_connection_lost = Mock()
    is_shutting_down = Mock(return_value=False)

    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down
    )

    opcode = 0x0015  # MGMT_OP_GET_CONNECTIONS

    # Setup command response
    response_future = protocol.setup_command_response(opcode)
    assert response_future is not None

    # Test that cleanup removes it from tracking
    protocol.cleanup_command_response(opcode)

    # Should not be in pending commands anymore
    assert opcode not in protocol._pending_commands
