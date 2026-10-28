# Docker Compose + Nginx

The panel, HTTPS proxy and certificate client run in separate containers. Only
Nginx publishes ports. The bot process has one instance, runs without root, and
keeps its SQLite database and uploads in a persistent bind mount. Do not scale it
or use multiple Uvicorn workers: that would duplicate Discord Gateway clients.

The defaults serve `https://IP:8443/panel/`. Port 80 serves ACME challenges and
returns 404 for other paths. Port 443 returns 404 and never redirects to the panel.
These ports can be changed in the Compose settings file.

## Settings

Copy `docker/compose.env.example` to `.deploy/compose.env` (create `.deploy`
first). This file contains deployment settings, not credentials. Set the public
IP and the UID/GID owning `.env` and the data directory. The default UID/GID in
the image are 10001:10001. Bind-mount sources must already exist.

Keep credentials in the normal local `.env`, mode 0600. For example:

```dotenv
PANEL_AUTH_MODE=discord
PANEL_BASE_PATH=/panel
DISCORD_OAUTH_REDIRECT_URI=https://89.117.62.251:8443/panel/auth/discord/callback
```

Keep the owner ID, bot tokens and OAuth credentials in the same file. Register
that exact callback in Discord Developer Portal. Local root hosting is still
supported with an empty `PANEL_BASE_PATH` and its matching callback.

The `.env` file is mounted read-only; it is not copied into the image or passed
as a Docker environment file. Nginx never receives bot/OAuth secrets. Compose
sets the container's internal bind address, data path and trusted proxy IPs.
The private network defaults to `172.31.86.0/29`; choose another subnet and the
two corresponding IPs if it overlaps an existing Docker network.

## Initial TLS certificate

For a public IP, use Certbot 5.4+ and the Let's Encrypt `shortlived` profile.
Domain certificates can use the standard domain option instead. Certificates
must use the name `panel` so the Nginx configuration finds them.

The ACME webroot must be reachable on public port 80 **before** starting the
final HTTPS Nginx container. On an existing server, temporarily add a static
route for `/.well-known/acme-challenge/` to the current proxy, pointing at the
configured ACME directory. On a clean server, a temporary HTTP-only Nginx is
sufficient. Do not stop unrelated services to obtain a certificate.

For the default directories on a clean server with port 80 free:

```sh
mkdir -p .deploy/acme-webroot .deploy/letsencrypt data
docker run -d --name discord-panel-acme-bootstrap -p 80:80 \
  -v "$PWD/.deploy/acme-webroot:/usr/share/nginx/html:ro" nginx:stable-alpine

docker compose --env-file .deploy/compose.env --profile tls run --rm certbot \
  certonly --non-interactive --agree-tos --register-unsafely-without-email \
  --preferred-profile shortlived --webroot -w /var/www/certbot \
  --ip-address 89.117.62.251 --cert-name panel

docker stop discord-panel-acme-bootstrap
docker rm discord-panel-acme-bootstrap
```

Use your own IP and configured paths. Back up `.deploy/letsencrypt`: it holds
the ACME account and private keys. Certbot has writable access; Nginx gets only
read-only access to certificate directories. The application gets no TLS keys.

## Start and inspect

```sh
docker compose --env-file .deploy/compose.env config --quiet
docker compose --env-file .deploy/compose.env up -d --build panel nginx
docker compose --env-file .deploy/compose.env ps
docker compose --env-file .deploy/compose.env logs -f panel
```

The healthcheck probes the local application using its canonical HTTPS origin.
It checks panel readiness, not Discord connectivity. Inspect the panel or bot
connection logs for individual bot states. Nginx keeps `/panel` in the upstream
path; the application mounts all routes and static assets under that prefix.
Requests outside it cannot access the API. Session cookies retain Path=/ as
required for the `__Host-` prefix; bot permissions and CSRF checks still apply.
Access logs contain only query-free URI paths, never OAuth codes or cookies.
Request error logging is disabled to avoid logging full callback URLs; use
HTTP status/access logs and application logs for diagnostics. Configuration
and process startup errors remain visible in the Nginx container log.

Containers use `restart: unless-stopped`, including after a server reboot.
After changing a mounted `.env`, recreate the application container with
`docker compose --env-file .deploy/compose.env up -d --force-recreate --no-deps panel`.
This also handles editors that replace the file inode atomically.

Images and containers can be rebuilt without removing the data mount. Never
use `down -v` or replace a populated data directory during an update.

## Renewal

Install `discord-panel-cert-renew.service.example` and `.timer.example` as
systemd units after setting the repository directory and Compose settings path.
Enable the timer. It runs Certbot in a temporary container twice daily and
reloads only this stack's Nginx after a successful renewal check:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now discord-panel-cert-renew.timer
sudo systemctl start discord-panel-cert-renew.service
```

No Docker socket is mounted in the certificate container. The scheduler runs
on the host; both ACME operations and Nginx run in Docker. IP certificates have
short validity periods, so keep renewal enabled. Recheck with `systemctl
list-timers discord-panel-cert-renew.timer` and the service journal.

## Migration from the tmux deployment

Build and test first. Stop **only this panel** gracefully, back up its `.env`
and complete `data/` directory (SQLite/WAL and uploads), then copy that data to
the configured persistent mount with the correct UID/GID. Copying an actively
written SQLite database with ordinary file copy is unsafe. Do not run the tmux
instance and container against the same database or start duplicate bot clients.

Release the old proxy's ports only after the certificate and Nginx config are
ready. Start Compose and check TLS, `/panel/login`, 401 on unauthenticated API,
OAuth redirect URI, users/grants, and bot connectivity. Keep the backup and old
proxy configuration for rollback. The old environment and data must remain
available until the new deployment is confirmed. The game projects and the
independent old Perimeter panel are outside this migration.
