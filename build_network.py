"""Build the bundled road-network extract used by the bridge-closure scenario.

Fetch once with Overpass (see README, "Infrastructure disruption"), then run:
  py -3 resilience\\build_network.py raw_overpass.json resilience\\data\\burgas_south.json
Data (c) OpenStreetMap contributors, ODbL.
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path

SPEED_KMH = {"motorway": 100, "trunk": 80, "primary": 70, "secondary": 60, "tertiary": 50,
             "unclassified": 40, "motorway_link": 50, "trunk_link": 50, "primary_link": 40,
             "secondary_link": 40, "tertiary_link": 30}
BBOX = (42.0, 27.35, 42.52, 28.0)


def km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def point(p):
    return round(p["lat"], 6), round(p["lon"], 6)


def oneway(tags):
    value = tags.get("oneway")
    if value in ("yes", "1", "true") or tags.get("junction") == "roundabout" or tags.get("highway") == "motorway":
        return 1
    return -1 if value == "-1" else 0


def build(raw):
    ways = [e for e in raw["elements"] if e["type"] == "way" and e.get("tags", {}).get("highway") in SPEED_KMH
            and len(e.get("geometry") or []) >= 2]
    uses = {}
    for way in ways:
        pts = [point(p) for p in way["geometry"]]
        for i, p in enumerate(pts):
            uses[p] = uses.get(p, 0) + (2 if i in (0, len(pts) - 1) else 1)
    index, nodes, edges = {}, [], []

    def node(p):
        if p not in index:
            index[p] = len(nodes)
            nodes.append(list(p))
        return index[p]

    for way in ways:
        tags = way["tags"]
        pts = [point(p) for p in way["geometry"]]
        speed, direction = SPEED_KMH[tags["highway"]], oneway(tags)
        if direction == -1:
            pts.reverse()
        start, length, shape = 0, 0.0, [pts[0]]
        for i in range(1, len(pts)):
            length += km(pts[i - 1], pts[i])
            shape.append(pts[i])
            if uses[pts[i]] > 1 or i == len(pts) - 1:
                edges.append({"a": node(pts[start]), "b": node(pts[i]), "km": round(length, 4),
                              "min": round(length / speed * 60, 3), "oneway": bool(direction),
                              "way": way["id"], "road": tags.get("ref") or tags.get("name") or tags["highway"],
                              "bridge": tags.get("bridge") not in (None, "no"),
                              "shape": [[round(y, 5), round(x, 5)] for y, x in shape]})
                start, length, shape = i, 0.0, [pts[i]]
    main = largest_component(len(nodes), edges)

    def snap(lat, lon):
        best = min(main, key=lambda n: km((lat, lon), nodes[n]))
        return best, round(km((lat, lon), nodes[best]), 3)

    places, hospitals = [], []
    for e in raw["elements"]:
        tags = e.get("tags", {})
        lat = e.get("lat", (e.get("center") or {}).get("lat"))
        lon = e.get("lon", (e.get("center") or {}).get("lon"))
        if lat is None and e.get("bounds"):
            b = e["bounds"]
            lat, lon = (b["minlat"] + b["maxlat"]) / 2, (b["minlon"] + b["maxlon"]) / 2
        if lat is None:
            continue
        if tags.get("place") in ("city", "town", "village"):
            n, d = snap(lat, lon)
            population = tags.get("population", "")
            places.append({"name": tags.get("name:en") or tags.get("name"), "kind": tags["place"],
                           "population": int(population) if population.isdigit() else None,
                           "lat": round(lat, 5), "lon": round(lon, 5), "node": n, "snap_km": d})
        elif tags.get("amenity") == "hospital":
            n, d = snap(lat, lon)
            hospitals.append({"name": tags.get("name:en") or tags.get("name") or "Unnamed (mapped as hospital)",
                              "lat": round(lat, 5), "lon": round(lon, 5), "node": n, "snap_km": d})
    return {"schema": "resilience-network-v1", "region": "Burgas south coast, Bulgaria (Sozopol, Primorsko, Tsarevo)",
            "bbox": BBOX, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "OpenStreetMap contributors (ODbL), via Overpass API",
            "speeds_kmh": SPEED_KMH, "nodes": nodes, "edges": edges, "places": places, "hospitals": hospitals,
            "crossings": crossings(nodes, edges, places)}


def largest_component(count, edges):
    parent = list(range(count))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for e in edges:
        parent[find(e["a"])] = find(e["b"])
    groups = {}
    for n in range(count):
        groups.setdefault(find(n), []).append(n)
    return max(groups.values(), key=len)


def crossings(nodes, edges, places):
    """Group bridge edges (including both carriageways) into crossings within ~150 m."""
    bridge = [i for i, e in enumerate(edges) if e["bridge"]]
    groups = []
    for i in bridge:
        mid = nodes[edges[i]["a"]]
        for group in groups:
            if edges[group[0]]["road"] == edges[i]["road"] and km(mid, nodes[edges[group[0]]["a"]]) < 0.15:
                group.append(i)
                break
        else:
            groups.append([i])
    result = []
    for n, group in enumerate(groups, 1):
        lat = sum(nodes[edges[i]["a"]][0] for i in group) / len(group)
        lon = sum(nodes[edges[i]["a"]][1] for i in group) / len(group)
        near = min(places, key=lambda p: km((lat, lon), (p["lat"], p["lon"])))
        road = edges[group[0]]["road"]
        label = f"Road {road}" if road.replace("-", "").isdigit() else road.replace("_", " ").title()
        result.append({"id": f"X{n:03d}", "name": f"{label} bridge near {near['name']}", "road": road,
                       "lat": round(lat, 5), "lon": round(lon, 5), "edges": group,
                       "length_m": round(sum(edges[i]["km"] for i in group) / len(group) * 1000)})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    network = build(json.loads(args.raw.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(network, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(network['nodes'])} nodes, {len(network['edges'])} edges, {len(network['crossings'])} crossings, "
          f"{len(network['places'])} settlements, {len(network['hospitals'])} hospital records -> {args.output}")


if __name__ == "__main__":
    main()

