# whenever

Shared calendars that several people use from their own calendar apps. Each person can read every event and add their own, and each event shows who created it.

- **CalDAV** for clients that can write (Apple Calendar, Thunderbird, DAVx5 on Android, Evolution).
- **Read-only ICS feeds** for clients that can only subscribe (Google Calendar, Outlook).
- **A simple admin web interface** for users, calendars and memberships.

FastAPI + SQLAlchemy + SQLite. No external services.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m whenever create-admin admin --display-name "Admin"
.venv/bin/python -m whenever serve            # http://127.0.0.1:8000/admin/
```

Or run it with uvicorn directly: `uvicorn whenever.main:create_app --factory`.

Run the tests with `.venv/bin/pytest`.

## How it works

1. The admin creates **users** and **calendars**, then ticks **memberships**. A member can always read the calendar; tick *write* as well to let them add events.
2. Each user has a secret **token**. Their page in the admin UI lists every URL they need.
3. Any member can read every event. Only the event's creator, or an admin, can change or delete it. Clients see other people's events as read-only through `current-user-privilege-set`; if they try to write anyway, the server rejects it with 403.
4. The creator's name is added to every event when it is served:
   - as `DESCRIPTION` "— Created by Alice", which every client displays
   - as `X-WHENEVER-OWNER` / `X-WHENEVER-OWNER-NAME`
   - optionally as a `[Alice]` title prefix (a per-calendar setting)

   These additions are removed again when a client saves the event back, so they don't pile up.

### URLs

| Purpose | URL | Auth |
|---|---|---|
| CalDAV account (server address) | `https://host/` or `https://host/dav/u/{username}/` | Basic: username + password **or** token |
| CalDAV without credentials | `https://host/dav/t/{token}/` (or `…/{token}/{calendar}/`) | token in the path |
| Read-only subscription | `https://host/feed/{token}/{calendar}.ics` | token in the path |
| Admin | `https://host/admin/` | admin username + password |

`/.well-known/caldav` redirects to `/dav/`, so clients that discover the server automatically (Apple, DAVx5) only need the hostname.

### Client notes

- **Apple Calendar / iOS:** add a CalDAV account with the server set to the hostname, plus the username and password (or the token as the password).
- **Thunderbird:** go to New Calendar → On the Network, and paste the per-calendar token URL. It won't ask for credentials.
- **DAVx5 (Android):** log in with a URL and username, using `https://host/` and the username with the password or token.
- **Google Calendar / Outlook:** add a calendar from a URL with the `.ics` feed. It is read-only, and Google refreshes it slowly (often every 12–24 hours).

## Configuration (environment variables)

| Variable | Default | |
|---|---|---|
| `WHENEVER_DATA_DIR` | `./data` | Holds the SQLite database and a generated session secret |
| `WHENEVER_DATABASE_URL` | `sqlite:///{data_dir}/whenever.db` | Any SQLAlchemy URL |
| `WHENEVER_SECRET_KEY` | generated | Signs the admin session cookie |
| `WHENEVER_BASE_URL` | request URL | Public URL used in the connection details the admin UI shows |
| `WHENEVER_ALLOW_TOKEN_URLS` | `true` | Set to `false` to require Basic auth for CalDAV |
| `WHENEVER_COOKIE_SECURE` | `false` | Set to `true` behind HTTPS |
| `WHENEVER_MAX_EVENT_SIZE` | `262144` | Largest accepted event, in bytes |
| `WHENEVER_FEED_REFRESH_MINUTES` | `15` | Refresh interval suggested in ICS feeds |

## Layout

```
whenever/
  main.py         app factory
  config.py       settings
  models.py       User, Calendar, Membership, Event, EventTombstone
  security.py     scrypt passwords, tokens, Basic auth
  ical.py         validation, creator annotations, feed building
  store.py        event writes; keeps sync_seq / ETags consistent
  feeds.py        /feed/{token}/{slug}.ics
  dav/            CalDAV: PROPFIND, REPORT (multiget, query, sync-collection), GET, PUT, DELETE
  admin/          admin routes and Jinja templates
tests/            pytest suite, including an end-to-end run with the `caldav` client library
```

## Supported CalDAV features

These cover what the major clients use:

- **Discovery:** current-user-principal and calendar-home-set.
- **Change detection:** ctag (`CS:getctag`), `sync-collection` with deletions, and ETags with `If-Match` / `If-None-Match`.
- **Reports:** `calendar-multiget`, and `calendar-query` with VEVENT time-range and UID filters.

Not supported:

- **MKCALENDAR, MOVE and PROPPATCH** return 403. Calendars are managed in the admin UI.
- **VTODO** (tasks) is rejected.
- **Recurring events always match a time-range query.** The server doesn't expand recurrences; the client filters them itself.

## Deploying behind nginx

The setup below runs the `whenever` app on `127.0.0.1:8000` under systemd, with nginx in front handling TLS on its own hostname (here `cal.example.org`).

Serve it from the root of its own hostname, not a sub-path such as `/calendar/`. Calendar clients look for `/.well-known/caldav` at the root of the host, and some also send PROPFIND to `/`.

### 1. Install the app

```bash
sudo useradd --system --home /opt/whenever --shell /usr/sbin/nologin whenever
sudo mkdir -p /opt/whenever /var/lib/whenever
sudo chown whenever: /opt/whenever /var/lib/whenever
sudo -u whenever git clone <your repo> /opt/whenever/app   # or copy the files
sudo -u whenever python3 -m venv /opt/whenever/venv
sudo -u whenever /opt/whenever/venv/bin/pip install /opt/whenever/app
sudo -u whenever WHENEVER_DATA_DIR=/var/lib/whenever \
    /opt/whenever/venv/bin/python -m whenever create-admin admin
```

### 2. Run it with systemd

Create `/etc/systemd/system/whenever.service`:

```ini
[Unit]
Description=whenever
After=network.target

[Service]
User=whenever
Group=whenever
Environment=WHENEVER_DATA_DIR=/var/lib/whenever
Environment=WHENEVER_BASE_URL=https://cal.example.org
Environment=WHENEVER_COOKIE_SECURE=true
# --proxy-headers and --forwarded-allow-ips make uvicorn trust nginx's X-Forwarded-* headers.
# --no-access-log keeps tokens out of the app's logs; nginx logs requests instead (see below).
ExecStart=/opt/whenever/venv/bin/uvicorn whenever.main:create_app --factory \
    --host 127.0.0.1 --port 8000 --workers 1 \
    --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/whenever
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now whenever
```

Keep `--workers 1` while using SQLite.

### 3. Configure nginx

nginx proxies every HTTP method by default, including the WebDAV ones (`PROPFIND`, `REPORT`, `PROPPATCH`). Don't enable nginx's own `dav_methods` module, and don't add `limit_except` blocks, on these locations.

Create `/etc/nginx/sites-available/whenever` and link it into `sites-enabled`:

```nginx
# Replace the token in /dav/t/<token>/ and /feed/<token>/ paths with *** in the access log.
map $request_uri $whenever_log_uri {
    "~^(?<pre>/(?:dav/t|feed)/)[^/?]+(?<post>.*)$"  "${pre}***${post}";
    default                                         $request_uri;
}
log_format whenever '$remote_addr - $remote_user [$time_local] '
                     '"$request_method $whenever_log_uri $server_protocol" '
                     '$status $body_bytes_sent "$http_user_agent"';

# Rate limit the admin login form, to slow down password guessing.
limit_req_zone $binary_remote_addr zone=whenever_login:10m rate=10r/m;

server {
    listen 80;
    listen [::]:80;
    server_name cal.example.org;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name cal.example.org;

    ssl_certificate     /etc/letsencrypt/live/cal.example.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/cal.example.org/privkey.pem;

    access_log /var/log/nginx/whenever.access.log whenever;

    # Must be at least WHENEVER_MAX_EVENT_SIZE (256 KB by default).
    client_max_body_size 1m;

    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;

    location = /admin/login {
        limit_req zone=whenever_login burst=5 nodelay;
        proxy_pass http://127.0.0.1:8000;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/whenever /etc/nginx/sites-enabled/
sudo certbot --nginx -d cal.example.org     # if you don't have a certificate yet
sudo nginx -t && sudo systemctl reload nginx
```

Notes:

- `proxy_pass` has **no trailing slash or path**, so nginx passes request paths through unchanged. CalDAV clients compare the `href`s the server returns with the URLs they requested, so the paths must match exactly.
- `X-Forwarded-Proto` lets the app know the original request was HTTPS, so the admin UI's redirects and the connection URLs it shows use `https://`. nginx forwards the `Authorization` header that Basic auth uses without any extra configuration.
- Leave nginx's default `proxy_buffering on`. Responses are small, and none of them stream.
- The default `error_log` doesn't include request paths, but debug-level logging does. Don't turn on debug logging in production unless you're happy for tokens to appear in it.

### 4. Check it

```bash
# Discovery: should print 301 with "Location: /dav/"
curl -si -X PROPFIND https://cal.example.org/.well-known/caldav | grep -iE '^(HTTP|location)'

# PROPFIND gets through nginx: should print 207 and an href ending /dav/u/admin/
curl -s -X PROPFIND -H 'Depth: 0' -u admin https://cal.example.org/dav/ | head -c 400; echo
```

Then open `https://cal.example.org/admin/`, log in, and use a user's connection page to set up a real client.

## Deployment and things to think about

- **Use HTTPS.** Tokens and Basic-auth passwords are as good as passwords. See [Deploying behind nginx](#deploying-behind-nginx), and set `WHENEVER_COOKIE_SECURE=true`.
- **Tokens appear in access logs** unless you remove them. The nginx config above writes them to its log as `***` and turns off uvicorn's access log. If a URL leaks anyway, rotate the token from the user's admin page.
- **Run a single process.** A SQLite file with one uvicorn worker is enough for small teams. For more, point `WHENEVER_DATABASE_URL` at PostgreSQL.
- **Back up the database** with `sqlite3 data/whenever.db ".backup backup.db"`, which is safe with WAL. Keep `data/secret_key` too.
- **Reminders (VALARM) are shared.** An alarm set by the creator is stored in the event, so every member gets it. Stripping VALARMs on save is a possible addition.
- **Invitations:** `ORGANIZER`/`ATTENDEE` are stored, but no emails or iTIP scheduling messages are sent.
- **Admin login has no rate limiting.** Put the proxy's rate limiting or fail2ban in front of `/admin/login` and the DAV endpoints.
- **Possible next steps:** an audit log of who changed what; hiding the details of `CLASS:PRIVATE` events from other members; database migrations (Alembic) once the schema needs to change; VTODO support; an "all my calendars" combined feed.
