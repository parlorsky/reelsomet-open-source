# Android agent

The Android project is under `android/`. It requires JDK 17 and SDK 34; minimum Android API level is 26. Build with Android Studio or the included Gradle wrapper:

```sh
cd android
./gradlew :app:testDebugUnitTest :app:assembleDebug
```

The APK is written to `app/build/outputs/apk/debug/app-debug.apk`. No production signing material is included.

On the device, install the APK and enter your own reachable server URL, device ID and device token. A new server setup creates a token for ID 1. Never reuse the same device ID across simultaneously connected phones. There is no baked-in production server address in this release.

Enable the app's Accessibility service in Android settings, grant the media permissions it requests, and configure battery settings so the operating system does not immediately stop its background work. Screen capture, remote input and app-specific flows may need additional explicit device permissions.

Install and sign in to the target publishing app yourself. Begin with one controlled test item. Verify both the visible platform result and the server log before increasing the schedule.

## Compatibility limits

Accessibility automation depends on UI structure, text labels, locale, permissions and Android vendor behavior. A compiling APK does not establish that every workflow works on a current Instagram, Pinterest or Reddit release. The source release has not been revalidated on physical devices; see [status](status.md).

Some retained utilities originated in managed device deployments. Inspect permissions and app-specific workflows before adapting them. Use only devices, accounts and content you are authorized to operate, and follow the target platform's rules.
