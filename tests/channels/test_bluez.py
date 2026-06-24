"""Tests for the BlueZ management API module."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import ANY, Mock, patch

import pytest
from btsocket.btmgmt_socket import BluetoothSocketError

from habluetooth.channels.bluez import (
    IO_CAPABILITY_NO_INPUT_NO_OUTPUT,
    MGMT_OP_ADD_ADV_PATTERNS_MONITOR,
    MGMT_OP_DISCONNECT,
    MGMT_OP_LOAD_LONG_TERM_KEYS,
    MGMT_OP_PAIR_DEVICE,
    MGMT_OP_REMOVE_ADV_MONITOR,
    MGMT_OP_START_DISCOVERY,
    MGMT_OP_STOP_DISCOVERY,
    MGMT_OP_UNPAIR_DEVICE,
    SCAN_TYPE_LE,
    BluetoothMGMTProtocol,
    LongTermKey,
    MGMTBluetoothCtl,
    bytes_mac_to_str,
    make_bluez_details,
)
from habluetooth.const import (
    BDADDR_LE_PUBLIC,
    BDADDR_LE_RANDOM,
    FAST_CONN_LATENCY,
    FAST_CONN_TIMEOUT,
    FAST_MAX_CONN_INTERVAL,
    FAST_MIN_CONN_INTERVAL,
    MEDIUM_CONN_LATENCY,
    MEDIUM_CONN_TIMEOUT,
    MEDIUM_MAX_CONN_INTERVAL,
    MEDIUM_MIN_CONN_INTERVAL,
    ConnectParams,
)
from habluetooth.scanner_bleak import HaScanner

if TYPE_CHECKING:
    from collections.abc import Callable

    from habluetooth.base_scanner import BaseHaScanner


class MockHaScanner(HaScanner):
    """Mock HaScanner for testing with Cython."""

    def __init__(self):
        """Initialize without calling parent __init__ to avoid BleakScanner setup."""
        self.source = "test"
        self.adapter = "hci0"
        self.connectable = True
        # Mock the method that will be called
        self._async_on_raw_advertisement: Any = Mock()


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
    # Create a mock socket for direct writes
    mock_socket = Mock()
    mock_socket.send = Mock(return_value=6)  # Default to successful send
    transport.get_extra_info = Mock(return_value=mock_socket)
    return transport


def test_connection_made(
    event_loop: asyncio.AbstractEventLoop, mock_transport: Mock
) -> None:
    """Test connection_made sets up the protocol correctly."""
    future = event_loop.create_future()
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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
    scanners: dict[int, BaseHaScanner] = {0: mock_scanner}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
    )

    # Create a DEVICE_FOUND event (event_code 0x0012). Header layout is
    # event_code (2), controller_idx (2), param_len (2); params layout
    # is address (6), address_type (1), rssi (1), flags (4),
    # ad_data_len (2), then ad_data.
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

    expected_address = bytes_mac_to_str(b"\xaa\xbb\xcc\xdd\xee\xff")
    mock_scanner._async_on_raw_advertisement.assert_called_once_with(
        expected_address,
        -56,
        ad_data,
        make_bluez_details(expected_address, "hci0"),
        ANY,
    )


def test_data_received_adv_monitor_device_found(
    event_loop: asyncio.AbstractEventLoop, mock_scanner: MockHaScanner
) -> None:
    """Test data_received handles ADV_MONITOR_DEVICE_FOUND event."""
    future = event_loop.create_future()
    scanners: dict[int, BaseHaScanner] = {0: mock_scanner}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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

    expected_address = bytes_mac_to_str(b"\xaa\xbb\xcc\xdd\xee\xff")
    mock_scanner._async_on_raw_advertisement.assert_called_once_with(
        expected_address,
        100,
        ad_data,
        make_bluez_details(expected_address, "hci0"),
        ANY,
    )


def test_data_received_cmd_complete_success(
    event_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test data_received handles successful MGMT_EV_CMD_COMPLETE."""
    future = event_loop.create_future()
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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
    scanners: dict[int, BaseHaScanner] = {0: mock_scanner}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
    )

    # Create a DEVICE_FOUND event but send it in chunks
    ad_data = b"\x02\x01\x06"
    param_len = 6 + 1 + 1 + 4 + 2 + len(ad_data)

    full_data = b"\x12\x00\x00\x00" + param_len.to_bytes(2, "little")
    full_data += b"\xaa\xbb\xcc\xdd\xee\xff\x01\xc8\x00\x00\x00\x00"
    full_data += len(ad_data).to_bytes(2, "little") + ad_data

    # Send header first
    protocol.data_received(full_data[:6])
    mock_scanner._async_on_raw_advertisement.assert_not_called()

    # Send rest of data
    protocol.data_received(full_data[6:])
    mock_scanner._async_on_raw_advertisement.assert_called_once()


def test_data_received_partial_data_split_in_params(
    event_loop: asyncio.AbstractEventLoop, mock_scanner: MockHaScanner
) -> None:
    """Test data_received handles data split in the middle of params."""
    future = event_loop.create_future()
    scanners: dict[int, BaseHaScanner] = {0: mock_scanner}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
    )

    # Create a DEVICE_FOUND event
    ad_data = b"\x02\x01\x06\x03\xff\x00\x01"  # Longer ad data
    param_len = 6 + 1 + 1 + 4 + 2 + len(ad_data)

    full_data = b"\x12\x00\x00\x00" + param_len.to_bytes(2, "little")
    full_data += b"\xaa\xbb\xcc\xdd\xee\xff\x01\xc8\x00\x00\x00\x00"
    full_data += len(ad_data).to_bytes(2, "little") + ad_data

    # Split in the middle of the address
    protocol.data_received(full_data[:10])  # Header + part of address
    mock_scanner._async_on_raw_advertisement.assert_not_called()

    # Send rest of data
    protocol.data_received(full_data[10:])
    expected_address = bytes_mac_to_str(b"\xaa\xbb\xcc\xdd\xee\xff")
    mock_scanner._async_on_raw_advertisement.assert_called_once_with(
        expected_address,
        -56,
        ad_data,
        make_bluez_details(expected_address, "hci0"),
        ANY,
    )


def test_data_received_multiple_small_chunks(
    event_loop: asyncio.AbstractEventLoop, mock_scanner: MockHaScanner
) -> None:
    """Test data_received handles data sent in many small chunks."""
    future = event_loop.create_future()
    scanners: dict[int, BaseHaScanner] = {0: mock_scanner}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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
            mock_scanner._async_on_raw_advertisement.assert_not_called()

    # After all bytes are sent, callback should be called once
    mock_scanner._async_on_raw_advertisement.assert_called_once()


def test_data_received_multiple_events_in_one_chunk(
    event_loop: asyncio.AbstractEventLoop,
    mock_scanner: Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test data_received handles multiple events in one data chunk."""
    future = event_loop.create_future()
    scanners: dict[int, BaseHaScanner] = {0: mock_scanner}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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
    mock_scanner._async_on_raw_advertisement.assert_called_once()
    assert "Connection parameters loaded successfully" in caplog.text


def test_data_received_partial_then_multiple_events(
    event_loop: asyncio.AbstractEventLoop, mock_scanner: MockHaScanner
) -> None:
    """Test partial data followed by multiple complete events."""
    future = event_loop.create_future()
    scanners: dict[int, BaseHaScanner] = {0: mock_scanner}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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
    mock_scanner._async_on_raw_advertisement.assert_not_called()

    # Send rest of first event + second event
    protocol.data_received(event1[15:] + event2)

    # Both callbacks should be called
    assert mock_scanner._async_on_raw_advertisement.call_count == 2
    calls = mock_scanner._async_on_raw_advertisement.call_args_list

    # First call
    first_address = bytes_mac_to_str(b"\x11\x22\x33\x44\x55\x66")
    assert calls[0][0] == (
        first_address,
        -56,
        ad_data1,
        make_bluez_details(first_address, "hci0"),
        ANY,
    )

    # Second call
    second_address = bytes_mac_to_str(b"\x77\x88\x99\xaa\xbb\xcc")
    assert calls[1][0] == (
        second_address,
        100,
        ad_data2,
        make_bluez_details(second_address, "hci0"),
        ANY,
    )


def test_data_received_cmd_complete_different_opcode(
    event_loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
) -> None:
    """Test data_received handles CMD_COMPLETE for different opcodes."""
    future = event_loop.create_future()
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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
    scanners: dict[int, BaseHaScanner] = {}  # No scanner for controller 0
    on_connection_lost = Mock()

    is_shutting_down = Mock(return_value=False)
    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
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
async def test_setup_success() -> None:
    """Test successful setup."""
    mock_sock = Mock()
    mock_sock.fileno.return_value = 1  # Mock socket file descriptor
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_transport = Mock()
    mock_protocol.transport = mock_transport

    # Mock the future that gets created and set
    loop = asyncio.get_running_loop()
    mock_future = loop.create_future()

    async def mock_create_connection(*args, **kwargs):
        # Set the future result to simulate connection made
        mock_future.set_result(None)
        return mock_transport, mock_protocol

    with (
        patch("habluetooth.channels.bluez.btmgmt_socket.open", return_value=mock_sock),
        patch.object(
            asyncio.get_running_loop(),
            "_create_connection_transport",
            side_effect=mock_create_connection,
        ),
        patch.object(
            asyncio.get_running_loop(),
            "create_future",
            side_effect=[
                mock_future,
                loop.create_future(),
            ],  # First for connection, second for on_connection_lost
        ),
        patch.object(
            MGMTBluetoothCtl,
            "_check_capabilities",
            return_value=True,  # Mock successful capability check
        ),
    ):
        ctl = MGMTBluetoothCtl(5.0, {})
        await ctl.setup()

        assert ctl.sock is mock_sock
        assert ctl.protocol is mock_protocol
        assert ctl._reconnect_task is not None

        # Clean up
        ctl._reconnect_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ctl._reconnect_task


@pytest.mark.asyncio
async def test_setup_timeout() -> None:
    """Test setup timeout."""
    mock_sock = Mock()

    async def slow_connect(*args, **kwargs):
        await asyncio.sleep(10)

    with (
        patch("habluetooth.channels.bluez.btmgmt_socket.open", return_value=mock_sock),
        patch.object(
            asyncio.get_running_loop(),
            "_create_connection_transport",
            side_effect=slow_connect,
        ),
        patch("habluetooth.channels.bluez.btmgmt_socket.close") as mock_close,
    ):
        ctl = MGMTBluetoothCtl(0.1, {})
        with pytest.raises(TimeoutError):
            await ctl.setup()

        mock_close.assert_called_once_with(mock_sock)


@pytest.mark.asyncio
async def test_load_conn_params_fast() -> None:
    """Test loading fast connection parameters."""
    mock_sock = Mock()
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_transport = Mock()
    mock_protocol.transport = mock_transport
    # Mock the _write_to_socket method
    mock_protocol._write_to_socket = Mock()

    ctl = MGMTBluetoothCtl(5.0, {})
    ctl.protocol = mock_protocol
    ctl.sock = mock_sock

    result = ctl.load_conn_params(
        0,  # adapter_idx
        "AA:BB:CC:DD:EE:FF",  # address
        BDADDR_LE_PUBLIC,  # address_type
        ConnectParams.FAST,
    )

    assert result is True

    # Verify the command was sent
    mock_protocol._write_to_socket.assert_called_once()
    call_args = mock_protocol._write_to_socket.call_args[0][0]

    # Check header (6 bytes)
    assert call_args[0:2] == b"\x35\x00"  # MGMT_OP_LOAD_CONN_PARAM
    assert call_args[2:4] == b"\x00\x00"  # adapter_idx = 0
    assert call_args[4:6] == b"\x11\x00"  # param_len = 17 (2 + 15)

    # Check command data
    assert call_args[6:8] == b"\x01\x00"  # param_count = 1
    assert call_args[8:14] == b"\xff\xee\xdd\xcc\xbb\xaa"  # address (reversed)
    assert call_args[14] == BDADDR_LE_PUBLIC  # address_type
    assert call_args[15:17] == FAST_MIN_CONN_INTERVAL.to_bytes(2, "little")
    assert call_args[17:19] == FAST_MAX_CONN_INTERVAL.to_bytes(2, "little")
    assert call_args[19:21] == FAST_CONN_LATENCY.to_bytes(2, "little")
    assert call_args[21:23] == FAST_CONN_TIMEOUT.to_bytes(2, "little")


@pytest.mark.asyncio
async def test_load_conn_params_medium() -> None:
    """Test loading medium connection parameters."""
    mock_sock = Mock()
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_transport = Mock()
    mock_protocol.transport = mock_transport
    # Mock the _write_to_socket method
    mock_protocol._write_to_socket = Mock()

    ctl = MGMTBluetoothCtl(5.0, {})
    ctl.protocol = mock_protocol
    ctl.sock = mock_sock

    result = ctl.load_conn_params(
        1,  # adapter_idx
        "11:22:33:44:55:66",  # address
        BDADDR_LE_RANDOM,  # address_type
        ConnectParams.MEDIUM,
    )

    assert result is True

    # Verify the command was sent
    mock_protocol._write_to_socket.assert_called_once()
    call_args = mock_protocol._write_to_socket.call_args[0][0]

    # Check header
    assert call_args[0:2] == b"\x35\x00"  # MGMT_OP_LOAD_CONN_PARAM
    assert call_args[2:4] == b"\x01\x00"  # adapter_idx = 1

    # Check parameters
    assert call_args[8:14] == b"\x66\x55\x44\x33\x22\x11"  # address (reversed)
    assert call_args[14] == BDADDR_LE_RANDOM  # address_type
    assert call_args[15:17] == MEDIUM_MIN_CONN_INTERVAL.to_bytes(2, "little")
    assert call_args[17:19] == MEDIUM_MAX_CONN_INTERVAL.to_bytes(2, "little")
    assert call_args[19:21] == MEDIUM_CONN_LATENCY.to_bytes(2, "little")
    assert call_args[21:23] == MEDIUM_CONN_TIMEOUT.to_bytes(2, "little")


def test_load_conn_params_no_protocol(caplog: pytest.LogCaptureFixture) -> None:
    """Test load_conn_params when protocol is not connected."""
    ctl = MGMTBluetoothCtl(5.0, {})

    result = ctl.load_conn_params(
        0,
        "AA:BB:CC:DD:EE:FF",
        BDADDR_LE_PUBLIC,
        ConnectParams.FAST,
    )

    assert result is False
    assert "Cannot load conn params: no connection" in caplog.text


def test_load_conn_params_invalid_address(caplog: pytest.LogCaptureFixture) -> None:
    """Test load_conn_params with invalid MAC address."""
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_transport = Mock()
    mock_protocol.transport = mock_transport

    ctl = MGMTBluetoothCtl(5.0, {})
    ctl.protocol = mock_protocol

    # Test with too short address
    result = ctl.load_conn_params(
        0,
        "AA:BB",
        BDADDR_LE_PUBLIC,
        ConnectParams.FAST,
    )

    assert result is False
    assert "Invalid MAC address: AA:BB" in caplog.text


def test_load_conn_params_transport_error(caplog: pytest.LogCaptureFixture) -> None:
    """Test load_conn_params with transport write error."""
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_transport = Mock()
    mock_socket = Mock()
    mock_socket.send.side_effect = Exception("Transport error")
    mock_transport.get_extra_info = Mock(return_value=mock_socket)
    mock_protocol.transport = mock_transport
    mock_protocol._sock = mock_socket
    mock_protocol._write_to_socket = Mock(side_effect=Exception("Transport error"))

    ctl = MGMTBluetoothCtl(5.0, {})
    ctl.protocol = mock_protocol

    result = ctl.load_conn_params(
        0,
        "AA:BB:CC:DD:EE:FF",
        BDADDR_LE_PUBLIC,
        ConnectParams.FAST,
    )

    assert result is False
    assert "Failed to load conn params" in caplog.text


@pytest.mark.asyncio
async def test_load_conn_params_explicit() -> None:
    """Test loading explicit connection parameters."""
    mock_sock = Mock()
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_transport = Mock()
    mock_protocol.transport = mock_transport
    mock_protocol._write_to_socket = Mock()

    ctl = MGMTBluetoothCtl(5.0, {})
    ctl.protocol = mock_protocol
    ctl.sock = mock_sock

    result = ctl.load_conn_params_explicit(
        0,  # adapter_idx
        "AA:BB:CC:DD:EE:FF",  # address
        BDADDR_LE_PUBLIC,  # address_type
        800,  # min_interval
        800,  # max_interval
        0,  # latency
        300,  # timeout
    )

    assert result is True

    mock_protocol._write_to_socket.assert_called_once()
    call_args = mock_protocol._write_to_socket.call_args[0][0]

    # Check header (6 bytes)
    assert call_args[0:2] == b"\x35\x00"  # MGMT_OP_LOAD_CONN_PARAM
    assert call_args[2:4] == b"\x00\x00"  # adapter_idx = 0
    assert call_args[4:6] == b"\x11\x00"  # param_len = 17 (2 + 15)

    # Check command data
    assert call_args[6:8] == b"\x01\x00"  # param_count = 1
    assert call_args[8:14] == b"\xff\xee\xdd\xcc\xbb\xaa"  # address (reversed)
    assert call_args[14] == BDADDR_LE_PUBLIC  # address_type
    assert call_args[15:17] == (800).to_bytes(2, "little")  # min_interval
    assert call_args[17:19] == (800).to_bytes(2, "little")  # max_interval
    assert call_args[19:21] == (0).to_bytes(2, "little")  # latency
    assert call_args[21:23] == (300).to_bytes(2, "little")  # timeout


def test_load_conn_params_explicit_no_protocol(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test load_conn_params_explicit when protocol is not connected."""
    ctl = MGMTBluetoothCtl(5.0, {})

    result = ctl.load_conn_params_explicit(
        0, "AA:BB:CC:DD:EE:FF", BDADDR_LE_PUBLIC, 800, 800, 0, 300
    )

    assert result is False
    assert "Cannot load conn params: no connection" in caplog.text


def test_load_conn_params_explicit_invalid_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test load_conn_params_explicit with invalid MAC address."""
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_transport = Mock()
    mock_protocol.transport = mock_transport

    ctl = MGMTBluetoothCtl(5.0, {})
    ctl.protocol = mock_protocol

    result = ctl.load_conn_params_explicit(
        0, "AA:BB", BDADDR_LE_PUBLIC, 800, 800, 0, 300
    )

    assert result is False
    assert "Invalid MAC address: AA:BB" in caplog.text


def test_load_conn_params_explicit_transport_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test load_conn_params_explicit with transport write error."""
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_transport = Mock()
    mock_protocol.transport = mock_transport
    mock_protocol._write_to_socket = Mock(side_effect=Exception("Transport error"))

    ctl = MGMTBluetoothCtl(5.0, {})
    ctl.protocol = mock_protocol

    result = ctl.load_conn_params_explicit(
        0, "AA:BB:CC:DD:EE:FF", BDADDR_LE_PUBLIC, 800, 800, 0, 300
    )

    assert result is False
    assert "Failed to load conn params" in caplog.text


def test_kernel_bug_workaround_send_returns_zero(
    event_loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that the kernel bug workaround handles send returning 0."""
    future = event_loop.create_future()
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()
    is_shutting_down = Mock(return_value=False)

    # Create a mock socket that returns 0 (kernel bug behavior)
    mock_socket = Mock()
    mock_socket.send = Mock(return_value=0)
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_socket
    )

    # Send some data
    test_data = b"\x25\x00\x00\x00\x00\x00"
    with caplog.at_level(logging.DEBUG):
        protocol._write_to_socket(test_data)

    # Verify the send was called and the workaround logged
    mock_socket.send.assert_called_once_with(test_data)
    assert "kernel bug fix" in caplog.text


def test_kernel_bug_workaround_send_raises_exception(
    event_loop: asyncio.AbstractEventLoop, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that _write_to_socket handles and re-raises exceptions."""
    future = event_loop.create_future()
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()
    is_shutting_down = Mock(return_value=False)

    # Create a mock socket that raises an exception
    mock_socket = Mock()
    mock_socket.send = Mock(side_effect=OSError("Socket error"))
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_socket
    )

    # Send some data and expect the exception to be re-raised
    test_data = b"\x25\x00\x00\x00\x00\x00"
    with pytest.raises(OSError, match="Socket error"):
        protocol._write_to_socket(test_data)

    # Verify the error was logged; the traceback carries the OSError text.
    assert "Failed to write to mgmt socket" in caplog.text
    assert "Socket error" in caplog.text
    mock_socket.send.assert_called_once_with(test_data)


def test_close() -> None:
    """Test close method."""
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_transport = Mock()
    mock_protocol.transport = mock_transport
    mock_sock = Mock()
    mock_reconnect_task = Mock()

    ctl = MGMTBluetoothCtl(5.0, {})
    ctl.protocol = mock_protocol
    ctl.sock = mock_sock
    ctl._reconnect_task = mock_reconnect_task

    with patch("habluetooth.channels.bluez.btmgmt_socket.close") as mock_close:
        ctl.close()

        mock_reconnect_task.cancel.assert_called_once()
        mock_transport.close.assert_called_once()
        mock_close.assert_called_once_with(mock_sock)
        assert ctl.protocol is None


def test_close_no_protocol() -> None:
    """Test close when protocol is not set."""
    ctl = MGMTBluetoothCtl(5.0, {})
    # Should not raise any exception
    with patch("habluetooth.channels.bluez.btmgmt_socket.close"):
        ctl.close()


@pytest.mark.asyncio
async def test_on_connection_lost() -> None:
    """Test _on_connection_lost callback."""
    ctl = MGMTBluetoothCtl(5.0, {})
    loop = asyncio.get_running_loop()
    ctl._on_connection_lost_future = loop.create_future()

    ctl._on_connection_lost()

    # _on_connection_lost sets the future to None after setting result
    assert ctl._on_connection_lost_future is None


@pytest.mark.asyncio
async def test_on_connection_lost_during_shutdown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test _on_connection_lost callback during shutdown."""
    ctl = MGMTBluetoothCtl(5.0, {})
    loop = asyncio.get_running_loop()
    ctl._on_connection_lost_future = loop.create_future()
    ctl._shutting_down = True

    with caplog.at_level(logging.DEBUG):
        ctl._on_connection_lost()

    # Should log shutdown message
    assert "Bluetooth management socket connection lost during shutdown" in caplog.text
    # Should not log reconnecting message
    assert "reconnecting" not in caplog.text
    # _on_connection_lost sets the future to None after setting result
    assert ctl._on_connection_lost_future is None


@pytest.mark.asyncio
async def test_reconnect_task() -> None:
    """Test reconnect_task behavior."""
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_transport = Mock()
    mock_protocol.transport = mock_transport

    establish_count = 0

    ctl = MGMTBluetoothCtl(5.0, {})

    async def mock_establish_connection() -> None:
        nonlocal establish_count
        establish_count += 1
        if establish_count == 1:
            # First call succeeds
            ctl.protocol = mock_protocol
            ctl._on_connection_lost_future = asyncio.get_running_loop().create_future()
        elif establish_count == 2:
            # Second call fails
            msg = "Test error"
            raise BluetoothSocketError(msg)
        else:
            # Stop the test
            raise asyncio.CancelledError

    with patch.object(
        ctl, "_establish_connection", side_effect=mock_establish_connection
    ):
        # Start the reconnect task
        task = asyncio.create_task(ctl.reconnect_task())

        # Trigger reconnection by calling _on_connection_lost
        await asyncio.sleep(0.1)
        ctl._on_connection_lost()

        # Wait for reconnection attempt
        await asyncio.sleep(1.5)

        # Cancel the task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert establish_count >= 2


@pytest.mark.asyncio
async def test_reconnect_task_timeout() -> None:
    """Test reconnect_task with connection timeout."""

    async def mock_establish_connection() -> None:
        msg = "Connection timeout"
        raise TimeoutError(msg)

    ctl = MGMTBluetoothCtl(5.0, {})
    ctl._on_connection_lost_future = None

    with patch.object(
        ctl, "_establish_connection", side_effect=mock_establish_connection
    ):
        # Run reconnect_task briefly
        task = asyncio.create_task(ctl.reconnect_task())
        await asyncio.sleep(0.1)

        # Cancel the task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_reconnect_task_shutdown() -> None:
    """Test reconnect_task exits when shutting down."""
    ctl = MGMTBluetoothCtl(5.0, {})
    loop = asyncio.get_running_loop()

    establish_called = False

    async def mock_establish_connection() -> None:
        nonlocal establish_called
        establish_called = True
        # Should not be called since we're shutting down
        msg = "Should not be called"
        raise AssertionError(msg)

    with patch.object(
        ctl, "_establish_connection", side_effect=mock_establish_connection
    ):
        # Set up connection lost future
        ctl._on_connection_lost_future = loop.create_future()

        # Start the reconnect task
        task = asyncio.create_task(ctl.reconnect_task())

        # Give it a moment to start
        await asyncio.sleep(0)

        # Simulate shutdown
        ctl._shutting_down = True

        # Trigger the future to wake up the task
        ctl._on_connection_lost_future.set_result(None)

        # Task should exit cleanly
        await task

        # _establish_connection should not have been called
        assert not establish_called


@pytest.mark.asyncio
async def test_command_response_context_manager() -> None:
    """Test the command_response context manager."""
    future = asyncio.get_running_loop().create_future()
    future.set_result(None)  # Mark connection as made
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()
    is_shutting_down = Mock(return_value=False)

    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
    )

    # Test successful command response
    opcode = 0x0015  # MGMT_OP_GET_CONNECTIONS
    async with protocol.command_response(opcode, 0) as response_future:
        # Verify we got a future
        assert response_future is not None
        assert isinstance(response_future, asyncio.Future)

        # Simulate receiving a response
        response_data = (
            b"\x01\x00"  # MGMT_EV_CMD_COMPLETE
            b"\x00\x00"  # controller index
            b"\x03\x00"  # param_len (3 bytes: opcode=2 + status=1)
            + opcode.to_bytes(2, "little")  # opcode
            + b"\x00"  # status (success)
        )

        protocol.data_received(response_data)

        # Get the result
        status, _data = await response_future
        assert status == 0  # Success

    # After context exits, future should be resolved
    assert response_future.done()


@pytest.mark.asyncio
async def test_command_response_cleanup_on_exception() -> None:
    """Test that command_response cleans up even if an exception occurs."""
    future = asyncio.get_running_loop().create_future()
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()
    is_shutting_down = Mock(return_value=False)

    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
    )

    opcode = 0x0015  # MGMT_OP_GET_CONNECTIONS

    # Test cleanup on exception
    async def _raise_inside_command_response() -> None:
        async with protocol.command_response(opcode, 0) as response_future:
            assert response_future is not None
            msg = "Test exception"
            raise ValueError(msg)

    with pytest.raises(ValueError, match="Test exception"):
        await _raise_inside_command_response()

    # The future should still exist after exception
    # (cleanup just removes it from internal tracking)


@pytest.mark.asyncio
async def test_get_connections_response_handling() -> None:
    """Test handling of GET_CONNECTIONS command response."""
    future = asyncio.get_running_loop().create_future()
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()
    is_shutting_down = Mock(return_value=False)

    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
    )

    opcode = 0x0015  # MGMT_OP_GET_CONNECTIONS

    # Use the command_response context manager to register the command
    async with protocol.command_response(opcode, 0) as response_future:
        # Test with permission denied status (0x14)
        response_data = (
            b"\x01\x00"  # MGMT_EV_CMD_COMPLETE
            b"\x00\x00"  # controller index
            b"\x03\x00"  # param_len
            + opcode.to_bytes(2, "little")  # opcode
            + b"\x14"  # status (permission denied)
        )

        protocol.data_received(response_data)

        # Verify the future was resolved with the status
        status, data = await response_future
        assert status == 0x14  # Permission denied
        assert data == b""  # No additional data for param_len <= 3


@pytest.mark.asyncio
async def test_get_connections_response_with_data() -> None:
    """Test GET_CONNECTIONS response with additional data."""
    future = asyncio.get_running_loop().create_future()
    scanners: dict[int, BaseHaScanner] = {}
    on_connection_lost = Mock()
    is_shutting_down = Mock(return_value=False)

    mock_sock = Mock()
    protocol = BluetoothMGMTProtocol(
        future, scanners, on_connection_lost, is_shutting_down, mock_sock
    )

    opcode = 0x0015  # MGMT_OP_GET_CONNECTIONS

    # Use the command_response context manager to register the command
    async with protocol.command_response(opcode, 0) as response_future:
        # Test with success status and additional data
        extra_data = b"\x01\x02\x03\x04"
        response_data = (
            b"\x01\x00"  # MGMT_EV_CMD_COMPLETE
            b"\x00\x00"  # controller index
            + (3 + len(extra_data)).to_bytes(
                2, "little"
            )  # param_len (opcode=2 + status=1 + extra_data)
            + opcode.to_bytes(2, "little")  # opcode
            + b"\x00"  # status (success)
            + extra_data  # additional response data
        )

        protocol.data_received(response_data)

        # Verify the future was resolved with status and data
        status, data = await response_future
        assert status == 0  # Success
        assert data == extra_data


@pytest.mark.asyncio
async def test_has_mgmt_capabilities_from_status() -> None:
    """Test _has_mgmt_capabilities_from_status helper function."""
    mgmt_ctl = MGMTBluetoothCtl(timeout=5.0, scanners={})

    # Test permission denied
    assert mgmt_ctl._has_mgmt_capabilities_from_status(0x14) is False

    # Test success
    assert mgmt_ctl._has_mgmt_capabilities_from_status(0x00) is True

    # Test invalid index (still has permissions)
    assert mgmt_ctl._has_mgmt_capabilities_from_status(0x11) is True

    # Test unknown status (assumes no permissions)
    assert mgmt_ctl._has_mgmt_capabilities_from_status(0xFF) is False
    assert mgmt_ctl._has_mgmt_capabilities_from_status(0x01) is False
    assert mgmt_ctl._has_mgmt_capabilities_from_status(0x0D) is False


@pytest.mark.asyncio
async def test_check_capabilities_success() -> None:
    """Test _check_capabilities when permissions are available."""
    mgmt_ctl = MGMTBluetoothCtl(timeout=5.0, scanners={})

    # Mock the protocol and transport
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_transport = Mock()
    mock_protocol.transport = mock_transport
    mgmt_ctl.protocol = mock_protocol

    # Mock command_response to return success
    def mock_command_response(opcode: int, controller_idx: int) -> object:
        future = asyncio.get_running_loop().create_future()
        future.set_result((0x00, b""))  # Success status

        class MockContext:
            async def __aenter__(self) -> asyncio.Future[tuple[int, bytes]]:
                return future

            async def __aexit__(self, *args: object) -> None:
                pass

        return MockContext()

    mock_protocol.command_response = mock_command_response
    # Mock the _write_to_socket method
    mock_protocol._write_to_socket = Mock()

    # Test capability check
    result = await mgmt_ctl._check_capabilities()
    assert result is True

    # Verify the command was sent
    mock_protocol._write_to_socket.assert_called_once()
    sent_data = mock_protocol._write_to_socket.call_args[0][0]
    # Check that it's a GET_CONNECTIONS command (opcode at bytes 0-1)
    assert sent_data[0:2] == b"\x15\x00"  # MGMT_OP_GET_CONNECTIONS little-endian


@pytest.mark.asyncio
async def test_check_capabilities_permission_denied() -> None:
    """Test _check_capabilities when permissions are denied."""
    mgmt_ctl = MGMTBluetoothCtl(timeout=5.0, scanners={})

    # Mock the protocol and transport
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_transport = Mock()
    mock_protocol.transport = mock_transport
    mgmt_ctl.protocol = mock_protocol

    # Mock command_response to return permission denied
    def mock_command_response(opcode: int, controller_idx: int) -> object:
        future = asyncio.get_running_loop().create_future()
        future.set_result((0x14, b""))  # Permission denied status

        class MockContext:
            async def __aenter__(self) -> asyncio.Future[tuple[int, bytes]]:
                return future

            async def __aexit__(self, *args: object) -> None:
                pass

        return MockContext()

    mock_protocol.command_response = mock_command_response

    # Test capability check
    result = await mgmt_ctl._check_capabilities()
    assert result is False


@pytest.mark.asyncio
async def test_check_capabilities_invalid_index() -> None:
    """Test _check_capabilities with invalid adapter index (still has permissions)."""
    mgmt_ctl = MGMTBluetoothCtl(timeout=5.0, scanners={})

    # Mock the protocol and transport
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_transport = Mock()
    mock_protocol.transport = mock_transport
    mgmt_ctl.protocol = mock_protocol

    # Mock command_response to return invalid index
    def mock_command_response(opcode: int, controller_idx: int) -> object:
        future = asyncio.get_running_loop().create_future()
        future.set_result((0x11, b""))  # Invalid index

        class MockContext:
            async def __aenter__(self) -> asyncio.Future[tuple[int, bytes]]:
                return future

            async def __aexit__(self, *args: object) -> None:
                pass

        return MockContext()

    mock_protocol.command_response = mock_command_response

    # Test capability check - invalid index means adapter doesn't exist
    # but we still have permissions
    result = await mgmt_ctl._check_capabilities()
    assert result is True


@pytest.mark.asyncio
async def test_check_capabilities_unknown_status() -> None:
    """Test _check_capabilities with unknown status code."""
    mgmt_ctl = MGMTBluetoothCtl(timeout=5.0, scanners={})

    # Mock the protocol and transport
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_transport = Mock()
    mock_protocol.transport = mock_transport
    mgmt_ctl.protocol = mock_protocol

    # Mock command_response to return unknown status
    def mock_command_response(opcode: int, controller_idx: int) -> object:
        future = asyncio.get_running_loop().create_future()
        future.set_result((0xFF, b""))  # Unknown status

        class MockContext:
            async def __aenter__(self) -> asyncio.Future[tuple[int, bytes]]:
                return future

            async def __aexit__(self, *args: object) -> None:
                pass

        return MockContext()

    mock_protocol.command_response = mock_command_response

    # Test capability check - unknown status assumes no permissions
    result = await mgmt_ctl._check_capabilities()
    assert result is False


@pytest.mark.asyncio
async def test_check_capabilities_timeout() -> None:
    """Test _check_capabilities when command times out."""
    mgmt_ctl = MGMTBluetoothCtl(timeout=5.0, scanners={})

    # Mock the protocol and transport
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_transport = Mock()
    mock_protocol.transport = mock_transport
    mgmt_ctl.protocol = mock_protocol

    # Mock command_response to timeout
    def mock_command_response(opcode: int, controller_idx: int) -> object:
        future = asyncio.get_running_loop().create_future()
        # Never resolve the future

        class MockContext:
            async def __aenter__(self) -> asyncio.Future[tuple[int, bytes]]:
                return future

            async def __aexit__(self, *args: object) -> None:
                pass

        return MockContext()

    mock_protocol.command_response = mock_command_response

    # Test capability check with a very short timeout
    with patch("habluetooth.channels.bluez.asyncio_timeout") as mock_timeout:
        # Make timeout raise immediately
        mock_timeout.side_effect = TimeoutError("Test timeout")

        result = await mgmt_ctl._check_capabilities()
        assert result is False


@pytest.mark.asyncio
async def test_check_capabilities_no_protocol() -> None:
    """Test _check_capabilities when protocol is not set."""
    mgmt_ctl = MGMTBluetoothCtl(timeout=5.0, scanners={})

    # No protocol set
    mgmt_ctl.protocol = None

    result = await mgmt_ctl._check_capabilities()
    assert result is False


@pytest.mark.asyncio
async def test_check_capabilities_no_transport() -> None:
    """Test _check_capabilities when transport is not set."""
    mgmt_ctl = MGMTBluetoothCtl(timeout=5.0, scanners={})

    # Mock protocol with no transport
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_protocol.transport = None
    mgmt_ctl.protocol = mock_protocol

    result = await mgmt_ctl._check_capabilities()
    assert result is False


@pytest.mark.asyncio
async def test_setup_with_failed_capabilities() -> None:
    """Test setup raises PermissionError when capabilities check fails."""
    with (
        patch("habluetooth.channels.bluez.btmgmt_socket") as mock_btmgmt,
        patch.object(MGMTBluetoothCtl, "_establish_connection") as mock_establish,
        patch.object(MGMTBluetoothCtl, "_check_capabilities", return_value=False),
    ):
        mock_socket = Mock()
        mock_socket.fileno.return_value = 99
        mock_btmgmt.open.return_value = mock_socket

        mgmt_ctl = MGMTBluetoothCtl(timeout=5.0, scanners={})

        # Mock successful connection establishment
        mock_establish.return_value = None

        # Set the socket on mgmt_ctl
        mgmt_ctl.sock = mock_socket

        # Mock protocol for close operation
        mock_protocol = Mock()
        mock_transport = Mock()
        mock_protocol.transport = mock_transport
        mgmt_ctl.protocol = mock_protocol

        # Setup should raise PermissionError
        with pytest.raises(PermissionError) as exc_info:
            await mgmt_ctl.setup()

        assert "Missing NET_ADMIN/NET_RAW capabilities" in str(exc_info.value)

        # Verify cleanup
        assert mgmt_ctl._shutting_down is True
        mock_transport.close.assert_called_once()
        mock_btmgmt.close.assert_called_once_with(mock_socket)


def _stub_command_response(
    status: int, data: bytes = b""
) -> Callable[[int, int], object]:
    """Build a command_response stub that resolves with (status, data)."""

    def _command_response(opcode: int, controller_idx: int) -> object:
        future = asyncio.get_running_loop().create_future()
        future.set_result((status, data))

        class MockContext:
            async def __aenter__(self) -> asyncio.Future[tuple[int, bytes]]:
                return future

            async def __aexit__(self, *args: object) -> None:
                pass

        return MockContext()

    return _command_response


def _mgmt_ctl_with_mock_protocol() -> tuple[MGMTBluetoothCtl, Mock]:
    """Return a MGMTBluetoothCtl wired to a mock protocol/transport."""
    mgmt_ctl = MGMTBluetoothCtl(timeout=5.0, scanners={})
    mock_protocol = Mock(spec=BluetoothMGMTProtocol)
    mock_protocol.transport = Mock()
    mock_protocol._write_to_socket = Mock()
    mgmt_ctl.protocol = mock_protocol
    return mgmt_ctl, mock_protocol


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [0x00, 0x0A])
async def test_start_discovery_success(status: int) -> None:
    """start_discovery returns True on success or busy and sends the LE bitmask."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(status)

    assert await mgmt_ctl.start_discovery(0) is True

    sent = mock_protocol._write_to_socket.call_args[0][0]
    # header opcode (2) + controller_idx (2) + param_len (2) + address-type byte
    assert sent[0:2] == MGMT_OP_START_DISCOVERY.to_bytes(2, "little")
    assert sent[2:4] == b"\x00\x00"
    assert sent[6] == SCAN_TYPE_LE


@pytest.mark.asyncio
async def test_start_discovery_failure() -> None:
    """start_discovery returns False on an error status."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x0C)

    assert await mgmt_ctl.start_discovery(0) is False


@pytest.mark.asyncio
async def test_stop_discovery_success() -> None:
    """stop_discovery returns True and targets the requested controller."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x00)

    assert await mgmt_ctl.stop_discovery(2) is True

    sent = mock_protocol._write_to_socket.call_args[0][0]
    assert sent[0:2] == MGMT_OP_STOP_DISCOVERY.to_bytes(2, "little")
    assert sent[2:4] == (2).to_bytes(2, "little")


@pytest.mark.asyncio
async def test_stop_discovery_busy_is_not_success() -> None:
    """BUSY on stop means discovery did not stop, so it must report False."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x0A)

    assert await mgmt_ctl.stop_discovery(0) is False


@pytest.mark.asyncio
async def test_discovery_command_timeout() -> None:
    """A command whose response never arrives times out and reports failure."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mgmt_ctl.timeout = 0.01

    def _never_resolving(opcode: int, controller_idx: int) -> object:
        future = asyncio.get_running_loop().create_future()  # never resolved

        class MockContext:
            async def __aenter__(self) -> asyncio.Future[tuple[int, bytes]]:
                return future

            async def __aexit__(self, *args: object) -> None:
                pass

        return MockContext()

    mock_protocol.command_response = _never_resolving

    assert await mgmt_ctl.start_discovery(0) is False


@pytest.mark.asyncio
async def test_discovery_no_connection() -> None:
    """Discovery and monitor commands fail gracefully when the socket is down."""
    mgmt_ctl = MGMTBluetoothCtl(timeout=5.0, scanners={})
    assert mgmt_ctl.protocol is None
    assert await mgmt_ctl.start_discovery(0) is False
    assert await mgmt_ctl.stop_discovery(0) is False
    assert await mgmt_ctl.add_adv_pattern_monitor(0) is None
    assert await mgmt_ctl.remove_adv_monitor(0, 7) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_type", [OSError, BluetoothSocketError])
async def test_discovery_command_response_error(exc_type: type[Exception]) -> None:
    """
    A socket error while sending a command is handled, not raised.

    BluetoothSocketError is not an OSError subclass, so it must be caught
    explicitly; _write_to_socket can raise either one.
    """
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()

    def _raising_command_response(opcode: int, controller_idx: int) -> object:
        class MockContext:
            async def __aenter__(self) -> asyncio.Future[tuple[int, bytes]]:
                msg = "socket gone"
                raise exc_type(msg)

            async def __aexit__(self, *args: object) -> None:
                pass

        return MockContext()

    mock_protocol.command_response = _raising_command_response

    assert await mgmt_ctl.start_discovery(0) is False
    assert await mgmt_ctl.add_adv_pattern_monitor(0) is None
    assert await mgmt_ctl.remove_adv_monitor(0, 7) is False


@pytest.mark.asyncio
async def test_command_response_skips_already_resolved_future() -> None:
    """A command-complete for an already-resolved future does not re-set it."""
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    future.set_result(None)
    protocol = BluetoothMGMTProtocol(
        future, {}, Mock(), Mock(return_value=False), Mock()
    )
    async with protocol.command_response(MGMT_OP_START_DISCOVERY, 0) as fut:
        # Resolve early, then deliver a late response for the same command.
        fut.set_result((0x00, b""))
        protocol.data_received(
            b"\x01\x00"  # MGMT_EV_CMD_COMPLETE
            b"\x00\x00"  # controller index 0
            b"\x03\x00"  # param_len
            + MGMT_OP_START_DISCOVERY.to_bytes(2, "little")
            + b"\x05"  # different status, must be ignored
        )
        assert fut.result() == (0x00, b"")


@pytest.mark.asyncio
async def test_command_response_for_unregistered_command_is_ignored() -> None:
    """A command-complete with no waiter is dropped without breaking later ones."""
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    future.set_result(None)
    protocol = BluetoothMGMTProtocol(
        future, {}, Mock(), Mock(return_value=False), Mock()
    )
    # No command_response registered for this opcode/controller: must not raise.
    protocol.data_received(
        b"\x01\x00"  # MGMT_EV_CMD_COMPLETE
        b"\x00\x00"  # controller index 0
        b"\x03\x00"  # param_len
        + MGMT_OP_START_DISCOVERY.to_bytes(2, "little")
        + b"\x00"  # status success
    )
    # A subsequently registered command still resolves normally.
    async with protocol.command_response(MGMT_OP_STOP_DISCOVERY, 0) as fut:
        protocol.data_received(
            b"\x01\x00"
            b"\x00\x00"
            b"\x03\x00" + MGMT_OP_STOP_DISCOVERY.to_bytes(2, "little") + b"\x00"
        )
        status, _ = await fut
        assert status == 0x00


@pytest.mark.asyncio
async def test_add_adv_pattern_monitor_success() -> None:
    """add_adv_pattern_monitor returns the handle and sends a match-all monitor."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    # Command complete returns the assigned monitor handle (uint16 little-endian).
    mock_protocol.command_response = _stub_command_response(0x00, b"\x07\x00")

    assert await mgmt_ctl.add_adv_pattern_monitor(0) == 7

    sent = mock_protocol._write_to_socket.call_args[0][0]
    assert sent[0:2] == MGMT_OP_ADD_ADV_PATTERNS_MONITOR.to_bytes(2, "little")
    # param_len 1, pattern_count 0 (match all)
    assert sent[4:6] == b"\x01\x00"
    assert sent[6] == 0x00


@pytest.mark.asyncio
async def test_add_adv_pattern_monitor_failure() -> None:
    """add_adv_pattern_monitor returns None on an error status."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x0C, b"")

    assert await mgmt_ctl.add_adv_pattern_monitor(0) is None


@pytest.mark.asyncio
async def test_add_adv_pattern_monitor_success_without_handle() -> None:
    """A success response missing the 2-byte handle is reported as failure."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x00, b"")

    assert await mgmt_ctl.add_adv_pattern_monitor(0) is None


@pytest.mark.asyncio
async def test_remove_adv_monitor() -> None:
    """remove_adv_monitor returns True on success and sends the handle."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x00)

    assert await mgmt_ctl.remove_adv_monitor(0, 7) is True

    sent = mock_protocol._write_to_socket.call_args[0][0]
    assert sent[0:2] == MGMT_OP_REMOVE_ADV_MONITOR.to_bytes(2, "little")
    assert sent[6:8] == (7).to_bytes(2, "little")


@pytest.mark.asyncio
async def test_remove_adv_monitor_failure() -> None:
    """remove_adv_monitor returns False on an error status."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x0C)

    assert await mgmt_ctl.remove_adv_monitor(0, 7) is False


@pytest.mark.asyncio
async def test_per_controller_command_response_isolation() -> None:
    """Concurrent same-opcode commands on two controllers do not collide."""
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    future.set_result(None)
    protocol = BluetoothMGMTProtocol(
        future, {}, Mock(), Mock(return_value=False), Mock()
    )

    async with (
        protocol.command_response(MGMT_OP_START_DISCOVERY, 0) as fut0,
        protocol.command_response(MGMT_OP_START_DISCOVERY, 1) as fut1,
    ):
        # Command complete for controller 1 only.
        protocol.data_received(
            b"\x01\x00"  # MGMT_EV_CMD_COMPLETE
            b"\x01\x00"  # controller index 1
            b"\x03\x00"  # param_len
            + MGMT_OP_START_DISCOVERY.to_bytes(2, "little")
            + b"\x00"  # status success
        )
        assert fut1.done()
        assert not fut0.done()
        status, _ = await fut1
        assert status == 0x00


def test_bytes_mac_to_str() -> None:
    """Test bytes_mac_to_str."""
    assert bytes_mac_to_str(b"\xff\xee\xdd\xcc\xbb\xaa") == "AA:BB:CC:DD:EE:FF"
    assert bytes_mac_to_str(b"\xff\xee\xdd\xcc\xbb\xaa") == "AA:BB:CC:DD:EE:FF"


def test_make_bluez_details() -> None:
    """Test make_bluez_details."""
    assert make_bluez_details("AA:BB:CC:DD:EE:FF", "hci0") == {
        "path": "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
        "props": {"Adapter": "/org/bluez/hci0"},
    }


# -- pairing / disconnect commands ----------------------------------------
@pytest.mark.asyncio
async def test_pair_device_success() -> None:
    """pair_device sends bdaddr + type + IO capability and returns True."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x00)

    assert await mgmt_ctl.pair_device(0, "AA:BB:CC:DD:EE:FF", BDADDR_LE_RANDOM) is True

    sent = mock_protocol._write_to_socket.call_args[0][0]
    assert sent[0:2] == MGMT_OP_PAIR_DEVICE.to_bytes(2, "little")
    assert sent[2:4] == b"\x00\x00"  # controller idx
    assert sent[6:12] == bytes([0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA])  # reversed
    assert sent[12] == BDADDR_LE_RANDOM
    assert sent[13] == IO_CAPABILITY_NO_INPUT_NO_OUTPUT


@pytest.mark.asyncio
async def test_pair_device_failure() -> None:
    """pair_device returns False on a non-success status."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x03)
    assert await mgmt_ctl.pair_device(0, "AA:BB:CC:DD:EE:FF", BDADDR_LE_PUBLIC) is False


@pytest.mark.asyncio
async def test_unpair_device_success() -> None:
    """unpair_device sends bdaddr + type + disconnect flag and returns True."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x00)

    assert (
        await mgmt_ctl.unpair_device(0, "AA:BB:CC:DD:EE:FF", BDADDR_LE_PUBLIC) is True
    )

    sent = mock_protocol._write_to_socket.call_args[0][0]
    assert sent[0:2] == MGMT_OP_UNPAIR_DEVICE.to_bytes(2, "little")
    assert sent[6:12] == bytes([0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA])
    assert sent[12] == BDADDR_LE_PUBLIC
    assert sent[13] == 1  # disconnect default


@pytest.mark.asyncio
async def test_unpair_device_without_disconnect() -> None:
    """unpair_device(disconnect=False) clears the disconnect flag."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x00)

    assert (
        await mgmt_ctl.unpair_device(
            0, "AA:BB:CC:DD:EE:FF", BDADDR_LE_PUBLIC, disconnect=False
        )
        is True
    )
    assert mock_protocol._write_to_socket.call_args[0][0][13] == 0


@pytest.mark.asyncio
async def test_disconnect_device_success() -> None:
    """disconnect_device sends bdaddr + type and returns True."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x00)

    assert (
        await mgmt_ctl.disconnect_device(0, "AA:BB:CC:DD:EE:FF", BDADDR_LE_RANDOM)
        is True
    )

    sent = mock_protocol._write_to_socket.call_args[0][0]
    assert sent[0:2] == MGMT_OP_DISCONNECT.to_bytes(2, "little")
    assert sent[6:12] == bytes([0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA])
    assert sent[12] == BDADDR_LE_RANDOM


@pytest.mark.asyncio
async def test_disconnect_device_no_response() -> None:
    """A command with no response (timeout) returns False."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.transport = None  # no connection -> _send_command_await None
    assert (
        await mgmt_ctl.disconnect_device(0, "AA:BB:CC:DD:EE:FF", BDADDR_LE_PUBLIC)
        is False
    )


@pytest.mark.asyncio
async def test_unpair_device_failure() -> None:
    """unpair_device returns False on a non-success status."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x03)
    assert (
        await mgmt_ctl.unpair_device(0, "AA:BB:CC:DD:EE:FF", BDADDR_LE_PUBLIC) is False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    ["pair_device", "unpair_device", "disconnect_device"],
)
@pytest.mark.parametrize(
    "bad_address",
    ["AA:BB", "ZZ:BB:CC:DD:EE:FF"],  # too short, and non-hex
)
async def test_pairing_commands_reject_invalid_address(
    command: str, bad_address: str
) -> None:
    """A malformed address is a soft failure, not an exception."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x00)
    assert await getattr(mgmt_ctl, command)(0, bad_address, BDADDR_LE_PUBLIC) is False
    mock_protocol._write_to_socket.assert_not_called()


# -- load long-term keys --------------------------------------------------
def _ltk(address: str = "AA:BB:CC:DD:EE:FF") -> LongTermKey:
    return LongTermKey(
        address=address,
        address_type=BDADDR_LE_RANDOM,
        key_type=1,
        central=False,
        encryption_size=16,
        ediv=0x1234,
        rand=bytes(range(8)),
        value=bytes(range(16)),
    )


@pytest.mark.asyncio
async def test_load_long_term_keys_success() -> None:
    """load_long_term_keys packs the count and a 36 byte record per key."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x00)

    assert await mgmt_ctl.load_long_term_keys(0, [_ltk()]) is True

    sent = mock_protocol._write_to_socket.call_args[0][0]
    assert sent[0:2] == MGMT_OP_LOAD_LONG_TERM_KEYS.to_bytes(2, "little")
    assert sent[6:8] == (1).to_bytes(2, "little")  # key_count
    record = sent[8:]
    assert len(record) == 36
    assert record[0:6] == bytes([0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA])  # reversed
    assert record[6] == BDADDR_LE_RANDOM
    assert record[7] == 1  # key_type
    assert record[8] == 0  # central flag (False)
    assert record[9] == 16  # encryption_size
    assert record[10:12] == (0x1234).to_bytes(2, "little")  # ediv
    assert record[12:20] == bytes(range(8))  # rand
    assert record[20:36] == bytes(range(16))  # value


@pytest.mark.asyncio
async def test_load_long_term_keys_empty_clears() -> None:
    """An empty key list sends a zero count (clears the controller's keys)."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x00)

    assert await mgmt_ctl.load_long_term_keys(0, []) is True
    sent = mock_protocol._write_to_socket.call_args[0][0]
    assert sent[6:8] == (0).to_bytes(2, "little")
    assert len(sent[8:]) == 0


@pytest.mark.asyncio
async def test_load_long_term_keys_failure() -> None:
    """A non-success status returns False."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x03)
    assert await mgmt_ctl.load_long_term_keys(0, [_ltk()]) is False


@pytest.mark.asyncio
async def test_load_long_term_keys_rejects_invalid_address() -> None:
    """A malformed address in a key is a soft failure with nothing sent."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x00)
    assert await mgmt_ctl.load_long_term_keys(0, [_ltk("AA:BB")]) is False
    mock_protocol._write_to_socket.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        LongTermKey("AA:BB:CC:DD:EE:FF", 1, 1, False, 16, 0, b"\x00" * 7, b"\x00" * 16),
        LongTermKey("AA:BB:CC:DD:EE:FF", 1, 1, False, 16, 0, b"\x00" * 8, b"\x00" * 15),
    ],
)
async def test_load_long_term_keys_rejects_malformed_key(key: LongTermKey) -> None:
    """A key with the wrong rand/value length is rejected without sending."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x00)
    assert await mgmt_ctl.load_long_term_keys(0, [key]) is False
    mock_protocol._write_to_socket.assert_not_called()


@pytest.mark.asyncio
async def test_load_long_term_keys_rejects_out_of_range_field() -> None:
    """An out-of-range integer field is a soft failure, not a struct.error."""
    mgmt_ctl, mock_protocol = _mgmt_ctl_with_mock_protocol()
    mock_protocol.command_response = _stub_command_response(0x00)
    bad = LongTermKey(
        "AA:BB:CC:DD:EE:FF", 1, 300, False, 16, 0, b"\x00" * 8, b"\x00" * 16
    )  # key_type 300 does not fit in a byte
    assert await mgmt_ctl.load_long_term_keys(0, [bad]) is False
    mock_protocol._write_to_socket.assert_not_called()
