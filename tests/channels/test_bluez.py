"""Tests for the BlueZ management API module."""

from __future__ import annotations

import asyncio
from unittest.mock import Mock, patch

import pytest
from btsocket.btmgmt_socket import BluetoothSocketError

from habluetooth.channels.bluez import (
    BluetoothMGMTProtocol,
    MGMTBluetoothCtl,
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
from habluetooth.scanner import HaScanner


@pytest.fixture
def mock_scanner() -> Mock:
    """Create a mock scanner."""
    scanner = Mock(spec=HaScanner)
    scanner._async_on_raw_bluez_advertisement = Mock()
    return scanner


@pytest.fixture
def mock_transport() -> Mock:
    """Create a mock transport."""
    transport = Mock()
    transport.write = Mock()
    return transport


class TestBluetoothMGMTProtocol:
    """Test the BluetoothMGMTProtocol class."""

    def test_connection_made(self, mock_transport: Mock) -> None:
        """Test connection_made sets up the protocol correctly."""
        future: asyncio.Future[None] = asyncio.Future()
        scanners: dict[int, HaScanner] = {}
        on_connection_lost = Mock()

        protocol = BluetoothMGMTProtocol(future, scanners, on_connection_lost)
        protocol.connection_made(mock_transport)

        assert protocol.transport is mock_transport
        assert future.done()
        assert future.result() is None

    def test_connection_lost(self, mock_transport: Mock) -> None:
        """Test connection_lost handles disconnection."""
        future: asyncio.Future[None] = asyncio.Future()
        scanners: dict[int, HaScanner] = {}
        on_connection_lost = Mock()

        protocol = BluetoothMGMTProtocol(future, scanners, on_connection_lost)
        protocol.connection_made(mock_transport)

        # Test with exception
        protocol.connection_lost(Exception("Test error"))
        assert protocol.transport is None
        on_connection_lost.assert_called_once()

    def test_connection_lost_no_exception(
        self, mock_transport: Mock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test connection_lost without exception."""
        future: asyncio.Future[None] = asyncio.Future()
        scanners: dict[int, HaScanner] = {}
        on_connection_lost = Mock()

        protocol = BluetoothMGMTProtocol(future, scanners, on_connection_lost)
        protocol.connection_made(mock_transport)

        # Test without exception
        protocol.connection_lost(None)
        assert "Bluetooth management socket connection closed" in caplog.text

    def test_data_received_device_found(self, mock_scanner: Mock) -> None:
        """Test data_received handles DEVICE_FOUND event."""
        future: asyncio.Future[None] = asyncio.Future()
        scanners: dict[int, HaScanner] = {0: mock_scanner}
        on_connection_lost = Mock()

        protocol = BluetoothMGMTProtocol(future, scanners, on_connection_lost)

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

    def test_data_received_adv_monitor_device_found(self, mock_scanner: Mock) -> None:
        """Test data_received handles ADV_MONITOR_DEVICE_FOUND event."""
        future: asyncio.Future[None] = asyncio.Future()
        scanners: dict[int, HaScanner] = {0: mock_scanner}
        on_connection_lost = Mock()

        protocol = BluetoothMGMTProtocol(future, scanners, on_connection_lost)

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
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test data_received handles successful MGMT_EV_CMD_COMPLETE."""
        future: asyncio.Future[None] = asyncio.Future()
        scanners: dict[int, HaScanner] = {}
        on_connection_lost = Mock()

        protocol = BluetoothMGMTProtocol(future, scanners, on_connection_lost)

        # Create a CMD_COMPLETE event for LOAD_CONN_PARAM
        header = b"\x01\x00"  # MGMT_EV_CMD_COMPLETE
        header += b"\x00\x00"  # controller_idx = 0
        header += b"\x03\x00"  # param_len = 3

        params = b"\x35\x00"  # opcode = MGMT_OP_LOAD_CONN_PARAM
        params += b"\x00"  # status = 0 (success)

        protocol.data_received(header + params)

        assert "Connection parameters loaded successfully" in caplog.text

    def test_data_received_cmd_complete_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test data_received handles failed MGMT_EV_CMD_COMPLETE."""
        future: asyncio.Future[None] = asyncio.Future()
        scanners: dict[int, HaScanner] = {}
        on_connection_lost = Mock()

        protocol = BluetoothMGMTProtocol(future, scanners, on_connection_lost)

        # Create a CMD_COMPLETE event with failure
        header = b"\x01\x00"  # MGMT_EV_CMD_COMPLETE
        header += b"\x01\x00"  # controller_idx = 1
        header += b"\x03\x00"  # param_len = 3

        params = b"\x35\x00"  # opcode = MGMT_OP_LOAD_CONN_PARAM
        params += b"\x0c"  # status = 12 (Not Supported)

        protocol.data_received(header + params)

        assert "Failed to load conn params: status=12" in caplog.text

    def test_data_received_cmd_status(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test data_received handles MGMT_EV_CMD_STATUS."""
        future: asyncio.Future[None] = asyncio.Future()
        scanners: dict[int, HaScanner] = {}
        on_connection_lost = Mock()

        protocol = BluetoothMGMTProtocol(future, scanners, on_connection_lost)

        # Create a CMD_STATUS event
        header = b"\x02\x00"  # MGMT_EV_CMD_STATUS
        header += b"\x00\x00"  # controller_idx = 0
        header += b"\x03\x00"  # param_len = 3

        params = b"\x35\x00"  # opcode = MGMT_OP_LOAD_CONN_PARAM
        params += b"\x01"  # status = 1 (Unknown Command)

        protocol.data_received(header + params)

        assert "Failed to load conn params: status=1" in caplog.text

    def test_data_received_partial_data(self, mock_scanner: Mock) -> None:
        """Test data_received handles partial data correctly."""
        future: asyncio.Future[None] = asyncio.Future()
        scanners: dict[int, HaScanner] = {0: mock_scanner}
        on_connection_lost = Mock()

        protocol = BluetoothMGMTProtocol(future, scanners, on_connection_lost)

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

    def test_data_received_unknown_event(self) -> None:
        """Test data_received ignores unknown events."""
        future: asyncio.Future[None] = asyncio.Future()
        scanners: dict[int, HaScanner] = {}
        on_connection_lost = Mock()

        protocol = BluetoothMGMTProtocol(future, scanners, on_connection_lost)

        # Create an unknown event
        header = b"\xff\x00"  # Unknown event code
        header += b"\x00\x00"  # controller_idx = 0
        header += b"\x04\x00"  # param_len = 4
        params = b"\x00\x00\x00\x00"

        # Should not raise any exception
        protocol.data_received(header + params)

    def test_data_received_no_scanner_for_controller(self) -> None:
        """Test data_received handles missing scanner gracefully."""
        future: asyncio.Future[None] = asyncio.Future()
        scanners: dict[int, HaScanner] = {}  # No scanner for controller 0
        on_connection_lost = Mock()

        protocol = BluetoothMGMTProtocol(future, scanners, on_connection_lost)

        # Create a DEVICE_FOUND event for controller 0
        ad_data = b"\x02\x01\x06"
        param_len = 6 + 1 + 1 + 4 + 2 + len(ad_data)

        header = b"\x12\x00\x00\x00" + param_len.to_bytes(2, "little")
        params = b"\xaa\xbb\xcc\xdd\xee\xff\x01\xc8\x00\x00\x00\x00"
        params += len(ad_data).to_bytes(2, "little") + ad_data

        # Should not raise any exception
        protocol.data_received(header + params)


class TestMGMTBluetoothCtl:
    """Test the MGMTBluetoothCtl class."""

    @pytest.mark.asyncio
    async def test_setup_success(self):
        """Test successful setup."""
        mock_sock = Mock()
        mock_sock.fileno.return_value = 1  # Mock socket file descriptor
        mock_protocol = Mock(spec=BluetoothMGMTProtocol)
        mock_transport = Mock()
        mock_protocol.transport = mock_transport

        # Mock the future that gets created and set
        mock_future: asyncio.Future[None] = asyncio.Future()

        async def mock_create_connection(*args, **kwargs):
            # Set the future result to simulate connection made
            mock_future.set_result(None)
            return mock_transport, mock_protocol

        with patch(
            "habluetooth.channels.bluez.btmgmt_socket.open", return_value=mock_sock
        ):
            with patch.object(
                asyncio.get_running_loop(),
                "_create_connection_transport",
                side_effect=mock_create_connection,
            ):
                with patch.object(
                    asyncio.get_running_loop(),
                    "create_future",
                    side_effect=[
                        mock_future,
                        asyncio.Future(),
                    ],  # First for connection, second for on_connection_lost
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
    async def test_setup_timeout(self):
        """Test setup timeout."""
        mock_sock = Mock()

        async def slow_connect(*args, **kwargs):
            await asyncio.sleep(10)

        with patch(
            "habluetooth.channels.bluez.btmgmt_socket.open", return_value=mock_sock
        ):
            with patch.object(
                asyncio.get_running_loop(),
                "_create_connection_transport",
                side_effect=slow_connect,
            ):
                with patch(
                    "habluetooth.channels.bluez.btmgmt_socket.close"
                ) as mock_close:
                    ctl = MGMTBluetoothCtl(0.1, {})
                    with pytest.raises(TimeoutError):
                        await ctl.setup()

                    mock_close.assert_called_once_with(mock_sock)

    @pytest.mark.asyncio
    async def test_load_conn_params_fast(self):
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
    async def test_load_conn_params_medium(self):
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

    def test_load_conn_params_no_protocol(self, caplog):
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

    def test_load_conn_params_invalid_address(self, caplog):
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

    def test_load_conn_params_transport_error(self, caplog):
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
        assert "Failed to load conn params: Transport error" in caplog.text

    def test_close(self):
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

    def test_close_no_protocol(self):
        """Test close when protocol is not set."""
        ctl = MGMTBluetoothCtl(5.0, {})
        # Should not raise any exception
        with patch("habluetooth.channels.bluez.btmgmt_socket.close"):
            ctl.close()

    @pytest.mark.asyncio
    async def test_on_connection_lost(self):
        """Test _on_connection_lost callback."""
        ctl = MGMTBluetoothCtl(5.0, {})
        loop = asyncio.get_running_loop()
        ctl._on_connection_lost_future = loop.create_future()

        ctl._on_connection_lost()

        # _on_connection_lost sets the future to None after setting result
        assert ctl._on_connection_lost_future is None

    @pytest.mark.asyncio
    async def test_reconnect_task(self) -> None:
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
                ctl._on_connection_lost_future = (
                    asyncio.get_running_loop().create_future()
                )
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
    async def test_reconnect_task_timeout(self) -> None:
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
