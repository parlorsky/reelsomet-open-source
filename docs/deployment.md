# Deployment

The default Compose service publishes port 8000 on `127.0.0.1` only. This is suitable for local inspection. A physical phone needs a network-reachable address; for remote use, put a TLS reverse proxy in front of the service and enable WebSocket upgrades.

```sh
docker compose up --build -d
docker compose logs reelsomet
```

The first-run log contains a setup token. Use it once at `/setup`, then choose an administrator password. The returned device token belongs to device ID 1. Keep both the logs and configuration private. Restart the service after changing optional integrations.

The named `reelsomet-data` volume contains `config.yaml`, SQLite and media. Stopping or recreating a container preserves that volume. `docker compose down -v` deletes it; do not use that command for an installation you want to keep.

## Public URL

Set `server.domain` in the persisted configuration to your public **hostname without a scheme**, for example `reels.example.com`. Several media download endpoints use this hostname to construct HTTPS URLs. A DNS hostname with TLS is the supported deployment pattern for device transfers. Legacy raw-IP handling in some adapters assumes port 8443 and is not suitable as a generic deployment configuration.

Preserve `/api/*`, `/ws/*`, and frontend paths at the proxy. Only authenticated users should reach control APIs. Do not use the Vite development server as a public production server. Configure proxy upload limits to match the media sizes you intend to accept.

## Backups and upgrades

Stop the service before making a filesystem backup of its volume, or use a SQLite-aware backup procedure that includes committed WAL state. Keep configuration, database and corresponding media together. Treat the backup as sensitive. Test restoration on a separate instance before relying on it.

Rebuild from a reviewed Git revision and retain the prior backup. This application performs additive database initialization on startup; there is no general downgrade tool. The original private VPS migration scripts are intentionally not distributed.

## Isolation

The image runs as an unprivileged user, exposes one service port, and has a health check. The sample Compose configuration drops Linux capabilities. These settings do not provide multi-tenant isolation: this release is designed for a trusted operator, with one administrator role and application-managed device tokens.

A complete deployment still needs your own TLS, backup policy, access controls, monitoring and physical-device validation. See [SECURITY.md](../SECURITY.md) for the concrete data boundary.
