import asyncio

from habluetooth import BluetoothScanningMode
from habluetooth.channels.bluez import MGMTBluetoothCtl
from habluetooth.scanner import HaScanner

int_ = int


class LoggingHaScanner(HaScanner):
    """Logging ha scanner."""

    def _async_on_raw_bluez_advertisement(
        self,
        address: bytes,
        address_type: int_,
        rssi: int_,
        flags: int_,
        data: bytes,
    ) -> None:
        """Handle raw advertisement data."""
        print(
            f"address={address!r}, address_type={address_type}, "
            f"rssi={rssi}, flags={flags}, data={data!r}"
        )


async def main() -> None:
    """Main function to test the Bluetooth management API."""
    # Create an instance of MGMTBluetoothCtl
    scanner = LoggingHaScanner(
        BluetoothScanningMode.ACTIVE, "hci0", "AA:BB:CC:DD:EE:FF"
    )
    mgmt_ctl = MGMTBluetoothCtl(timeout=5.0, scanners={0: scanner})

    # Set up the management interface
    await mgmt_ctl.setup()

    try:
        await asyncio.Event().wait()
    finally:
        # Close the management interface when done
        mgmt_ctl.close()


if __name__ == "__main__":
    # Run the main function
    asyncio.run(main())
