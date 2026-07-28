"""Persistent noble-house genealogies: people, marriages, births, and deaths
recorded against the currently loaded world.

Stored as a JSON sidecar next to the world's own .npz file (world.npz ->
world.dynasty.json) rather than inside WorldMap's own save format -- this is
GM-authored campaign history, not generated terrain/economy data, and
WorldMap's numpy-array save/load (see mapgen/worldmap.py) isn't a good fit
for an open-ended, growing relational log. Referencing a house's nation_id
or a person's notes is enough to place it on the loaded world without
WorldMap itself needing to know dynasties exist.

Dates are a plain in-game year (int) -- nothing else in the generator has a
calendar, so a full month/day system would be new scope, not reuse.
"""

import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

import numpy as np

from mapgen.naming import NameGenerator

DEFAULT_START_YEAR = 1000


def _uid():
    return uuid.uuid4().hex


@dataclass
class Person:
    uid: str = field(default_factory=_uid)
    name: str = ""
    sex: str = ""                        # free text, purely descriptive -- no tree logic depends on it
    birth_year: Optional[int] = None
    death_year: Optional[int] = None
    house_id: Optional[str] = None       # house of birth/membership
    father_id: Optional[str] = None
    mother_id: Optional[str] = None
    spouse_id: Optional[str] = None      # current spouse, maintained by record_marriage/record_death
    title: str = ""                      # free text, e.g. "Duke of Ashmere"
    notes: str = ""

    def is_alive(self, current_year):
        return self.death_year is None or self.death_year > current_year


@dataclass
class House:
    uid: str = field(default_factory=_uid)
    name: str = ""
    nation_id: Optional[int] = None      # which nation (in the loaded WorldMap) this house rules, if any
    seat_settlement_uid: Optional[str] = None
    founder_id: Optional[str] = None
    notes: str = ""


@dataclass
class Event:
    uid: str = field(default_factory=_uid)
    year: int = 0
    kind: str = ""                       # "birth" | "marriage" | "death" | "note"
    person_ids: list = field(default_factory=list)
    description: str = ""
    house_id: Optional[str] = None       # set on "note" events so a note with no
                                          # linked people (e.g. "sacked by raiders")
                                          # still shows up in that house's log


@dataclass
class Dynasty:
    current_year: int = DEFAULT_START_YEAR
    people: dict = field(default_factory=dict)   # uid -> Person
    houses: dict = field(default_factory=dict)   # uid -> House
    events: list = field(default_factory=list)   # list[Event], oldest first

    # ---- persistence ----

    def to_dict(self):
        return {
            "current_year": self.current_year,
            "people": {uid: asdict(p) for uid, p in self.people.items()},
            "houses": {uid: asdict(h) for uid, h in self.houses.items()},
            "events": [asdict(e) for e in self.events],
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            current_year=data.get("current_year", DEFAULT_START_YEAR),
            people={uid: Person(**p) for uid, p in data.get("people", {}).items()},
            houses={uid: House(**h) for uid, h in data.get("houses", {}).items()},
            events=[Event(**e) for e in data.get("events", [])],
        )

    @classmethod
    def load(cls, path):
        if not os.path.exists(path):
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                return cls.from_dict(json.load(f))
        except Exception:
            return cls()

    def save(self, path):
        # A rolling one-deep backup, same insurance AppState.save() already
        # gives the world.npz itself.
        if os.path.exists(path):
            try:
                shutil.copy2(path, path + ".bak")
            except OSError:
                pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
        os.replace(tmp, path)

    # ---- houses ----

    def create_house(self, name, nation_id=None, seat_settlement_uid=None, notes=""):
        house = House(name=name, nation_id=nation_id, seat_settlement_uid=seat_settlement_uid, notes=notes)
        self.houses[house.uid] = house
        return house

    def delete_house(self, house_id):
        if house_id not in self.houses:
            return False
        del self.houses[house_id]
        for person in self.people.values():
            if person.house_id == house_id:
                person.house_id = None
        return True

    # ---- people ----

    def add_person(self, name, sex="", birth_year=None, house_id=None,
                    father_id=None, mother_id=None, notes=""):
        person = Person(name=name, sex=sex, birth_year=birth_year, house_id=house_id,
                         father_id=father_id, mother_id=mother_id, notes=notes)
        self.people[person.uid] = person
        return person

    def delete_person(self, person_id):
        if person_id not in self.people:
            return False
        del self.people[person_id]
        for person in self.people.values():
            if person.spouse_id == person_id:
                person.spouse_id = None
        for house in self.houses.values():
            if house.founder_id == person_id:
                house.founder_id = None
        return True

    # ---- events: the source of truth for what happened and when ----

    def record_birth(self, name, year, sex="", father_id=None, mother_id=None,
                      house_id=None, description=""):
        if house_id is None:
            father = self.people.get(father_id)
            mother = self.people.get(mother_id)
            house_id = (father.house_id if father else None) or (mother.house_id if mother else None)
        child = self.add_person(name=name, sex=sex, birth_year=year, house_id=house_id,
                                 father_id=father_id, mother_id=mother_id)
        parents = [p.name for p in (self.people.get(father_id), self.people.get(mother_id)) if p]
        desc = description or (f"{child.name} born" + (f" to {' and '.join(parents)}" if parents else ""))
        self.events.append(Event(year=year, kind="birth",
                                  person_ids=[uid for uid in (child.uid, father_id, mother_id) if uid],
                                  description=desc))
        return child

    def record_marriage(self, person_a_id, person_b_id, year, description=""):
        a, b = self.people.get(person_a_id), self.people.get(person_b_id)
        if not a or not b:
            raise KeyError("both people must already exist")
        a.spouse_id, b.spouse_id = b.uid, a.uid
        desc = description or f"{a.name} married {b.name}"
        self.events.append(Event(year=year, kind="marriage", person_ids=[a.uid, b.uid], description=desc))

    def record_death(self, person_id, year, description=""):
        person = self.people.get(person_id)
        if not person:
            raise KeyError("no such person")
        person.death_year = year
        desc = description or f"{person.name} died"
        self.events.append(Event(year=year, kind="death", person_ids=[person.uid], description=desc))

    def record_note(self, year, description, house_id=None, person_ids=None):
        """A free-form log entry for anything that isn't a birth/marriage/
        death -- a treaty, a war, a coronation -- tied to a year and, unlike
        the other event kinds, not required to reference any Person at all.
        house_id is what makes an event with no person_ids still show up in
        that house's own log (see server.py's renderEventLog equivalent)."""
        self.events.append(Event(year=year, kind="note", person_ids=list(person_ids or []),
                                  description=description, house_id=house_id))


def _random_names(rng):
    namer = NameGenerator(int(rng.integers(0, 2**31)))
    given = namer.person_name(culture_idx=int(rng.integers(0, 3)))
    surname = namer.nation_name(culture_idx=int(rng.integers(0, 3)), max_len=14)
    return given, surname


def _surname_of(name):
    """Everything after the first space -- used to keep a generated
    relative's surname matching the person they're related to, the same
    way generate_founder matches a founder's surname to their house."""
    return name.split(" ", 1)[1] if " " in name else name


def generate_founder(dynasty, house, year=None):
    """Add a founder Person to an already-created (founderless) House."""
    rng = np.random.default_rng()
    given, surname = _random_names(rng)
    # Reuse the house's own family name (if phrased "House X") rather than a
    # fresh random surname, so a generated founder's name matches the house
    # they're founding.
    house_surname = house.name.split(" ", 1)[1] if house.name.startswith("House ") else surname
    founding_year = year if year is not None else dynasty.current_year - int(rng.integers(20, 45))
    founder = dynasty.add_person(name=f"{given} {house_surname}", birth_year=founding_year,
                                  house_id=house.uid, notes="Founder of the house.")
    house.founder_id = founder.uid
    return founder


def generate_founding_house(dynasty, nation_id, nation_name, year=None):
    """Quick-start a nation's ruling house: creates both the House and its
    founder in one call, so a GM has something to build on without filling
    out two forms just to get started."""
    rng = np.random.default_rng()
    _, surname = _random_names(rng)
    house = dynasty.create_house(name=f"House {surname}", nation_id=nation_id,
                                  notes=f"Ruling house of {nation_name}." if nation_name else "")
    generate_founder(dynasty, house, year=year)
    return house


def generate_spouse(dynasty, person, year=None):
    """Add a spouse for an already-existing (unmarried) Person and record
    the marriage in one step -- the tree-click equivalent of generate_founder,
    for filling out a house without a form per relative."""
    if person.spouse_id:
        raise ValueError("this person already has a spouse")
    rng = np.random.default_rng()
    given, random_surname = _random_names(rng)
    surname = _surname_of(person.name) or random_surname
    base_year = person.birth_year if person.birth_year is not None else dynasty.current_year - 30
    spouse_birth = base_year + int(rng.integers(-6, 7))
    spouse = dynasty.add_person(name=f"{given} {surname}", birth_year=spouse_birth, house_id=person.house_id)
    marriage_year = year if year is not None else max(base_year, spouse_birth) + int(rng.integers(18, 26))
    dynasty.record_marriage(person.uid, spouse.uid, marriage_year)
    return spouse


def generate_child(dynasty, person, year=None):
    """Add a child for an already-existing Person (and their spouse, if any)
    and record the birth in one step. father_id/mother_id here are just the
    two parent slots the rest of the app already treats generically (see
    record_birth) -- not an assumption about either parent's sex."""
    rng = np.random.default_rng()
    spouse = dynasty.people.get(person.spouse_id) if person.spouse_id else None
    given, random_surname = _random_names(rng)
    surname = _surname_of(person.name) or random_surname
    parent_birth = max(
        person.birth_year if person.birth_year is not None else dynasty.current_year - 30,
        spouse.birth_year if spouse and spouse.birth_year is not None else 0,
    )
    child_year = year if year is not None else parent_birth + int(rng.integers(20, 35))
    return dynasty.record_birth(name=f"{given} {surname}", year=child_year,
                                 father_id=person.uid, mother_id=spouse.uid if spouse else None,
                                 house_id=person.house_id)
