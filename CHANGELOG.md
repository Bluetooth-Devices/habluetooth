# Changelog

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
