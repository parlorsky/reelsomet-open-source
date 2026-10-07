# Development

Use Python 3.11+, Node.js 22.12+, FFmpeg/ffprobe, and Git. Commands below run from the repository root unless stated otherwise.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock -r requirements-dev.txt
python -m pytest -q -ra
npm ci --prefix web
npm run build --prefix web
```

FFmpeg tests synthesize color frames and sine-wave audio. API tests use temporary SQLite databases and mocked device bridges; they do not connect to real phones, Telegram or LLM providers. Historical test modules for retired Studio routes and private migration scripts are explicitly skipped. Ghost integration tests require the separately available engine. Known regressions use strict expected-failure markers and are listed in [status](status.md).

For live UI development, start `python -m server.main` in one terminal and `npm run dev --prefix web` in another. Open http://localhost:3000. Vite proxies `/api` and `/ws` to port 8000. Keep the development server bound to your own machine.

## Android

Use JDK 17, Android SDK platform 34 and Build Tools 34.0.0. Set `ANDROID_HOME` or create your own ignored `android/local.properties` with `sdk.dir=...`.

```sh
cd android
./gradlew :app:testDebugUnitTest :app:assembleDebug
```

The repository includes the Gradle wrapper, source and unit tests. It does not include a signing key or prebuilt APK. The debug build uses the standard local debug key; configure your own signing process for release distribution.

## Dependency and secret checks

```sh
npm audit --prefix web
python -m pip install pip-audit
python -m pip_audit -r requirements.lock
gitleaks git . --redact
```

Python runtime dependencies are resolved with hashes in `requirements.lock`; frontend dependencies are locked in `web/package-lock.json`. The GitHub workflow builds the server tests, web UI, Android app and container. See the workflow result for the actual revision tested.

## Local data

`var/` is ignored. Configuration can hold account credentials and provider tokens; the database and logs can contain personal information. Use synthetic accounts when adding fixtures, and avoid copying a real deployment into the source tree. An example configuration is documentation, not a credential store.
