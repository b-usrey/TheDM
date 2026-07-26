FROM python:3.13-slim

WORKDIR /app

# Only requirements.txt first so the pip install layer is cached across
# rebuilds that don't touch dependencies.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# mapgenerator/ and dndpython/ must already be checked out (git submodules --
# see README) by the time this runs; Docker's build context is just whatever
# is on disk, it has no special handling for them.
COPY . .

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# --workers 1 is not a tuning knob -- the app keeps per-user state
# (AppState registry, rate limiter, UserStore/InviteStore locks) in one
# process's memory, so a second worker process would fork it into a
# disconnected copy instead of sharing it. --threads gives the same
# per-request concurrency the dev server's threaded=True provided.
CMD ["gunicorn", "--workers", "1", "--worker-class", "gthread", "--threads", "8", "--bind", "0.0.0.0:8000", "webapp.server:app"]
