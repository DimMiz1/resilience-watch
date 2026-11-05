"""Bridge-closure scenario: indirect (connectivity) cost of losing one crossing.

Uses a bundled OpenStreetMap road extract (see build_network.py). Every number is tagged:
observed = mapped OSM data, modelled = computed travel times, assumed = user inputs or fixed parameters.
"""
import heapq
import json
import math
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).with_name("data") / "burgas_south.json"
REGIONAL_HOSPITAL = "УМБАЛ Бургас"


class Network:
    def __init__(self, data):
        self.data = data
        self.nodes = data["nodes"]
        self.edges = data["edges"]
        self.crossings = {c["id"]: c for c in data["crossings"]}
        self.forward = [[] for _ in self.nodes]
        self.backward = [[] for _ in self.nodes]
        for i, e in enumerate(self.edges):
            self.forward[e["a"]].append((e["b"], i))
            self.backward[e["b"]].append((e["a"], i))
            if not e["oneway"]:
                self.forward[e["b"]].append((e["a"], i))
                self.backward[e["a"]].append((e["b"], i))
        self.regional = [h for h in data["hospitals"] if h["name"] == REGIONAL_HOSPITAL]

    def times(self, sources, closed=frozenset(), reverse=True):
        """Minutes from every node to the nearest source (reverse graph) or from sources outward."""
        adjacency = self.backward if reverse else self.forward
        best = {s: 0.0 for s in sources}
        queue = [(0.0, s) for s in sources]
        heapq.heapify(queue)
        while queue:
            t, n = heapq.heappop(queue)
            if t > best.get(n, math.inf):
                continue
            for m, i in adjacency[n]:
                if i in closed:
                    continue
                nt = t + self.edges[i]["min"]
                if nt < best.get(m, math.inf):
                    best[m] = nt
                    heapq.heappush(queue, (nt, m))
        return best

    def closed_edges(self, ids):
        unknown = [x for x in ids if x not in self.crossings]
        if unknown or not ids:
            raise ValueError(f"Unknown crossing id: {', '.join(unknown) or '(none)'}")
        return frozenset(i for x in ids for i in self.crossings[x]["edges"])

    def detour(self, crossing_id, closed):
        """Extra minutes for a vehicle that would have used this crossing (worst direction)."""
        worst = 0.0
        for i in self.crossings[crossing_id]["edges"]:
            e = self.edges[i]
            for a, b in [(e["a"], e["b"])] + ([] if e["oneway"] else [(e["b"], e["a"])]):
                t = self.times([a], closed, reverse=False).get(b)
                if t is None:
                    return None
                worst = max(worst, t - e["min"])
        return round(worst, 1)


@lru_cache(maxsize=1)
def load(path=DATA):
    return Network(json.loads(Path(path).read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def _ranked(net):
    rows = []
    for c in net.data["crossings"]:
        m = scenario([c["id"]], net=net)["modelled"]
        rows.append({k: c[k] for k in ("id", "name", "road", "lat", "lon", "length_m")} |
                    {"population_losing_access": m["population_losing_access"],
                     "population_disconnected": m["population_disconnected"],
                     "settlements_affected": m["settlements_affected"]})
    return sorted(rows, key=lambda r: (-r["population_disconnected"], -r["population_losing_access"],
                                       -r["settlements_affected"], r["id"]))


def bridges(net=None):
    net = net or load()
    d = net.data
    return {"region": d["region"], "source": d["source"], "built_at": d["built_at"],
            "crossings": _ranked(net),
            "ranking": "Ordered by modelled consequence of a 24-hour closure (people cut off, then people losing 30-minute hospital access).",
            "note": "Bridge positions are mapped in OpenStreetMap. Their structural condition is not known."}


def _number(value, name, low, high):
    if value in (None, ""):
        return None
    number = float(value)
    if not low <= number <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return number


def scenario(ids, hours=24, threshold=30, journeys=None, occupancy=None, value_of_time=None, net=None):
    net = net or load()
    hours = _number(hours, "hours", 1, 720)
    threshold = _number(threshold, "threshold", 5, 180)
    journeys = _number(journeys, "journeys", 0, 200000)
    occupancy = _number(occupancy, "occupancy", 1, 5)
    value_of_time = _number(value_of_time, "value_of_time", 0, 200)
    closed = net.closed_edges(ids)
    hospitals = [h["node"] for h in net.data["hospitals"]]
    regional = [h["node"] for h in net.regional]
    base_h, scen_h = net.times(hospitals), net.times(hospitals, closed)
    base_r, scen_r = net.times(regional), net.times(regional, closed)

    def minutes(table, node):
        value = table.get(node)
        return None if value is None else round(value, 1)

    settlements, lost, cut, people_cut = [], 0, [], 0
    for p in net.data["places"]:
        row = {"name": p["name"], "lat": p["lat"], "lon": p["lon"], "population": p["population"],
               "hospital_before": minutes(base_h, p["node"]), "hospital_after": minutes(scen_h, p["node"]),
               "regional_before": minutes(base_r, p["node"]), "regional_after": minutes(scen_r, p["node"])}
        a, b = row["hospital_before"], row["hospital_after"]
        row["disconnected"] = b is None and a is not None
        row["hospital_delta"] = None if b is None or a is None else round(b - a, 1)
        ra, rb = row["regional_before"], row["regional_after"]
        row["regional_delta"] = None if rb is None or ra is None else round(rb - ra, 1)
        row["lost_access"] = a is not None and a <= threshold and (b is None or b > threshold)
        if row["lost_access"] and p["population"]:
            lost += p["population"]
        if row["disconnected"]:
            cut.append(p["name"])
            people_cut += p["population"] or 0
        if row["disconnected"] or (row["hospital_delta"] or 0) >= 1 or (row["regional_delta"] or 0) >= 1:
            settlements.append(row)
    settlements.sort(key=lambda r: (not r["disconnected"], -(r["hospital_delta"] or 0), -(r["regional_delta"] or 0)))

    detours = {x: net.detour(x, closed) for x in ids}
    person_hours = cost = None
    if journeys is not None and occupancy is not None and all(v is not None for v in detours.values()):
        person_hours = round(journeys * hours / 24 * occupancy * max(detours.values()) / 60)
        if value_of_time is not None:
            cost = round(person_hours * value_of_time)
    return {
        "closed": [{k: net.crossings[x][k] for k in ("id", "name", "road", "lat", "lon")} | {"detour_min": detours[x]}
                   for x in ids],
        "wording": f"Assume {'this crossing is' if len(ids) == 1 else 'these crossings are'} closed for {hours:g} hours.",
        "observed": {"settlements_in_region": len(net.data["places"]),
                     "hospital_records": len(net.data["hospitals"]),
                     "population_source": "OpenStreetMap population tags (census-derived, may be outdated)"},
        "modelled": {"settlements_affected": len(settlements),
                     "population_losing_access": lost,
                     "threshold_min": threshold,
                     "disconnected_settlements": cut,
                     "population_disconnected": people_cut,
                     "person_hours_lost": person_hours,
                     "value_of_time_eur": cost},
        "assumed": {"closure_hours": hours, "daily_journeys": journeys, "occupancy": occupancy,
                    "value_of_time_eur_per_hour": value_of_time,
                    "speeds_kmh": net.data["speeds_kmh"],
                    "notes": ["Only roads classed unclassified and above are modelled; local streets or tracks may still offer a way through.",
                              "Free-flow speeds by road class; no congestion or ferry alternatives.",
                              "Hospital records are OSM amenity=hospital; emergency capability is not verified.",
                              "Person-hours are only computed from journeys and occupancy that you supply.",
                              "Disconnected journeys are reported separately, never as infinite hours."]},
        "settlements": settlements,
    }
