"""Direct CDSE Sentinel Hub statistics. Credentials stay in the backend."""
from datetime import timedelta
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from feeds import FeedError, MAX_BYTES, record_time, utc_now

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
STATS_URL = "https://sh.dataspace.copernicus.eu/statistics/v1"
_TOKEN = None
_TOKEN_LOCK = threading.Lock()

EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{bands: ["B03", "B08", "B12", "SCL", "dataMask"]}],
    output: [{id: "indices", bands: 3}, {id: "dataMask", bands: 1}]
  };
}
function evaluatePixel(s) {
  const clear = s.dataMask && [4, 5, 6].includes(s.SCL);
  const waterDenom = s.B03 + s.B08;
  const burnDenom = s.B08 + s.B12;
  const valid = clear && waterDenom > 0 && burnDenom > 0;
  const ndwi = valid ? (s.B03 - s.B08) / waterDenom : 0;
  const nbr = valid ? (s.B08 - s.B12) / burnDenom : 0;
  return {indices: [ndwi, nbr, ndwi > 0 ? 1 : 0], dataMask: [valid ? 1 : 0]};
}
"""


def post_json(url, body, headers=None, encoded=False):
    data = body if encoded else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers=headers or {
        "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise FeedError("Copernicus response exceeded the size limit.")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    except urllib.error.HTTPError as error:
        raise FeedError(f"Copernicus returned HTTP {error.code}; check CDSE credentials, access and quota.") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise FeedError("Copernicus request failed or returned invalid JSON.") from error


def access_token(client_id, secret):
    global _TOKEN
    with _TOKEN_LOCK:
        if _TOKEN and _TOKEN[0] == (client_id, secret) and time.monotonic() < _TOKEN[2]:
            return _TOKEN[1]
        body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                       "client_id": client_id, "client_secret": secret}).encode()
        reply = post_json(TOKEN_URL, body, {"Content-Type": "application/x-www-form-urlencoded"}, encoded=True)
        token, lifetime = reply.get("access_token"), reply.get("expires_in")
        if not isinstance(token, str) or not token or not finite(lifetime) or lifetime <= 0:
            raise FeedError("Copernicus authentication returned no usable token.")
        _TOKEN = ((client_id, secret), token, time.monotonic() + max(0, lifetime - 30))
        return token


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def statistics_request(lat, lon):
    end = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    dx = 2.5 / (111.32 * math.cos(math.radians(lat)))
    dy = 2.5 / 111.32
    return {
        "input": {
            "bounds": {"bbox": [lon - dx, lat - dy, lon + dx, lat + dy],
                       "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"}},
            "data": [{"type": "sentinel-2-l2a",
                      "dataFilter": {"maxCloudCoverage": 80, "mosaickingOrder": "leastCC"}}],
        },
        "aggregation": {
            "timeRange": {"from": (end - timedelta(days=30)).isoformat(), "to": end.isoformat()},
            "aggregationInterval": {"of": "P5D"},
            "resx": 0.020 / (111.32 * math.cos(math.radians(lat))),
            "resy": 0.020 / 111.32, "evalscript": EVALSCRIPT,
        },
        "calculations": {"indices": {}},
    }


def parse_statistics(reply):
    intervals = reply.get("data")
    if not isinstance(intervals, list):
        raise FeedError("Copernicus returned no statistics intervals.")
    observations = []
    skipped = 0
    for interval in intervals:
        if not isinstance(interval, dict):
            raise FeedError("Copernicus statistics contained a malformed interval.")
        if interval.get("error"):
            raise FeedError("Copernicus could not process a statistics interval; retry or check quota.")
        try:
            bands = interval["outputs"]["indices"]["bands"]
            stats = [bands[f"B{k}"]["stats"] for k in range(3)]
            total, missing = stats[0]["sampleCount"], stats[0]["noDataCount"]
            if not finite(total) or not finite(missing) or total <= 0 or not 0 <= missing <= total:
                raise ValueError("invalid coverage")
            if total == missing:
                skipped += 1
                continue
            means = [s["mean"] for s in stats]
            if not all(finite(x) for x in means) or not all(-1 <= x <= 1 for x in means[:2]) or not 0 <= means[2] <= 1:
                raise ValueError("invalid index values")
            stamp = interval["interval"]
            if not isinstance(stamp["from"], str) or not isinstance(stamp["to"], str):
                raise ValueError("invalid interval dates")
            observations.append({"from": stamp["from"], "to": stamp["to"],
                                 "ndwi_mean": round(means[0], 4), "nbr_mean": round(means[1], 4),
                                 "water_like_fraction": round(means[2], 4),
                                 "valid_pixel_fraction": round((total - missing) / total, 4)})
        except (KeyError, TypeError, ValueError) as error:
            raise FeedError("Copernicus statistics had malformed bands or coverage.") from error
    observations.sort(key=lambda o: o["from"])
    return observations, skipped


def sentinel_indices_feed(lat, lon):
    client_id = os.environ.get("CDSE_CLIENT_ID", "").strip()
    secret = os.environ.get("CDSE_CLIENT_SECRET", "").strip()
    if not client_id or not secret:
        return {"configuration_required": True, "observations": [],
                "message": "Set CDSE_CLIENT_ID and CDSE_CLIENT_SECRET from the team's Sentinel Hub OAuth client "
                           "in the server environment. Do not paste credentials into chat or commit them."}
    token = access_token(client_id, secret)
    reply = post_json(STATS_URL, statistics_request(lat, lon),
                      {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    observations, skipped = parse_statistics(reply)
    if not observations:
        raise FeedError("No valid cloud-masked Sentinel-2 pixels in the last 30 days; satellite indicators unknown.")
    usable = [o for o in observations if o["valid_pixel_fraction"] >= 0.5]
    change = None
    if len(usable) >= 2:
        before, after = usable[0], usable[-1]
        change = {
            "before": before["from"], "after": after["from"],
            "water_like_change_percentage_points": round(
                100 * (after["water_like_fraction"] - before["water_like_fraction"]), 2),
            "nbr_mean_drop": round(before["nbr_mean"] - after["nbr_mean"], 4),
        }
    return {
        "observations": observations, "comparison": change, "empty_intervals": skipped,
        "area_width_km": 5, "nominal_pixel_m": 20,
        "provider": "Copernicus Data Space Sentinel Hub Statistical API",
        "method": "Sentinel-2 L2A; SCL vegetation/bare-soil/water only; NDWI=(B03-B08)/(B03+B08), "
                  "NBR=(B08-B12)/(B08+B12). Five-day least-cloud mosaics.",
        **record_time(observations[-1]["to"]),
        "message": "Area-average spectral indicators, not ML, confirmed flooding or confirmed burns. "
                   "Cloud masks may miss clouds; dates may cover different valid pixels. NBR decline can reflect "
                   "harvesting or seasonal change. Comparison requires at least 50% valid pixels in both windows; "
                   "it is not a co-registered pixelwise dNBR map.",
    }
