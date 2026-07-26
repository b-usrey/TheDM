# TheDM

Multi-user web hosting layer for [MapGenerator](https://github.com/b-usrey/MapGeneration) (a procedural fantasy world generator) and [DnDPython](https://github.com/b-usrey/DnDPython) (a D&D 5e combat simulator), sharing one login. Both projects are included as git submodules; this repo is just the Flask app, accounts, and per-user storage that ties them together.

## Run it with Docker (recommended)

```bash
git clone --recurse-submodules https://github.com/b-usrey/TheDM.git
cd TheDM
cp .env.example .env
```

Fill in `.env`:
- `MAPGEN_SECRET_KEY` -- generate one with `python -c "import secrets; print(secrets.token_hex(32))"`
- `MAPGEN_INVITE_CODE` -- a shared code required to sign up (leave blank for open signup)
- `MAPGEN_ADMIN_USERNAME` -- the account that can reach `/admin` once it exists

Then:

```bash
docker compose up -d --build
```

The app is now on `http://localhost:5000`. Accounts, world files, and D&D scenarios persist in `./data` on the host (bind-mounted into the container), so `docker compose down`/`up` doesn't lose anything.

If you cloned without `--recurse-submodules`, run `git submodule update --init` first.

## Run it without Docker

```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
export MAPGEN_SECRET_KEY=...  # same env vars as above
python -m webapp.server --host 127.0.0.1 --port 5000
```

This uses Flask's built-in dev server -- fine for local use, but the Docker path (gunicorn) is what's meant for anything reachable beyond your own machine.

## Admin page

Sign up an account with the username you set as `MAPGEN_ADMIN_USERNAME`, then visit `/admin` to see registered accounts, delete accounts, and generate single-use invite codes for beta testers (separate from the standing `MAPGEN_INVITE_CODE`).
