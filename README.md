# TheDM

Multi-user web hosting layer for [MapGenerator](https://github.com/b-usrey/MapGeneration) (a procedural fantasy world generator) and [DnDPython](https://github.com/b-usrey/DnDPython) (a D&D 5e combat simulator), sharing one login. Both projects are included as git submodules; this repo is just the Flask app, accounts, and per-user storage that ties them together.

## Setting up your own server

### 1. Prerequisites

- Docker and the Compose plugin (`docker compose version` should print something, not "command not found")
- git

### 2. Clone and configure

```bash
git clone --recurse-submodules https://github.com/b-usrey/TheDM.git
cd TheDM
cp .env.example .env
```

If you cloned without `--recurse-submodules`, run `git submodule update --init` before continuing -- the Docker build just copies whatever's on disk, so it needs `mapgenerator/` and `dndpython/` to actually be checked out first.

Fill in `.env`:
- `MAPGEN_SECRET_KEY` -- generate one with `python3 -c "import secrets; print(secrets.token_hex(32))"`
- `MAPGEN_INVITE_CODE` -- a shared code required to sign up (leave blank for open signup)
- `MAPGEN_ADMIN_USERNAME` -- the account that can reach `/admin` once it exists (pick a username you're about to create in the next step)

### 3. Bring it up

```bash
docker compose up -d --build
```

The first build takes a minute or two (installing numpy/matplotlib/scipy/flask/gunicorn from scratch). Once it's done, check that it's actually serving:

```bash
docker compose ps                                             # should show "Up"
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5000/  # should print 200
```

Accounts, world files, and D&D scenarios persist in `./data` on the host (bind-mounted into the container), so `docker compose down`/`up` -- or even deleting and rebuilding the image -- never loses anything as long as `./data` itself is left alone.

### 4. Create your admin account

Visit `http://<your-server>:5000/app` and sign up using the exact username you set as `MAPGEN_ADMIN_USERNAME`. That account can then reach `/admin` to see registered accounts, delete accounts, and generate single-use invite codes for beta testers (separate from the standing `MAPGEN_INVITE_CODE` everyone else signs up with).

### 5. Exposing it beyond your own machine (optional)

By default this is only reachable from the machine it's running on (or your LAN, via the server's local IP). To make it reachable from anywhere, put something in front of port 5000 that handles TLS -- a few common options, roughly easiest to most involved:

- **Tailscale Funnel** -- no router configuration, no domain needed, gives you a `https://your-machine.your-tailnet.ts.net` URL. `sudo tailscale funnel --bg 5000` (the `--bg` matters: without it, the funnel only stays up while that terminal session is open).
- **A reverse proxy** (Caddy, nginx, Traefik) on a domain you control, terminating TLS and forwarding to `127.0.0.1:5000`. More setup, but gives you your own domain.
- **Plain router port-forwarding** -- works, but there's no TLS unless you add it yourself, so credentials would cross the internet in plaintext. Not recommended.

## Run it without Docker

```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
export MAPGEN_SECRET_KEY=...  # same env vars as above
python -m webapp.server --host 127.0.0.1 --port 5000
```

This uses Flask's built-in dev server -- fine for local use, but the Docker path (gunicorn) is what's meant for anything reachable beyond your own machine.

## Troubleshooting

**`curl` to the app returns nothing / connection refused, right after starting it up.** Give it a few seconds -- there's a brief window between the container reporting "Up" and gunicorn actually accepting connections. Retry the curl from step 3.

**It works from `curl 127.0.0.1:...` on the server itself, but not from another device on the network.** Something between the two is blocking the port -- check `sudo ufw status` (or whatever firewall you're running) for a rule covering that port.

**Accounts/worlds you copied into `./data` don't show up after copying them in.** `UserStore`/`InviteStore` only read `users.json`/`invites.json` once, at startup. Copying files into the bind-mounted `./data` directory while the container is already running doesn't get picked up until you restart it:
```bash
docker compose restart
```

**Don't raise `--workers` in the Dockerfile's gunicorn command.** All per-user state (session registry, rate limiter, the account/invite stores' file locks) lives in one process's memory. A second worker *process* would fork that state into a disconnected copy instead of sharing it -- multiple *threads* (already configured) is the safe way to get concurrency here.
