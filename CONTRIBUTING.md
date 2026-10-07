# Contributing to Reelsomet

Start with [the architecture](docs/architecture.md) and [local development](docs/development.md).
Small, focused pull requests are welcome: device adapters, queue reliability, accessibility selectors, documentation and tests.

1. Fork the repository and create a topic branch.
2. Install `requirements.lock` and `requirements-dev.txt`, then use `python -m pip install -e . --no-deps` for editable development. Install the frontend with `npm ci --prefix web`.
3. Run `python -m pytest` and `npm run build --prefix web`.
4. For Android changes, run `cd android && ./gradlew testDebugUnitTest assembleDebug`.
5. Describe the behavior, your validation and any device/app versions you tested.

Never commit config files, access tokens, account credentials, phone dumps, private media, signing keys or production databases. Use synthetic data in tests. Do not run tests against someone else's devices or accounts. Keep device execution disabled until an operator explicitly configures it.

Document protocol changes on both the Python and Kotlin sides. Prefer bounded retries, explicit result states and idempotent commands. Tests should cover observable behavior, especially cancellation, disconnects and duplicate events.

By submitting a contribution, you agree to license your contribution under this repository's MIT license. Third-party code must retain its original notices.
