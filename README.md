# Resilience Watch

A team prototype for **CASSINI Hackathon – Challenge 2: Space-enabled crisis
response and civil resilience**. It brings EU / Copernicus data into one
local dashboard for fires and floods. It covers:

- What is happening now: EFFIS burnt areas, satellite fire detections and
  Copernicus EMS activations.
- Where rain will collect and whether rivers will rise: a trained flood
  model, Copernicus DEM drainage analysis and GloFAS.
- Lightning and fire-weather factors from the ECMWF forecast.
- Advisory **directions away from known hazards**, with nearby assembly
  points and hospitals.

It is a starting point for the team, not a finished product and not an
official warning service. **Follow the emergency services for your actual
country and region. BG-ALERT is relevant only in Bulgaria.**

## Share with a mentor: one browser link

The mentor-facing introduction is at **`/review`**. It explains the concept,
how to open the map, and the current limitations before asking for location.
Viewing this introduction does not request GPS or start provider lookups.
The live app still offers automatic location, with a manual city choice if
permission is declined.

After public HTTPS deployment, the introduction's **Prepare email** opens
an editable email draft and **Copy link** copies the same canonical `/review`
URL. Nothing is sent automatically, and the link contains no coordinates,
selected area or calculated destination. Localhost, numeric IPs, local-domain
addresses and non-HTTPS previews cannot generate a mentor email link. Your
mentor needs only a current browser; installing on a home screen is optional.
Some embedded mail browsers restrict GPS: open the link in the device's main
browser if that happens.

### Prepared Frankfurt deployment (not published yet)

`render.yaml` configures one Docker web service in **Frankfurt, Germany**,
on Render's `free` compute plan, with `/api/health` as its health check. Render
provides the public HTTPS address; no custom domain is necessary. This is
EU-region hosting, **not an EU-owned or fully sovereign infrastructure claim**:
Render is a separate cloud provider, and external map/data services remain.
The same Dockerfile can instead run on a university-controlled or EU-owned host.

One team member performs setup; the mentor does not:

1. Create a **private, app-only Git repository** and upload the contents of the
   generated deployment ZIP at its root. Do not upload the Colibri workspace,
   models, `.env` files, private datasets or existing caches.
2. Sign in to [Render](https://dashboard.render.com/) and connect that repository.
   Choose a new **Blueprint**, using its root `render.yaml`. Review the service
   name, **Frankfurt** region and **Free** plan before approving deployment.
   The configuration name must not target an existing unrelated service.
3. After the host reports the deployment live, open its **actual generated HTTPS
   URL** from another device/network, then open `/review`. Check the app,
   provider coverage and location-denial flow before sending it.
4. Use **Prepare email** on `/review`, edit the message and recipient, then send
   it yourself. The same link works on supported phones, tablets and computers.

No repository, hosting account or cloud service is created by the package.
No public URL exists until those setup steps are completed. Optional CDSE
credentials belong in the host's secret/environment settings, never in the ZIP
or browser. Provider failures are still possible after deployment; this package
does not supply an official shelter inventory.

**Free-demo limitations:** Render sleeps after 15 idle minutes; reopening can
take about a minute. Files and cached facility lists disappear on restart,
redeploy or sleep. Outbound API traffic, bandwidth/build usage and monthly
instance-hour limits can suspend the demo. Review account billing and spend
limits: usage overages can incur charges if a payment method is configured.
There is no paid service or automatic upgrade declared here. Do not use this
free prototype for operational emergency response.

Build an app-only source package with standard Python:

```powershell
py -3 resilience\package_release.py --output C:\path\resilience-watch-mentor.zip
```

The ZIP uses explicit runtime, static and regression-test allowlists. It omits
Colibri files, `.cache`, `.env`, model data, local generation logs and unrelated
workspace content. The Docker build context also uses an allowlist. It is a
**source/deployment package**, not a phone executable: email the deployed URL
to the mentor, not a ZIP that requires them to run Python.
Commands elsewhere in this README use the original workspace layout. In the
extracted app-only package, omit the `resilience\` prefix; Colibri integration
tests under `c\tests` are not part of that package.

The container runs as a non-root user and has a writable ephemeral facility
cache. Application access logs omit URL query strings (including GPS values).
Your host and upstream providers may maintain their own logs; provider use
and applicable privacy policies still need team review.

## Installable phone-app prototype

The app now requests location first and automatically checks nearby mapped
assembly/shelter candidates, separately from slow ML training. The first
candidate is the closest **mapped candidate without a known report-buffer
conflict**, not the closest verified safe refuge. Hospitals and fire stations
are excluded from this shortlist. A pin is shown on request; no unchecked
navigation route is drawn or automatically launched.

`GET /api/destinations?lat=...&lon=...&accuracy_m=...` requires an explicit
location and returns `resilience-destinations-v1`. It retrieves OSM facilities,
FIRMS, EFFIS, GDACS and CEMS concurrently using the existing bounded cache.
Known report-centre buffers exclude candidates; remaining candidates are
sorted by recomputed straight-line distance. Buffer sizes reuse the existing
illustrative hazard assumptions, not validated inundation/fire boundaries.
Stale known hazard evidence still excludes places. Missing, stale, older-than-
10-minute retrievals, truncated CEMS responses and skipped records flag
`assessment_incomplete`. Retrieval time does not prove observation freshness.
Device accuracy absent or above 100 m also flags an incomplete assessment;
100 m is a UI uncertainty convention, not a calibrated safety threshold.
Every candidate's `safety_status` stays `unknown` and
`recommended_destination` stays `null`, including with available feeds.
Shelter opening, suitability for the incident, access, capacity, road closures
and route exposure must be confirmed with authorities.

Foreground GPS updates recheck after movement exceeding the greater of
250 m or reported accuracy, no more than once per minute; stationary locations
are rechecked after five minutes. These are provider-load controls, not hazard
response thresholds. Manual area selection and Stop location updates cancel
tracking; hiding the app stops it and returning requests a fresh position.
Late responses from previous areas are ignored. Candidate results expire
after five minutes and are cleared on offline, invalid/stale location or
location-update failure. This is not background emergency monitoring.

The web manifest and service worker provide a standalone installation path
and offline help. Only the offline help page and icon are cached; no API
response, hazard dashboard or location history is stored by the service
worker. Android/desktop can use the browser's Install app action; iPhone can
use Safari's Share > Add to Home Screen. The in-app install button is shown
only when the browser offers installation.

**Phone deployment:** by default the server binds to `127.0.0.1`, which a
phone cannot reach. To serve any device, run it on a team-controlled host
behind HTTPS (browsers only allow GPS on HTTPS):

```powershell
py -3 resilience\server.py --host 0.0.0.0 --port 8080   # or set HOST / PORT
docker build -t resilience-watch resilience; docker run -p 8080:8080 resilience-watch
```

Terminate TLS at the hosting platform or a reverse proxy. Do not expose the
development server as a production emergency service. Physical-device
installation, operational availability and emergency-service acceptance
remain unverified.

**"Official EU safe spot":** no EU-wide official register of evacuation
points or shelters exists, and Copernicus does not publish one. Official
points are designated by national or local civil protection. The safe-spot
panel therefore shows community-mapped OpenStreetMap candidates, labelled as
unofficial, alongside the country's emergency number and curated official
civil-protection links. If every Overpass mirror fails, the app falls back to
that area's last good list (up to seven days old, dated, stored in
`resilience/.cache/`). Because that copy is old, the check is marked
incomplete.

**Fire status:** the top card separates active fire evidence (FIRMS, EFFIS,
GDACS and CEMS reports) from weather-based fire danger (ECMWF rule). When it
is raining, humid or recently wet and the rule is low, it shows "very
unlikely now". Wet conditions do not prove that ignition is impossible.

The MIT [Ponytail](https://github.com/DietrichGebert/ponytail) core skill is
installed as `.github/skills/ponytail/SKILL.md`, with its license alongside it.
It informs the smallest complete change: existing stdlib server, existing
adapters and browser platform capabilities, without a new framework or model.
No plugin hooks or third-party scripts were installed/executed. The current
app skill registry did not reload the new project skill during this session;
start a new session for discovery. Ponytail is engineering guidance, not a
runtime hazard model, and does not establish prediction reliability.

## Website engineering and design approach

This pass applies guidance from [Superpowers](https://github.com/obra/superpowers)
(MIT), [Hallmark](https://github.com/Nutlope/hallmark) (MIT) and
[Impeccable](https://github.com/pbakaus/impeccable) (Apache-2.0). Their installers,
agents and runtime code are not included or executed by this application.

Superpowers informs test-first changes and evidence before completion claims.
Hallmark informs a layout built around the actual task: see the local map, then
inspect forecasts or official guidance. Impeccable informs progressive disclosure,
readable input fields, explicit loading/failure states, and consolidation of the
previous overlapping styles into one visual system.

The map now precedes the timeline and advanced layer settings. Location is still
requested automatically, but a denied/unavailable request has a visible manual
recovery action rather than a message hidden inside a closed section. Nearby
facilities appear only in their selected view. Keyboard navigation has a skip
link and visible focus; primary touch controls are at least 44 pixels high.
The system font avoids another external download. The blue-purple gradient is
a functional rainfall legend, not decorative branding.

With the device's reduced-motion preference enabled, user-started playback
advances in discrete hourly steps every two seconds instead of cross-fading.
Changing that preference pauses playback. Forecast data, unknown-value masks,
country contacts and research feeds retain their existing contracts.

These improvements do not make this a certified or deployed emergency product.
Operational use still requires provider agreements/availability planning,
maintained regional contacts, imagery credentials, independent model validation
and review with emergency-service/GIS specialists. There are no verified safe
routes, measured flood depths or guaranteed real-time coverage.

## Map-first CASSINI 12 workspace

**Rain / evacuation UX and latency:** the map uses an intensity palette from
light blue through blue/violet to deep purple. Hourly display thresholds are
0.1, 1, 3, 6, 10 and 20 mm; daily/window thresholds are 1, 10, 30, 60, 100 and
150 mm. These are visualization bins, not official warning categories or water
depth. A 24-hour slider and Play/Pause control follow successive hourly model
fields. The 7×7 native-spacing mesh supplies 49 samples over 1.5°.
Piecewise-linear barycentric interpolation produces a smooth raster inside
triangles between returned model centres, with a continuous blue-purple palette.
Missing samples are masked and nothing is extrapolated outside the mesh.
Invalid provider values, including negative precipitation, become explicitly
unknown per sample rather than zero or failure of the entire area. Derived
daily totals remain unknown if any contributing day is invalid; quality counts
are included in the feed and versioned API reports partial inputs.
During a cross-fade, triangles affected by unknown values in either hour are
masked in both images so missing future data cannot look like rain clearing.
Cross-fades between hourly frames show forecast evolution, not observed storm
tracking or wind-advection. Apparent rain shapes are interpolated estimates,
not radar boundaries, measured rainfall or street-level accuracy.
Spatial weights are precomputed once on a 192×192 Web Mercator-aligned raster.
At most 28 raster images are cached, including the next hourly frame; animation
only changes two image overlays and does not issue new API calls. Playback
stops at the final hour, on area/view changes or when the page is hidden.

Evacuation information defaults to mapped assembly/shelter candidates (A/S).
Medical/response facilities (H/F) have a separate filter, never silently treated
as shelters. Actual opening, hazard exposure, capacity and route safety remain
unverified. Consult the linked regional authorities for designated destinations.

The browser requests rainfall and emergency contacts directly and paints each
result immediately. Fire evidence loads independently; terrain, GloFAS,
Sentinel statistics, flood training and facility queries are requested only on
opening their views. Rain/location refresh is bounded at 30 seconds and does
not wait for Overpass or ML. Area changes abort old browser requests and clear
old evidence. Repeated requests are deduplicated; map geometry is not rebuilt
on unrelated feed updates. Provider processing may continue after client abort;
no fixed live response-time guarantee is claimed.
Facilities have a 25-second browser deadline; each European Overpass mirror
has an 8-second socket timeout and a 7-second query limit. Timeouts display an
explicit unknown status with a direct local-authority link. Short query limits
may reject complex areas rather than pretending an incomplete list is complete.

**Versioned weather API:** `GET /api/weather/v1?lat=...&lon=...` returns
`resilience-weather-v1`, rainfall/weather/emergency feed envelopes, explicit
partial statuses, time and units, European-data lineage, and links to optional
Copernicus terrain, river, Sentinel and emergency-mapping evidence. `/api/map`
remains the richer research aggregate, not the mobile critical path.
ECMWF forecasts delivered through Open-Meteo are not Copernicus satellite
rainfall observations. Local computation uses no LLM or AI credits at runtime;
third-party delivery/tiles still prevent claiming a fully sovereign deployment.

The default public view is phone-first: automatic location, a short rain/fire
summary, one map with Rain / Fire reports / Nearby places presets, and large
country-specific emergency / nearby places / local authority links. Only rain is initially enabled;
terrain, forecast-time controls, coordinate fields and research models are
inside closed expandable sections. The status never labels an area safe.
Nearby places are still unverified facilities, not evacuation recommendations.
The emergency telephone link opens the device dialler; it does not place an
automatic call. This remains a data-exploration prototype, not an emergency
navigation or alert service.

**Country and region:** `/api/emergency` resolves the selected coordinates with
Photon (OpenStreetMap), independently of the hazard feeds. A curated contact
list supplies 112 for all 27 EU countries, 999 for the UK and 911 for the US.
Additional service numbers are configured for France, Germany, Czechia and the
UK, with official number references. BG-ALERT is shown only in Bulgaria.
Country-specific national portals or European Commission authority directories
are provided for EU countries; regional/local links are currently curated for
Sofia, Gabrovo, Plovdiv, Varna, Lombardy, the Valencian Community and Prague.
Other regions explicitly say their local link is not configured. This is not a
worldwide exhaustive directory or a live authority-warning integration.

Administrative matching is approximate, particularly near borders; returned
places more than 5 km from the selected point are rejected. Unknown countries,
lookup failures and stale context never become a guessed telephone action.
Calls connect at the phone's actual location, not the selected remote map point.
Official contact links were reviewed 2026-11-03 and require ongoing maintenance.

Photon's public demo has no availability guarantee and requires reasonable
usage: <https://github.com/komoot/photon#demo-server>. This low-volume local
prototype serializes lookups (at least 1.1 seconds apart) and caches up to 128
points for 24 hours in memory. A selected coordinate is shared with this lookup
provider; there is no disk-based location history. `PHOTON_REVERSE_URL` can
point to a self-hosted compatible reverse endpoint for larger deployments.

The blue-and-white institutional-inspired design deliberately includes no EU
emblem or official-service claim. The persistent banner identifies this as an
independent CASSINI 12 prototype.

**Water-depth illustration:** actual flood depth is explicitly unavailable.
After selecting a terrain collector, the team can choose a hypothetical
collection area and runoff fraction. The diagram shows assumed retained
inflow divided by that area (m3/m2 = m), on a capped 0–2 m visual scale.
There is no detected basin footprint, retention, drainage or infiltration
simulation; this is neither measured depth nor a hydrodynamic forecast.

**Nearby facilities:** `/api/places` independently queries mapped OSM assembly
points, shelters, hospitals and fire stations within 25 km. Question-mark
markers and the facility list explicitly mark every place unverified.
They are not automatically safe evacuation spots; access, capacity, current
operation and routes are unknown. Provider failures stay visible without
blocking precipitation layers.
Queries use European Overpass endpoints and are bounded to 80 OSM objects,
with at most 40 markers and the six nearest returned objects in the list.
Assembly/shelter candidates are preserved ahead of medical/response facilities;
the six-place list is nearest within the selected facility category.
This is not a complete or authority-maintained inventory. Retrieval time is
displayed separately; facility observation time is unknown. A provider timeout
or partial query remains an explicit error, never a successful empty result.

Run the scenario UI regression with `node resilience\test_water.js` from the
workspace root alongside `node resilience\test_location.js`.
`node resilience\test_mobile.js` covers simple map presets, missing data,
country-specific call links and removal of stale country contacts.
`node resilience\test_rain_renderer.js` checks spatial interpolation, moving
forecast fields, native-point values, dry/unknown masks and no extrapolation.

The primary interface is an interactive map, with an inspector for the GIS
and cost-modelling teammates. The framing is European space data for civil
resilience, not national branding or an official EU product. Gabrovo is the
pilot available as a manual preset; Milan, Valencia and Prague are additional area presets, with explicit
source gaps where supplementary feeds are unavailable.

- Click empty map to watch a new area; select a marker or the ranked collector
  list to inspect its values. **Fit local terrain** returns to the sampled grid.
- **Automatic location first:** startup requests browser geolocation, subject
  to the user's permission. No fixed-area data is loaded while permission is
  pending. Denial, timeout or unavailable positioning leaves the area unselected
  and explains manual presets, map-click or coordinate entry. A manual selection
  made while permission is pending wins over a late position callback.
  Device accuracy is reported. Coordinates go to the backend and data providers;
  the server uses a bounded cache, not persistent location tracking.
- **Blue precipitation areas:** 49 requests spaced 0.25 degrees, with
  elevation downscaling disabled, are merged by provider-returned model
  coordinates. The animated raster interpolates between returned model centres;
  raw GeoJSON polygons still represent approximate nominal 0.25-degree grid
  support, **not observed storm contours or live radar**. The default view is the modelled preceding
  hour ending at the displayed UTC time; daily and three-day totals remain
  available. Hourly and daily colour thresholds are labelled separately.
  Missing hourly rain is unknown, not zero. No data source is errorless.
- **Runoff collectors:** candidate low points and high upstream-count cells,
  sampled at 1 km from Copernicus DEM GLO-90. Marker size represents sampled
  uphill area, not flood probability. Inspect terrain height, sampled downhill
  slope, local relief and rain assumptions.
- **Downhill connections:** D8 neighbour-to-neighbour paths, not observed river
  or drainage infrastructure. Coarse sampling, flats, DEM voids and the finite
  grid can produce misleading low points or incomplete uphill areas.
- **Runoff scenario:** a 0–100% slider scales sampled-area inflow
  `sampled upstream km² × 1,000 × rain mm × assumed runoff fraction`.
  It does not model water retention, depth, soil infiltration or building damage.
  The default 30% is an illustrative assumption for team discussion, not a fit.
- **Other factors:** next available forecast hours, rain intensity, shallow-soil
  moisture, wind, humidity and storm energy. These point estimates are shown
  separately from the whole-calendar-day rain samples. Soil moisture is not
  a calibrated saturation or infiltration threshold.
- Toggle GloFAS river-cell forecasts, thermal/fire evidence, EFFIS FWI, active
  flood/fire CEMS centroids, and Sentinel acquisition footprints. Historical
  closed CEMS activations are not displayed as current map incidents.
  **EFFIS historical mapped burns have their own off-by-default layer**;
  burnt land is not a currently burning fire. Current wet-weather fire rules
  are shown separately from the worst rule in the next 48 hours. Rain lowers
  the weather score, but neither November nor low scores prove zero ignition.
  Thermal anomalies are sensor evidence, not automatically confirmed fires.
  Fire markers older than 24 hours and GDACS fire reports not marked current
  are excluded from the active map layer, but remain traceable in the records.
  This display filter is not proof that older fires are extinguished.

### API for GIS and frontend teammates

`GET /api/map?lat=42.8742&lon=25.3187` returns `schema: resilience-map-v1`,
`location`, `generated_at`, `incomplete_inputs`, `feeds`, and `geojson`.
The four independent map inputs are `rainfall`, `ponding`, `weather_risk`
and `glofas`, each with its own status, retrieval time, error and limitation.
An HTTP 200 can contain unavailable inputs; clients must check feed statuses,
not assume the whole map is complete. Invalid coordinates return HTTP 400.

`geojson` is a FeatureCollection in WGS84 **longitude, latitude** order.
It contains rainfall forecast sample points and approximate nominal grid-support
polygons with the modelled hour and daily values, candidate runoff collector
points, downhill LineStrings, the modelled river-cell point and the selected
point's weather factors. Every feature includes its `layer`, data `kind`,
source, status and retrieval time. No known flood polygons or satellite
detection claims are generated from these model features. Stale features
retain their stale status; unavailable inputs produce no features.

The separate `/api/rainfall` and `/api/ponding` endpoints remain available.
For direct Sentinel-2 imagery indicators use `/api/sentinel_indices`; those
still require the team's CDSE credentials. Galileo can support device
positioning in a future field workflow, but the prototype does not claim to
verify which GNSS constellation the browser uses.

## Run

Double-click `START-RESILIENCE.bat` and open <http://127.0.0.1:8088>.
Alternatively, from the checkout root:

```powershell
py -3 resilience\server.py
```

The app needs Python 3.10+ and the standard library only: no installs for the
public feeds. Direct Sentinel imagery statistics require CDSE OAuth credentials
(see below). It does not need Colibri running. The server listens on loopback
only. The first flood-model request for a new place downloads about 10 years
of data and trains for roughly 10–40 s; after that, the model is cached for
the day.

## Data: EU / Copernicus first

| Layer | Source | What it really is |
|---|---|---|
| Burnt areas | **Copernicus EMS – EFFIS** WFS (MODIS 30 days + VIIRS NRT) | Mapped perimeters of land that burned. They show where land burned, not where the fire front is now. |
| Fire Weather Index | **EFFIS** WMS `mf010.fwi` | Official Copernicus fire-danger forecast layer, shown on the map. |
| River forecast | **Copernicus EMS – GloFAS** (via Open-Meteo flood API) | 30-day discharge forecast for a ~5 km river cell, compared with the same cell's 10-year record (p90 / p99 / record). |
| Activations | **Copernicus EMS Rapid Mapping** | Official activations (code, category, centroid) with a link to the mapped products. |
| Terrain | **Copernicus DEM GLO-90** (via Open-Meteo elevation) | Height used for evacuation scoring and rain pile-up analysis. |
| Rain history | **Copernicus C3S ERA5** reanalysis (via Open-Meteo archive) | Daily rainfall since 2016, used to train the flood model. |
| Weather forecast | **ECMWF IFS 0.25°** (via Open-Meteo) | Rain, wind, humidity, temperature, CAPE and soil moisture. |
| Sentinel catalogue | **Copernicus Data Space STAC** | Latest Sentinel-1/2 acquisition footprints. These are footprints, not hazard areas. |
| Sentinel water / burn indicators | **CDSE Sentinel Hub Statistical API**, direct authenticated access | Cloud-masked Sentinel-2 L2A NDWI, NBR and water-like pixel fraction for a 5 km box; six five-day mosaics. Not confirmed flood or fire extents. |
| Thermal detections | NASA FIRMS VIIRS (gap filler) | Satellite hot spots, also used inside EFFIS. Used for fire tracking. |
| Disaster reports | GDACS (UN / European Commission) | Major flood events, as centroids. |
| Places | OpenStreetMap Overpass | Assembly points, hospitals and fire stations. |

Each source is independently `available`, `unconfigured`, `error` or `stale`.
Missing data is shown as unknown, never as "safe". No fake records are
inserted. CEMS reads at most 300 records and reports provider totals and
pagination gaps; closed activations are not counted as currently active.

## Machine learning and risk indicators

| Part | Type | How it works | Validation shown in the UI |
|---|---|---|---|
| **Flood model** (`ml.flood_ml_feed`) | Trained logistic regression, per location | It explicitly requests ERA5 rainfall and trains on 10 years of rain and GloFAS discharge, predicting a river cell's 90th-percentile high flow within 3 days. Features: future 3-day rain, past 3-day and 30-day rain, river level relative to its median, and season. At inference, future rain comes from ECMWF forecasts. Class weighting is undone when returning scores. | Oldest 80% train / newest 20% test. AUC, river-only AUC, Brier score, days caught and false alarms are shown. **Retrospective tests use realized future rain, not archived forecasts**, so skill can be optimistic. GloFAS labels are modelled high flow, not observed flooding. Output is an experimental score, not a calibrated flood probability. |
| **Where rain piles up** (`ml.ponding_feed`) | Terrain analysis, not ML | D8 flow accumulation on a 9 × 9 km grid of the Copernicus DEM with 1 km spacing. It identifies closed low points and drainage lines, multiplied by the ECMWF 3-day rain. | Not a mapped flood extent. Drains, soil and buildings are not modelled, and the runoff volume is an upper bound. |
| **Lightning** (`ml.weather_risk_feed`) | Transparent rules | Uses ECMWF CAPE over the next 48 h, plus "dry lightning" hours (storm energy with almost no rain and dry air), which can ignite fires. | No local lightning labels exist yet, so this is **not trained**. |
| **Fire weather** (`ml.weather_risk_feed`) | Transparent rules | A points system for heat, dry air, wind, days without rain and dry soil. Rain lowers the score. | Not trained. The official product is the EFFIS FWI layer. |
| **Fire occurrence ML** (`fire_ml.py`) | Opt-in supervised logistic regression | The team supplies reviewed daily fire/non-fire labels and consistent weather features for a single location. The trained JSON model feeds an ECMWF 3-day outlook. No labels or trained models are fabricated. | Chronological test AUC and Brier score; dataset hash and train/test dates are saved. No transfer to other sites, no spread prediction, no archived-forecast validation. |

All judgement-call thresholds are in `ml.FACTORS` so the team can discuss and
tune them in one place. **Neither model scores nor coarse DEM low points are
converted into known flooded-area buffers.** Rainfall gaps make runoff volume
unknown rather than silently treating missing days as zero rainfall.

## Direct Copernicus imagery indicators

The public Sentinel STAC catalogue works without credentials. To process
imagery, a teammate should create a Sentinel Hub OAuth client through their
Copernicus Data Space account and set `CDSE_CLIENT_ID` and
`CDSE_CLIENT_SECRET` in the environment of the shell starting the server.
Do not paste credentials into chat, put them in a job prompt, or commit them.
Instructions: <https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Overview/Authentication.html>.

`/api/sentinel_indices?lat=42.8742&lon=25.3187` uses the
[Statistical API](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Statistical.html)
to request a bounded 5 km box over the last 30 complete days, at approximately
20 m sampling. Five-day least-cloud mosaics retain SCL classes 4, 5 and 6 and
exclude invalid denominators. The result shows:

- Mean NDWI `(B03-B08)/(B03+B08)` and NBR `(B08-B12)/(B08+B12)`.
- Fraction of valid pixels with NDWI above zero, and valid-pixel coverage.
- Water-like fraction change and mean NBR decline, only when two windows
  have at least 50% valid pixels.

These are **area statistics, not a pixelwise flood or dNBR map**. Different
windows can include different clear pixels; vegetation changes, harvesting,
shadows and cloud-mask errors can resemble hazards. All-cloud windows are
unknown, not "no water". OAuth access and processing quota are required;
without credentials the card is explicitly `unconfigured`. Tokens stay on
the backend and are cached only until their expiry.

## Train a fire model as a team

`fire_ml.py` does not scrape satellite detections into automatic "truth".
The GIS and ML teammates should agree a local area, acquisition timing,
label reliability and non-fire coverage first. An EFFIS mapped burn is not
necessarily an ignition on the publication date; a missing detection is not
automatically a verified negative.

Provide a CSV with at least 200 rows, one per UTC day for a **single fixed
latitude and longitude**, with these columns:

```text
date,latitude,longitude,temperature_c,humidity_pct,wind_kmh,dry_days,soil_moisture,fire_observed
```

Features must consistently represent daily maximum temperature (C), minimum
humidity (%), maximum wind (km/h), consecutive preceding dry days (rain below
2 mm/day), and daily mean 0-7 cm soil moisture (m3/m3). `fire_observed` is a
team-reviewed 0 or 1 for that day's local area. Both chronological partitions
must contain both classes. Do not mix sites or duplicate dates.

```powershell
py -3 resilience\fire_ml.py --dataset team-fire-labels.csv --output team-fire-model.json
$env:FIRE_MODEL_PATH = (Resolve-Path team-fire-model.json).Path
py -3 resilience\server.py
```

Existing model files are not overwritten. The dashboard refuses to apply a
model more than 0.01 degrees from its training coordinates. AUC measures
ranking, not calibration; the score is not a certified ignition probability.
The 30-day dry-spell input can be censored, which is reported. Agree training
coverage accordingly. A synthetic test only checks the training machinery,
not real-world fire-prediction skill.

## Evacuation guidance (advisory)

`/api/evacuate?lat=..&lon=..` combines the following inputs:

- Fire tracks: FIRMS detections are clustered at 2 km. The centroid shift
  between the first and last satellite pass gives the observed heading and
  speed, and the ECMWF wind gives the downwind direction.
- Recent EFFIS burnt areas.
- GDACS floods, active CEMS activations and the GloFAS level. Experimental
  flood scores and DEM collectors are provided as context, not confirmed extents.

It then scores 8 directions at 5 and 15 km by:

- Clearance from the known hazard buffers.
- Avoiding the area downwind of fires.
- Gaining height when flooding is possible.

The response lists:

- The best directions and the directions to avoid.
- Nearby assembly points, hospitals and fire stations, with OSM directions
  links.
- Every input's status.

It does not know road closures, traffic, bridges or shelter capacity.

## Team guide: who can own what

The code is split so that each role from the team plan can work independently:

| Role | Files / functions | Good first tasks |
|---|---|---|
| GIS analysis (Sentinel, change detection, APIs) | `feeds.satellite_feed`, `copernicus.py`, `hazards.effis_feed` | Configure the team's CDSE OAuth client, inspect cloud coverage and Sentinel-2 indices; validate them against mapped events. Add co-registered Sentinel-1 flood-water / Sentinel-2 dNBR maps. |
| Routes and networks (road closures, Galileo) | `hazards.evacuation_plan`, `score_direction`, `overpass_places` | Replace straight-line directions with real road routing (OSRM / GraphHopper). Mark roads that cross flood low points. Explain the role of Galileo positioning. |
| Cost model and prioritization | `ml.FACTORS`, a new `costs.py` | Indirect-cost indicators: blocked roads × detour time, affected population and businesses. Justify the loss coefficients. |
| Data / ML | `ml.py`, `fire_ml.py`, `test_ml.py`, `test_copernicus.py` | Curate reliable EFFIS fire/non-fire daily labels and train the opt-in fire model. Validate flood scores on archived ECMWF forecasts, compare with persistence and GloFAS, and evaluate calibration. Lightning still needs separate observed labels. |
| Development (web app, map, dashboard) | `server.py`, `static/` | Mobile layout, Bulgarian translation, charts for GloFAS / outlook. |
| Business and presentation | this README, the UI text | Users, business model, pitch, TAIKAI submission. |

Ground rules that keep it trustworthy:

- Every model shows its validation, and every rule is labelled as a rule.
- Missing data is never shown as safe.
- Never fabricate records.

## Local model contribution (Colibri)

The local `qwen3.6-colibri` model on this laptop generated small, tested helper
functions through the job queue (`c\local_jobs.py`):

- The original `local_helpers.py` (distance, observation age, FIRMS CSV).
- `local_geo.py`: bearing, destination point, hotspot clustering and
  percentile.
- `local_ml.py`: logistic-regression training and ROC AUC.

Every function was reviewed before integration. Times, tokens and any
hand-written fallbacks are recorded in `local-generation.json`, and the
prompts and checks are in `jobs\`. The rest of the application was written
and integrated by the coordinating assistant. It is a team starting point,
not a finished product.

## Checks

```powershell
py -3 -m unittest discover -s resilience -p "test_*.py"
py -3 -m unittest discover -s c\tests -p "test_local_*.py"
node --check resilience\static\app.js
foreach ($test in @("test_product_ui.js", "test_location.js", "test_requests.js",
  "test_water.js", "test_mobile.js", "test_rain_view.js", "test_rain_renderer.js",
  "test_phone_app.js", "test_sharing.js")) {
  node "resilience\$test"
  if ($LASTEXITCODE -ne 0) { throw "Failed: $test" }
}
```

Tests use mocks and synthetic data, never live APIs. They cover parsing,
staleness, flood-model training and refusal, D8 drainage, lightning and fire
rules, fire tracking, GloFAS levels, CEMS parsing and evacuation scoring.

### Optional FIRMS key

The public NASA Europe 24 h CSV works without a key for Bulgaria
(41–45 N, 22–29 E). Elsewhere, set your own key from
<https://firms.modaps.eosdis.nasa.gov/api/map_key/> in the shell that starts
the server: `$env:FIRMS_MAP_KEY = "your-key"`. Never commit keys; the backend
never returns them to the browser.

## Emergency-use boundary

In immediate danger in Bulgaria / the EU, call **112** and follow local
authorities and **BG-ALERT**. This prototype has not been validated by
emergency services. Its directions avoid known hazards only, and model outputs
are statistical estimates for one river cell or one point.

## Sources and attribution

- Copernicus EMS: EFFIS <https://forest-fire.emergency.copernicus.eu/>, GloFAS
  <https://global-flood.emergency.copernicus.eu/>, Rapid Mapping
  <https://rapidmapping.emergency.copernicus.eu/>
- Copernicus Climate Change Service ERA5, Copernicus DEM, Copernicus Data Space
  <https://dataspace.copernicus.eu/>
- ECMWF IFS open data; Open-Meteo APIs (CC BY 4.0) <https://open-meteo.com/>
- GDACS <https://www.gdacs.org/>; NASA FIRMS <https://firms.modaps.eosdis.nasa.gov/>
- Map data © OpenStreetMap contributors (ODbL); Leaflet 1.9.4 (BSD-2-Clause)
