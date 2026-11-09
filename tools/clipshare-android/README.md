# T-Deck Clip (Android)

Share-sheet companion for the vtOS `clip` service. See
[docs/CLIPBOARD.md](../../docs/CLIPBOARD.md) for the protocol.

## Build

Needs JDK 17 and the Android SDK (platform 34). Gradle wrapper is included
(Gradle 8.9, AGP 8.5.2).

```bash
export JAVA_HOME=/opt/homebrew/opt/openjdk@17 ANDROID_HOME=$HOME/Library/Android/sdk
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

## Use

1. On the T-Deck: `clip pair` (shows a 6-digit code).
2. Open **T-Deck Clip**, type the code, press **Find T-Deck & pair**. Off the LAN, type the T-Deck's address first (e.g. Tailscale IP).
3. In any app: Share → **Send to T-Deck**. A toast confirms; the T-Deck shows
   `clip: N chars received`.

Plain text only, up to 4096 bytes. The app uses cleartext HTTP (the device has
no TLS); prefer the Tailscale address on untrusted networks.
