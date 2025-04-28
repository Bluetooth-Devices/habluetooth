import asyncio

from habluetooth.channels.bluez import MGMTBluetoothCtl


async def main() -> None:
    """Main function to test the Bluetooth management API."""
    # Create an instance of MGMTBluetoothCtl
    mgmt_ctl = MGMTBluetoothCtl(timeout=5.0)

    # Set up the management interface
    await mgmt_ctl.setup()

    try:
        await asyncio.Event().wait()
    finally:
        # Close the management interface when done
        await mgmt_ctl.close()


if __name__ == "__main__":
    # Run the main function
    asyncio.run(main())
