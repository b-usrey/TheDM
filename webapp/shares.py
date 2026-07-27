"""Read-only share links for a world file: a token that grants unauthenticated,
view-only access to one specific (owner, filename) pair, with GM notes
stripped out server-side (see server.py's _public_world_snapshot). Stored the
same way as InviteStore/UserStore -- a small JSON file, fine for the handful
of links a GM would ever have open at once.
"""

import json
import os
import secrets
import threading
import time


class ShareStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._shares = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self._shares = json.load(f)

    def _persist(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._shares, f, indent=2)
        os.replace(tmp, self.path)

    def _find_token(self, username, filename):
        for token, info in self._shares.items():
            if info["username"] == username and info["filename"] == filename:
                return token
        return None

    def find_for(self, username, filename):
        with self._lock:
            return self._find_token(username, filename)

    def create(self, username, filename):
        """Idempotent -- a world that's already shared keeps its existing
        link rather than invalidating it (a GM re-opening the World tab
        shouldn't silently break a link they already handed out)."""
        with self._lock:
            existing = self._find_token(username, filename)
            if existing:
                return existing
            token = secrets.token_urlsafe(16)
            self._shares[token] = {
                "username": username,
                "filename": filename,
                "created_at": time.time(),
            }
            self._persist()
            return token

    def get(self, token):
        with self._lock:
            return self._shares.get(token)

    def revoke_for(self, username, filename):
        with self._lock:
            token = self._find_token(username, filename)
            if not token:
                return False
            del self._shares[token]
            self._persist()
            return True
