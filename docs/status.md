# Release status

Version 0.1.0 is an **experimental source release**, intended for inspection, self-hosted evaluation and reuse. The repository's CI is the revision-specific record of automated validation.

## Automated coverage

Backend tests cover authentication, setup, configuration, temporary SQLite data, queue behavior, scheduler decisions, APIs and WebSocket contracts. FFmpeg tests generate synthetic media. The web build runs TypeScript checks and a production Vite build. Android unit tests and APK compilation run separately from Python. The container build is tested independently.

These checks do not establish real-device publication success. No physical Android phone, production account, Telegram delivery or external LLM provider was used to validate this source release.

## Known issue

**KNOWN-001 — Pinterest Telegram completion notifications.** The inherited tests expect a Telegram message after a successful or terminally failed pin publication. The current source does not wire those notifications. Two tests remain strict expected failures so an implementation change must update this record. Queue and command results are tested separately; do not rely on Telegram for confirmation of a Pinterest post.

## Explicitly excluded test environments

- The external `ghostcli` package and donor pools are not bundled; their integration tests skip without it.
- Legacy Studio routes no longer exist in this application's current dashboard/server surface.
- Private VPS export/restore scripts are not part of the public distribution.

These exclusions are stated in the test modules, rather than silently ignored by a passing CI command.

## Operational limitations

- Instagram, Pinterest and Reddit UI flows can break when the installed apps change.
- The dashboard uses one administrator role; the service is not a multi-tenant product.
- SQLite and local file storage are the tested persistence design. Horizontal scaling is not validated.
- Ghost-specific photo preparation requires the optional external engine; it is disabled by default. Ordinary existing-video queues work without it.
- A timeout can be ambiguous after a device has performed an action. Inspect actual results before retrying a publication.
- There are no throughput benchmarks, formal reliability guarantees or claimed research results.
