"""Single-use invite codes, layered on top of the constant MAPGEN_INVITE_CODE
env var: that one code is meant to be shared once and reused by everyone,
while these are minted individually (e.g. one per beta tester) and stop
working after their first signup. Stored the same way as UserStore -- a
small JSON file, fine for a handful of codes."""

import json
import os
import secrets
import threading
import time

# Excludes 0/O and 1/I/L so a code read aloud or hand-copied doesn't get
# mistyped into a different valid-looking code.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _generate_code():
    group = lambda: "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
    return f"{group()}-{group()}"


class InviteStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._invites = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self._invites = json.load(f)

    def _persist(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._invites, f, indent=2)
        os.replace(tmp, self.path)

    def create(self, note=""):
        with self._lock:
            while True:
                code = _generate_code()
                if code not in self._invites:
                    break
            self._invites[code] = {
                "created_at": time.time(),
                "note": note,
                "used_by": None,
                "used_at": None,
            }
            self._persist()
            return code

    def list(self):
        with self._lock:
            return [
                {"code": code, **info}
                for code, info in sorted(self._invites.items(), key=lambda kv: kv[1]["created_at"], reverse=True)
            ]

    def is_valid(self, code):
        with self._lock:
            info = self._invites.get(code)
            return bool(info) and info["used_by"] is None

    def consume(self, code, username):
        with self._lock:
            info = self._invites.get(code)
            if not info or info["used_by"] is not None:
                return False
            info["used_by"] = username
            info["used_at"] = time.time()
            self._persist()
            return True

    def delete(self, code):
        with self._lock:
            if code not in self._invites:
                return False
            del self._invites[code]
            self._persist()
            return True
