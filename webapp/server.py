"""Local interactive viewer/editor for a saved WorldMap (.npz).

Serves the rendered map plus a JSON snapshot of settlements/nations, and
exposes rename/retier/delete endpoints that mutate the in-memory WorldMap and
autosave it back to the same .npz path after every edit.
"""

import argparse
import copy
import json
import os
import secrets
import shutil
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import replace

# mapgen/render and DnDPython's core/data/utils live in the mapgenerator/ and
# dndpython/ git submodules (siblings of this repo's own root, not inside
# webapp/) -- putting each submodule's root on sys.path lets both this file's
# `from mapgen...`/`from render...` imports and dnd_api.py's `from core...`/
# `from data...`/`from utils...` imports resolve without renaming anything in
# either upstream repo.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "mapgenerator"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "dndpython"))

import numpy as np
from flask import Flask, jsonify, redirect, request, send_from_directory, session
from werkzeug.local import LocalProxy

from mapgen.config import WorldConfig
from mapgen.poi import DEFAULT_POI_KIND, POI_KINDS, PointOfInterest
from mapgen.region import RegionBounds, build_region
from mapgen.settlements import Settlement, Tier
from mapgen.worldmap import WorldMap, build_world
from render.render_map import render_legend_to_png_bytes, render_to_png_bytes
from webapp.dnd_api import register_dnd_routes
from webapp.invites import InviteStore
from webapp.users import UserStore, clean_password, clean_username

MAX_NAME_LEN = 63  # settlement/nation names are saved as numpy "U64" -- longer
                    # would silently truncate on save, so this is enforced first
MAX_TITLE_LEN = 80
MAX_NOTES_LEN = 4000  # free-text GM/author lore -- generous, but still bounded so a
                       # pasted novel doesn't silently get truncated by the "U4000" save dtype

ALLOWED_STYLES = {"realistic", "classic", "atmospheric"}
DEFAULT_STYLE = "classic"

MIN_WORLD_SIZE = 128
MAX_WORLD_SIZE = 768  # generation time grows roughly with size^2 -- 768 already
                       # takes well over a minute; this is a sanity cap, not a
                       # recommendation to actually run at the ceiling

MIN_REGION_RESOLUTION = 128
MAX_REGION_RESOLUTION = 1024  # region generation is much slower than a full world at
                                # the same output size -- its context-padding halo (see
                                # mapgen/region.py) grows in pixel terms as the selected
                                # area shrinks or resolution rises, so small/high-res
                                # selections can take a minute or more even at this cap

ALLOWED_GRID_KINDS = {"none", "square", "hex"}
MILES_TO_KM = 1.60934  # matches mapgen/agriculture.py's own conversion


def _km_per_cell(world):
    """The grid overlay's cell-size control is in miles (matching how a
    hex-crawl is normally quoted, e.g. "6 miles per hex") but render_map.py's
    grid drawing works in raw grid cells -- this recovers the world's real-
    world scale from its saved generation config to convert between the two."""
    try:
        return float(json.loads(world.config_json).get("km_per_cell", 4.0))
    except Exception:
        return 4.0


def _grid_cell_size_from_miles(world, miles):
    if not miles or miles <= 0:
        return 0.0
    return (miles * MILES_TO_KM) / _km_per_cell(world)

# Root directory holding users.json and each user's own world files. Every
# load/save/generate file operation is restricted to the current session's
# user subdirectory and a plain basename -- this app is reachable from a
# browser over the open internet now, so path traversal ("../../whatever")
# and cross-user file access are blocked by construction, not denylisting.
WORLDS_DIR = os.path.abspath(os.environ.get("MAPGEN_DATA_DIR", "."))


def _user_dir(username):
    path = os.path.join(WORLDS_DIR, "users", username)
    os.makedirs(path, exist_ok=True)
    return path


def _safe_world_path(raw_filename):
    name = os.path.basename((raw_filename or "").strip())
    if not name:
        return None, "filename cannot be empty"
    if not name.lower().endswith(".npz"):
        name += ".npz"
    return os.path.join(_user_dir(session["username"]), name), None


MAX_UNDO_STEPS = 20


class AppState:
    def __init__(self, path):
        self.path = path
        self.world = WorldMap.load(path)
        self.lock = threading.Lock()
        self._png_cache = {}         # style -> PNG bytes
        self._legend_png_cache = {}  # style -> PNG bytes
        self._undo_stack = []        # lightweight snapshots, see push_undo()

    def mark_dirty(self):
        self._png_cache = {}
        self._legend_png_cache = {}

    def save(self):
        # A rolling one-deep on-disk backup -- cheap insurance against a bad
        # edit outliving the in-memory undo stack (e.g. after restarting the
        # server, which clears it) or against a save that goes wrong partway.
        if os.path.exists(self.path):
            try:
                shutil.copy2(self.path, self.path + ".bak")
            except OSError:
                pass
        self.world.save(self.path)

    def use_world(self, world, path):
        """Swap in a freshly loaded or freshly generated world as the active one."""
        self.world = world
        self.path = path
        self._undo_stack = []
        self.mark_dirty()

    def push_undo(self):
        """Snapshot the parts of the world that edit endpoints can change --
        terrain rasters, fief structure etc. are set once at generation time
        and never touched by an edit, so there's no need to pay for copying
        them on every single rename."""
        self._undo_stack.append({
            "settlements": copy.deepcopy(self.world.settlements),
            "nation_names": list(self.world.nation_names),
            "nation_titles": list(self.world.nation_titles),
            "nation_notes": list(self.world.nation_notes),
            "title": self.world.title,
            "points_of_interest": copy.deepcopy(self.world.points_of_interest),
        })
        if len(self._undo_stack) > MAX_UNDO_STEPS:
            self._undo_stack.pop(0)

    def undo(self):
        if not self._undo_stack:
            return False
        snap = self._undo_stack.pop()
        self.world.settlements = snap["settlements"]
        self.world.nation_names = snap["nation_names"]
        self.world.nation_titles = snap["nation_titles"]
        self.world.nation_notes = snap["nation_notes"]
        self.world.title = snap["title"]
        self.world.points_of_interest = snap["points_of_interest"]
        self.mark_dirty()
        self.save()
        return True

    def restore_backup(self):
        """Roll the whole file back to its state before the last save() --
        survives a server restart, unlike the undo stack. A direct file copy,
        not routed through save(), so it doesn't immediately clobber the very
        backup it's restoring from."""
        bak_path = self.path + ".bak"
        if not os.path.exists(bak_path):
            return False
        shutil.copy2(bak_path, self.path)
        self.world = WorldMap.load(self.path)
        self._undo_stack = []
        self.mark_dirty()
        return True

    def render_png(self, style, show_duchy=False, show_county=False, show_barony=False,
                   grid_kind="none", grid_miles=None):
        # legend=False: the JS marker overlay positions markers by percentage
        # of this image's width/height, matched 1:1 to the data grid, so the
        # map image must not include the legend panel (see
        # render_legend_to_png_bytes for why that would misalign markers).
        key = (style, show_duchy, show_county, show_barony, grid_kind, grid_miles)
        if key not in self._png_cache:
            grid_cell_size = _grid_cell_size_from_miles(self.world, grid_miles)
            self._png_cache[key] = render_to_png_bytes(
                self.world, style=style, labels=True, legend=False,
                show_duchy=show_duchy, show_county=show_county, show_barony=show_barony,
                grid_kind=grid_kind, grid_cell_size=grid_cell_size)
        return self._png_cache[key]

    def render_legend_png(self, style):
        if style not in self._legend_png_cache:
            self._legend_png_cache[style] = render_legend_to_png_bytes(self.world, style=style)
        return self._legend_png_cache[style]


app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = os.environ.get("MAPGEN_SECRET_KEY") or secrets.token_hex(32)
register_dnd_routes(app, _user_dir)

# Optional shared code required to create a NEW account (not to log in) --
# keeps signup invite-only on a public Funnel URL without needing per-user
# credentials to be handed out by hand. Unset in local dev, so testing
# doesn't require configuring one.
INVITE_CODE = os.environ.get("MAPGEN_INVITE_CODE")

# The one account allowed to see /admin -- unset in local dev, so testing
# doesn't require configuring one (means no account can reach /admin locally
# unless this is set, which is fine since there's nothing to administer yet).
ADMIN_USERNAME = os.environ.get("MAPGEN_ADMIN_USERNAME")


def _is_admin():
    username = session.get("username")
    return bool(ADMIN_USERNAME) and username == ADMIN_USERNAME

# Sliding-window rate limit for login/signup -- in-memory only, so it resets
# on restart and doesn't share state across worker processes, but this app
# runs as a single process, so that's not a real gap here. Not meant to
# survive an actual DDoS, just to make password-guessing and username
# enumeration impractically slow.
_RATE_LIMIT_WINDOW = 60.0
_RATE_LIMIT_MAX = 8
_rate_limit_lock = threading.Lock()
_rate_limit_hits = defaultdict(deque)


def _rate_limited(bucket, key):
    now = time.time()
    full_key = (bucket, key)
    with _rate_limit_lock:
        hits = _rate_limit_hits[full_key]
        while hits and now - hits[0] > _RATE_LIMIT_WINDOW:
            hits.popleft()
        if len(hits) >= _RATE_LIMIT_MAX:
            return True
        hits.append(now)
        return False


os.makedirs(WORLDS_DIR, exist_ok=True)
users = UserStore(os.path.join(WORLDS_DIR, "users.json"))
invites = InviteStore(os.path.join(WORLDS_DIR, "invites.json"))
print(f"User accounts + world files stored under: {WORLDS_DIR}")

# One AppState per logged-in user, created lazily on first access and kept
# for the life of the process. _registry_lock only guards the dict itself
# (creating a new user's entry) -- each AppState still has its own lock
# guarding edits to that one user's world, so different users' requests
# never block each other.
_state_registry = {}
_registry_lock = threading.Lock()


def _get_or_create_state():
    username = session.get("username")
    if not username:
        return None
    with _registry_lock:
        st = _state_registry.get(username)
        if st is None:
            path = os.path.join(_user_dir(username), "world.npz")
            if not os.path.exists(path):
                # Every new account gets its own freshly generated starter
                # map immediately -- there's no "no world loaded" state to
                # otherwise represent in AppState.
                config = WorldConfig(size=256, seed=secrets.randbelow(2**31))
                build_world(config).save(path)
            st = AppState(path)
            _state_registry[username] = st
        return st


# A drop-in stand-in for a real AppState: every attribute access resolves
# the current request's logged-in user and forwards to *their* AppState, so
# none of the route bodies below need to know multi-user login exists.
state = LocalProxy(_get_or_create_state)


@app.before_request
def _require_login():
    if request.path == "/" or request.path == "/app" or request.path.startswith("/static/") or request.path.startswith("/api/auth/"):
        return None
    username = session.get("username")
    if username and not users.exists(username):
        # The account was deleted (e.g. by an admin) while this browser
        # still had a valid session cookie for it -- without this check,
        # continuing to browse would silently regenerate a fresh starter
        # world under the same username instead of actually being logged out.
        session.clear()
        username = None
    if not username:
        # /dnd/* and /admin serve whole HTML pages (not JSON) -- send a
        # logged-out visitor to the login form instead of dumping raw JSON.
        if request.path == "/dnd" or request.path.startswith("/dnd/") or request.path == "/admin":
            return redirect("/app")
        return jsonify({"error": "login required"}), 401
    return None


@app.route("/api/auth/me")
def api_auth_me():
    username = session.get("username")
    if not username:
        return jsonify({"error": "not logged in"}), 401
    return jsonify({"username": username, "is_admin": _is_admin()})


@app.route("/api/auth/signup", methods=["POST"])
def api_auth_signup():
    if _rate_limited("signup", request.remote_addr):
        return jsonify({"error": "too many attempts -- wait a minute and try again"}), 429
    body = request.get_json(silent=True) or {}
    provided_code = str(body.get("invite_code") or "")
    single_use_code = None
    if INVITE_CODE:
        if secrets.compare_digest(provided_code, INVITE_CODE):
            pass  # the standing code -- reusable, nothing to consume
        elif invites.is_valid(provided_code):
            single_use_code = provided_code
        else:
            return jsonify({"error": "invalid invite code"}), 403
    username, err = clean_username(body.get("username"))
    if err:
        return jsonify({"error": err}), 400
    password, err = clean_password(body.get("password"))
    if err:
        return jsonify({"error": err}), 400
    if not users.create(username, password):
        # Deliberately generic, and the same 400 status as the validation
        # errors above -- a distinct "that username is taken" (409) would
        # let someone enumerate valid usernames by trying many signups.
        return jsonify({"error": "couldn't create that account -- try a different username"}), 400
    if single_use_code:
        # Consumed only now that the account actually exists -- a typo'd
        # password earlier must not burn a tester's one-time code.
        invites.consume(single_use_code, username)
    session["username"] = username
    return jsonify({"username": username})


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    body = request.get_json(silent=True) or {}
    username, _ = clean_username(body.get("username"))
    if _rate_limited("login", username or request.remote_addr):
        return jsonify({"error": "too many attempts -- wait a minute and try again"}), 429
    password = body.get("password") or ""
    if not username or not users.verify(username, password):
        return jsonify({"error": "invalid username or password"}), 401
    session["username"] = username
    return jsonify({"username": username})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    session.clear()
    return jsonify({"ok": True})


def _clean_name(raw):
    if not isinstance(raw, str):
        return None, "name must be a string"
    name = raw.strip()
    if not name:
        return None, "name cannot be empty"
    if len(name) > MAX_NAME_LEN:
        return None, f"name cannot be longer than {MAX_NAME_LEN} characters"
    return name, None


def _clean_title(raw):
    # Unlike settlement/nation names, an empty title is valid -- it just
    # means "no title plaque".
    if not isinstance(raw, str):
        return None, "title must be a string"
    title = raw.strip()
    if len(title) > MAX_TITLE_LEN:
        return None, f"title cannot be longer than {MAX_TITLE_LEN} characters"
    return title, None


def _clean_rank_title(raw):
    if not isinstance(raw, str):
        return None, "title must be a string"
    title = raw.strip()
    if not title:
        return None, "title cannot be empty"
    if len(title) > MAX_NAME_LEN:
        return None, f"title cannot be longer than {MAX_NAME_LEN} characters"
    return title, None


def _clean_notes(raw):
    # Like the map title, empty notes are valid -- it just means nothing's been written yet.
    if not isinstance(raw, str):
        return None, "notes must be a string"
    notes = raw.strip()
    if len(notes) > MAX_NOTES_LEN:
        return None, f"notes cannot be longer than {MAX_NOTES_LEN} characters"
    return notes, None


def _nation_titles(world, nid):
    if 0 <= nid < len(world.nation_titles):
        return world.nation_titles[nid]
    return ("Duke", "Count", "Baron")


def _nation_notes(world, nid):
    if 0 <= nid < len(world.nation_notes):
        return world.nation_notes[nid]
    return ""


def _fief_nation(world, level, fid):
    """Walk up from a fief id to the id of the nation that owns it."""
    if fid is None or fid < 0:
        return None
    if level == "duchy":
        return world.duchy_nation[fid] if fid < len(world.duchy_nation) else None
    if level == "county":
        return _fief_nation(world, "duchy", world.county_duchy[fid] if fid < len(world.county_duchy) else -1)
    if level == "barony":
        return _fief_nation(world, "county", world.barony_county[fid] if fid < len(world.barony_county) else -1)
    return None


def _fief_name(world, level, fid):
    if fid is None or fid < 0:
        return None
    seat_list = {"duchy": world.duchy_seat, "county": world.county_seat, "barony": world.barony_seat}[level]
    if not seat_list:
        # No fief data at all for this world (e.g. a zoomed region -- see
        # mapgen/region.py, which carries settlements' own fief_rank/duchy_id/etc.
        # over from the parent but doesn't regenerate the world-level fief
        # rasters/seat lists at the new resolution). Say nothing rather than
        # guessing "Crown demesne" for every settlement.
        return None
    rank_idx = {"duchy": 0, "county": 1, "barony": 2}[level]
    nation_id = _fief_nation(world, level, fid)
    title = _nation_titles(world, nation_id if nation_id is not None else -1)[rank_idx]

    seat_idx = seat_list[fid] if fid < len(seat_list) else -1
    if seat_idx < 0:
        return "Crown demesne" if level == "duchy" else None
    if seat_idx >= len(world.settlements):
        return None
    return f"{title} of {world.settlements[seat_idx].name}"


def _fief_gold(world, level, fid):
    """Total economic output of every settlement under this fief -- the gold
    its duke/count/baron effectively controls. Sums by membership, not by
    generation origin, so a manually-placed settlement (see
    api_create_settlement) counts the same as a procedurally-generated one."""
    if fid is None or fid < 0:
        return 0.0
    attr = {"duchy": "duchy_id", "county": "county_id", "barony": "barony_id"}[level]
    return sum(s.economic_output for s in world.settlements if getattr(s, attr) == fid)


def _is_synced_region(world):
    """True if the active world is a zoomed region that still knows where
    its parent file lives -- old region files predate parent_path (see
    mapgen/worldmap.py's load() fallback) and just don't sync, same as if
    the parent had been moved or deleted since."""
    return (world.region_bounds is not None and bool(world.parent_path)
            and os.path.exists(world.parent_path))


def _region_to_parent_xy(world, x, y):
    x0, y0, x1, y1 = world.region_bounds
    h, w = world.biome.shape
    pixels_per_unit = w / (x1 - x0)  # square pixels -- see mapgen/region.py's build_region()
    return x0 + x / pixels_per_unit, y0 + y / pixels_per_unit


def _sync_to_parent(world, mutate):
    """Best-effort push of an edit made while viewing a region back onto its
    parent world file. `mutate(parent_world)` applies the equivalent change
    to a freshly-loaded copy of the parent, which is then saved back over
    the same path -- never raises, since a missing/renamed/corrupt parent
    file must not block the region's own edit (already applied and saved
    by the caller before this runs)."""
    if not _is_synced_region(world):
        return
    try:
        parent = WorldMap.load(world.parent_path)
        mutate(parent)
        if os.path.exists(world.parent_path):
            try:
                shutil.copy2(world.parent_path, world.parent_path + ".bak")
            except OSError:
                pass
        parent.save(world.parent_path)
    except Exception:
        pass


def _sync_create_settlement(world, settlement):
    def mutate(parent):
        px, py = _region_to_parent_xy(world, settlement.x, settlement.y)
        h, w = parent.biome.shape
        # Recompute nation ownership from the parent's own territory raster
        # rather than carrying over the region's -- the region's copy was
        # itself only resampled from the parent to begin with.
        nation_id = int(parent.nation_id[int(py), int(px)]) if (0 <= px < w and 0 <= py < h) else -1
        parent.settlements.append(replace(settlement, x=px, y=py, nation_id=nation_id))
    _sync_to_parent(world, mutate)


def _sync_update_settlement(world, uid, **fields):
    def mutate(parent):
        for s in parent.settlements:
            if s.uid == uid:
                for key, value in fields.items():
                    setattr(s, key, value)
                return
    _sync_to_parent(world, mutate)


def _sync_delete_settlement(world, uid):
    def mutate(parent):
        parent.settlements = [s for s in parent.settlements if s.uid != uid]
    _sync_to_parent(world, mutate)


def _sync_create_poi(world, poi):
    def mutate(parent):
        px, py = _region_to_parent_xy(world, poi.x, poi.y)
        parent.points_of_interest.append(replace(poi, x=px, y=py))
    _sync_to_parent(world, mutate)


def _sync_update_poi(world, uid, **fields):
    def mutate(parent):
        for p in parent.points_of_interest:
            if p.uid == uid:
                for key, value in fields.items():
                    setattr(p, key, value)
                return
    _sync_to_parent(world, mutate)


def _sync_delete_poi(world, uid):
    def mutate(parent):
        parent.points_of_interest = [p for p in parent.points_of_interest if p.uid != uid]
    _sync_to_parent(world, mutate)


def _resolve_style():
    style = request.args.get("style", DEFAULT_STYLE)
    return style if style in ALLOWED_STYLES else DEFAULT_STYLE


def _flag(name):
    return request.args.get(name) in ("1", "true", "True")


def _resolve_grid_kind():
    kind = request.args.get("grid", "none")
    return kind if kind in ALLOWED_GRID_KINDS else "none"


def _settlement_dict(sid, s):
    h, w = state.world.biome.shape
    return {
        "id": sid,
        "name": s.name,
        "tier": s.tier.name.lower(),
        "x": s.x,
        "y": s.y,
        "x_pct": 100.0 * s.x / max(w - 1, 1),
        "y_pct": 100.0 * s.y / max(h - 1, 1),
        "nation_id": s.nation_id,
        "population": s.population,
        "area_km2": s.area_km2,
        "economic_output": s.economic_output,
        "resource": s.resource,
        "farmland_ceiling": s.farmland_ceiling,
        "fief_rank": s.fief_rank,
        "duchy_name": _fief_name(state.world, "duchy", s.duchy_id),
        "county_name": _fief_name(state.world, "county", s.county_id),
        "barony_name": _fief_name(state.world, "barony", s.barony_id),
        "duchy_gold": _fief_gold(state.world, "duchy", s.duchy_id),
        "county_gold": _fief_gold(state.world, "county", s.county_id),
        "barony_gold": _fief_gold(state.world, "barony", s.barony_id),
        "notes": s.notes,
    }


def _poi_dict(pid, p):
    h, w = state.world.biome.shape
    return {
        "id": pid,
        "name": p.name,
        "kind": p.kind,
        "x": p.x,
        "y": p.y,
        "x_pct": 100.0 * p.x / max(w - 1, 1),
        "y_pct": 100.0 * p.y / max(h - 1, 1),
        "notes": p.notes,
    }


def _nation_centroid_pct(nation_id):
    mask = state.world.nation_id == nation_id
    if not mask.any():
        return None, None
    h, w = mask.shape
    ys, xs = np.nonzero(mask)
    return 100.0 * float(xs.mean()) / max(w - 1, 1), 100.0 * float(ys.mean()) / max(h - 1, 1)


def _world_snapshot():
    world = state.world
    h, w = world.biome.shape

    settlements = [_settlement_dict(i, s) for i, s in enumerate(world.settlements)]

    nations = []
    for nid, name in enumerate(world.nation_names):
        members = [s for s in world.settlements if s.nation_id == nid]
        capital = next((s for s in world.settlements if s.nation_id == nid and s.tier == Tier.CAPITAL), None)
        capital_id = next((i for i, s in enumerate(world.settlements)
                            if s.nation_id == nid and s.tier == Tier.CAPITAL), None)
        x_pct, y_pct = _nation_centroid_pct(nid)
        duke_title, count_title, baron_title = _nation_titles(world, nid)
        nations.append({
            "id": nid,
            "name": name,
            "capital_id": capital_id,
            "capital_name": capital.name if capital else None,
            "settlement_count": len(members),
            "total_population": sum(s.population for s in members),
            "total_gold": sum(s.economic_output for s in members),
            "x_pct": x_pct,
            "y_pct": y_pct,
            "duke_title": duke_title,
            "count_title": count_title,
            "baron_title": baron_title,
            "notes": _nation_notes(world, nid),
        })

    pois = [_poi_dict(i, p) for i, p in enumerate(world.points_of_interest)]

    return {
        "width": w,
        "height": h,
        "seed": world.seed,
        "path": state.path,
        "title": world.title,
        "settlements": settlements,
        "nations": nations,
        "points_of_interest": pois,
        "poi_kinds": list(POI_KINDS),
        "can_undo": bool(state._undo_stack),
    }


@app.route("/")
def landing():
    return send_from_directory(app.static_folder, "landing.html")


@app.route("/app")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/world")
def api_world():
    with state.lock:
        return jsonify(_world_snapshot())


@app.route("/api/render.png")
def api_render_png():
    style = _resolve_style()
    show_duchy, show_county, show_barony = _flag("duchy"), _flag("county"), _flag("barony")
    grid_kind = _resolve_grid_kind()
    grid_miles = request.args.get("grid_size", type=float)
    with state.lock:
        png = state.render_png(style, show_duchy=show_duchy, show_county=show_county, show_barony=show_barony,
                                grid_kind=grid_kind, grid_miles=grid_miles)
    return app.response_class(png, mimetype="image/png")


@app.route("/api/legend.png")
def api_legend_png():
    style = _resolve_style()
    with state.lock:
        png = state.render_legend_png(style)
    return app.response_class(png, mimetype="image/png")


@app.route("/api/export.png")
def api_export_png():
    """A shareable, standalone PNG -- unlike /api/render.png (which must never
    include the legend, see AppState.render_png), this bakes the legend into
    the same image by default so the download is a single self-contained file."""
    style = _resolve_style()
    show_duchy, show_county, show_barony = _flag("duchy"), _flag("county"), _flag("barony")
    include_legend = request.args.get("legend", "1") in ("1", "true", "True")
    grid_kind = _resolve_grid_kind()
    grid_miles = request.args.get("grid_size", type=float)
    with state.lock:
        grid_cell_size = _grid_cell_size_from_miles(state.world, grid_miles)
        png = render_to_png_bytes(
            state.world, style=style, labels=True, legend=include_legend,
            show_duchy=show_duchy, show_county=show_county, show_barony=show_barony,
            grid_kind=grid_kind, grid_cell_size=grid_cell_size)
        title = state.world.title or "map"

    safe_title = "".join(c if c.isalnum() else "_" for c in title).strip("_") or "map"
    response = app.response_class(png, mimetype="image/png")
    response.headers["Content-Disposition"] = f'attachment; filename="{safe_title}_{style}.png"'
    return response


@app.route("/api/settlements", methods=["POST"])
def api_create_settlement():
    body = request.get_json(silent=True) or {}
    try:
        x, y = float(body.get("x")), float(body.get("y"))
    except (TypeError, ValueError):
        return jsonify({"error": "x/y must be numbers"}), 400
    raw_tier = body.get("tier", "village")
    try:
        tier = Tier[str(raw_tier).upper()]
    except KeyError:
        valid = ", ".join(t.name.lower() for t in Tier)
        return jsonify({"error": f"tier must be one of: {valid}"}), 400
    name, err = _clean_name(body.get("name") or "New settlement")
    if err:
        return jsonify({"error": err}), 400

    with state.lock:
        h, w = state.world.biome.shape
        if not (0 <= x < w and 0 <= y < h):
            return jsonify({"error": f"x/y must be within the current world (0-{w} x 0-{h})"}), 400
        # Inherit whichever nation already controls this spot, same as a
        # procedurally-placed settlement would -- score=0.0 since that field
        # is only ever read during the one-time generation-time ranking in
        # mapgen/political.py and mapgen/fiefs.py, never afterward.
        nation_id = int(state.world.nation_id[int(y), int(x)])
        state.push_undo()
        settlement = Settlement(x=x, y=y, tier=tier, name=name, score=0.0, nation_id=nation_id)
        state.world.settlements.append(settlement)
        state.mark_dirty()
        state.save()
        _sync_create_settlement(state.world, settlement)
        return jsonify(_settlement_dict(len(state.world.settlements) - 1, settlement))


@app.route("/api/settlements/<int:sid>/name", methods=["POST"])
def api_rename_settlement(sid):
    body = request.get_json(silent=True) or {}
    name, err = _clean_name(body.get("name"))
    if err:
        return jsonify({"error": err}), 400
    with state.lock:
        if not (0 <= sid < len(state.world.settlements)):
            return jsonify({"error": "no such settlement"}), 404
        state.push_undo()
        state.world.settlements[sid].name = name
        state.mark_dirty()
        state.save()
        _sync_update_settlement(state.world, state.world.settlements[sid].uid, name=name)
        return jsonify(_settlement_dict(sid, state.world.settlements[sid]))


@app.route("/api/settlements/<int:sid>/tier", methods=["POST"])
def api_retier_settlement(sid):
    body = request.get_json(silent=True) or {}
    raw_tier = body.get("tier", "")
    try:
        tier = Tier[str(raw_tier).upper()]
    except KeyError:
        valid = ", ".join(t.name.lower() for t in Tier)
        return jsonify({"error": f"tier must be one of: {valid}"}), 400
    with state.lock:
        if not (0 <= sid < len(state.world.settlements)):
            return jsonify({"error": "no such settlement"}), 404
        state.push_undo()
        state.world.settlements[sid].tier = tier
        state.mark_dirty()
        state.save()
        _sync_update_settlement(state.world, state.world.settlements[sid].uid, tier=tier)
        return jsonify(_settlement_dict(sid, state.world.settlements[sid]))


def _clean_nonneg_number(raw, field, cast):
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        return None, f"{field} must be a number"
    if value < 0:
        return None, f"{field} cannot be negative"
    return value, None


@app.route("/api/settlements/<int:sid>/stats", methods=["POST"])
def api_set_settlement_stats(sid):
    """Population/area/economic output are normally computed at generation
    time (see mapgen/economy.py), but a settlement placed by hand starts at
    all zeros -- and even a generated one's numbers are just a starting
    point, not gospel, for a GM who wants to hand-tune their world."""
    body = request.get_json(silent=True) or {}
    with state.lock:
        if not (0 <= sid < len(state.world.settlements)):
            return jsonify({"error": "no such settlement"}), 404
        s = state.world.settlements[sid]
        population, area_km2, economic_output = s.population, s.area_km2, s.economic_output

        if "population" in body:
            population, err = _clean_nonneg_number(body["population"], "population", int)
            if err:
                return jsonify({"error": err}), 400
        if "area_km2" in body:
            area_km2, err = _clean_nonneg_number(body["area_km2"], "area_km2", float)
            if err:
                return jsonify({"error": err}), 400
        if "economic_output" in body:
            economic_output, err = _clean_nonneg_number(body["economic_output"], "economic_output", float)
            if err:
                return jsonify({"error": err}), 400

        state.push_undo()
        s.population, s.area_km2, s.economic_output = population, area_km2, economic_output
        state.save()
        _sync_update_settlement(state.world, s.uid, population=population,
                                 area_km2=area_km2, economic_output=economic_output)
        return jsonify(_settlement_dict(sid, s))


@app.route("/api/settlements/<int:sid>/notes", methods=["POST"])
def api_set_settlement_notes(sid):
    body = request.get_json(silent=True) or {}
    notes, err = _clean_notes(body.get("notes", ""))
    if err:
        return jsonify({"error": err}), 400
    with state.lock:
        if not (0 <= sid < len(state.world.settlements)):
            return jsonify({"error": "no such settlement"}), 404
        state.push_undo()
        state.world.settlements[sid].notes = notes
        state.save()
        _sync_update_settlement(state.world, state.world.settlements[sid].uid, notes=notes)
        return jsonify(_settlement_dict(sid, state.world.settlements[sid]))


@app.route("/api/settlements/<int:sid>", methods=["DELETE"])
def api_delete_settlement(sid):
    with state.lock:
        if not (0 <= sid < len(state.world.settlements)):
            return jsonify({"error": "no such settlement"}), 404
        uid = state.world.settlements[sid].uid
        state.push_undo()
        del state.world.settlements[sid]
        state.mark_dirty()
        state.save()
        _sync_delete_settlement(state.world, uid)
        return jsonify({"ok": True})


@app.route("/api/title", methods=["POST"])
def api_set_title():
    body = request.get_json(silent=True) or {}
    title, err = _clean_title(body.get("title", ""))
    if err:
        return jsonify({"error": err}), 400
    with state.lock:
        state.push_undo()
        state.world.title = title
        state.mark_dirty()
        state.save()
        return jsonify({"title": title})


@app.route("/api/nations/<int:nid>/name", methods=["POST"])
def api_rename_nation(nid):
    body = request.get_json(silent=True) or {}
    name, err = _clean_name(body.get("name"))
    if err:
        return jsonify({"error": err}), 400
    with state.lock:
        if not (0 <= nid < len(state.world.nation_names)):
            return jsonify({"error": "no such nation"}), 404
        state.push_undo()
        state.world.nation_names[nid] = name
        state.mark_dirty()
        state.save()
        return jsonify({"id": nid, "name": name})


@app.route("/api/nations/<int:nid>/titles", methods=["POST"])
def api_set_nation_titles(nid):
    body = request.get_json(silent=True) or {}
    with state.lock:
        if not (0 <= nid < len(state.world.nation_names)):
            return jsonify({"error": "no such nation"}), 404
        current = list(_nation_titles(state.world, nid))
        for i, key in enumerate(("duke", "count", "baron")):
            if key not in body:
                continue
            cleaned, err = _clean_rank_title(body[key])
            if err:
                return jsonify({"error": err}), 400
            current[i] = cleaned

        state.push_undo()
        while len(state.world.nation_titles) < len(state.world.nation_names):
            state.world.nation_titles.append(("Duke", "Count", "Baron"))
        state.world.nation_titles[nid] = tuple(current)
        state.save()
        return jsonify({"id": nid, "duke": current[0], "count": current[1], "baron": current[2]})


@app.route("/api/nations/<int:nid>/notes", methods=["POST"])
def api_set_nation_notes(nid):
    body = request.get_json(silent=True) or {}
    notes, err = _clean_notes(body.get("notes", ""))
    if err:
        return jsonify({"error": err}), 400
    with state.lock:
        if not (0 <= nid < len(state.world.nation_names)):
            return jsonify({"error": "no such nation"}), 404
        state.push_undo()
        while len(state.world.nation_notes) < len(state.world.nation_names):
            state.world.nation_notes.append("")
        state.world.nation_notes[nid] = notes
        state.save()
        return jsonify({"id": nid, "notes": notes})


def _clean_poi_kind(raw):
    kind = str(raw or "").strip().lower()
    if kind not in POI_KINDS:
        return None, f"kind must be one of: {', '.join(POI_KINDS)}"
    return kind, None


@app.route("/api/pois", methods=["POST"])
def api_create_poi():
    body = request.get_json(silent=True) or {}
    try:
        x, y = float(body.get("x")), float(body.get("y"))
    except (TypeError, ValueError):
        return jsonify({"error": "x/y must be numbers"}), 400
    kind, err = _clean_poi_kind(body.get("kind", DEFAULT_POI_KIND))
    if err:
        return jsonify({"error": err}), 400
    name, err = _clean_name(body.get("name") or "New landmark")
    if err:
        return jsonify({"error": err}), 400

    with state.lock:
        h, w = state.world.biome.shape
        if not (0 <= x < w and 0 <= y < h):
            return jsonify({"error": f"x/y must be within the current world (0-{w} x 0-{h})"}), 400
        state.push_undo()
        poi = PointOfInterest(x=x, y=y, kind=kind, name=name)
        state.world.points_of_interest.append(poi)
        state.mark_dirty()
        state.save()
        _sync_create_poi(state.world, poi)
        return jsonify(_poi_dict(len(state.world.points_of_interest) - 1, poi))


@app.route("/api/pois/<int:pid>", methods=["POST"])
def api_update_poi(pid):
    body = request.get_json(silent=True) or {}
    with state.lock:
        if not (0 <= pid < len(state.world.points_of_interest)):
            return jsonify({"error": "no such point of interest"}), 404

        name = state.world.points_of_interest[pid].name
        if "name" in body:
            name, err = _clean_name(body["name"])
            if err:
                return jsonify({"error": err}), 400

        kind = state.world.points_of_interest[pid].kind
        if "kind" in body:
            kind, err = _clean_poi_kind(body["kind"])
            if err:
                return jsonify({"error": err}), 400

        notes = state.world.points_of_interest[pid].notes
        if "notes" in body:
            notes, err = _clean_notes(body["notes"])
            if err:
                return jsonify({"error": err}), 400

        state.push_undo()
        poi = state.world.points_of_interest[pid]
        poi.name, poi.kind, poi.notes = name, kind, notes
        state.mark_dirty()
        state.save()
        _sync_update_poi(state.world, poi.uid, name=name, kind=kind, notes=notes)
        return jsonify(_poi_dict(pid, poi))


@app.route("/api/pois/<int:pid>", methods=["DELETE"])
def api_delete_poi(pid):
    with state.lock:
        if not (0 <= pid < len(state.world.points_of_interest)):
            return jsonify({"error": "no such point of interest"}), 404
        uid = state.world.points_of_interest[pid].uid
        state.push_undo()
        del state.world.points_of_interest[pid]
        state.mark_dirty()
        state.save()
        _sync_delete_poi(state.world, uid)
        return jsonify({"ok": True})


@app.route("/api/undo", methods=["POST"])
def api_undo():
    with state.lock:
        if not state.undo():
            return jsonify({"error": "nothing to undo"}), 400
        return jsonify(_world_snapshot())


@app.route("/api/restore-backup", methods=["POST"])
def api_restore_backup():
    with state.lock:
        if not state.restore_backup():
            return jsonify({"error": "no backup file found for the current world"}), 400
        return jsonify(_world_snapshot())


@app.route("/api/worlds/upload", methods=["POST"])
def api_upload_world():
    """The only way to bring a world into the app: upload a .npz from
    whatever device/browser you're using. Replaces the old "list files
    already sitting in the server's directory" picker -- makes sense once the
    app runs on a separate machine you reach remotely rather than one you're
    sitting at."""
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "no file provided"}), 400
    if not file.filename.lower().endswith(".npz"):
        return jsonify({"error": "file must be a .npz"}), 400
    path, err = _safe_world_path(file.filename)
    if err:
        return jsonify({"error": err}), 400

    with state.lock:
        # Save to a temp path and validate before touching the real target,
        # so a bad/corrupt upload never clobbers a working file of the same name.
        tmp_path = path + ".upload_tmp"
        file.save(tmp_path)
        try:
            world = WorldMap.load(tmp_path)
        except Exception as e:
            os.remove(tmp_path)
            return jsonify({"error": f"not a valid world file: {e}"}), 400
        os.replace(tmp_path, path)
        state.use_world(world, path)
        return jsonify(_world_snapshot())


@app.route("/api/worlds/download")
def api_download_world():
    with state.lock:
        path = state.path
    directory = os.path.dirname(os.path.abspath(path)) or "."
    filename = os.path.basename(path)
    return send_from_directory(directory, filename, as_attachment=True, download_name=filename)


@app.route("/api/worlds/list")
def api_list_worlds():
    """Every .npz already sitting in the current user's own directory --
    generated worlds, zoomed regions, save-as copies. Scoped to _user_dir, so
    unlike the old pre-multi-user file list this can't leak another
    account's filenames."""
    directory = _user_dir(session["username"])
    current = os.path.basename(state.path)
    files = [
        {"filename": name, "current": name == current}
        for name in sorted(os.listdir(directory))
        if name.lower().endswith(".npz")
    ]
    return jsonify({"files": files})


@app.route("/api/worlds/switch", methods=["POST"])
def api_switch_world():
    body = request.get_json(silent=True) or {}
    path, err = _safe_world_path(body.get("filename"))
    if err:
        return jsonify({"error": err}), 400
    if not os.path.exists(path):
        return jsonify({"error": "no such file"}), 404
    with state.lock:
        try:
            world = WorldMap.load(path)
        except Exception as e:
            return jsonify({"error": f"failed to load: {e}"}), 400
        state.use_world(world, path)
        return jsonify(_world_snapshot())


@app.route("/api/worlds/delete", methods=["POST"])
def api_delete_world():
    body = request.get_json(silent=True) or {}
    path, err = _safe_world_path(body.get("filename"))
    if err:
        return jsonify({"error": err}), 400
    with state.lock:
        if os.path.abspath(path) == os.path.abspath(state.path):
            return jsonify({"error": "cannot delete the file you're currently viewing -- "
                                      "switch to another file first"}), 400
        if not os.path.exists(path):
            return jsonify({"error": "no such file"}), 404
        os.remove(path)
        bak_path = path + ".bak"
        if os.path.exists(bak_path):
            os.remove(bak_path)
        return jsonify({"ok": True})


@app.route("/api/worlds/save", methods=["POST"])
def api_save_world_as():
    body = request.get_json(silent=True) or {}
    path, err = _safe_world_path(body.get("filename"))
    if err:
        return jsonify({"error": err}), 400
    with state.lock:
        state.path = path
        state.save()
        return jsonify({"filename": os.path.basename(path)})


@app.route("/api/generate", methods=["POST"])
def api_generate_world():
    body = request.get_json(silent=True) or {}

    try:
        seed = int(body.get("seed", 0))
        size = int(body.get("size", 512))
    except (TypeError, ValueError):
        return jsonify({"error": "seed and size must be integers"}), 400
    if not (MIN_WORLD_SIZE <= size <= MAX_WORLD_SIZE):
        return jsonify({"error": f"size must be between {MIN_WORLD_SIZE} and {MAX_WORLD_SIZE}"}), 400

    continent_bias = body.get("continent_bias")
    if continent_bias not in (None, ""):
        try:
            continent_bias = float(continent_bias)
        except (TypeError, ValueError):
            return jsonify({"error": "continent_bias must be a number"}), 400
        if not (0.0 <= continent_bias <= 1.0):
            return jsonify({"error": "continent_bias must be between 0.0 and 1.0"}), 400
    else:
        continent_bias = None

    path, err = _safe_world_path(body.get("filename") or "world.npz")
    if err:
        return jsonify({"error": err}), 400

    config = WorldConfig(size=size, seed=seed, continent_bias=continent_bias)
    with state.lock:
        world = build_world(config)
        world.save(path)
        state.use_world(world, path)
        return jsonify(_world_snapshot())


@app.route("/api/region", methods=["POST"])
def api_zoom_region():
    body = request.get_json(silent=True) or {}
    try:
        x0, y0 = float(body.get("x0")), float(body.get("y0"))
        x1, y1 = float(body.get("x1")), float(body.get("y1"))
    except (TypeError, ValueError):
        return jsonify({"error": "x0/y0/x1/y1 must be numbers"}), 400
    if not (x1 > x0 and y1 > y0):
        return jsonify({"error": "region bounds must have x1 > x0 and y1 > y0"}), 400

    try:
        resolution = int(body.get("resolution", 512))
    except (TypeError, ValueError):
        return jsonify({"error": "resolution must be an integer"}), 400
    if not (MIN_REGION_RESOLUTION <= resolution <= MAX_REGION_RESOLUTION):
        return jsonify({"error": f"resolution must be between {MIN_REGION_RESOLUTION} and {MAX_REGION_RESOLUTION}"}), 400

    path, err = _safe_world_path(body.get("filename") or "region.npz")
    if err:
        return jsonify({"error": err}), 400

    with state.lock:
        world = state.world
        if world.region_bounds is not None:
            return jsonify({"error": "this world is already a zoomed region -- load the original "
                                      "full world before zooming again (region bounds are in the "
                                      "original world's coordinate space, not this region's)"}), 400

        h, w = world.biome.shape
        if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
            return jsonify({"error": f"bounds must be within the current world (0-{w} x 0-{h})"}), 400

        try:
            config = WorldConfig(**json.loads(world.config_json))
        except Exception:
            return jsonify({"error": "current world has no usable config to regenerate a region from"}), 400

        bounds = RegionBounds(x0=x0, y0=y0, x1=x1, y1=y1)
        region_world = build_region(
            config, world.elevation_lo, world.elevation_hi, world.sea_level, bounds,
            resolution=resolution,
            parent_settlements=world.settlements,
            parent_nation_id=world.nation_id,
            parent_nation_names=world.nation_names,
            parent_points_of_interest=world.points_of_interest,
        )
        # Carried over by hand -- build_region only concerns itself with terrain
        # + settlements/nations, not these cosmetic, world-level fields.
        region_world.title = world.title
        region_world.nation_titles = list(world.nation_titles)
        # Lets edits made while viewing this region sync back to the parent
        # (see _sync_to_parent) -- resolved to an absolute path since state.path
        # may be relative to the process's cwd, which this region's own file
        # won't share once loaded independently later.
        region_world.parent_path = os.path.abspath(state.path)

        region_world.save(path)
        state.use_world(region_world, path)
        return jsonify(_world_snapshot())


def _admin_user_stats(username):
    directory = _user_dir(username)
    world_files = []
    total_bytes = 0
    for name in sorted(os.listdir(directory)):
        full = os.path.join(directory, name)
        if not os.path.isfile(full):
            continue
        total_bytes += os.path.getsize(full)
        if name.lower().endswith(".npz"):
            world_files.append(name)
    scenarios_dir = os.path.join(directory, "dnd_scenarios")
    scenario_count = 0
    if os.path.isdir(scenarios_dir):
        scenario_count = len([f for f in os.listdir(scenarios_dir) if f.endswith(".json")])
    return {
        "username": username,
        "world_files": world_files,
        "total_bytes": total_bytes,
        "scenario_count": scenario_count,
        "is_admin": username == ADMIN_USERNAME,
    }


@app.route("/admin")
def admin_page():
    if not _is_admin():
        return jsonify({"error": "forbidden"}), 403
    return send_from_directory(app.static_folder, "admin.html")


@app.route("/api/admin/users")
def api_admin_users():
    if not _is_admin():
        return jsonify({"error": "forbidden"}), 403
    return jsonify({"users": [_admin_user_stats(u) for u in users.list_usernames()]})


@app.route("/api/admin/users/<username>", methods=["DELETE"])
def api_admin_delete_user(username):
    if not _is_admin():
        return jsonify({"error": "forbidden"}), 403
    if username == ADMIN_USERNAME:
        return jsonify({"error": "cannot delete the admin account"}), 400
    # Remove the login first -- if the directory removal below ever fails
    # partway, we want "can no longer log in" over "still has a valid
    # password but no data", not the other way around.
    if not users.delete(username):
        return jsonify({"error": "no such user"}), 404
    with _registry_lock:
        _state_registry.pop(username, None)
    shutil.rmtree(_user_dir(username), ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/api/admin/invites")
def api_admin_invites():
    if not _is_admin():
        return jsonify({"error": "forbidden"}), 403
    return jsonify({"invites": invites.list()})


@app.route("/api/admin/invites", methods=["POST"])
def api_admin_create_invite():
    if not _is_admin():
        return jsonify({"error": "forbidden"}), 403
    body = request.get_json(silent=True) or {}
    note = str(body.get("note") or "")[:MAX_TITLE_LEN]
    return jsonify({"code": invites.create(note=note)})


@app.route("/api/admin/invites/<code>", methods=["DELETE"])
def api_admin_delete_invite(code):
    if not _is_admin():
        return jsonify({"error": "forbidden"}), 403
    if not invites.delete(code):
        return jsonify({"error": "no such code"}), 404
    return jsonify({"ok": True})


def parse_args():
    parser = argparse.ArgumentParser(description="Multi-user web app for viewing/editing WorldMap .npz files.")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    return parser.parse_args()


def main():
    # Only for local/dev runs (`python -m webapp.server`) -- WORLDS_DIR/users/
    # invites are already set up above at import time, since a production
    # WSGI server (gunicorn) imports this module for its `app` object and
    # never calls main().
    args = parse_args()
    print(f"Serving on http://{args.host}:{args.port}")
    # threaded=True: each logged-in user gets their own AppState with its own
    # lock, so one user's slow request (e.g. generating a new world) no
    # longer blocks every other user's page loads.
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
