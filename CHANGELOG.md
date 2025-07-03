# Changelog

## v4.0.0 (2025-07-03)

### Features


- Support bleak 1.x ([`a739199`](https://github.com/Bluetooth-Devices/habluetooth/commit/a739199cf6d56f5db316b149134c11eabfab9f1c))


## v3.49.0 (2025-06-03)

### Features


- Add raw_advertisement_data to diagnostics ([`a77933b`](https://github.com/Bluetooth-Devices/habluetooth/commit/a77933b8dd0195ab827907ea918c572cd1686750))


## v3.48.2 (2025-05-03)

### Bug fixes


- Remove duplicate _connecting slot from basehascanner ([`230bb03`](https://github.com/Bluetooth-Devices/habluetooth/commit/230bb038eea8ae07a3fe798ec15792489d06cd66))


## v3.48.1 (2025-05-03)

### Bug fixes


- Pin cython to <3.1 ([`21dc734`](https://github.com/Bluetooth-Devices/habluetooth/commit/21dc7340c548713c4539d8d8a067a2a574623906))


## v3.48.0 (2025-05-03)

### Features


- Refactor scanner history to live on the scanner itself ([`ea0d2fc`](https://github.com/Bluetooth-Devices/habluetooth/commit/ea0d2fc088832a1b3f8c7859c82e2e05bf1261f9))


## v3.47.1 (2025-05-03)

### Bug fixes


- Ensure logging does not fail when there is only a single scanner ([`d81378e`](https://github.com/Bluetooth-Devices/habluetooth/commit/d81378e6b4adedead6d04ab23be7b655cd3785fb))


## v3.47.0 (2025-05-03)

### Bug fixes


- Require bluetooth-auto-recovery >= 1.5.1 ([`8164ce5`](https://github.com/Bluetooth-Devices/habluetooth/commit/8164ce512084fe898cb80c5e44f664dde4751113))


### Features


- Avoid thundering heard of connections ([`943cc20`](https://github.com/Bluetooth-Devices/habluetooth/commit/943cc2043731f8d6fbb541f4d7ffcd37d8c6b4f3))


## v3.46.0 (2025-05-03)

### Features


- Improve recovery when adapter has gone silent and needs a usb reset ([`a4dd395`](https://github.com/Bluetooth-Devices/habluetooth/commit/a4dd395b7a8e70cb0ae94d97422d35eb638daaa5))


## v3.45.0 (2025-04-29)

### Features


- Improve performance of _async_on_advertisement_internal ([`be0b5a6`](https://github.com/Bluetooth-Devices/habluetooth/commit/be0b5a6d0da07f2f881984c92c0c7671117d3e5a))


## v3.44.0 (2025-04-28)

### Features


- Save the raw data in storage ([`eaf4107`](https://github.com/Bluetooth-Devices/habluetooth/commit/eaf41072ecc915b2de23ad3c9a03148f4b313f17))


## v3.43.0 (2025-04-28)

### Features


- Migrate storage code from bluetooth_adapters ([`5d671f9`](https://github.com/Bluetooth-Devices/habluetooth/commit/5d671f95b9a7964bfa871c7b42061a71a98ce80e))


## v3.42.0 (2025-04-27)

### Features


- Add raw field to bluetoothserviceinfobleak ([`343f18b`](https://github.com/Bluetooth-Devices/habluetooth/commit/343f18bfbbf3ebbee31e64beab60b2686700797f))


## v3.41.0 (2025-04-27)

### Features


- Add new _async_on_raw_advertisement base scanner api ([`fb2a487`](https://github.com/Bluetooth-Devices/habluetooth/commit/fb2a487c06cf102c17509410f916b5c06728df98))


## v3.40.0 (2025-04-27)

### Features


- Require bluetooth-data-tools 1.28.0 or later ([`e154136`](https://github.com/Bluetooth-Devices/habluetooth/commit/e154136db9f15d33c6de3d89bf9e4e53e03c690a))


## v3.39.0 (2025-04-17)

### Features


- Improve performance of _async_on_advertisement ([`0fc0500`](https://github.com/Bluetooth-Devices/habluetooth/commit/0fc0500d74cdc3d320111df979bef784a51a2eac))


## v3.38.1 (2025-04-14)

### Bug fixes


- Add missing dbus-fast dep on linux ([`5746448`](https://github.com/Bluetooth-Devices/habluetooth/commit/57464488482626577e9f84c42ab1ff100b7857b3))


## v3.38.0 (2025-03-22)

### Bug fixes


- Use project.license key ([`1decf97`](https://github.com/Bluetooth-Devices/habluetooth/commit/1decf9704f7db33bc8094880651321c1b58420c8))


### Features


- Improve performance of previous source checks ([`8d96528`](https://github.com/Bluetooth-Devices/habluetooth/commit/8d96528f605231f3089319c789390d784c45b4c5))


## v3.37.0 (2025-03-21)

### Features


- Improve performance of _prefer_previous_adv_from_different_source ([`73ec210`](https://github.com/Bluetooth-Devices/habluetooth/commit/73ec2107375be217ffb0310194be8c3d4f20e150))


## v3.36.0 (2025-03-21)

### Features


- Improve performance of filtering apple data ([`9f56840`](https://github.com/Bluetooth-Devices/habluetooth/commit/9f568405ae987de0fb3953d6ae7b39eabacde9ef))


## v3.35.0 (2025-03-21)

### Features


- Optimize previous local name matching ([`fadb722`](https://github.com/Bluetooth-Devices/habluetooth/commit/fadb722b8ded2bc15bd56b641a963d4c4d19838e))


## v3.34.1 (2025-03-21)

### Bug fixes


- Revert adding _async_on_advertisements ([`4bc3cb8`](https://github.com/Bluetooth-Devices/habluetooth/commit/4bc3cb89baf52570deec4f27ed3cd935249525ec))


## v3.34.0 (2025-03-21)

### Features


- Rename _async_on_raw_advertisement to _async_on_raw_advertisements ([`b3acb88`](https://github.com/Bluetooth-Devices/habluetooth/commit/b3acb882d888a33567ece3e7f9d0fa1d2b4c6acd))


## v3.33.0 (2025-03-21)

### Features


- Add _async_on_raw_advertisement ([`24d128f`](https://github.com/Bluetooth-Devices/habluetooth/commit/24d128fe4854135647e9a41c7eeaf1784fbda0bf))


## v3.32.0 (2025-03-15)

### Features


- Improve performance of dispatching discovery info to subclasses ([`d0fae7d`](https://github.com/Bluetooth-Devices/habluetooth/commit/d0fae7ddd9158903f6621888cc4c75480822ae35))


## v3.31.0 (2025-03-15)

### Features


- Avoid building on demand advertisementdata if there are no bleak callbacks ([`ae977b9`](https://github.com/Bluetooth-Devices/habluetooth/commit/ae977b9d53c29c581ff6394a2078d2a2b01066dd))


## v3.30.0 (2025-03-15)

### Features


- Improve performance of on demand advertisementdata construction ([`ab005cb`](https://github.com/Bluetooth-Devices/habluetooth/commit/ab005cbef5e2ece74a0facd502fca7173ba2b1fc))


## v3.29.0 (2025-03-15)

### Features


- Improve performance for device with large manufacturer data history ([`ec1f6aa`](https://github.com/Bluetooth-Devices/habluetooth/commit/ec1f6aa7989cea2a589029362461dba4f7a8f0db))


## v3.28.0 (2025-03-15)

### Features


- Improve performance of local name checks ([`9f57d2f`](https://github.com/Bluetooth-Devices/habluetooth/commit/9f57d2fcc23595b376d5785162c21633514f44bd))


## v3.27.0 (2025-03-14)

### Features


- Improve performance of base_scanner ([`5b8c59c`](https://github.com/Bluetooth-Devices/habluetooth/commit/5b8c59c7ffadead5997fa457b07ff37ec8ec31b5))


## v3.26.0 (2025-03-14)

### Features


- Improve manager performance ([`e0bdace`](https://github.com/Bluetooth-Devices/habluetooth/commit/e0bdace8180ff3ac450447be99f700fd647fb659))


## v3.25.1 (2025-03-13)

### Bug fixes


- Downgrade scanner gone quiet logger to debug ([`d450ffc`](https://github.com/Bluetooth-Devices/habluetooth/commit/d450ffca38dec015f44b5be08af484fe8ca09866))


## v3.25.0 (2025-03-05)

### Bug fixes


- Use trusted publishing for wheels ([`c726687`](https://github.com/Bluetooth-Devices/habluetooth/commit/c726687affb0025037676b76cf4ecefdef0da23f))


### Features


- Add armv7l to wheel builds ([`e394707`](https://github.com/Bluetooth-Devices/habluetooth/commit/e394707b6b7ffc54e6dc5b8c038a08c5404f1777))


- Reduce wheel sizes ([`5e6b644`](https://github.com/Bluetooth-Devices/habluetooth/commit/5e6b64476ff2db7a215d1b0d58ef01c04b839d34))


## v3.24.1 (2025-02-27)

### Bug fixes


- Update scanner discover signature for newer bleak ([`a071cb8`](https://github.com/Bluetooth-Devices/habluetooth/commit/a071cb8e3f921da30055b94a74a4b0aa339e53de))


## v3.24.0 (2025-02-22)

### Features


- Improve logging of scanner failures and time_since_last_detection ([`f0ff045`](https://github.com/Bluetooth-Devices/habluetooth/commit/f0ff04586849bda3933fbe98e8e1335c308999c4))


## v3.23.0 (2025-02-21)

### Features


- Add debug logging for connection paths ([`562d469`](https://github.com/Bluetooth-Devices/habluetooth/commit/562d46912e7596febc3ebcc0301280e6f334172b))


## v3.22.1 (2025-02-20)

### Bug fixes


- Try to force stop discovery if its stuck on ([`e28d836`](https://github.com/Bluetooth-Devices/habluetooth/commit/e28d836d28f0b8062831ee209ba54a7735c4d5ae))


## v3.22.0 (2025-02-18)

### Features


- Allow remote scanners to set current and requested mode ([`a39ba18`](https://github.com/Bluetooth-Devices/habluetooth/commit/a39ba184e0d01f983133534e4fd7c1b6202210fb))


## v3.21.1 (2025-02-04)

### Bug fixes


- Update poetry to v2 ([`aefe36e`](https://github.com/Bluetooth-Devices/habluetooth/commit/aefe36e2507566224267f371511c1f1c748a37a9))


## v3.21.0 (2025-02-01)

### Features


- Reduce remote scanner adv processing overhead ([`7bf302b`](https://github.com/Bluetooth-Devices/habluetooth/commit/7bf302bac3855cf7e229dd2744acce513b2e2ee4))


## v3.20.1 (2025-02-01)

### Bug fixes


- Remove unused centralbluetoothmanager in models ([`7466034`](https://github.com/Bluetooth-Devices/habluetooth/commit/74660343b30fec50b927fdddd92e72eacb4da6cf))


- Precision loss when comparing advs from different sources ([`02279a9`](https://github.com/Bluetooth-Devices/habluetooth/commit/02279a95ca5b590768bd631bf39ee507a64db7ad))


## v3.20.0 (2025-02-01)

### Features


- Reduce adv tracker overhead ([`69168a6`](https://github.com/Bluetooth-Devices/habluetooth/commit/69168a64572ab3fba696d2afedeb015953afb0cc))


## v3.19.0 (2025-02-01)

### Features


- Reduce overhead to convert non-connectable bluetoothserviceinfobleak to connectable ([`37fc839`](https://github.com/Bluetooth-Devices/habluetooth/commit/37fc839d5fc73ff6f784ec8041606be82d58322b))


## v3.18.0 (2025-02-01)

### Features


- Refactor scanner_adv_received to reduce ref counting ([`a1945ce`](https://github.com/Bluetooth-Devices/habluetooth/commit/a1945cedc2373082814e8f4b4426a50c79788305))


## v3.17.1 (2025-01-31)

### Bug fixes


- Ensure allocations are available if the adapter never makes any connections ([`b3dfa48`](https://github.com/Bluetooth-Devices/habluetooth/commit/b3dfa48dba2482c16f61fceaf9a0f58ea55df982))


## v3.17.0 (2025-01-31)

### Features


- Remove the need to call set_manager to set up ([`1312bf7`](https://github.com/Bluetooth-Devices/habluetooth/commit/1312bf7d978ff585e66d99bde766e85773fce006))


## v3.16.0 (2025-01-31)

### Features


- Allow bluetoothmanager to be created with defaults ([`70b2f69`](https://github.com/Bluetooth-Devices/habluetooth/commit/70b2f6952fbd3ecd499a4c66ec305869158a428e))


## v3.15.0 (2025-01-31)

### Features


- Include findmy packets in wanted adverts ([`5217850`](https://github.com/Bluetooth-Devices/habluetooth/commit/5217850934bfed5d8e70f8b43c84cd97cf53cdac))


## v3.14.0 (2025-01-29)

### Features


- Add allocations to diagnostics ([`aa41088`](https://github.com/Bluetooth-Devices/habluetooth/commit/aa4108872478720ab4cbcf52c5add015441fe72d))


## v3.13.0 (2025-01-28)

### Features


- Add async_register_scanner_registration_callback and async_current_scanners to the manager ([`99fcb46`](https://github.com/Bluetooth-Devices/habluetooth/commit/99fcb46a73ea6cb8f01817263d01a342365be78f))


## v3.12.0 (2025-01-22)

### Features


- Add support for connection allocations for non-connectable scanners ([`d76b7c9`](https://github.com/Bluetooth-Devices/habluetooth/commit/d76b7c9624b6c4e6beedc1bd56dd1a3c0df70eec))


## v3.11.2 (2025-01-22)

### Bug fixes


- Re-release again for failed arm runners ([`af2bb50`](https://github.com/Bluetooth-Devices/habluetooth/commit/af2bb50879713378a32339e490a57b56083a4fa7))


## v3.11.1 (2025-01-22)

### Bug fixes


- Re-release due to failed github action ([`90e2192`](https://github.com/Bluetooth-Devices/habluetooth/commit/90e2192ff75c13ccf610fd06a61e64d60dfd1a18))


## v3.11.0 (2025-01-22)

### Features


- Add api for getting current slot allocations ([`0a9bef9`](https://github.com/Bluetooth-Devices/habluetooth/commit/0a9bef927c5f29c3e724fb60aa06706b6d896f82))


## v3.10.0 (2025-01-21)

### Features


- Add support for getting callbacks when adapter allocations change ([`c6fd2ba`](https://github.com/Bluetooth-Devices/habluetooth/commit/c6fd2babf0c6438ff85220edef95df3d3b4fae9c))


## v3.9.2 (2025-01-20)

### Bug fixes


- Increase rssi switch value to 16 ([`db367db`](https://github.com/Bluetooth-Devices/habluetooth/commit/db367dbef3fa883348a72cf17e29d9c26a09de53))


## v3.9.1 (2025-01-20)

### Bug fixes


- Increase rssi switch threshold for advertisements ([`297c269`](https://github.com/Bluetooth-Devices/habluetooth/commit/297c2693f9a2c007f0e70175c24416c8bb7da099))


## v3.9.0 (2025-01-17)

### Features


- Switch to native arm runners for wheel builds ([`bf7e98b`](https://github.com/Bluetooth-Devices/habluetooth/commit/bf7e98b099597916bb7566eb03472023f8acef97))


## v3.8.0 (2025-01-10)

### Features


- Add async_register_disappeared_callback ([`ec1d445`](https://github.com/Bluetooth-Devices/habluetooth/commit/ec1d4456ca15c6fca3248f2e5d73fcb1ba9d36c6))


## v3.7.0 (2025-01-05)

### Bug fixes


- Publish workflow ([`341c8a4`](https://github.com/Bluetooth-Devices/habluetooth/commit/341c8a4b72fb2818a3bed44632048d8570fc3b67))


### Features


- Start building wheels for python 3.13 ([`26dd831`](https://github.com/Bluetooth-Devices/habluetooth/commit/26dd831c28f3c0dfe0745769749e795e7937c7df))


- Add codspeed benchmarks ([`5905fbd`](https://github.com/Bluetooth-Devices/habluetooth/commit/5905fbd2c54adea04c0e55fe8a299f771e6f31ed))


### Unknown



## v3.6.0 (2024-10-20)

### Features


- Speed up creation of advertisementdata namedtuple ([`28f7e60`](https://github.com/Bluetooth-Devices/habluetooth/commit/28f7e6093c3985da16e537bc9d989d839ad80c56))


## v3.5.0 (2024-10-05)

### Features


- Add support for python 3.13 ([`b8a4783`](https://github.com/Bluetooth-Devices/habluetooth/commit/b8a4783a43f6e771321974d2c085e5e0dda9e195))


## v3.4.1 (2024-09-22)

### Bug fixes


- Ensure build system required cython 3 ([`dc85d2f`](https://github.com/Bluetooth-Devices/habluetooth/commit/dc85d2fd1b8c8e4d8eb4515aa60af06782fc8722))


## v3.4.0 (2024-09-02)

### Features


- Add a fast cython init path for bluetoothserviceinfobleak ([`f532ed2`](https://github.com/Bluetooth-Devices/habluetooth/commit/f532ed215b429f0bbd14dacc30f87c53f22af245))


## v3.3.2 (2024-08-20)

### Bug fixes


- Disable 3.13 wheels ([`9e8bbff`](https://github.com/Bluetooth-Devices/habluetooth/commit/9e8bbff6179e08bd6e05341ff48fff3adc5c6157))


## v3.3.1 (2024-08-20)

### Bug fixes


- Bump cibuildwheel to fix wheel builds ([`68d838a`](https://github.com/Bluetooth-Devices/habluetooth/commit/68d838a1d2adab9efe1fb5eba65e81b5dcc9a351))


## v3.3.0 (2024-08-20)

### Bug fixes


- Cleanup advertisementmonitor mapper ([`7d3483d`](https://github.com/Bluetooth-Devices/habluetooth/commit/7d3483d87d3e03c19cf528a1838acce5b194533e))


### Features


- Override devicefound and devicelost for passive monitoring ([`a802859`](https://github.com/Bluetooth-Devices/habluetooth/commit/a8028596bf3576a35750ae8575f173c75f918f28))


## v3.2.0 (2024-07-27)

### Features


- Small speed ups to scanner detection callback ([`7a5129a`](https://github.com/Bluetooth-Devices/habluetooth/commit/7a5129a40a12382c089453880210c41bb0f28a32))


## v3.1.3 (2024-06-24)

### Bug fixes


- Wheel builds ([`b9a8eec`](https://github.com/Bluetooth-Devices/habluetooth/commit/b9a8eec4f79c2098c0ec318b6b1ff7e3376febf2))


## v3.1.2 (2024-06-24)

### Bug fixes


- Fix license classifier ([`04aaaa1`](https://github.com/Bluetooth-Devices/habluetooth/commit/04aaaa186c755b869c8d75678f563f6a9c089829))


## v3.1.1 (2024-05-23)

### Bug fixes


- Missing classmethod decorator on find_device_by_address ([`aa08b13`](https://github.com/Bluetooth-Devices/habluetooth/commit/aa08b136660cddea7c356274c21f20b6d0eef1fa))


## v3.1.0 (2024-05-22)

### Features


- Speed up dispatching bleak callbacks ([`cbc8b26`](https://github.com/Bluetooth-Devices/habluetooth/commit/cbc8b26f90b9ea4f2a8569c0625b527dd37ef180))


## v3.0.1 (2024-05-03)

### Bug fixes


- Ensure lazy advertisement uses none when name is not present ([`c300f73`](https://github.com/Bluetooth-Devices/habluetooth/commit/c300f73ba82d3549ea4c156ef11023e9478c8b6c))


## v3.0.0 (2024-05-02)

### Features


- Make generation of advertisementdata lazy ([`25f8437`](https://github.com/Bluetooth-Devices/habluetooth/commit/25f843795927ad663a1d5ef1fa9472ec366b9da5))


## v2.8.1 (2024-05-02)

### Bug fixes


- Add missing find_device_by_address mapping ([`cc8e57e`](https://github.com/Bluetooth-Devices/habluetooth/commit/cc8e57eef7b97a6f2a30488a64d156cb5023c6c6))


## v2.8.0 (2024-04-17)

### Features


- Add support for recovering failed adapters after reboot ([`04948c3`](https://github.com/Bluetooth-Devices/habluetooth/commit/04948c337adf0f7b291e4e33618a7eae6dc4ebc2))


## v2.7.0 (2024-04-17)

### Features


- Improve fallback to passive mode when active mode fails ([`17ecc01`](https://github.com/Bluetooth-Devices/habluetooth/commit/17ecc012e096bec0113efea9ceb6a21bb50023fe))


## v2.6.0 (2024-04-17)

### Features


- Speed up stopping the scanner when its stuck setting up ([`bba8b51`](https://github.com/Bluetooth-Devices/habluetooth/commit/bba8b514490d98dca1020bbfefd9dc1e6a79af5f))


## v2.5.3 (2024-04-17)

### Bug fixes


- Ensure scanner is stopped on cancellation ([`a21d70a`](https://github.com/Bluetooth-Devices/habluetooth/commit/a21d70a1ac88135eade61c0abc8912c5b04a6b8b))


## v2.5.2 (2024-04-16)

### Bug fixes


- Ensure discovered_devices returns an empty list for offline scanners ([`2350543`](https://github.com/Bluetooth-Devices/habluetooth/commit/23505437c98529f692ab2dc0f5c3bdb5c9b7e3bd))


## v2.5.1 (2024-04-16)

### Bug fixes


- Wheel builds ([`5bd671a`](https://github.com/Bluetooth-Devices/habluetooth/commit/5bd671a159292dffe30a69639411926d0bc28123))


## v2.5.0 (2024-04-16)

### Features


- Fallback to passive scanning if active cannot start ([`3fae981`](https://github.com/Bluetooth-Devices/habluetooth/commit/3fae98162e6b0279375823a3b6e60ee51b87c1bb))


## v2.4.2 (2024-02-29)

### Bug fixes


- Android beacons in passive mode with flags 0x02 ([`8330e18`](https://github.com/Bluetooth-Devices/habluetooth/commit/8330e187550ec00ed415d3650a2c231921fb8ae7))


## v2.4.1 (2024-02-23)

### Bug fixes


- Avoid concurrent refreshes of adapters ([`d355b17`](https://github.com/Bluetooth-Devices/habluetooth/commit/d355b1768705706dec7062ad5d6267089d87a88e))


## v2.4.0 (2024-01-22)

### Features


- Improve error reporting resolution suggestions ([`afff5ba`](https://github.com/Bluetooth-Devices/habluetooth/commit/afff5ba4dfd8a5582174b367ae5ed9c9953b81e9))


## v2.3.1 (2024-01-22)

### Bug fixes


- Ensure unavailable callbacks can be removed from fired callbacks ([`65e7706`](https://github.com/Bluetooth-Devices/habluetooth/commit/65e7706ef4cdb99f9df5a00f666ab1d30e92e3b1))


## v2.3.0 (2024-01-22)

### Features


- Reduce overhead to remove callbacks by using sets to store callbacks ([`05ceb85`](https://github.com/Bluetooth-Devices/habluetooth/commit/05ceb85901b17f72988068997c7f39bc0179dca2))


## v2.2.0 (2024-01-14)

### Features


- Improve remote scanner performance ([`c549b1c`](https://github.com/Bluetooth-Devices/habluetooth/commit/c549b1cf9bbbda0c39dfce92d2888d5b990211da))


## v2.1.0 (2024-01-10)

### Features


- Add support for windows ([`788dd77`](https://github.com/Bluetooth-Devices/habluetooth/commit/788dd77ffac6664083821d5ba8b264725a3baaff))


## v2.0.2 (2024-01-04)

### Bug fixes


- Handle subclassed str in the client wrapper ([`f18a30e`](https://github.com/Bluetooth-Devices/habluetooth/commit/f18a30e48fe064993dc64f3af01c5d64b676a82f))


## v2.0.1 (2023-12-31)

### Bug fixes


- Switching scanners too quickly ([`bd53685`](https://github.com/Bluetooth-Devices/habluetooth/commit/bd536854457bd8b27f9e91921965b88b0ff798c3))


## v2.0.0 (2023-12-21)

### Features


- Simplify async_register_scanner by removing connectable argument ([`10ac6da`](https://github.com/Bluetooth-Devices/habluetooth/commit/10ac6da0672c121b5f0246ed688e98111adc7339))


## v1.0.0 (2023-12-12)

### Features


- Eliminate the need to pass the new_info_callback ([`65c54a6`](https://github.com/Bluetooth-Devices/habluetooth/commit/65c54a68500be6053677511ffd21ce3dca4b6991))


## v0.11.1 (2023-12-11)

### Bug fixes


- Do not schedule an expire when restoring devices ([`144cf15`](https://github.com/Bluetooth-Devices/habluetooth/commit/144cf15050a68cca66e7a2e24a5ddc7b87c32e41))


## v0.11.0 (2023-12-11)

### Features


- Relocate bluetoothserviceinfobleak ([`4f4f32d`](https://github.com/Bluetooth-Devices/habluetooth/commit/4f4f32d78d6abe21e28171f54ff5f3b17c8fb702))


## v0.10.0 (2023-12-07)

### Features


- Small speed ups to base_scanner ([`e1ff7e9`](https://github.com/Bluetooth-Devices/habluetooth/commit/e1ff7e9fb91a274b1a4bf6943a26e2a3f19780e7))


## v0.9.0 (2023-12-06)

### Features


- Speed up processing incoming service infos ([`55f6522`](https://github.com/Bluetooth-Devices/habluetooth/commit/55f6522ffc2adaf7e203ff4d2c1b13adc5d8c6a2))


## v0.8.0 (2023-12-06)

### Features


- Auto build the cythonized manager ([`c3441e5`](https://github.com/Bluetooth-Devices/habluetooth/commit/c3441e5095d62e6e70c2c879c4b5c109a87f463c))


- Add cython implementation for manager ([`266a602`](https://github.com/Bluetooth-Devices/habluetooth/commit/266a6022fb433ef9399f72e87b18b86897524784))


## v0.7.0 (2023-12-05)

### Features


- Port bluetooth manager from ha ([`757640a`](https://github.com/Bluetooth-Devices/habluetooth/commit/757640a7b7f60072588168501148ba750316f170))


## v0.6.1 (2023-12-04)

### Bug fixes


- Add missing cythonize for the adv tracker ([`8140195`](https://github.com/Bluetooth-Devices/habluetooth/commit/8140195a27ef83ea89ca643a5899d80839e574ae))


## v0.6.0 (2023-12-04)

### Features


- Port advertisement_tracker ([`378667b`](https://github.com/Bluetooth-Devices/habluetooth/commit/378667bce851b5076ee79ff223a72501c5575325))


## v0.5.1 (2023-12-04)

### Bug fixes


- Remove slots to keep hascanner patchable ([`d068f48`](https://github.com/Bluetooth-Devices/habluetooth/commit/d068f480d292619a1fc49a1256be98bdc6efadd6))


## v0.5.0 (2023-12-03)

### Features


- Port local scanner support from ha ([`1b1d0e4`](https://github.com/Bluetooth-Devices/habluetooth/commit/1b1d0e4bc17a44a1b20382da6ae28ea8e50e80b7))


## v0.4.0 (2023-12-03)

### Features


- Add more typing for incoming bluetooth data ([`de590e5`](https://github.com/Bluetooth-Devices/habluetooth/commit/de590e5c886801ff4a87f99c118be8855f337bd0))


## v0.3.0 (2023-12-03)

### Features


- Refactor to be able to use __pyx_pyobject_fastcall ([`e15074b`](https://github.com/Bluetooth-Devices/habluetooth/commit/e15074b172242f44f641e5232ebdf6297537a2b8))


- Add basic pxd ([`fd97d07`](https://github.com/Bluetooth-Devices/habluetooth/commit/fd97d07db7c0e8e0e877e1544fd0e392d14448b3))


## v0.2.0 (2023-12-03)

### Features


- Add cython pxd for base_scanner ([`0195710`](https://github.com/Bluetooth-Devices/habluetooth/commit/0195710bc25c8c3cc68b17a8f31cf281494fdc22))


## v0.1.0 (2023-12-03)

### Features


- Port base scanner from ha ([`e01a57b`](https://github.com/Bluetooth-Devices/habluetooth/commit/e01a57b6e0003ea8fe64b8e6e11ce09a35c1ada2))


## v0.0.1 (2023-12-02)

### Bug fixes


- Reserve name ([`5493984`](https://github.com/Bluetooth-Devices/habluetooth/commit/5493984483902039ca396498122e6094524bbae6))
