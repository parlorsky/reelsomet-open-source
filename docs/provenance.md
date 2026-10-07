# Source provenance

This initial release combines the author's Android working tree and the server/dashboard working tree, with selected newer plain-text sources from a deployment snapshot. It starts a new public Git history because operational history contained configuration and material unrelated to a reusable source release.

Included: Kotlin and Python sources, Vue/TypeScript sources, Gradle wrapper, test code, generic workflow definitions, documentation and newly produced architecture assets.

Excluded: real account databases, media libraries, provider credentials, device tokens, signing keys, customer-specific deployment scripts, the older desktop controller, third-party donor assets, compiled-only extension modules and deployment archives. The excluded production snapshot is not required to build this source distribution.

The source release removes the former activation and hardware-binding layer. A small read-only compatibility endpoint remains for clients expecting `/api/license/status`; it reports the MIT edition. Administrator and device authentication remain separate and active.

Not all historical tests describe the current application. Tests for replaced internal methods and changed media folders were aligned with available source. Retired Studio API and private migration tests remain explicitly skipped with explanations. Optional Ghost tests skip when that external package is unavailable. Unresolved behavior is listed openly in [status](status.md).

The original deployment environment may have run compiled components with behavior not reconstructible from the available source. This repository does not claim binary equivalence with that environment.
