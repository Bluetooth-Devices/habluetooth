"""Tests for the BlueZ management control module."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import Mock, patch

import pytest
from btsocket.btmgmt_socket import BluetoothSocketError

from habluetooth.channels.bluez import BluetoothMGMTProtocol
from habluetooth.channels.bluez_mgmt import MGMTBluetoothCtl
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
        patch(
            "habluetooth.channels.bluez_mgmt.btmgmt_socket.open", return_value=mock_sock
        ),
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
        patch(
            "habluetooth.channels.bluez_mgmt.btmgmt_socket.open", return_value=mock_sock
        ),
        patch.object(
            asyncio.get_running_loop(),
            "_create_connection_transport",
            side_effect=slow_connect,
        ),
        patch("habluetooth.channels.bluez_mgmt.btmgmt_socket.close") as mock_close,
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
    mock_transport.write.assert_called_once()
    call_args = mock_transport.write.call_args[0][0]

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
    mock_transport.write.assert_called_once()
    call_args = mock_transport.write.call_args[0][0]

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
    mock_transport.write.side_effect = Exception("Transport error")
    mock_protocol.transport = mock_transport

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

    with patch("habluetooth.channels.bluez_mgmt.btmgmt_socket.close") as mock_close:
        ctl.close()

        mock_reconnect_task.cancel.assert_called_once()
        mock_transport.close.assert_called_once()
        mock_close.assert_called_once_with(mock_sock)
        assert ctl.protocol is None


def test_close_no_protocol() -> None:
    """Test close when protocol is not set."""
    ctl = MGMTBluetoothCtl(5.0, {})
    # Should not raise any exception
    with patch("habluetooth.channels.bluez_mgmt.btmgmt_socket.close"):
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
            raise BluetoothSocketError("Test error")
        else:
            # Stop the test
            raise asyncio.CancelledError()

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
        raise TimeoutError("Connection timeout")

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
        raise Exception("Should not be called")

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

    # Mock the new setup/cleanup pattern
    def mock_setup_command_response(opcode: int) -> asyncio.Future[tuple[int, bytes]]:
        future = asyncio.get_running_loop().create_future()
        future.set_result((0x00, b""))  # Success status
        return future

    mock_protocol.setup_command_response = mock_setup_command_response
    mock_protocol.cleanup_command_response = Mock()

    # Test capability check
    result = await mgmt_ctl._check_capabilities()
    assert result is True

    # Verify the command was sent
    mock_transport.write.assert_called_once()
    sent_data = mock_transport.write.call_args[0][0]
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

    # Mock the new setup/cleanup pattern
    def mock_setup_command_response(opcode: int) -> asyncio.Future[tuple[int, bytes]]:
        future = asyncio.get_running_loop().create_future()
        future.set_result((0x14, b""))  # Permission denied status
        return future

    mock_protocol.setup_command_response = mock_setup_command_response
    mock_protocol.cleanup_command_response = Mock()

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

    # Mock the new setup/cleanup pattern
    def mock_setup_command_response(opcode: int) -> asyncio.Future[tuple[int, bytes]]:
        future = asyncio.get_running_loop().create_future()
        future.set_result((0x11, b""))  # Invalid index
        return future

    mock_protocol.setup_command_response = mock_setup_command_response
    mock_protocol.cleanup_command_response = Mock()

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

    # Mock the new setup/cleanup pattern
    def mock_setup_command_response(opcode: int) -> asyncio.Future[tuple[int, bytes]]:
        future = asyncio.get_running_loop().create_future()
        future.set_result((0xFF, b""))  # Unknown status
        return future

    mock_protocol.setup_command_response = mock_setup_command_response
    mock_protocol.cleanup_command_response = Mock()

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

    # Mock the new setup/cleanup pattern to timeout
    def mock_setup_command_response(opcode: int) -> asyncio.Future[tuple[int, bytes]]:
        return asyncio.get_running_loop().create_future()
        # Never resolve the future

    mock_protocol.setup_command_response = mock_setup_command_response
    mock_protocol.cleanup_command_response = Mock()

    # Test capability check with a very short timeout
    with patch("habluetooth.channels.bluez_mgmt.asyncio_timeout") as mock_timeout:
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
        patch("habluetooth.channels.bluez_mgmt.btmgmt_socket") as mock_btmgmt,
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
