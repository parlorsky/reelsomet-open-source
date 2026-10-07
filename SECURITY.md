# Security

Report sensitive vulnerabilities through GitHub's **Security → Report a vulnerability** on this repository. Do not put credentials, exploitation details or private device logs in public issues.

## Deployment model

Reelsomet is a single-operator control plane, not a hardened multi-tenant service. It can instruct connected phones and handle media and optional account credentials. Restrict its network access accordingly.

- The local default binds to `127.0.0.1`. Docker Compose publishes only to the host loopback interface.
- First-run setup requires a randomly generated, one-use token printed in the server console. Configuration is written with mode `0600`; setup consumes the token.
- Administrator requests require JWT authentication; devices use individual tokens. Remote screen control additionally requires password confirmation.
- Use HTTPS/WSS through a reverse proxy before connecting phones over an untrusted network. See [deployment](docs/deployment.md).
- SQLite and application files are not encrypted at rest. If you save social account credentials, the database and backups contain sensitive data. Protect the volume, host and backups and limit administrator access.
- Do not expose the legacy on-phone HTTP transport to the Internet. Prefer outbound WebSocket connections.
- Optional LLM and Telegram integrations send data to services you configure. They are disabled on a fresh installation.

The repository contains source and synthetic tests. Production configs, account datasets, media, old Git histories, compiled server extensions and prebuilt APKs are intentionally excluded.

No dependency scan proves a system secure. Device automation needs operational testing for each phone and target application version.
