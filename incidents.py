"""Incident mode: live closures, cost clock, proposed response and repair priority.

Status flow per crossing: suspected (automatic or anyone) -> reported (anyone) -> confirmed (operator)
-> reopened (operator). Only confirmed closures drive the cost clock and the response plan.
Satellite data can flag risk; a person confirms failure.
"""
from datetime import datetime, timedelta, timezone
import hmac
import json
import os
from pathlib import Path
import threading

import network

STATUSES = ("suspected", "reported", "confirmed", "reopened")
OPERATOR_ONLY = ("confirmed", "reopened")
STORE = Path(os.environ.get("INCIDENT_STORE", Path(__file__).with_name(".cache") / "incidents.json"))
MAX_INCIDENTS = 300
_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc)


def _time(value, now):
    if value in (None, ""):
        return now
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Time must include a timezone, e.g. 2026-11-05T10:00:00+02:00")
    parsed = parsed.astimezone(timezone.utc)
    if parsed > now + timedelta(minutes=5) or parsed < now - timedelta(days=30):
        raise ValueError("Time must be within the last 30 days and not in the future.")
    return parsed


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _read():
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []


def _write(items):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STORE)


def operator_mode():
    return bool(os.environ.get("OPERATOR_KEY"))


def _authorise(status, key):
    expected = os.environ.get("OPERATOR_KEY")
    if status in OPERATOR_ONLY and expected and not hmac.compare_digest(str(key or ""), expected):
        raise PermissionError("Only an authority operator can confirm or reopen a closure.")


def _optional(value, name, low, high):
    return network._number(value, name, low, high)


def update(body, net=None, now=None):
    net, now = net or network.load(), now or _now()
    crossing, status = body.get("crossing"), body.get("status")
    if crossing not in net.crossings:
        raise ValueError("Unknown crossing.")
    if status not in STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(STATUSES)}.")
    _authorise(status, body.get("key"))
    at = _time(body.get("at"), now)
    note = str(body.get("note") or "")[:200]
    with _lock:
        items = _read()
        active = next((i for i in items if i["crossing"] == crossing and i["status"] != "reopened"), None)
        if active is None:
            if status == "reopened":
                raise ValueError("This crossing has no open incident.")
            if len(items) >= MAX_INCIDENTS:
                raise ValueError("Incident log is full; reset it first.")
            active = {"id": f"I{len(items) + 1:04d}", "crossing": crossing, "name": net.crossings[crossing]["name"],
                      "history": []}
            items.append(active)
        if status == "confirmed" and ("failed_at" not in active or body.get("at")):
            active["failed_at"] = _iso(at)
        if status == "reopened":
            if "failed_at" in active and at < datetime.fromisoformat(active["failed_at"]):
                raise ValueError("Reopening time is before the failure time.")
            active["reopened_at"] = _iso(at)
        for field, low, high in (("expected_hours", 1, 2160), ("vehicles_per_day", 0, 200000), ("occupancy", 1, 5)):
            if body.get(field) not in (None, ""):
                active[field] = _optional(body[field], field, low, high)
        active["status"] = status
        active["history"].append({"status": status, "at": _iso(at), "recorded_at": _iso(now), "note": note})
        _write(items)
        return active


def reset(key=None):
    _authorise("confirmed", key)
    with _lock:
        _write([])


def load_demo(key=None, net=None, now=None):
    """Replay of 5 Sep 2023, shifted to now: CEMS EMSR693 reported road Tsarevo-Sinemorets closed."""
    net, now = net or network.load(), now or _now()
    reset(key)
    ranked = [c for c in network.bridges(net)["crossings"] if c["road"] == "9901"]
    main = ranked[0]["id"]
    update({"crossing": main, "status": "reported", "at": _iso(now - timedelta(hours=6, minutes=30)), "key": key,
            "note": "Replay of 5 Sep 2023. EMSR693: 'The main road Tsarevo - Sinemoretz is closed.' Exact bridge not confirmed."},
           net, now)
    update({"crossing": main, "status": "confirmed", "at": _iso(now - timedelta(hours=6)), "expected_hours": 72,
            "key": key, "note": "Demo: confirmed by operator."}, net, now)
    import watcher  # local import: watcher depends on this module
    watcher.apply(watcher.replay_signals(net), net, now)


def _hours(start, end):
    return max(0.0, (end - start).total_seconds() / 3600)


def situation(net=None, now=None):
    net, now = net or network.load(), now or _now()
    with _lock:
        items = _read()
    active = [i for i in items if i["status"] != "reopened"]
    confirmed = [i for i in active if i["status"] == "confirmed"]
    reported = [i for i in active if i["status"] == "reported"]
    suspected = [i for i in active if i["status"] == "suspected"]
    result = {"now": _iso(now), "operator_auth": operator_mode(), "incidents": items,
              "counts": {s: sum(1 for i in items if i["status"] == s) for s in STATUSES},
              "impact": None, "if_reports_confirmed": None, "cost": [], "isolation_person_hours": 0,
              "repair_priority": [], "actions": []}
    ids = [i["crossing"] for i in confirmed]
    if ids:
        impact = network.scenario(ids, net=net)
        result["impact"] = impact
        detours = {c["id"]: c["detour_min"] for c in impact["closed"]}
        for i in confirmed:
            result["cost"].append(_clock(i, detours[i["crossing"]], now))
        result["isolation_person_hours"] = _isolation(confirmed, net, now)
        result["repair_priority"] = _repair(ids, impact, net)
    if reported:
        extra = network.scenario(ids + [i["crossing"] for i in reported], net=net)["modelled"]
        result["if_reports_confirmed"] = {k: extra[k] for k in ("population_losing_access", "population_disconnected",
                                                                "disconnected_settlements")}
    for i in [i for i in items if i["status"] == "reopened" and "failed_at" in i]:
        result["cost"].append(_clock(i, None, now) | {"final": True})
    result["actions"] = _actions(result, confirmed, reported, suspected)
    return result


def _clock(incident, detour, now):
    start = datetime.fromisoformat(incident["failed_at"])
    end = datetime.fromisoformat(incident["reopened_at"]) if incident.get("reopened_at") else now
    elapsed = _hours(start, end)
    row = {"incident": incident["id"], "crossing": incident["crossing"], "name": incident["name"],
           "failed_at": incident["failed_at"], "hours_closed": round(elapsed, 2), "detour_min": detour,
           "expected_hours": incident.get("expected_hours"), "person_hours": None, "rate_per_hour": None,
           "trips_prevented": None, "projected_person_hours": None}
    vehicles, occupancy = incident.get("vehicles_per_day"), incident.get("occupancy")
    if vehicles is None or occupancy is None:
        row["needs"] = "vehicles_per_day and occupancy (no traffic figures are invented)"
        return row
    if detour is None and not incident.get("reopened_at"):
        row["trips_prevented"] = round(vehicles / 24 * elapsed)
        return row
    if detour is not None:
        rate = vehicles / 24 * occupancy * detour / 60
        row["rate_per_hour"] = round(rate, 2)
        row["person_hours"] = round(rate * elapsed)
        if row["expected_hours"]:
            row["projected_person_hours"] = round(rate * max(row["expected_hours"], elapsed))
    return row


def _isolation(confirmed, net, now):
    """People x hours cut off from all mapped hospitals, following the order in which closures were confirmed."""
    events = sorted(confirmed, key=lambda i: i["failed_at"])
    total = 0.0
    for k, incident in enumerate(events):
        start = datetime.fromisoformat(incident["failed_at"])
        end = datetime.fromisoformat(events[k + 1]["failed_at"]) if k + 1 < len(events) else now
        cut = network.scenario([i["crossing"] for i in events[:k + 1]], net=net)["modelled"]["population_disconnected"]
        total += cut * _hours(start, end)
    return round(total)


def _repair(ids, impact, net):
    base = impact["modelled"]
    rows = []
    for crossing in ids:
        rest = [x for x in ids if x != crossing]
        after = network.scenario(rest, net=net)["modelled"] if rest else {"population_disconnected": 0,
                                                                          "population_losing_access": 0}
        rows.append({"crossing": crossing, "name": net.crossings[crossing]["name"],
                     "reconnects_people": base["population_disconnected"] - after["population_disconnected"],
                     "restores_access_people": base["population_losing_access"] - after["population_losing_access"]})
    return sorted(rows, key=lambda r: (-r["reconnects_people"], -r["restores_access_people"], r["crossing"]))


def _actions(result, confirmed, reported, suspected):
    actions = []
    impact = result["impact"]
    if impact:
        for s in impact["settlements"]:
            people = f"{s['population']:,} people" if s["population"] else "population unknown"
            if s["disconnected"]:
                actions.append({"priority": "critical", "kind": "isolated", "text":
                                f"{s['name']} ({people}) has no road access to emergency care. Establish air or sea "
                                f"medical evacuation capability, inform the mayor, check vulnerable residents and "
                                f"consider a temporary medical post."})
            elif (s["hospital_delta"] or 0) >= 10:
                route = f" via {', '.join(s['route_after'])}" if s.get("route_after") else ""
                actions.append({"priority": "high", "kind": "detour", "text":
                                f"Ambulances for {s['name']} ({people}): route{route} to {s['hospital_after_name']}, "
                                f"now {s['hospital_after']} min (+{s['hospital_delta']} min)."})
        top = result["repair_priority"][0] if result["repair_priority"] else None
        if top and len(result["repair_priority"]) > 1 and (top["reconnects_people"] or top["restores_access_people"]):
            actions.append({"priority": "high", "kind": "repair", "text":
                            f"Repair or provide a temporary crossing at {top['name']} first: it reconnects "
                            f"{top['reconnects_people']:,} people and restores 30-minute access for "
                            f"{top['restores_access_people']:,}."})
        for i in confirmed:
            actions.append({"priority": "medium", "kind": "closure", "text":
                            f"Keep {i['name']} closed and signposted; place police at the approach junctions."})
    for i in reported:
        actions.append({"priority": "high", "kind": "verify", "text":
                        f"Verify the report at {i['name']} and confirm or dismiss it."})
    groups = {}
    for i in suspected:
        groups.setdefault(i["history"][-1]["note"] or "automatic flag", []).append(i["name"])
    for note, all_names in groups.items():
        names = list(dict.fromkeys(all_names))
        listed = ", ".join(names[:5]) + (f" and others" if len(names) > 5 else "")
        actions.append({"priority": "medium", "kind": "inspect", "text":
                        f"Inspect {len(all_names)} crossing{'s' if len(all_names) > 1 else ''} ({listed}). Reason: {note}"})
    order = {"critical": 0, "high": 1, "medium": 2}
    return sorted(actions, key=lambda a: order[a["priority"]])
