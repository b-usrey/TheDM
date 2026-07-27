"""GM utility generators: random NPCs, biome-flavored encounters, and
travel-time estimates between two points on the current world.

Kept separate from server.py's route bodies (same split as mapgen/naming.py)
since these are self-contained content generators -- they take a WorldMap
(or its biome grid) and some coordinates, not Flask/session state.
"""

import json
import math

import numpy as np

from mapgen.biomes import Biome
from mapgen.naming import NameGenerator

# ---- Travel ----

MILES_TO_KM = 1.60934  # matches mapgen/agriculture.py's own conversion

# Rough terrain-difficulty multipliers on top of straight-line distance --
# not meant to model literal road networks, just "this kind of country slows
# a party down" for hex-crawl-style trip planning. 1.0 = open, easy going.
# Water biomes are handled separately (see travel_estimate) rather than given
# a multiplier here.
_TERRAIN_MULTIPLIER = {
    Biome.GRASSLAND: 1.0,
    Biome.SAVANNA: 1.1,
    Biome.TEMPERATE_FOREST: 1.4,
    Biome.TAIGA: 1.4,
    Biome.TUNDRA: 1.5,
    Biome.DESERT: 1.6,
    Biome.TEMPERATE_RAINFOREST: 1.8,
    Biome.TROPICAL_RAINFOREST: 2.0,
    Biome.ALPINE: 3.0,
    Biome.ICE_CAP: 3.0,
}
_WATER_BIOMES = {Biome.LAKE, Biome.OCEAN_SHALLOW, Biome.OCEAN_DEEP}

# Standard 5e travel paces, in miles/day, for a party on foot.
_PACE_MILES_PER_DAY = {"fast": 30.0, "normal": 24.0, "slow": 18.0}
_SEA_MILES_PER_DAY = 48.0  # ~2 knots, a laden sailing ship (5e DMG vehicle speeds)

_TRAVEL_SAMPLES = 24  # points sampled along the straight-line route to estimate terrain mix


def _km_per_cell(world):
    try:
        return float(json.loads(world.config_json).get("km_per_cell", 4.0))
    except Exception:
        return 4.0


def travel_estimate(world, x0, y0, x1, y1):
    """Straight-line distance between two world-coordinate points, plus an
    estimated travel time under standard travel paces (or sea speed, if the
    route is mostly open water)."""
    h, w = world.biome.shape
    km_per_cell = _km_per_cell(world)
    dx, dy = x1 - x0, y1 - y0
    cell_dist = math.hypot(dx, dy)
    km = cell_dist * km_per_cell
    miles = km / MILES_TO_KM

    water_hits = 0
    multipliers = []
    for i in range(_TRAVEL_SAMPLES + 1):
        t = i / _TRAVEL_SAMPLES
        px = min(max(int(round(x0 + dx * t)), 0), w - 1)
        py = min(max(int(round(y0 + dy * t)), 0), h - 1)
        biome = Biome(int(world.biome[py, px]))
        if biome in _WATER_BIOMES:
            water_hits += 1
        else:
            multipliers.append(_TERRAIN_MULTIPLIER.get(biome, 1.0))

    water_fraction = water_hits / (_TRAVEL_SAMPLES + 1)
    land_multiplier = sum(multipliers) / len(multipliers) if multipliers else 1.0

    result = {
        "distance_km": round(km, 1),
        "distance_miles": round(miles, 1),
        "water_fraction": round(water_fraction, 2),
    }

    if water_fraction >= 0.5:
        result["mode"] = "sea"
        result["days"] = {"sea": round(miles / _SEA_MILES_PER_DAY, 1)}
        result["note"] = ("Route is mostly open water -- this estimate assumes sea travel "
                           "the whole way; a party without a ship will need one.")
    else:
        adjusted_miles = miles * land_multiplier
        result["mode"] = "land"
        result["terrain_multiplier"] = round(land_multiplier, 2)
        result["days"] = {pace: round(adjusted_miles / rate, 1)
                          for pace, rate in _PACE_MILES_PER_DAY.items()}
        if water_fraction > 0:
            result["note"] = (f"About {round(water_fraction * 100)}% of this route crosses open "
                               "water -- a ferry, ship, or bridge may be needed along the way.")
        else:
            result["note"] = None

    return result


# ---- Random NPCs ----

_RACES = ["Human", "Human", "Human", "Elf", "Dwarf", "Halfling", "Half-Elf",
          "Half-Orc", "Gnome", "Tiefling", "Dragonborn"]

_OCCUPATIONS = [
    "Innkeeper", "Blacksmith", "Farmer", "Town guard", "Merchant", "Priest",
    "Beggar", "Bard", "Hunter", "Fisherman", "Scholar", "Alchemist",
    "Stablehand", "Sailor", "Tailor", "Miner", "Herbalist", "Moneylender",
    "Retired soldier", "Fortune teller", "Gravedigger", "Smuggler",
    "Traveling peddler", "Noble's steward", "Cutpurse", "Cartographer",
]

_TRAITS = [
    "Speaks in an odd rhyme", "Missing an eye, doesn't say how", "Constantly counting coins",
    "Overly formal, even for casual talk", "Flinches at loud noises", "Collects buttons",
    "Never sits with their back to the door", "Laughs at inappropriate times",
    "Smells strongly of a specific herb", "Refuses to say a certain word",
    "Talks to an animal companion like it understands", "Extremely superstitious",
    "Whittles constantly, even mid-conversation", "Trusts no one who won't share a drink first",
    "Repeats the last few words people say", "Wears a faded military insignia",
    "Keeps a lucky charm visible at all times", "Suspiciously well-informed about local gossip",
    "Owes someone money and is nervous about it", "Has a nickname nobody remembers the origin of",
]

_MOTIVATIONS = [
    "Wants to leave town but can't afford to", "Secretly in debt to a dangerous person",
    "Looking for a missing family member", "Hoping to buy back a family heirloom",
    "Trying to keep a secret from a spouse", "Wants revenge on a former business partner",
    "Believes they're cursed", "Saving up for a specific, expensive dream",
    "Grooming an apprentice to take over the trade", "Quietly informs for the local authorities",
    "Hiding a criminal past under a new name", "Genuinely just wants to help travelers",
]


def generate_npc():
    rng = np.random.default_rng()
    # A fresh NameGenerator per roll -- its own-name uniqueness tracking
    # (see mapgen/naming.py) only matters within a single generation run, not
    # across independent quick-rolls.
    namer = NameGenerator(int(rng.integers(0, 2**31)))
    return {
        "name": namer.settlement_name(culture_idx=int(rng.integers(0, 3)), max_len=16),
        "race": str(rng.choice(_RACES)),
        "occupation": str(rng.choice(_OCCUPATIONS)),
        "trait": str(rng.choice(_TRAITS)),
        "motivation": str(rng.choice(_MOTIVATIONS)),
    }


# ---- Random encounters ----

# DnDPython's own monster registry (see webapp/dnd_api.py) only covers four
# generic humanoids -- too sparse to drive a full biome-tagged bestiary, so
# these tables are flavor-text first. Entries that happen to match one of
# those four types carry a "monster_type" key so the frontend can offer a
# shortcut into the combat simulator; entries without one are narrative-only.
_BIOME_GROUPS = {
    Biome.OCEAN_DEEP: "sea",
    Biome.OCEAN_SHALLOW: "sea",
    Biome.LAKE: "sea",
    Biome.ICE_CAP: "frozen",
    Biome.TUNDRA: "frozen",
    Biome.TAIGA: "forest",
    Biome.TEMPERATE_FOREST: "forest",
    Biome.TEMPERATE_RAINFOREST: "jungle",
    Biome.TROPICAL_RAINFOREST: "jungle",
    Biome.GRASSLAND: "plains",
    Biome.SAVANNA: "plains",
    Biome.DESERT: "desert",
    Biome.ALPINE: "mountain",
}

_ENCOUNTER_TABLES = {
    "sea": [
        {"description": "A pod of dolphins escorts the party's boat for a while.", "difficulty": "none"},
        {"description": "Wreckage and floating cargo crates drift past -- something sank recently.", "difficulty": "none"},
        {"description": "A merchant vessel signals for aid; its crew is down with fever.", "difficulty": "easy"},
        {"description": "Pirates in a fast raider ship demand a toll.", "difficulty": "hard"},
        {"description": "Something large and unseen bumps the hull from below.", "difficulty": "medium"},
    ],
    "frozen": [
        {"description": "Tracks of a large predator cross the snow, heading the same way as the party.", "difficulty": "easy"},
        {"description": "A stranded traveler, half-frozen, begs for shelter.", "difficulty": "none"},
        {"description": "A pack of wolves, thin and desperate, has been trailing the party.", "difficulty": "medium"},
        {"description": "A sudden whiteout forces the party to make camp early.", "difficulty": "none"},
        {"description": "Frost-touched raiders ambush from behind a snowbank.", "difficulty": "hard"},
    ],
    "forest": [
        {"description": "A goblin scouting party is arguing loudly enough to be heard first.", "difficulty": "easy", "monster_type": "GOBLIN"},
        {"description": "An old hunting cabin, recently abandoned in a hurry.", "difficulty": "none"},
        {"description": "A bugbear ambushes from the underbrush.", "difficulty": "medium", "monster_type": "BUGBEAR"},
        {"description": "A druid watches from a distance, testing the party's intentions.", "difficulty": "none"},
        {"description": "An orc raiding party is dividing up loot from a recent raid.", "difficulty": "hard", "monster_type": "ORC"},
    ],
    "jungle": [
        {"description": "Biting insects and thick heat slow everyone down.", "difficulty": "none"},
        {"description": "Ruins draped in vines suggest something old is nearby.", "difficulty": "none"},
        {"description": "A hobgoblin patrol marches a captured prisoner between them.", "difficulty": "medium", "monster_type": "HOBGOBLIN"},
        {"description": "Poisonous plants block the easiest path forward.", "difficulty": "easy"},
        {"description": "Distant drums suggest the party has been noticed.", "difficulty": "hard"},
    ],
    "plains": [
        {"description": "A caravan of merchants offers to travel together for safety.", "difficulty": "none"},
        {"description": "A lone rider approaches fast, waving for help.", "difficulty": "none"},
        {"description": "Bandits block the road ahead, demanding a toll.", "difficulty": "medium"},
        {"description": "A herd of wild horses thunders past, spooked by something.", "difficulty": "none"},
        {"description": "An orc war-band is visible on the horizon, moving fast.", "difficulty": "hard", "monster_type": "ORC"},
    ],
    "desert": [
        {"description": "A mirage on the horizon turns out to be a real oasis -- or does it?", "difficulty": "none"},
        {"description": "A sandstorm rolls in with little warning.", "difficulty": "easy"},
        {"description": "Nomad outriders approach to size up the party before deciding on words or weapons.", "difficulty": "medium"},
        {"description": "Buried ruins have been partly uncovered by shifting dunes.", "difficulty": "none"},
        {"description": "Goblin raiders spring from behind the dunes.", "difficulty": "medium", "monster_type": "GOBLIN"},
    ],
    "mountain": [
        {"description": "A rockslide forces a detour.", "difficulty": "none"},
        {"description": "A hermit's cabin clings to the slope, smoke rising from its chimney.", "difficulty": "none"},
        {"description": "Bugbears have made a den in a nearby cave.", "difficulty": "medium", "monster_type": "BUGBEAR"},
        {"description": "The path narrows along a sheer drop.", "difficulty": "easy"},
        {"description": "Hobgoblin sentries spot the party first from a high ledge.", "difficulty": "hard", "monster_type": "HOBGOBLIN"},
    ],
}


def generate_encounter(world, x, y):
    rng = np.random.default_rng()
    h, w = world.biome.shape
    px = min(max(int(round(x)), 0), w - 1)
    py = min(max(int(round(y)), 0), h - 1)
    biome = Biome(int(world.biome[py, px]))
    group = _BIOME_GROUPS.get(biome, "plains")
    table = _ENCOUNTER_TABLES[group]
    entry = dict(table[int(rng.integers(0, len(table)))])
    entry["biome"] = biome.name.replace("_", " ").title()
    entry["x"], entry["y"] = px, py
    return entry
