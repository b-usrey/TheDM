"""Flask routes exposing the DnDPython combat simulator (vendored as the
dndpython/ git submodule) and serving the adapted DnDPython frontend pages
(webapp/dnd_pages/).

Ported from DnDPython/api.py (a FastAPI app) to plain Flask so it can share
this app's process, session-based login, and per-user storage instead of
running as a separate service. Response/error shapes intentionally match
FastAPI's conventions (HTTPException -> {"detail": ...}) because the adapted
frontend pages were written against that API and still expect them.

DnDPython's own modules import each other as bare `core.X`/`data.X`/`utils.X`
(its native style, unchanged from github.com/b-usrey/DnDPython) rather than a
renamed `dnd.X` -- see server.py's sys.path setup, which is what makes that
resolve to the submodule instead of colliding with anything of ours.
"""

import contextlib
import importlib
import io
import json
import os
import pkgutil
import random as _random

from flask import jsonify, request, send_from_directory, session

import data.features
for _mod in pkgutil.iter_modules(data.features.__path__):
    if _mod.name != "base":
        importlib.import_module(f"data.features.{_mod.name}")

from core.combat_manager import CombatManager
from core.events import EventBus
from core.InitiativeManager import InitiativeManager
from data.features.base import Feature
from data.features.homebrew import validate_homebrew_class
from data.monsters.monsters import MONSTER_REGISTRY
from utils.combat_logger import CombatLogger
from utils.creatureFactory import CreatureFactory
from utils.encounter_builder import build_encounter, score_encounter
from utils.scenarioLoader import ScenarioLoader, build_map, place_creatures

_HERE = os.path.dirname(os.path.abspath(__file__))
DND_ROOT = os.path.abspath(os.path.join(_HERE, "..", "dndpython"))
PAGES_DIR = os.path.join(_HERE, "dnd_pages")

BUNDLED_SCENARIOS_DIR = os.path.join(DND_ROOT, "scenarios")  # shipped samples, read-only
CLASSES_DIR = os.path.join(DND_ROOT, "data", "classes")
ITEMS_PATH = os.path.join(DND_ROOT, "data", "items.json")

MAX_EPISODES = 50  # simulate() runs synchronously in the request; keep a single
                    # request from tying up a worker for too long


def _detail_error(message, status=400):
    return jsonify({"detail": message}), status


def _run_episode(scenario_data, silent=True, strategy=None):
    captured = io.StringIO()
    ctx = contextlib.redirect_stdout(captured) if silent else contextlib.nullcontext()

    with ctx:
        event = EventBus()
        factory = CreatureFactory()
        loader = ScenarioLoader(factory, event)
        players, monsters = loader.load(scenario_data)

        monster_idx = 0
        for tmpl in scenario_data.get("monsters", []):
            mtype = tmpl.get("type", "").upper()
            count = tmpl.get("count", 1)
            role = tmpl.get("weapon_role", "random")
            if mtype not in MONSTER_REGISTRY:
                monster_idx += count
                continue
            all_attacks = MONSTER_REGISTRY[mtype].get("attacks", [])
            melee_attacks = [a for a in all_attacks if a.get("attack_type", "melee") == "melee"]
            ranged_attacks = [a for a in all_attacks if a.get("attack_type", "melee") != "melee"]
            for _ in range(count):
                if monster_idx >= len(monsters):
                    break
                m = monsters[monster_idx]
                if role == "all":
                    m._attack_templates = all_attacks
                elif role == "melee":
                    m._attack_templates = melee_attacks or all_attacks
                elif role == "ranged":
                    m._attack_templates = ranged_attacks or all_attacks
                else:
                    m._attack_templates = _random.choice([melee_attacks, ranged_attacks]) or all_attacks
                monster_idx += 1

        battle_map = build_map(scenario_data)
        place_creatures(scenario_data, players, monsters, battle_map)
        initiative = InitiativeManager(players + monsters, event)
        max_rounds = scenario_data.get("max_rounds", 100)
        cm = CombatManager(event, initiative, battle_map, max_rounds=max_rounds)
        if strategy:
            from core.ml_strategy import Strategy as StrategyEnum
            try:
                cm.ai.current_strategy = StrategyEnum[strategy.upper()]
            except KeyError:
                pass
        logger = CombatLogger(event, initiative)   # in-memory only, no file path
        outcome = cm.run()
        logger.close()

    log = captured.getvalue()
    outcome_lower = outcome.lower()
    if "blue" in outcome_lower:
        winner = "blue"
    elif "red" in outcome_lower:
        winner = "red"
    else:
        winner = None

    all_creatures = players + monsters
    summaries = [
        {
            "name": c.name,
            "team": getattr(c, "team", "unknown"),
            "hp": max(c.hp, 0),
            "max_hp": c.max_hp,
            "alive": c.is_alive(),
        }
        for c in all_creatures
    ]

    return {
        "outcome": outcome,
        "winner": winner,
        "rounds": cm.initiative.round,
        "creatures": summaries,
        "log": log,
        "events": logger.records,
    }


def _party_levels_from_scenario(scenario_data):
    """Total character level per player (summed across multiclass levels),
    for score_encounter's DMG XP-budget math."""
    levels = []
    for p in scenario_data.get("players", []):
        classes = p.get("classes", [])
        levels.append(sum(lvl for _, lvl in classes) if classes else 1)
    return levels


# Tags that duplicate the "trigger" field's own semantics (see CombatLogger's
# attack-record schema) -- excluded from by_tag so e.g. "Extra Attack" isn't
# reported twice, once correctly under by_trigger and once as a meaningless
# "feature usage" entry under by_tag.
_TRIGGER_TAGS = {"bonus_action", "extra_attack"}


def _aggregate_character_stats(episodes_events, team="blue"):
    """
    Tier-1 character-analyzer stats, built from CombatLogger's structured
    event records across all simulated episodes (not just the last one).

    Args:
        episodes_events: list of per-episode event-record lists (each
            episode's "events" list from _run_episode).
        team: which team's characters to report on.

    Returns {character_name: {damage_by_round, by_trigger, by_tag}}.
    damage_by_round includes both "attack" records (weapon/spell-attack-
    roll damage) and "save_damage" records (Fireball/Thunderwave/etc.) --
    without the latter, a save-based caster's damage would be invisible.
    by_trigger/by_tag cover attack records only (save-based spells have no
    such per-attack concepts).
    """
    n_episodes = len(episodes_events) or 1
    damage_by_round = {}   # name -> round -> total damage across all episodes
    trigger_stats    = {}  # name -> trigger -> {attempts, hits, crits, damage}
    tag_stats        = {}  # name -> tag -> {attempts, hits, damage}
    names_seen = set()

    def _bump_round_damage(name, rnd, dmg):
        damage_by_round.setdefault(name, {})
        damage_by_round[name][rnd] = damage_by_round[name].get(rnd, 0.0) + dmg

    for events in episodes_events:
        for r in events:
            rtype = r.get("type")
            if rtype not in ("attack", "save_damage"):
                continue
            if r.get("team") != team:
                continue
            name = r["creature"]
            names_seen.add(name)
            rnd = r.get("round", 1)

            if rtype == "save_damage":
                _bump_round_damage(name, rnd, r.get("damage", 0) or 0)
                continue

            # "attack" record
            hit  = bool(r.get("hit"))
            crit = bool(r.get("critical"))
            dmg  = r.get("damage", 0) or 0
            trig = r.get("trigger", "action")

            if hit:
                _bump_round_damage(name, rnd, dmg)

            ts = trigger_stats.setdefault(name, {}).setdefault(
                trig, {"attempts": 0, "hits": 0, "crits": 0, "damage": 0.0})
            ts["attempts"] += 1
            if hit:
                ts["hits"] += 1
                ts["damage"] += dmg
            if crit:
                ts["crits"] += 1

            for tag in r.get("tags", []):
                if tag in _TRIGGER_TAGS:
                    continue
                tg = tag_stats.setdefault(name, {}).setdefault(
                    tag, {"attempts": 0, "hits": 0, "damage": 0.0})
                tg["attempts"] += 1
                if hit:
                    tg["hits"] += 1
                    tg["damage"] += dmg

    result = {}
    for name in names_seen:
        rounds = damage_by_round.get(name, {})
        result[name] = {
            "damage_by_round": {
                str(rnd): round(total / n_episodes, 2)
                for rnd, total in sorted(rounds.items())
            },
            "by_trigger": {
                trig: {
                    "attempts":           s["attempts"],
                    "hits":               s["hits"],
                    "crits":              s["crits"],
                    "hit_rate":           round(s["hits"] / s["attempts"], 3) if s["attempts"] else 0.0,
                    "crit_rate":          round(s["crits"] / s["attempts"], 3) if s["attempts"] else 0.0,
                    "avg_damage_per_hit": round(s["damage"] / s["hits"], 2) if s["hits"] else 0.0,
                }
                for trig, s in trigger_stats.get(name, {}).items()
            },
            "by_tag": {
                tag: {
                    "attempts":           s["attempts"],
                    "hits":               s["hits"],
                    "hit_rate":           round(s["hits"] / s["attempts"], 3) if s["attempts"] else 0.0,
                    "avg_damage_per_hit": round(s["damage"] / s["hits"], 2) if s["hits"] else 0.0,
                }
                for tag, s in tag_stats.get(name, {}).items()
            },
        }
    return result


def register_dnd_routes(app, user_dir):
    """user_dir(username) -> path to that user's own storage directory,
    same helper server.py already uses for world files."""

    def _user_scenarios_dir(username):
        path = os.path.join(user_dir(username), "dnd_scenarios")
        os.makedirs(path, exist_ok=True)
        return path

    def _find_scenario_path(username, filename):
        """User-saved scenarios shadow bundled ones of the same name."""
        safe_name = os.path.basename(filename)
        user_path = os.path.join(_user_scenarios_dir(username), safe_name)
        if os.path.exists(user_path):
            return user_path
        bundled_path = os.path.join(BUNDLED_SCENARIOS_DIR, safe_name)
        if os.path.exists(bundled_path):
            return bundled_path
        return None

    def _user_classes_dir(username):
        path = os.path.join(user_dir(username), "dnd_classes")
        os.makedirs(path, exist_ok=True)
        return path

    def _class_filename(name):
        return f"{os.path.basename((name or '').strip().lower())}.json"

    def _find_class_path(username, name):
        """User-saved (homebrew) classes shadow bundled ones of the same name."""
        safe_name = _class_filename(name)
        user_path = os.path.join(_user_classes_dir(username), safe_name)
        if os.path.exists(user_path):
            return user_path
        bundled_path = os.path.join(CLASSES_DIR, safe_name)
        if os.path.exists(bundled_path):
            return bundled_path
        return None

    # ---- Reference data -----------------------------------------------

    def _class_summary(data, homebrew):
        return {
            "name": data["class_name"],
            "hit_die": data["hit_die"],
            "saving_throws": data.get("saving_throws", []),
            "armor_profs": data.get("armor_proficiencies", []),
            "weapon_profs": data.get("weapon_proficiencies", []),
            "subclasses": list(data.get("subclasses", {}).keys()),
            "features_by_level": {
                lvl: [f["name"] for f in feats]
                for lvl, feats in data.get("features_by_level", {}).items()
            },
            "homebrew": homebrew,
        }

    @app.route("/api/dnd/classes")
    def api_dnd_list_classes():
        username = session["username"]
        results = []
        seen = set()

        # User's own (potentially homebrew) classes shadow bundled ones
        # of the same name.
        user_dir_path = _user_classes_dir(username)
        for name in sorted(os.listdir(user_dir_path)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(user_dir_path, name), encoding="utf-8") as f:
                data = json.load(f)
            results.append(_class_summary(data, homebrew=True))
            seen.add(name)

        for name in sorted(os.listdir(CLASSES_DIR)):
            if not name.endswith(".json") or name in seen:
                continue
            with open(os.path.join(CLASSES_DIR, name), encoding="utf-8") as f:
                data = json.load(f)
            results.append(_class_summary(data, homebrew=False))

        return jsonify({"classes": results})

    @app.route("/api/dnd/classes/<name>")
    def api_dnd_get_class(name):
        username = session["username"]
        path = _find_class_path(username, name)
        if not path:
            return _detail_error(f"Class '{name}' not found.", 404)
        with open(path, encoding="utf-8") as f:
            return jsonify(json.load(f))

    @app.route("/api/dnd/classes", methods=["POST"])
    def api_dnd_save_class():
        """Save a homebrew class. Every feature entry is either a name that
        must already exist in Feature.REGISTRY, or an inline "homebrew"
        block interpreted by data.features.homebrew.HomebrewFeature at
        runtime -- no code from the upload is ever executed."""
        username = session["username"]
        body = request.get_json(silent=True) or {}
        class_data = body.get("class")
        overwrite = bool(body.get("overwrite", False))

        if not isinstance(class_data, dict):
            return _detail_error("Request body must contain a 'class' object.", 422)
        class_name = class_data.get("class_name")
        if not isinstance(class_name, str) or not class_name.strip():
            return _detail_error("Class must have a non-empty 'class_name'.", 422)

        errors = validate_homebrew_class(class_data)
        if errors:
            return _detail_error("Invalid homebrew class: " + "; ".join(errors), 422)

        safe_name = _class_filename(class_name)
        user_dir_path = _user_classes_dir(username)
        out_path = os.path.join(user_dir_path, safe_name)
        if os.path.exists(out_path) and not overwrite:
            return _detail_error(
                f"You already have a class named '{class_name}'. Set overwrite=true to replace it.", 409
            )
        if os.path.exists(os.path.join(CLASSES_DIR, safe_name)) and not overwrite:
            return _detail_error(
                f"'{class_name}' is a built-in class name. Set overwrite=true to save your own "
                f"version under that name (only for your account -- the built-in class is untouched).",
                409,
            )

        # Write to a temp path first so a bad write never leaves a
        # half-written file behind, matching the world-upload pattern in
        # server.py.
        tmp_path = out_path + ".upload_tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(class_data, f, indent=2)
        os.replace(tmp_path, out_path)
        return jsonify({"saved": True, "name": class_name, "filename": safe_name})

    @app.route("/api/dnd/classes/<name>", methods=["DELETE"])
    def api_dnd_delete_class(name):
        """Only ever deletes from the current user's own directory -- a
        bundled class can never be removed this way, even if it's shadowed."""
        username = session["username"]
        safe_name = _class_filename(name)
        path = os.path.join(_user_classes_dir(username), safe_name)
        if not os.path.exists(path):
            return _detail_error(f"'{name}' not found in your custom classes.", 404)
        os.remove(path)
        return jsonify({"deleted": True, "name": name})

    @app.route("/api/dnd/items")
    def api_dnd_list_items():
        with open(ITEMS_PATH, encoding="utf-8") as f:
            raw = json.load(f)

        weapons, armor, trinkets = [], [], []
        for key, item in raw.items():
            if key == "comment":
                continue
            t = item.get("type", "")
            if t == "weapon":
                weapons.append({
                    "name": item["name"],
                    "damage_die": item.get("damage_die"),
                    "damage_type": item.get("damageType"),
                    "ability": item.get("ability"),
                    "attack_type": item.get("attack_type"),
                    "range": f"{item.get('normal_range', 5)}/{item.get('long_range', 5)}",
                    "properties": item.get("properties", []),
                    "attack_bonus": item.get("attack_bonus", 0),
                    "damage_bonus": item.get("damage_bonus", 0),
                })
            elif t == "armor":
                armor.append({
                    "name": item["name"],
                    "base_ac": item.get("base_ac"),
                    "armor_type": item.get("armor_type"),
                    "magic_bonus": item.get("magic_bonus", 0),
                })
            elif t == "trinket":
                trinkets.append({
                    "name": item["name"],
                    "feature": item.get("feature"),
                    "description": item.get("description", ""),
                })
        return jsonify({"weapons": weapons, "armor": armor, "trinkets": trinkets})

    @app.route("/api/dnd/features")
    def api_dnd_list_features():
        _CLASS_ONLY = {
            "Rage", "Reckless Attack", "Danger Sense", "Extra Attack", "Extra Attack II",
            "Extra Attack III", "Fast Movement", "Feral Instinct", "Brutal Critical",
            "Brutal Critical II", "Brutal Critical III", "Relentless Rage",
            "Persistent Rage", "Primal Champion", "Frenzy", "Mindless Rage",
            "Retaliation", "Bear Totem Spirit", "Lay on Hands", "Divine Smite",
            "Aura of Protection", "Aura of Courage", "Improved Divine Smite",
            "Sacred Weapon", "Vow of Enmity", "Soul of Vengeance",
            "Sneak Attack", "Cunning Action", "Uncanny Dodge", "Evasion",
            "Slippery Mind", "Elusive", "Stroke of Luck", "Assassinate", "Death Strike",
            "Eldritch Blast", "Pact Magic", "Agonizing Blast", "Repelling Blast", "Hex",
            "Dark One's Blessing", "Fiendish Resilience", "Second Wind", "Action Surge",
            "Action Surge II", "Indomitable", "Indomitable II", "Indomitable III",
            "Improved Critical", "Superior Critical", "Survivor",
            "Favored Foe", "Roving", "Feral Senses", "Foe Slayer",
            "Dread Ambusher", "Iron Mind", "Stalker's Flurry", "Shadowy Dodge",
            "Nature's Veil", "Land's Stride",
        }
        all_names = sorted(Feature.REGISTRY.keys())
        standalone = [n for n in all_names if n not in _CLASS_ONLY and not n.startswith("ASI")]
        return jsonify({"all_features": all_names, "standalone_feats": standalone})

    @app.route("/api/dnd/monsters")
    def api_dnd_list_monsters():
        return jsonify({"monsters": sorted(MONSTER_REGISTRY.keys())})

    @app.route("/api/dnd/monsters/<name>")
    def api_dnd_get_monster(name):
        key = name.upper()
        if key not in MONSTER_REGISTRY:
            return _detail_error(f"Monster '{name}' not found.", 404)
        return jsonify(MONSTER_REGISTRY[key])

    # ---- Encounter generation -------------------------------------------

    @app.route("/api/dnd/encounter", methods=["POST"])
    def api_dnd_generate_encounter():
        """Randomly build a combat-balanced encounter for a party at a
        target difficulty, using the official 5e DMG XP-budget math (see
        utils.encounter_builder.build_encounter in the dndpython
        submodule). Response's "monsters" list is shaped exactly like a
        scenario JSON's "monsters" entry, so the frontend can drop it
        straight into the existing scenario save/simulate flow."""
        body = request.get_json(silent=True) or {}
        raw_levels = body.get("party_levels")
        difficulty = body.get("difficulty", "medium")

        if not isinstance(raw_levels, list) or not raw_levels:
            return _detail_error("'party_levels' must be a non-empty list of character levels.", 422)
        try:
            party_levels = [int(lvl) for lvl in raw_levels]
        except (TypeError, ValueError):
            return _detail_error("'party_levels' must all be integers.", 422)

        kwargs = {}
        for key in ("max_monsters", "max_distinct_types"):
            if key in body:
                kwargs[key] = body[key]

        try:
            result = build_encounter(party_levels, difficulty=difficulty, **kwargs)
        except (ValueError, KeyError, TypeError) as exc:
            return _detail_error(str(exc), 422)

        return jsonify(result)

    # ---- Scenarios (bundled samples + per-user saved) ------------------

    @app.route("/api/dnd/scenarios")
    def api_dnd_list_scenarios():
        username = session["username"]
        names = set()
        if os.path.isdir(BUNDLED_SCENARIOS_DIR):
            names.update(f for f in os.listdir(BUNDLED_SCENARIOS_DIR) if f.endswith(".json"))
        names.update(f for f in os.listdir(_user_scenarios_dir(username)) if f.endswith(".json"))
        return jsonify({"scenarios": sorted(names)})

    @app.route("/api/dnd/scenarios/<filename>")
    def api_dnd_get_scenario(filename):
        username = session["username"]
        path = _find_scenario_path(username, filename)
        if not path:
            return _detail_error(f"Scenario '{filename}' not found.", 404)
        with open(path, encoding="utf-8") as f:
            return jsonify(json.load(f))

    @app.route("/api/dnd/scenarios", methods=["POST"])
    def api_dnd_save_scenario():
        username = session["username"]
        body = request.get_json(silent=True) or {}
        scenario = body.get("scenario")
        filename = body.get("filename")
        overwrite = bool(body.get("overwrite", False))
        if not isinstance(scenario, dict) or "players" not in scenario or "monsters" not in scenario:
            return _detail_error("Scenario must contain 'players' and 'monsters' keys.", 422)
        if not filename:
            return _detail_error("'filename' is required.", 422)
        if not filename.endswith(".json"):
            filename += ".json"
        safe_name = os.path.basename(filename)

        out_path = os.path.join(_user_scenarios_dir(username), safe_name)
        if os.path.exists(out_path) and not overwrite:
            return _detail_error(f"'{safe_name}' already exists. Set overwrite=true to replace it.", 409)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(scenario, f, indent=2)
        return jsonify({"saved": True, "filename": safe_name, "path": out_path})

    @app.route("/api/dnd/scenarios/<filename>", methods=["DELETE"])
    def api_dnd_delete_scenario(filename):
        username = session["username"]
        safe_name = os.path.basename(filename)
        path = os.path.join(_user_scenarios_dir(username), safe_name)
        if not os.path.exists(path):
            return _detail_error(f"'{safe_name}' not found.", 404)
        os.remove(path)
        return jsonify({"deleted": True, "filename": safe_name})

    # ---- Character validation & simulation ------------------------------

    @app.route("/api/dnd/validate-character", methods=["POST"])
    def api_dnd_validate_character():
        body = request.get_json(silent=True) or {}
        player = body.get("player")
        if not isinstance(player, dict):
            return _detail_error("Request body must contain a 'player' object.", 422)

        required = ["name", "classes", "stats", "items", "equipped"]
        missing = [k for k in required if k not in player]
        if missing:
            return _detail_error(f"Player definition missing required keys: {missing}", 422)

        test_scenario = {
            "map": {"width": 10, "height": 10, "walls": [], "difficult_terrain": []},
            "positions": {player["name"]: [2, 2], "monsters": [[7, 7]]},
            "players": [player],
            "monsters": [{"type": "GOBLIN", "count": 1}],
        }

        buf = io.StringIO()
        warnings = []
        try:
            with contextlib.redirect_stdout(buf):
                event = EventBus()
                factory = CreatureFactory()
                loader = ScenarioLoader(factory, event)
                players, _ = loader.load(test_scenario)
                pc = players[0]
        except Exception as exc:
            return _detail_error(f"Failed to load character: {exc}", 422)

        log = buf.getvalue()
        for line in log.splitlines():
            if "couldn't find" in line.lower() or "not found" in line.lower():
                warnings.append(line.strip())

        return jsonify({
            "valid": True,
            "message": f"HP {pc.max_hp}, AC {pc.ac}",
            "warnings": warnings,
            "hp": pc.max_hp,
            "ac": pc.ac,
            "features": [f.name for f in pc.features],
            "log": log,
        })

    @app.route("/api/dnd/simulate", methods=["POST"])
    def api_dnd_simulate():
        username = session["username"]
        body = request.get_json(silent=True) or {}

        scenario_name = body.get("scenario_name")
        scenario_data = body.get("scenario")
        if scenario_name:
            path = _find_scenario_path(username, scenario_name)
            if not path:
                return _detail_error(f"Scenario '{scenario_name}' not found.", 404)
            with open(path, encoding="utf-8") as f:
                scenario_data = json.load(f)
        elif not isinstance(scenario_data, dict):
            return _detail_error("Provide either 'scenario_name' or 'scenario' in the request body.", 422)

        try:
            episodes = int(body.get("episodes", 1))
        except (TypeError, ValueError):
            episodes = 1
        n = max(1, min(episodes, MAX_EPISODES))
        silent = bool(body.get("silent", True))
        strategy = body.get("strategy")

        wins = {"blue": 0, "red": 0, "none": 0}
        total_rounds = 0
        rounds_seen = []
        tpk_count = 0
        any_down_count = 0
        win_hp_pcts = []
        pc_death_counts = {}
        all_events = []
        last = {}
        try:
            for _ in range(n):
                last = _run_episode(scenario_data, silent=silent, strategy=strategy)
                winner = last["winner"] or "none"
                wins[winner] += 1
                total_rounds += last["rounds"]
                rounds_seen.append(last["rounds"])
                all_events.append(last.get("events", []))

                blue = [c for c in last["creatures"] if c["team"] == "blue"]
                blue_alive = [c for c in blue if c["alive"]]
                if blue and not blue_alive:
                    tpk_count += 1
                if len(blue_alive) < len(blue):
                    any_down_count += 1
                for c in blue:
                    pc_death_counts.setdefault(c["name"], 0)
                    if not c["alive"]:
                        pc_death_counts[c["name"]] += 1
                if winner == "blue":
                    total_max = sum(c["max_hp"] for c in blue)
                    if total_max > 0:
                        win_hp_pcts.append(sum(c["hp"] for c in blue) / total_max)
        except Exception as exc:
            return _detail_error(str(exc), 500)

        try:
            difficulty = score_encounter(
                _party_levels_from_scenario(scenario_data),
                scenario_data.get("monsters", []),
            )
        except (ValueError, KeyError):
            difficulty = None

        character_stats = _aggregate_character_stats(all_events, team="blue")

        return jsonify({
            "episodes_run": n,
            "wins": wins,
            "win_rate": wins["blue"] / n if n else 0.0,
            "avg_rounds": total_rounds / n if n else 0.0,
            "sample_outcome": last.get("outcome", ""),
            "creatures": last.get("creatures", []),
            "log": last.get("log", ""),
            "sample_events": last.get("events", []),
            "character_stats": character_stats,
            "aggregate": {
                "tpk_rate":               tpk_count / n if n else 0.0,
                "any_pc_down_rate":       any_down_count / n if n else 0.0,
                "avg_party_hp_pct_on_win": (sum(win_hp_pcts) / len(win_hp_pcts)
                                            if win_hp_pcts else None),
                "rounds_min":             min(rounds_seen) if rounds_seen else 0,
                "rounds_max":             max(rounds_seen) if rounds_seen else 0,
                "per_pc_death_rate":      {name: count / n for name, count in pc_death_counts.items()},
            },
            "difficulty": difficulty,
        })

    # ---- Adapted frontend pages -----------------------------------------

    _PAGE_FILES = {"builder", "simulator", "monsters", "scenarios", "classes"}

    @app.route("/dnd")
    def dnd_landing():
        return send_from_directory(PAGES_DIR, "index.html")

    @app.route("/dnd/<page>")
    def dnd_page(page):
        if page not in _PAGE_FILES:
            return _detail_error("Not found.", 404)
        return send_from_directory(PAGES_DIR, f"{page}.html")
