# Changelog

## v3.0.0 (2024-05-02)

### Breaking

- Make generation of advertisementdata lazy (#42) ([`25f8437`](https://github.com/Bluetooth-Devices/habluetooth/commit/25f843795927ad663a1d5ef1fa9472ec366b9da5))

## v2.8.1 (2024-05-02)

### Fix

- Add missing find_device_by_address mapping (#43) ([`cc8e57e`](https://github.com/Bluetooth-Devices/habluetooth/commit/cc8e57eef7b97a6f2a30488a64d156cb5023c6c6))

## v2.8.0 (2024-04-17)

### Feature

- Add support for recovering failed adapters after reboot (#40) ([`04948c3`](https://github.com/Bluetooth-Devices/habluetooth/commit/04948c337adf0f7b291e4e33618a7eae6dc4ebc2))

## v2.7.0 (2024-04-17)

### Feature

- Improve fallback to passive mode when active mode fails (#39) ([`17ecc01`](https://github.com/Bluetooth-Devices/habluetooth/commit/17ecc012e096bec0113efea9ceb6a21bb50023fe))

## v2.6.0 (2024-04-17)

### Feature

- Speed up stopping the scanner when its stuck setting up (#37) ([`bba8b51`](https://github.com/Bluetooth-Devices/habluetooth/commit/bba8b514490d98dca1020bbfefd9dc1e6a79af5f))

## v2.5.3 (2024-04-17)

### Fix

- Ensure scanner is stopped on cancellation (#36) ([`a21d70a`](https://github.com/Bluetooth-Devices/habluetooth/commit/a21d70a1ac88135eade61c0abc8912c5b04a6b8b))

## v2.5.2 (2024-04-16)

### Fix

- Ensure discovered_devices returns an empty list for offline scanners (#35) ([`2350543`](https://github.com/Bluetooth-Devices/habluetooth/commit/23505437c98529f692ab2dc0f5c3bdb5c9b7e3bd))

## v2.5.1 (2024-04-16)

### Fix

- Wheel builds (#34) ([`5bd671a`](https://github.com/Bluetooth-Devices/habluetooth/commit/5bd671a159292dffe30a69639411926d0bc28123))

## v2.5.0 (2024-04-16)

### Feature

- Fallback to passive scanning if active cannot start (#33) ([`3fae981`](https://github.com/Bluetooth-Devices/habluetooth/commit/3fae98162e6b0279375823a3b6e60ee51b87c1bb))

## v2.4.2 (2024-02-29)

### Fix

- Android beacons in passive mode with flags 0x02 (#31) ([`8330e18`](https://github.com/Bluetooth-Devices/habluetooth/commit/8330e187550ec00ed415d3650a2c231921fb8ae7))

## v2.4.1 (2024-02-23)

### Fix

- Avoid concurrent refreshes of adapters (#30) ([`d355b17`](https://github.com/Bluetooth-Devices/habluetooth/commit/d355b1768705706dec7062ad5d6267089d87a88e))

## v2.4.0 (2024-01-22)

### Feature

- Improve error reporting resolution suggestions (#29) ([`afff5ba`](https://github.com/Bluetooth-Devices/habluetooth/commit/afff5ba4dfd8a5582174b367ae5ed9c9953b81e9))

## v2.3.1 (2024-01-22)

### Fix

- Ensure unavailable callbacks can be removed from fired callbacks (#28) ([`65e7706`](https://github.com/Bluetooth-Devices/habluetooth/commit/65e7706ef4cdb99f9df5a00f666ab1d30e92e3b1))

## v2.3.0 (2024-01-22)

### Feature

- Reduce overhead to remove callbacks by using sets to store callbacks (#27) ([`05ceb85`](https://github.com/Bluetooth-Devices/habluetooth/commit/05ceb85901b17f72988068997c7f39bc0179dca2))

## v2.2.0 (2024-01-14)

### Feature

- Improve remote scanner performance (#26) ([`c549b1c`](https://github.com/Bluetooth-Devices/habluetooth/commit/c549b1cf9bbbda0c39dfce92d2888d5b990211da))

## v2.1.0 (2024-01-10)

### Feature

- Add support for windows (#25) ([`788dd77`](https://github.com/Bluetooth-Devices/habluetooth/commit/788dd77ffac6664083821d5ba8b264725a3baaff))

## v2.0.2 (2024-01-04)

### Fix

- Handle subclassed str in the client wrapper (#24) ([`f18a30e`](https://github.com/Bluetooth-Devices/habluetooth/commit/f18a30e48fe064993dc64f3af01c5d64b676a82f))

## v2.0.1 (2023-12-31)

### Fix

- Switching scanners too quickly (#23) ([`bd53685`](https://github.com/Bluetooth-Devices/habluetooth/commit/bd536854457bd8b27f9e91921965b88b0ff798c3))

## v2.0.0 (2023-12-21)

### Breaking

- Simplify async_register_scanner by removing connectable argument (#22) ([`10ac6da`](https://github.com/Bluetooth-Devices/habluetooth/commit/10ac6da0672c121b5f0246ed688e98111adc7339))

## v1.0.0 (2023-12-12)

### Breaking

- Eliminate the need to pass the new_info_callback (#21) ([`65c54a6`](https://github.com/Bluetooth-Devices/habluetooth/commit/65c54a68500be6053677511ffd21ce3dca4b6991))

## v0.11.1 (2023-12-11)

### Fix

- Do not schedule an expire when restoring devices (#20) ([`144cf15`](https://github.com/Bluetooth-Devices/habluetooth/commit/144cf15050a68cca66e7a2e24a5ddc7b87c32e41))

## v0.11.0 (2023-12-11)

### Feature

- Relocate bluetoothserviceinfobleak (#18) ([`4f4f32d`](https://github.com/Bluetooth-Devices/habluetooth/commit/4f4f32d78d6abe21e28171f54ff5f3b17c8fb702))

## v0.10.0 (2023-12-07)

### Feature

- Small speed ups to base_scanner (#17) ([`e1ff7e9`](https://github.com/Bluetooth-Devices/habluetooth/commit/e1ff7e9fb91a274b1a4bf6943a26e2a3f19780e7))

## v0.9.0 (2023-12-06)

### Feature

- Speed up processing incoming service infos (#16) ([`55f6522`](https://github.com/Bluetooth-Devices/habluetooth/commit/55f6522ffc2adaf7e203ff4d2c1b13adc5d8c6a2))

## v0.8.0 (2023-12-06)

### Feature

- Auto build the cythonized manager (#15) ([`c3441e5`](https://github.com/Bluetooth-Devices/habluetooth/commit/c3441e5095d62e6e70c2c879c4b5c109a87f463c))
- Add cython implementation for manager (#14) ([`266a602`](https://github.com/Bluetooth-Devices/habluetooth/commit/266a6022fb433ef9399f72e87b18b86897524784))

## v0.7.0 (2023-12-05)

### Feature

- Port bluetooth manager from ha (#13) ([`757640a`](https://github.com/Bluetooth-Devices/habluetooth/commit/757640a7b7f60072588168501148ba750316f170))

## v0.6.1 (2023-12-04)

### Fix

- Add missing cythonize for the adv tracker (#12) ([`8140195`](https://github.com/Bluetooth-Devices/habluetooth/commit/8140195a27ef83ea89ca643a5899d80839e574ae))

## v0.6.0 (2023-12-04)

### Feature

- Port advertisement_tracker (#11) ([`378667b`](https://github.com/Bluetooth-Devices/habluetooth/commit/378667bce851b5076ee79ff223a72501c5575325))

## v0.5.1 (2023-12-04)

### Fix

- Remove slots to keep hascanner patchable (#10) ([`d068f48`](https://github.com/Bluetooth-Devices/habluetooth/commit/d068f480d292619a1fc49a1256be98bdc6efadd6))

## v0.5.0 (2023-12-03)

### Feature

- Port local scanner support from ha (#9) ([`1b1d0e4`](https://github.com/Bluetooth-Devices/habluetooth/commit/1b1d0e4bc17a44a1b20382da6ae28ea8e50e80b7))

## v0.4.0 (2023-12-03)

### Feature

- Add more typing for incoming bluetooth data (#8) ([`de590e5`](https://github.com/Bluetooth-Devices/habluetooth/commit/de590e5c886801ff4a87f99c118be8855f337bd0))

## v0.3.0 (2023-12-03)

### Feature

- Refactor to be able to use __pyx_pyobject_fastcall (#7) ([`e15074b`](https://github.com/Bluetooth-Devices/habluetooth/commit/e15074b172242f44f641e5232ebdf6297537a2b8))
- Add basic pxd (#6) ([`fd97d07`](https://github.com/Bluetooth-Devices/habluetooth/commit/fd97d07db7c0e8e0e877e1544fd0e392d14448b3))

## v0.2.0 (2023-12-03)

### Feature

- Add cython pxd for base_scanner (#5) ([`0195710`](https://github.com/Bluetooth-Devices/habluetooth/commit/0195710bc25c8c3cc68b17a8f31cf281494fdc22))

## v0.1.0 (2023-12-03)

### Feature

- Port base scanner from ha (#2) ([`e01a57b`](https://github.com/Bluetooth-Devices/habluetooth/commit/e01a57b6e0003ea8fe64b8e6e11ce09a35c1ada2))

## v0.0.1 (2023-12-02)

### Fix

- Reserve name (#1) ([`5493984`](https://github.com/Bluetooth-Devices/habluetooth/commit/5493984483902039ca396498122e6094524bbae6))
