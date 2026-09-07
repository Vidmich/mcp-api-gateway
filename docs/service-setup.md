# Running it as a service

The gateway never daemonises. It runs in the foreground, logs to stderr, and
exits `0` when it is sent `SIGINT`/`SIGTERM` (`SIGBREAK` on Windows) after
letting in-flight requests finish. There is no `--daemon`, no PID file, and no
built-in log rotation, because every supervisor below already does those things
better than a bespoke implementation would.

That makes every recipe here the same three decisions in different syntax:

1. **which account it runs as** — a dedicated one that owns the data directory
   and nothing else;
2. **where its config and data live** — named explicitly, never inherited from a
   working directory;
3. **where its secrets come from** — the environment, so they are not in a file
   that gets copied around.

## Common ground

Point it at both paths explicitly and it stops mattering where it was started
from:

```bash
/opt/mcp-api-gateway/bin/mcp-api-gateway \
  --config /etc/mcp-api-gateway/config.toml \
  --data-dir /var/lib/mcp-api-gateway
```

Put the secrets in the environment rather than in the config file
([configuration.md](configuration.md#environment-variables) has the naming
rule):

```bash
MCP_API_GATEWAY_MCP__AUTH_TOKEN=...
MCP_API_GATEWAY_ADMIN__USERNAME=admin
MCP_API_GATEWAY_ADMIN__PASSWORD_HASH=pbkdf2_sha256$600000$...
```

`/healthz` is the liveness endpoint. It is never behind the admin session and
never behind the `/mcp` token, so a probe needs no credentials:

```bash
curl -fsS http://127.0.0.1:8080/healthz
```

```json
{"status":"ok","version":"0.1.0","uptime_seconds":208.087,"config_path":"/etc/mcp-api-gateway/config.toml"}
```

Logs go to stderr, one line per event, with the level first. Under systemd and
launchd that is the journal or a file you nominate; under NSSM and Docker it is
whatever those are told to capture. `--log-level debug` adds a line per HTTP
request — useful while setting a service up, noisy afterwards.

## systemd

`/etc/systemd/system/mcp-api-gateway.service`:

```ini
[Unit]
Description=mcp-api-gateway
Documentation=https://github.com/Vidmich/mcp-api-gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=mcp-api-gateway
Group=mcp-api-gateway
ExecStart=/opt/mcp-api-gateway/bin/mcp-api-gateway \
  --config /etc/mcp-api-gateway/config.toml \
  --data-dir /var/lib/mcp-api-gateway
EnvironmentFile=-/etc/mcp-api-gateway/env
Restart=on-failure
RestartSec=5
# It exits 0 on SIGTERM once in-flight requests have drained; give it room to.
KillSignal=SIGTERM
TimeoutStopSec=30

# It needs one writable directory and nothing else.
StateDirectory=mcp-api-gateway
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/mcp-api-gateway
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
```

`/etc/mcp-api-gateway/env`, owned by root and mode `0600`:

```
MCP_API_GATEWAY_MCP__AUTH_TOKEN=a-long-random-string
MCP_API_GATEWAY_ADMIN__USERNAME=admin
MCP_API_GATEWAY_ADMIN__PASSWORD_HASH=pbkdf2_sha256$600000$...
```

Then:

```bash
sudo useradd --system --home /var/lib/mcp-api-gateway --shell /usr/sbin/nologin mcp-api-gateway
sudo systemctl daemon-reload
sudo systemctl enable --now mcp-api-gateway
journalctl -u mcp-api-gateway -f
```

`StateDirectory=` creates `/var/lib/mcp-api-gateway` with the right owner on
every start, which is what makes `ProtectSystem=strict` survivable: the gateway
writes there and nowhere else.

## launchd (macOS)

A user agent, at `~/Library/LaunchAgents/dev.mcp-api-gateway.plist`, runs while
you are logged in — the right shape for a gateway serving MCP clients on your
own machine. Swap in `/Library/LaunchDaemons` and add `UserName` for a system-wide
one.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>dev.mcp-api-gateway</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/mcp-api-gateway</string>
    <string>--config</string>
    <string>/Users/you/Library/Application Support/mcp-api-gateway/config.toml</string>
    <string>--data-dir</string>
    <string>/Users/you/Library/Application Support/mcp-api-gateway/data</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>MCP_API_GATEWAY_MCP__AUTH_TOKEN</key>
    <string>a-long-random-string</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/you/Library/Logs/mcp-api-gateway.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/you/Library/Logs/mcp-api-gateway.log</string>
</dict>
</plist>
```

```bash
launchctl load -w ~/Library/LaunchAgents/dev.mcp-api-gateway.plist
launchctl list | grep mcp-api-gateway
tail -f ~/Library/Logs/mcp-api-gateway.log
```

`KeepAlive` with `SuccessfulExit: false` restarts it when it crashes but not
when you stop it deliberately — the gateway exits `0` on a signal, which is what
makes that distinction work. A plist keeps no secrets safe from anyone who can
read the file; on a shared Mac, prefer a config file the account owns.

## Windows

### NSSM

[NSSM](https://nssm.cc/) wraps a foreground program as a real Windows service,
which is what this needs. From an elevated prompt:

```
nssm install mcp-api-gateway "C:\Program Files\mcp-api-gateway\Scripts\mcp-api-gateway.exe"
nssm set mcp-api-gateway AppParameters "--config C:\ProgramData\mcp-api-gateway\config.toml --data-dir C:\ProgramData\mcp-api-gateway\data"
nssm set mcp-api-gateway AppDirectory "C:\ProgramData\mcp-api-gateway"
nssm set mcp-api-gateway AppEnvironmentExtra "MCP_API_GATEWAY_MCP__AUTH_TOKEN=a-long-random-string"
nssm set mcp-api-gateway AppStdout "C:\ProgramData\mcp-api-gateway\logs\service.log"
nssm set mcp-api-gateway AppStderr "C:\ProgramData\mcp-api-gateway\logs\service.log"
nssm set mcp-api-gateway AppRotateFiles 1
nssm set mcp-api-gateway Start SERVICE_AUTO_START
nssm start mcp-api-gateway
```

NSSM stops a console program by sending Ctrl-C first, which is the graceful path
— the gateway drains and exits `0`. Leave `AppStopMethodConsole` at its default
rather than shortening it below the time a slow upstream call can take.

The account the service runs as must own the data directory: `keys.json` is
created readable by its creator alone, so a service that later runs as a
different user cannot read its own key.

### Task Scheduler

No extra software, and a worse fit — it starts things, it does not supervise
them. Use it when installing NSSM is not an option:

```
schtasks /create /tn "mcp-api-gateway" /sc onstart /ru "SYSTEM" /rl HIGHEST ^
  /tr "\"C:\Program Files\mcp-api-gateway\Scripts\mcp-api-gateway.exe\" --config C:\ProgramData\mcp-api-gateway\config.toml --data-dir C:\ProgramData\mcp-api-gateway\data"
```

Then, in Task Scheduler, on the task's **Settings** tab, set *If the task fails,
restart every* to 1 minute and untick *Stop the task if it runs longer than*.
Without both, a crash is permanent and a long-running gateway is killed after
three days. There is nowhere to put an environment variable, so secrets have to
go in the config file — which makes that file's ACL the only thing protecting
them.

## Docker

The image builds the wheel from a checkout, because the project is not on PyPI
yet ([install.md](install.md#the-name)).

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist

FROM python:3.12-slim
RUN useradd --system --create-home --home-dir /var/lib/mcp-api-gateway gateway
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
USER gateway
WORKDIR /var/lib/mcp-api-gateway
VOLUME ["/var/lib/mcp-api-gateway"]
EXPOSE 8080
# 0.0.0.0 inside the container only. Publish it to loopback on the host.
ENV MCP_API_GATEWAY_SERVER__HOST=0.0.0.0
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"
ENTRYPOINT ["mcp-api-gateway"]
CMD ["--config", "/var/lib/mcp-api-gateway/config.toml", "--data-dir", "/var/lib/mcp-api-gateway"]
```

```bash
docker build -t mcp-api-gateway .
docker volume create mcp-api-gateway-data
docker run -d --name mcp-api-gateway \
  -p 127.0.0.1:8080:8080 \
  -v mcp-api-gateway-data:/var/lib/mcp-api-gateway \
  -e MCP_API_GATEWAY_MCP__AUTH_TOKEN=a-long-random-string \
  --restart unless-stopped \
  mcp-api-gateway
```

Or with compose:

```yaml
services:
  gateway:
    build: .
    ports:
      - "127.0.0.1:8080:8080"
    volumes:
      - gateway-data:/var/lib/mcp-api-gateway
    environment:
      MCP_API_GATEWAY_MCP__AUTH_TOKEN: a-long-random-string
    restart: unless-stopped

volumes:
  gateway-data:
```

Two things to get right. **The volume is not optional**: without it, the
database and — worse — `keys.json` are inside the container's writable layer,
and every stored upstream credential dies with the container. **Publish to
`127.0.0.1`, not to `0.0.0.0`**, unless the gateway is meant to be reachable
from the network and you have read [security.md](security.md).

`docker stop` sends `SIGTERM` and waits ten seconds by default; the gateway
drains and exits `0` well inside that unless an upstream call is hanging on a
30-second timeout. `--stop-timeout 35` covers even that.

## Behind a reverse proxy

The gateway speaks plain HTTP and has no TLS of its own, so anything reachable
beyond the local machine wants a proxy in front. Keep the gateway on loopback so
the proxy is the only way in.

```nginx
server {
    listen 443 ssl;
    server_name gateway.example.com;

    ssl_certificate     /etc/letsencrypt/live/gateway.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/gateway.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        # A tool call takes as long as the upstream does; keep this at or above
        # http.timeout_seconds.
        proxy_read_timeout 120s;
    }

    location /mcp {
        proxy_pass http://127.0.0.1:8080/mcp;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Connection "";
        # /mcp streams. Buffering here turns a live response into a stalled one.
        proxy_buffering off;
        chunked_transfer_encoding on;
        proxy_read_timeout 3600s;
    }
}
```

The gateway ignores `X-Forwarded-For` and `X-Forwarded-Proto` — it makes no
decision from the client address, so nothing breaks if they are absent and
nothing is fooled if they are forged.

## What has been tested

The recipes are not equally proven, and it is worth saying which is which.

| Recipe | Status |
|---|---|
| The command line every recipe runs | **Verified.** A wheel installed into a clean virtual environment, started with `--config` and `--data-dir`, serving `/healthz`, `/ui` and `/mcp`. |
| Graceful stop on a signal | **Verified**, by an integration test that signals a real server mid-request and asserts the request finishes and the process exits `0`. |
| systemd unit | **Untested.** Written against systemd 252 semantics; no Linux host was available. |
| launchd plist | **Untested.** No macOS host was available. |
| NSSM | **Untested.** NSSM was not installed on the machine this was written on. |
| Task Scheduler | **Untested.** Registering a system-wide scheduled task was out of scope for a documentation change. |
| Dockerfile and compose file | **Untested.** No Docker daemon was available. |
| nginx configuration | **Untested.** |

If you run one of them, an issue or a pull request correcting it is worth more
than the paragraph above.
