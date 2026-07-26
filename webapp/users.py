"""Per-user account storage for multi-user mode: a small JSON file mapping
username -> password hash. Fine for a handful of friends; if this ever needs
real concurrency or many accounts, swap for a proper database."""

import json
import os
import re
import threading

from werkzeug.security import check_password_hash, generate_password_hash

USERNAME_RE = re.compile(r"^[a-z0-9_]{3,24}$")
MIN_PASSWORD_LEN = 6


class UserStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._users = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self._users = json.load(f)

    def _persist(self):
        # Write-to-temp-then-replace so a crash mid-write can't corrupt the
        # file out from under every other user's login.
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._users, f, indent=2)
        os.replace(tmp, self.path)

    def create(self, username, password):
        with self._lock:
            if username in self._users:
                return False
            self._users[username] = {"password_hash": generate_password_hash(password)}
            self._persist()
            return True

    def verify(self, username, password):
        record = self._users.get(username)
        if not record:
            return False
        return check_password_hash(record["password_hash"], password)


def clean_username(raw):
    name = (raw or "").strip().lower()
    if not USERNAME_RE.match(name):
        return None, "username must be 3-24 characters: lowercase letters, numbers, underscore only"
    return name, None


def clean_password(raw):
    if not isinstance(raw, str) or len(raw) < MIN_PASSWORD_LEN:
        return None, f"password must be at least {MIN_PASSWORD_LEN} characters"
    return raw, None
