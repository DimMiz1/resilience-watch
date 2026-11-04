"use strict";
const $ = id => document.getElementById(id);
const colors = {fire: "#ffab69", hotspot: "#ffab69", flood: "#77b7ff"};
const names = {rainfall: "ECMWF rainfall samples", effis: "Copernicus EFFIS", glofas: "Copernicus GloFAS", cems: "Copernicus EMS Rapid Mapping", flood_ml: "Flood model", ponding: "Rain pile-up terrain", weather_risk: "Lightning & fire weather", sentinel_indices: "Copernicus Sentinel-2 indices", fire_ml: "Local fire ML", gdacs: "GDACS reports", firms: "NASA FIRMS", weather: "ECMWF weather", satellite: "Copernicus catalogue"};
const mapSources = ["rainfall", "ponding", "weather_risk", "glofas"];
names.places = "Nearby mapped facilities (unverified)";
names.emergency = "Country, region & official contacts";
let locationPoint = {lat: 42.8742, lon: 25.3187};
let feeds = {}, map, pointMarker, groups = {}, fwiLayer, busy = false, generation = 0;
const checkedAt = {};
let selectedMapItem = null;
let locationReady = false;
let locationOrigin = "manual", deviceAccuracy = null, applyingDeviceLocation = false;
let locationWatch = null, locationEpoch = 0, lastDeviceUpdate = 0;
let destinationRequest = null, destinationExpiry = null;
let installPrompt = null;
let placeFilter = "assembly";
let renderedMapState = [];
const pendingFeeds = new Map();
const coreSources = ["rainfall", "emergency", "firms", "gdacs", "cems", "weather_risk"];
let rainView = null, rainAnimation = null;
const motionPreference = window.matchMedia("(prefers-reduced-motion: reduce)");

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function when(value) {
  if (!value) return "Unknown";
  const date = new Date(/[zZ]$|[+-]\d\d:\d\d$/.test(value) ? value : value + "Z");
  return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString([], {timeZoneName: "short"});
}
function age(record) {
  if (!record.observed_at) return "Observation time unknown";
  const stamp = /[zZ]$|[+-]\d\d:\d\d$/.test(record.observed_at) ? record.observed_at : record.observed_at + "Z";
  const minutes = (Date.now() - new Date(stamp).getTime()) / 60000;
  if (!Number.isFinite(minutes)) return "Observation time unknown";
  if (minutes < -5) return "SOURCE TIME IS IN THE FUTURE";
  const hours = Math.max(0, minutes) / 60;
  return `${hours < 1 ? Math.round(hours * 60) + " min" : hours.toFixed(1) + " h"} old${hours > 24 ? " · OLDER THAN 24H" : ""}`;
}
function link(text, url) {
  const element = node("a", text);
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:") return node("span", text);
    element.href = parsed.href;
    element.target = "_blank";
    element.rel = "noopener noreferrer";
    return element;
  } catch {
    return node("span", text);
  }
}
function sourceAvailable(id) { return feeds[id]?.status === "available"; }
function canShow(id) { return ["available", "stale"].includes(feeds[id]?.status); }
function eventRecords() {
  return ["gdacs", "firms"].flatMap(id => canShow(id) ? (feeds[id].data?.events || []).map(event => ({...event, feed_id: id})) : []);
}
function initMap() {
  if (!window.L) {
    $("map").replaceChildren(node("p", "Interactive map unavailable: Leaflet could not load. All feed statuses and records remain available below.", "map-placeholder"));
    return;
  }
  $("map").replaceChildren();
  map = L.map("map").setView([50, 15], 4);
  map.createPane("rainForecast");
  map.getPane("rainForecast").style.zIndex = "350";
  const tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);
  let tileWarningShown = false;
  tiles.on("tileerror", () => {
    if (tileWarningShown) return;
    tileWarningShown = true;
    $("global-error").textContent = "Some basemap tiles could not load. Hazard records may still load; map background coverage is incomplete.";
    $("global-error").hidden = false;
  });
  for (const id of ["fire", "burns", "flood", "scenes", "water", "rain", "drainage", "river", "places", "evac"]) groups[id] = L.layerGroup();
  groups.evac.addTo(map);
  fwiLayer = L.tileLayer.wms("https://maps.effis.emergency.copernicus.eu/effis", {
    layers: "mf010.fwi", styles: "", format: "image/png", transparent: true, opacity: 0.55,
    attribution: "Fire Weather Index &copy; Copernicus EMS EFFIS"});
  fwiLayer.on("tileerror", () => {
    if (!$("show-fwi").checked) return;
    $("global-error").textContent = "The EFFIS Fire Weather Index overlay could not load. Fire-danger coverage is unknown; other layers are independent.";
    $("global-error").hidden = false;
  });
  pointMarker = L.circleMarker([locationPoint.lat, locationPoint.lon], {
    radius: 7, color: "#fff", fillColor: "#55d5bf", fillOpacity: 1, weight: 2, bubblingMouseEvents: false
  }).bindPopup("Selected observation point. Not a shelter or safe destination.");
  map.on("click", event => {
    if ($("show-rain").checked && rainView?.mesh && canShow("rainfall")) {
      const amount = RainRaster.valueAt(rainView.mesh, rainValues(Number($("rain-hour").value)), event.latlng.lat, event.latlng.lng);
      if (amount !== null) {
        const popup = node("div");
        popup.append(node("strong", `${amount.toFixed(1)} mm · interpolated forecast`),
          node("p", `${$("rain-time-label").textContent}. Estimate between ECMWF model centres, not a local measurement or water depth.`));
        L.popup().setLatLng(event.latlng).setContent(popup).openOn(map);
        return;
      }
    }
    $("latitude").value = event.latlng.lat.toFixed(4);
    $("longitude").value = event.latlng.lng.toFixed(4);
    $("place").value = "custom";
    $("location-form").requestSubmit();
  });
  applyLayers();
}
function applyLayers() {
  $("nearby").hidden = !$("show-places").checked;
  if (!map) return;
  if ($("show-fwi").checked) fwiLayer.addTo(map); else map.removeLayer(fwiLayer);
  for (const id of ["fire", "burns", "flood", "scenes", "water", "rain", "drainage", "river", "places"]) {
    if ($(`show-${id}`).checked) groups[id].addTo(map);
    else map.removeLayer(groups[id]);
  }
}
function eventCard(event) {
  const element = node("div", undefined, "record");
  element.append(node("strong", event.label));
  element.append(node("div", `${event.source} · ${event.distance_km} km from selection · ${event.geometry_kind}`, "meta"));
  const metadata = node("div", `Observed / updated: ${when(event.observed_at)} · ${age(event)}`, "meta");
  if (event.older_than_24h || feeds[event.feed_id]?.status === "stale") metadata.classList.add("old");
  element.append(metadata);
  if (feeds[event.feed_id]?.status === "stale") element.append(node("div", "STALE CACHE · latest refresh failed", "meta old"));
  if (event.alert) element.append(node("div", `GDACS alert: ${event.alert} · source reports current: ${event.source_reports_current ? "yes" : "no"}`, "meta"));
  if (event.confidence) element.append(node("div", `Sensor confidence code: ${event.confidence}. Not a safety assessment.`, "meta"));
  element.append(link("Open source report ↗", event.report_url));
  return element;
}
function renderMap() {
  if (!map) return;
  const state = ["fire", "burns", "flood", "scenes", "water", "rain", "drainage", "river", "places"].map(id => feeds[id]);
  state.push(feeds.firms, feeds.gdacs, feeds.cems, feeds.effis, feeds.ponding, feeds.rainfall, feeds.glofas,
    feeds.satellite, $("rain-window").value, $("rain-hour").value, $("runoff-factor").value, placeFilter);
  if (state.length === renderedMapState.length && state.every((value, i) => value === renderedMapState[i])) return;
  renderedMapState = state;
  for (const id of ["fire", "burns", "flood", "scenes", "water", "rain", "drainage", "river", "places"]) groups[id].clearLayers();
  if (canShow("places")) {
    for (const place of visiblePlaces()) {
      const role = facilityRole(place);
      const symbol = {assembly: "A", shelter: "S", medical: "H", response: "F"}[role];
      L.marker([place.lat, place.lon], {
        icon: L.divIcon({className: `facility-marker facility-${role}`, html: symbol, iconSize: [30, 30]}),
        title: `${place.name} · unverified facility`, bubblingMouseEvents: false
      }).bindPopup(placeDetail(place)).addTo(groups.places);
    }
  }
  if (canShow("effis")) {
    for (const area of feeds.effis.data?.areas || []) {
      const popup = node("div");
      popup.append(node("strong", area.label), node("p", `${area.kind} · ${area.area_ha ?? "?"} ha · started ${area.fire_started || "unknown"}`),
        node("p", `Updated ${when(area.observed_at)} · ${age(area)}${area.growth_ha_per_day ? ` · avg ${area.growth_ha_per_day} ha/day` : ""}`),
        link("EFFIS current situation ↗", area.report_url));
      L.geoJSON(area.geometry, {bubblingMouseEvents: false, style: {color: area.recent ? "#ff6b4a" : "#b9805f", weight: 1.5, fillOpacity: area.recent ? 0.35 : 0.15}})
        .bindPopup(popup).addTo(groups.burns);
    }
  }
  if (canShow("cems")) {
    for (const act of feeds.cems.data?.nearby || []) {
      if (!act.active || !["flood", "fire", "wildfire", "forest fire"].includes(act.category.toLowerCase())) continue;
      const type = act.category.toLowerCase() === "flood" ? "flood" : "fire";
      const popup = node("div");
      popup.append(node("strong", `${act.code} · ${act.label}`), node("p", `${act.category} · ${act.active ? "ACTIVE" : "closed"} · ${act.distance_km} km`),
        link("Open CEMS activation ↗", act.report_url));
      L.marker([act.lat, act.lon], {title: act.code}).bindPopup(popup).addTo(groups[type]);
    }
  }
  if (canShow("ponding")) {
    for (const cell of feeds.ponding.data?.cells || []) {
      if (!cell.downstream) continue;
      L.polyline([[cell.lat, cell.lon], [cell.downstream.lat, cell.downstream.lon]],
        {color: "#55d5bf", weight: 1 + Math.min(3, cell.upstream_km2 / 10), opacity: 0.35, interactive: false})
        .addTo(groups.drainage);
    }
    for (const cell of feeds.ponding.data?.collectors || []) {
      L.circleMarker([cell.lat, cell.lon], {radius: 5 + Math.sqrt(cell.upstream_km2) * 2,
        color: "#55d5bf", weight: cell.pit ? 3 : 1.5, fillOpacity: cell.pit ? 0.65 : 0.25, bubblingMouseEvents: false})
        .bindTooltip(`${cell.pit ? "Candidate low point" : "Runoff collector"} · ${cell.upstream_km2} km² sampled uphill`)
        .bindPopup(collectorDetail(cell)).on("click", () => selectMapItem("collector", `${cell.row}-${cell.col}`)).addTo(groups.water);
    }
  }
  renderRainLayer();
  if (canShow("glofas")) {
    const g = feeds.glofas.data, cell = g?.cell;
    if (Number.isFinite(cell?.lat) && Number.isFinite(cell?.lon)) {
      const popup = node("div");
      popup.append(node("strong", "Copernicus GloFAS modelled river cell"),
        stat("Peak ensemble median", `${g.peak.median_m3s} m³/s · ${g.peak.date}`),
        stat("Relative high-flow category", g.peak.level),
        node("p", "Forecast of a river model cell, not a gauged level, river outline or flood extent.", "small"));
      L.circleMarker([cell.lat, cell.lon], {radius: 9, color: "#c3a1ff", weight: 3,
        fillOpacity: 0.5, bubblingMouseEvents: false}).bindPopup(popup).addTo(groups.river);
    }
  }
  for (const event of eventRecords()) {
    if (event.type !== "flood" && (event.older_than_24h ||
        (event.feed_id === "gdacs" && !event.source_reports_current))) continue;
    const type = event.type === "flood" ? "flood" : "fire";
    L.circleMarker([event.lat, event.lon], {radius: 7, color: colors[event.type], weight: 2,
      fillOpacity: feeds[event.feed_id].status === "stale" ? 0.2 : 0.65, bubblingMouseEvents: false
    }).bindPopup(eventCard(event)).addTo(groups[type]);
  }
  if (canShow("satellite")) {
    for (const scene of feeds.satellite.data?.scenes || []) {
      const popup = node("div");
      popup.append(node("strong", scene.collection), node("p", "Acquisition footprint only. NOT a fire or flood boundary."),
        node("p", `${when(scene.observed_at)} · ${age(scene)}`));
      L.geoJSON(scene.geometry, {bubblingMouseEvents: false, style: {color: "#c3a1ff", weight: 1, fillOpacity: 0.02, dashArray: "5 5"}})
        .bindPopup(popup).addTo(groups.scenes);
    }
  }
}
function renderSummary() {
  const fire = feeds.firms, flood = feeds.gdacs, satellite = feeds.satellite, weather = feeds.weather;
  $("fire-count").textContent = sourceAvailable("firms") ? fire.data.events.length : "--";
  $("fire-note").textContent = !fire ? "Loading thermal observations…" : sourceAvailable("firms") ? `Past ${fire.data.window_hours}h · anomalies, not spread` : `${fire.status.toUpperCase()} · coverage unknown`;
  $("flood-count").textContent = sourceAvailable("gdacs") ? flood.data.events.filter(e => e.type === "flood").length : "--";
  $("flood-note").textContent = sourceAvailable("gdacs") ? "Major reports only; not a clearance" : `${flood?.status || "Loading"} · coverage unknown`;
  $("scene-count").textContent = sourceAvailable("satellite") ? satellite.data.scenes.length : "--";
  $("scene-note").textContent = sourceAvailable("satellite") ? "Latest 8 / 14 days · NOT hazard areas" : `${satellite?.status || "Loading"} · catalogue unavailable`;
  if (sourceAvailable("weather")) {
    const data = weather.data, c = data.current;
    $("temperature").textContent = c.temperature_2m == null ? "N/A" : `${c.temperature_2m} °C`;
    $("weather-note").textContent = `Model estimate · ${age(data)}`;
    $("weather-details").replaceChildren(
      node("div", `Wind: ${c.wind_speed_10m ?? "N/A"} km/h · from ${c.wind_direction_10m ?? "N/A"}°`),
      node("div", `Precipitation: ${c.precipitation ?? "N/A"} mm / model interval`),
      node("div", `Model valid time: ${when(c.time)}`),
      node("p", "Not a fire-spread model or local flood measurement.")
    );
  } else {
    $("temperature").textContent = "--";
    $("weather-note").textContent = `${weather?.status || "Loading"} · weather unknown`;
    $("weather-details").textContent = "Weather unavailable for this point. No conditions inferred.";
  }
}
function renderRecords() {
  const events = eventRecords();
  $("event-list").replaceChildren(...events.map(eventCard));
  if (!events.length) {
    $("event-list").append(node("p", "No records displayed. This may mean no matching major reports, missing hotspot configuration, a provider outage or a coverage gap. It never means this area is safe.", "muted small"));
  }
  const scenes = canShow("satellite") ? feeds.satellite.data?.scenes || [] : [];
  $("scene-list").replaceChildren();
  for (const scene of scenes) {
    const card = node("div", undefined, "record");
    card.append(node("strong", scene.id), node("div", `${scene.collection} · ${when(scene.observed_at)} · ${age(scene)}`, "meta"),
      node("div", `Cloud cover: ${scene.cloud_percent == null ? "not supplied" : scene.cloud_percent + "%"} · footprint only`, "meta"),
      link("Explore Copernicus catalogue ↗", scene.catalogue_url));
    if (feeds.satellite.status === "stale") card.append(node("div", "STALE CACHE · latest refresh failed", "meta old"));
    $("scene-list").append(card);
  }
  if (!scenes.length) $("scene-list").append(node("p", "No catalogue records displayed. Check the source status below.", "muted small"));
}
function renderSources() {
  $("source-list").replaceChildren();
  for (const id of Object.keys(names)) {
    const feed = feeds[id], card = node("article", undefined, "source-card");
    card.append(node("span", (feed?.status || (pendingFeeds.has(id) ? "loading" : "on demand")).toUpperCase(), `state ${feed?.status || ""}`),
      node("h3", feed?.source || names[id]));
    if (feed) {
      card.append(node("p", feed.limitation || feed.error || "Provider request has not completed."),
        node("p", `Retrieved: ${when(feed.fetched_at)}`),
        node("p", `Last attempted: ${when(feed.last_attempt_at || checkedAt[id])}`));
      if (feed.error) card.append(node("p", feed.error, "old"));
      if (feed.data?.message) card.append(node("p", feed.data.message));
      if (feed.data?.quality) card.append(node("p", feed.data.quality.message, "old"));
      if (id === "firms" && feed.data && feed.status !== "unconfigured") {
        card.append(node("p", `Newest observation in full source response (not necessarily local): ${when(feed.data.latest_source_observation)}`));
        if (feed.data.source_observation_age_minutes > feed.data.window_hours * 60) {
          card.append(node("p", "SOURCE OBSERVATIONS ARE OLDER THAN THE ADVERTISED WINDOW. Coverage may be delayed.", "old"));
        }
      }
      if (feed.data?.skipped_records) card.append(node("p", `${feed.data.skipped_records} malformed source records excluded.`));
    } else card.append(node("p", pendingFeeds.has(id) ? "Fetching source. Unknown coverage until complete."
      : "Deferred to save requests and compute. Open its view to load; coverage is unknown."));
    $("source-list").append(card);
  }
}
function stat(label, value) {
  const row = node("div", undefined, "stat");
  row.append(node("span", label, "muted"), node("strong", value));
  return row;
}
function selectedRain(sample) {
  const day = $("rain-window").value;
  if (day === "hour") {
    const hour = Number($("rain-hour").value);
    return sample.hourly ? sample.hourly[hour]?.rain_mm ?? null : hour === 0 ? sample.current_hour?.rain_mm ?? null : null;
  }
  return day === "all" ? sample.total_mm : sample.daily.find(d => d.date === day)?.rain_mm ?? null;
}
function rainColor(amount, hourly) {
  const thresholds = hourly ? [0.1, 1, 3, 6, 10, 20] : [1, 10, 30, 60, 100, 150];
  const palette = ["#d8f3ff", "#a5dcfa", "#54b2f4", "#337cf2", "#6654d9", "#9635c6", "#4c146d"];
  return palette[thresholds.filter(limit => amount >= limit).length];
}
function rainValues(hour) {
  const data = feeds.rainfall?.data;
  return (data?.samples || []).map(sample => $("rain-window").value === "hour"
    ? sample.hourly?.[hour]?.rain_mm ?? (hour === 0 ? sample.current_hour?.rain_mm ?? null : null)
    : selectedRain(sample));
}
function rasterFrame(hour, unknownIndices = []) {
  const window = $("rain-window").value;
  const key = `${window}-${window === "hour" ? hour : "total"}-${unknownIndices.join(",")}`;
  if (rainView.frames.has(key)) return rainView.frames.get(key);
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = rainView.mesh.size;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Your browser cannot draw the forecast raster.");
  const values = rainValues(hour);
  for (const index of unknownIndices) values[index] = null;
  const pixels = RainRaster.pixels(rainView.mesh, values, window === "hour");
  context.putImageData(new ImageData(pixels, canvas.width, canvas.height), 0, 0);
  const url = canvas.toDataURL("image/png");
  // Bound daily/hourly image cache. Spatial weights and data are retained separately.
  if (rainView.frames.size >= 28) rainView.frames.delete(rainView.frames.keys().next().value);
  rainView.frames.set(key, url);
  return url;
}
function renderRainLayer(position = Number($("rain-hour").value)) {
  if (!map || !canShow("rainfall")) { rainView = null; stopRainAnimation(); return; }
  try {
    const data = feeds.rainfall.data;
    if (!rainView || rainView.data !== data) {
      stopRainAnimation();
      const mesh = RainRaster.build(data);
      const bounds = [[mesh.bounds.south, mesh.bounds.west], [mesh.bounds.north, mesh.bounds.east]];
      rainView = {data, mesh, frames: new Map(),
        lower: L.imageOverlay("", bounds, {pane: "rainForecast", interactive: false}),
        upper: L.imageOverlay("", bounds, {pane: "rainForecast", interactive: false})};
    }
    const lower = Math.floor(position), upper = Math.min(23, lower + 1), fraction = position - lower;
    const lowerValues = rainValues(lower), upperValues = rainValues(upper);
    const unknownIndices = lowerValues.flatMap((value, index) =>
      !Number.isFinite(value) || (fraction > 0 && !Number.isFinite(upperValues[index])) ? [index] : []);
    const opacity = feeds.rainfall.status === "stale" ? 0.45 : 1;
    // Image decoding is cached; only opacity changes between forecast hours.
    const lowerUrl = rasterFrame(lower, unknownIndices);
    if (rainView.lowerUrl !== lowerUrl) { rainView.lower.setUrl(lowerUrl); rainView.lowerUrl = lowerUrl; }
    rainView.lower.setOpacity(opacity).addTo(groups.rain);
    const upperUrl = rasterFrame(upper, unknownIndices);
    if (rainView.upperUrl !== upperUrl) { rainView.upper.setUrl(upperUrl); rainView.upperUrl = upperUrl; }
    rainView.upper.setOpacity(fraction * opacity).addTo(groups.rain);
    rainView.lower.setOpacity((1 - fraction) * opacity);
    $("rain-play").disabled = $("rain-window").value !== "hour";
    const unknown = unknownIndices.length;
    const status = unknown
      ? `${unknown} model samples unknown in this hour/window or transition; affected areas are transparent, not dry.`
      : rainAnimation !== null ? motionPreference.matches
        ? "Playing hourly steps with reduced motion; these are forecasts, not observations."
        : "Playing hourly forecasts; cross-fades are not observed storm motion."
      : "Smoothed between model centres. Play follows 24 hourly forecasts; outside the sampled area is unknown.";
    if ($("rain-animation-status").textContent !== status) $("rain-animation-status").textContent = status;
  } catch (error) {
    stopRainAnimation();
    rainView = null;
    groups.rain.clearLayers();
    $("rain-animation-status").textContent = `Rain visualisation unavailable: ${error.message}. No replacement storm shapes are invented.`;
    $("rain-play").disabled = true;
  }
}
function stopRainAnimation() {
  if (rainAnimation !== null) cancelAnimationFrame(rainAnimation);
  rainAnimation = null;
  $("rain-play").textContent = "Play next 24 hours";
  $("rain-play").setAttribute("aria-pressed", "false");
  if ($("rain-animation-status").textContent.startsWith("Playing")) {
    $("rain-animation-status").textContent = "Forecast paused. Outside the sampled area is unknown.";
  }
}
function forecastPosition(elapsedMilliseconds, startHour, reducedMotion) {
  const progress = reducedMotion ? Math.floor(elapsedMilliseconds / 2000) : elapsedMilliseconds / 1000;
  return Math.min(23, startHour + progress);
}
function playRainAnimation() {
  if (rainAnimation !== null) { stopRainAnimation(); renderRainLayer(); return; }
  if (!rainView || $("rain-window").value !== "hour") return;
  let startHour = Number($("rain-hour").value);
  if (startHour >= 23) startHour = 0;
  const start = performance.now();
  const reducedMotion = motionPreference.matches;
  $("rain-play").textContent = "Pause forecast";
  $("rain-play").setAttribute("aria-pressed", "true");
  $("rain-animation-status").textContent = "Playing 24 hourly forecasts. In-between frames are visual cross-fades—not tracked storm motion.";
  let lastHour = -1;
  function tick(now) {
    const position = forecastPosition(now - start, startHour, reducedMotion);
    const hour = Math.floor(position);
    $("rain-hour").value = String(hour);
    if (hour !== lastHour) {
      lastHour = hour;
      const stamp = rainView?.data.samples[0]?.hourly?.[hour]?.valid_at;
      $("rain-time-label").textContent = stamp ? `Hour ending ${when(stamp)}` : "Forecast hour unavailable";
      $("rain-hour").setAttribute("aria-valuetext", $("rain-time-label").textContent);
    }
    renderRainLayer(position);
    if (!rainView || position >= 23) { stopRainAnimation(); return; }
    rainAnimation = requestAnimationFrame(tick);
  }
  rainAnimation = requestAnimationFrame(tick);
}
function facilityRole(place) {
  return place.facility_role || (place.kind === "assembly point" ? "assembly" : place.kind === "shelter" ? "shelter" : place.kind === "hospital" ? "medical" : "response");
}
function visiblePlaces() {
  if (!canShow("places")) return [];
  return feeds.places.data.places.filter(place =>
    ["assembly", "shelter"].includes(facilityRole(place)) === (placeFilter === "assembly"));
}
function collectorDetail(cell) {
  const detail = node("div");
  const water = feeds.ponding.data;
  const runoff = Number($("runoff-factor").value) / 100;
  detail.append(node("strong", cell.pit ? "Candidate terrain low point" : "Downhill runoff collector"),
    node("p", `${cell.lat}, ${cell.lon} · ${feeds.ponding.status.toUpperCase()} · Copernicus DEM + ECMWF`, "small"),
    stat("Terrain height", `${cell.elevation_m} m`),
    stat("Sampled uphill area", `${cell.upstream_km2} km²`),
    stat("Steepest downhill slope (sampled)", `${cell.downhill_slope_pct}%`),
    stat("Neighbourhood relief", `${cell.local_relief_m} m`),
    stat("Uniform 3-day rain assumption", `${water.rain_next_3d_mm} mm`),
    node("p", `Whole UTC days: ${water.rain_dates.join(", ")}`, "small"),
    stat(`Illustrative inflow at ${Math.round(runoff * 100)}% runoff`,
      `${Math.round(cell.inflow_m3_if_all_runs_off * runoff).toLocaleString()} m³`),
    node("p", "Scenario: sampled uphill area × point rain × chosen runoff fraction. Not stored-water volume, depth, a measured flood or a calibrated catchment model.", "old small"),
    node("p", water.message, "muted small"));
  return detail;
}
function rainDetail(sample) {
  const detail = node("div");
  const amount = selectedRain(sample);
  detail.append(node("strong", "ECMWF modelled precipitation area"),
    node("p", `${sample.model_cell.lat}, ${sample.model_cell.lon} · ${feeds.rainfall.status.toUpperCase()}`, "small"),
    stat("Selected-window rain per square metre", amount == null ? "Unknown" : `${amount.toFixed(1)} litres / m²`),
    node("p", "1 mm of rainfall equals 1 litre per m² before runoff, infiltration or evaporation. It does not mean streets have this depth of water.", "small"),
    stat("Modelled preceding-hour rain", sample.current_hour.rain_mm === null ? "Unknown" : `${sample.current_hour.rain_mm} mm`),
    node("p", `Hour ends ${when(sample.current_hour.valid_at)}. Model estimate, not live radar.`, "small"),
    ...sample.daily.map(d => stat(`${d.date} UTC`, d.rain_mm === null ? "Unknown" : `${d.rain_mm} mm`)),
    stat("Three-day total", sample.total_mm === null ? "Unknown" : `${sample.total_mm} mm`),
    stat("Underlying model cell", `${sample.model_cell.lat}, ${sample.model_cell.lon}`),
    node("p", feeds.rainfall.data.message, "muted small"));
  return detail;
}
function selectMapItem(kind, id) {
  selectedMapItem = {kind, id};
  renderMapInspector();
  if (kind === "collector") {
    $("map-detail").closest("details").open = true;
    if (window.matchMedia("(max-width: 900px)").matches) $("map-detail").scrollIntoView({behavior: "smooth", block: "start"});
  }
}
function placeDetail(place) {
  const box = node("div");
  box.append(node("strong", place.name), stat("Mapped type", place.kind),
    stat("Straight-line distance", `${place.distance_km} km`),
    node("p", "UNVERIFIED FACILITY — not a confirmed evacuation shelter. Access, capacity and safe routes unknown.", "old"),
    link("Inspect this place in OpenStreetMap", `https://www.openstreetmap.org/?mlat=${place.lat}&mlon=${place.lon}#map=17/${place.lat}/${place.lon}`));
  return box;
}
function renderDepthScenario(cell) {
  const area = Number($("scenario-area").value);
  const box = $("water-scenario");
  if (!cell) { box.textContent = "Select a terrain collector to explore an assumed water-level scenario."; return; }
  if (!Number.isFinite(area) || area < 100 || area > 10000000) {
    box.textContent = "Enter a hypothetical area from 100 to 10,000,000 m². No scenario calculated.";
    return;
  }
  const volume = cell.inflow_m3_if_all_runs_off * Number($("runoff-factor").value) / 100;
  const depth = volume / area;
  const drawing = node("div", undefined, "water-vessel");
  const fill = node("div", undefined, "water-fill");
  fill.style.height = `${Math.min(100, depth / 2 * 100)}%`;
  drawing.append(fill, node("strong", `${depth.toFixed(2)} m`, "water-value"));
  box.replaceChildren(node("p", "HYPOTHETICAL WATER LEVEL · NOT PREDICTED DEPTH", "scenario-label"), drawing,
    stat("Assumed retained volume / area", `${Math.round(volume).toLocaleString()} m³ / ${area.toLocaleString()} m²`),
    node("p", "Volume ÷ chosen area. Diagram scale 0–2 m; larger values are capped visually, not numerically.", "small"));
}
function renderMapInspector() {
  const rain = canShow("rainfall") ? feeds.rainfall.data : null;
  const previousDay = $("rain-window").value;
  $("rain-window").replaceChildren(new Option("Current modelled hour", "hour"), new Option("Three-day total", "all"),
    ...(rain?.dates || []).map(d => new Option(d + " UTC", d)));
  if (["hour", "all", ...(rain?.dates || [])].includes(previousDay)) $("rain-window").value = previousDay;
  $("rain-legend").textContent = $("rain-window").value === "hour"
    ? "mm / modelled hour: 0.1 · 1 · 3 · 6 · 10 · 20+. Purple = high rain intensity, not an official warning."
    : "mm / selected UTC window: 1 · 10 · 30 · 60 · 100 · 150+. Not flood depth or official warning levels.";
  const hour = Number($("rain-hour").value);
  const stamp = rain?.cells[0]?.hourly?.[hour]?.valid_at || (hour === 0 ? rain?.cells[0]?.current_hour?.valid_at : null);
  $("rain-time-label").textContent = $("rain-window").value === "hour"
    ? stamp ? `Hour ending ${when(stamp)}` : "Forecast hour unavailable"
    : "Daily / multi-day total selected";
  $("rain-hour").disabled = $("rain-window").value !== "hour" || !rain;
  $("rain-hour").setAttribute("aria-valuetext", $("rain-time-label").textContent);
  const terrain = canShow("ponding") ? feeds.ponding.data : null;
  $("collector-list").replaceChildren();
  if (terrain) {
    for (const [index, cell] of terrain.collectors.slice(0, 6).entries()) {
      const button = node("button", `${index + 1}. ${cell.pit ? "Candidate low point" : "Collector"} · ${cell.upstream_km2} km² · ${cell.elevation_m} m`);
      button.type = "button";
      button.addEventListener("click", () => {
        selectMapItem("collector", `${cell.row}-${cell.col}`);
        if (map) map.panTo([cell.lat, cell.lon]);
      });
      $("collector-list").append(button);
    }
    if (!terrain.collectors.length) $("collector-list").append(node("p", "No collector meets this coarse-grid filter. This does not rule out flooding.", "small"));
    $("collector-list").append(node("p", `${terrain.valid_cells} valid DEM samples; ${terrain.missing_cells} missing. Grid ${terrain.grid_km} km wide.`, "muted small"));
  } else $("collector-list").append(unavailable("ponding"));
  const item = selectedMapItem;
  const cell = item?.kind === "collector" ? terrain?.cells.find(c => `${c.row}-${c.col}` === item.id) : null;
  const sample = item?.kind === "rain" ? rain?.cells.find(s => s.id === item.id) : null;
  renderDepthScenario(cell);
  const placesBox = $("nearby-places");
  placesBox.replaceChildren();
  if (canShow("places")) {
    for (const place of visiblePlaces().slice(0, 6)) {
      const button = node("button", `${place.name} · ${place.kind} · ${place.distance_km} km`);
      button.type = "button";
      button.addEventListener("click", () => {
        if (map) {
          setMapMode("places");
          map.setView([place.lat, place.lon], 14);
          L.popup().setLatLng([place.lat, place.lon]).setContent(placeDetail(place)).openOn(map);
          $("map").scrollIntoView({behavior: "smooth", block: "center"});
        }
      });
      placesBox.append(button);
    }
    if (!visiblePlaces().length) placesBox.append(node("p", placeFilter === "assembly"
      ? "No mapped assembly points or shelters returned. No safe destination is identified. Check local authorities; do not treat hospitals as shelters."
      : "No medical / response facilities returned. This does not mean none exist.", "small old"));
    placesBox.append(node("p", `${feeds.places.status.toUpperCase()} · ${feeds.places.data.message}`, "small muted"));
    placesBox.append(node("p", `${feeds.places.data.coverage_note} Retrieved ${when(feeds.places.data.retrieved_at)}; facility observation time unknown.`, "small muted"));
  } else placesBox.append(unavailable("places"));
  $("map-detail").replaceChildren(cell ? collectorDetail(cell) : sample ? rainDetail(sample)
    : node("p", "Select a runoff collector or a rainfall sample. Low points are candidates for GIS investigation, not confirmed flooded places.", "muted small"));
  const wr = canShow("weather_risk") ? feeds.weather_risk.data : null;
  const f = wr?.weather_factors;
  if (f) {
    $("map-factors").replaceChildren(
      stat(`Rain over ${wr.hours_checked} forecast hours`, `${f.rain_total_mm} mm`),
      stat("Heaviest modelled hourly rain", `${f.peak_hourly_rain_mm} mm`),
      node("p", `Peak rain: ${when(f.peak_rain_at)} · valid ${when(f.valid_from)} to ${when(f.valid_to)}`, "muted small"),
      stat("Topsoil moisture (0–7 cm)", f.mean_topsoil_moisture == null ? "Unknown" : `${f.mean_topsoil_moisture} m³/m³`),
      stat("Maximum wind / minimum humidity", `${f.max_wind_kmh} km/h / ${f.min_humidity_pct}%`),
      stat("Thunderstorm energy / CAPE", `${wr.lightning.max_cape_jkg} J/kg`),
      stat("Current fire-weather rule", wr.current_fire_weather.level.toUpperCase()),
      stat("Wet-weather context", `${wr.current_fire_weather.rain_mm} mm / modelled hour · RH ${wr.current_fire_weather.humidity_pct}%`),
      stat("Worst fire-weather rule in next 48h", wr.fire_weather.level.toUpperCase()),
      node("p", "Point-model factors, not spatial flood likelihood. CAPE is storm potential, not measured lightning. EFFIS FWI provides the separate European fire-danger layer.", "muted small"));
  } else $("map-factors").replaceChildren(unavailable("weather_risk"));
  const missing = mapSources.filter(id => feeds[id]?.status !== "available");
  $("map-status").textContent = missing.length ? `${busy ? "Loading / " : ""}unknown or stale: ${missing.map(id => names[id]).join(", ")}`
    : `Blue areas: ${rain.cells.length} nominal model-grid supports · not radar / errorless storm boundaries`;
  if (!locationReady) $("map-status").textContent = "Waiting for location permission or manual area selection.";
}
function unavailable(id) {
  const feed = feeds[id];
  if (!feed) return node("p", pendingFeeds.has(id) ? "Loading this source…" : "Not loaded yet. Open its view to request data.", "muted small");
  return node("p", `${feed.status.toUpperCase()}: ${feed.error || feed.data?.message || "no data"}. Risk unknown, not low.`, "old");
}
function renderRisk() {
  const flood = canShow("flood_ml") ? feeds.flood_ml.data : null;
  if (flood) {
    const v = flood.validation;
    const box = node("div");
    box.append(node("div", `Score ${flood.peak_probability.toFixed(3)}`, "big"),
      node("p", flood.question, "small"),
      ...flood.outlook.map(o => stat(o.date, `${o.probability.toFixed(3)} · rain next 3d ${o.rain_next_3d_mm} mm`)),
      stat("Test AUC (river level only)", `${v.auc} (${v.auc_river_level_only})`),
      stat("Test events caught / false alarms", `${v.detected_events_pct ?? "–"}% / ${v.false_alarm_pct ?? "–"}%`),
      stat("Retrospective Brier score (lower is better)", String(v.brier_score)),
      node("p", v.caveat, "old small"),
      node("p", `Trained ${flood.trained_on.join(" → ")}. ${v.method}. ${flood.message}`, "muted small"));
    $("risk-flood").replaceChildren(box);
  } else $("risk-flood").replaceChildren(unavailable("flood_ml"));

  const water = canShow("ponding") ? feeds.ponding.data : null;
  if (water) {
    const box = node("div");
    box.append(stat("ECMWF rain next 3 days", `${water.rain_next_3d_mm} mm`),
      ...water.collectors.slice(0, 5).map(c => stat(`${c.pit ? "Low point" : "Drainage line"} ${c.lat}, ${c.lon}`, `${c.upstream_km2} km² · ${c.elevation_m} m`)),
      node("p", water.message, "muted small"));
    $("risk-water").replaceChildren(box);
  } else $("risk-water").replaceChildren(unavailable("ponding"));

  const wr = canShow("weather_risk") ? feeds.weather_risk.data : null;
  if (wr) {
    const l = wr.lightning, f = wr.fire_weather, box = node("div");
    box.append(stat("Thunderstorm potential", `${l.level.toUpperCase()} · CAPE ${l.max_cape_jkg} J/kg`),
      stat("Dry-lightning hours (fire ignition)", String(l.dry_lightning_hours.length)),
      stat("Next 48h maximum fire-weather rule", `${f.level.toUpperCase()} · ${f.points} pts at ${when(f.at)}`),
      stat("Days without rain", String(f.days_without_rain)),
      node("p", "Points: " + Object.entries(f.factors).map(([k, v]) => `${k} ${v > 0 ? "+" : ""}${v}`).join(", "), "small"),
      node("p", `Current fire-weather rule: ${wr.current_fire_weather.level.toUpperCase()} at ${when(wr.current_fire_weather.valid_at)}. ${wr.current_fire_weather.message}`, "small"),
      node("p", wr.message, "muted small"));
    $("risk-weather").replaceChildren(box);
  } else $("risk-weather").replaceChildren(unavailable("weather_risk"));

  const box = node("div");
  if (canShow("glofas")) {
    const g = feeds.glofas.data;
    box.append(stat("GloFAS peak (30 days)", `${g.peak.level.toUpperCase()} · ${g.peak.median_m3s} m³/s on ${g.peak.date}`),
      stat("Worst ensemble member", g.peak.worst_member_level.toUpperCase()));
  } else box.append(unavailable("glofas"));
  if (canShow("cems")) {
    const near = feeds.cems.data.nearby.filter(a => a.active);
    box.append(stat("Active CEMS activations ≤600 km", String(near.length)),
      stat("Open in Europe (within provider response)", String(feeds.cems.data.active_europe_count_in_response)),
      ...(feeds.cems.data.has_more ? [node("p", "Provider response is partial; more activations exist.", "old")] : []),
      ...near.slice(0, 3).map(a => link(`${a.code} ${a.label} (${a.distance_km} km) ↗`, a.report_url)));
  } else box.append(unavailable("cems"));
  $("risk-official").replaceChildren(box);

  const sentinel = canShow("sentinel_indices") ? feeds.sentinel_indices.data : null;
  if (sentinel) {
    const detail = node("div");
    for (const o of sentinel.observations) {
      detail.append(stat(`${o.from.slice(0, 10)} to ${o.to.slice(0, 10)}`,
        `Water-like pixels ${(o.water_like_fraction * 100).toFixed(1)}% · valid ${(o.valid_pixel_fraction * 100).toFixed(0)}%`),
      stat("Mean NDWI / NBR", `${o.ndwi_mean} / ${o.nbr_mean}`));
    }
    if (sentinel.comparison) {
      detail.append(stat("Water-like change (percentage points)", String(sentinel.comparison.water_like_change_percentage_points)),
        stat("Area-mean NBR decline", String(sentinel.comparison.nbr_mean_drop)));
    } else detail.append(node("p", "Not enough adequately clear windows for a comparison.", "old"));
    detail.append(node("p", sentinel.message, "muted small"));
    $("risk-sentinel").replaceChildren(detail);
  } else $("risk-sentinel").replaceChildren(unavailable("sentinel_indices"));

  const fireML = canShow("fire_ml") ? feeds.fire_ml.data : null;
  if (fireML) {
    const detail = node("div");
    detail.append(...fireML.outlook.map(o => stat(o.date, `Fire occurrence score ${o.score.toFixed(3)}`)),
      stat("Chronological test AUC / Brier", `${fireML.validation.auc} / ${fireML.validation.brier_score}`),
      node("p", fireML.message, "muted small"));
    $("risk-fire-ml").replaceChildren(detail);
  } else $("risk-fire-ml").replaceChildren(unavailable("fire_ml"));
}
function clearDestinations(message) {
  destinationRequest?.abort();
  destinationRequest = null;
  clearTimeout(destinationExpiry);
  $("destination-results").replaceChildren();
  $("destination-evidence").replaceChildren(node("p", "No current assessment. Route safety, shelter access and capacity remain unknown."));
  $("destination-status").textContent = message;
  $("destination-panel").setAttribute("aria-busy", "false");
  if (map) groups.evac.clearLayers();
}
async function loadDestinations() {
  if (!locationReady || document.hidden || destinationRequest) return;
  if (locationOrigin === "device" && Date.now() - lastDeviceUpdate > 300000) {
    clearDestinations("Device location is stale. Find my location again before checking candidates.");
    return;
  }
  const requestGeneration = generation;
  const origin = {...locationPoint};
  const controller = new AbortController();
  clearDestinations("Checking nearby assembly/shelter candidates against available hazard reports…");
  destinationRequest = controller;
  $("destination-panel").setAttribute("aria-busy", "true");
  $("destination-origin").textContent = locationOrigin === "device"
    ? `Device location · accuracy about ${Math.round(deviceAccuracy)} m. Updates while the app is visible.`
    : "Selected area, not confirmed as your device location. Distances are from this selected point.";
  const query = new URLSearchParams(origin);
  if (locationOrigin === "device" && deviceAccuracy !== null) query.set("accuracy_m", deviceAccuracy);
  const timeout = setTimeout(() => controller.abort(), 45000);
  try {
    const response = await fetch(`/api/destinations?${query}`, {signal: controller.signal});
    const plan = await response.json();
    if (!response.ok) throw new Error(plan.error || `HTTP ${response.status}`);
    if (plan.schema !== "resilience-destinations-v1" || plan.safety_status !== "unknown" ||
        !Array.isArray(plan.candidates) || !Array.isArray(plan.excluded) ||
        !Array.isArray(plan.incomplete_inputs) || !Array.isArray(plan.limitations) ||
        !["assessment_incomplete", "candidates_screened", "no_eligible_candidates"].includes(plan.status) ||
        plan.location?.lat !== origin.lat || plan.location?.lon !== origin.lon ||
        !Number.isFinite(Date.parse(plan.generated_at)) ||
        Math.abs(Date.now() - Date.parse(plan.generated_at)) > 300000 ||
        plan.candidates.some(p => !Number.isFinite(p.lat) || Math.abs(p.lat) > 90 ||
          !Number.isFinite(p.lon) || Math.abs(p.lon) > 180 ||
          !Number.isFinite(p.distance_km) || p.distance_km < 0 || p.safety_status !== "unknown" ||
          p.screening !== "no_known_buffer_conflict")) {
      throw new Error("Invalid or expired candidate assessment");
    }
    if (generation !== requestGeneration || destinationRequest !== controller) return;
    renderDestinations(plan);
    destinationExpiry = setTimeout(() => clearDestinations(
      "Candidate assessment expired. Refresh to recheck; no current destination assessment."),
      Math.max(0, 300000 - (Date.now() - Date.parse(plan.generated_at))));
  } catch (error) {
    if (generation === requestGeneration && destinationRequest === controller) {
      clearDestinations(`Candidate checks unavailable: ${error.name === "AbortError" ? "request timed out" : error.message}. Follow local authorities.`);
    }
  } finally {
    clearTimeout(timeout);
    if (destinationRequest === controller) {
      destinationRequest = null;
      $("destination-panel").setAttribute("aria-busy", "false");
    }
  }
}
function renderDestinations(plan) {
  $("destination-status").textContent = plan.origin_in_known_buffer
    ? "Your location overlaps a known report buffer. Follow emergency-service instructions; no safe route is known."
    : !plan.candidates.length && plan.status === "assessment_incomplete"
      ? plan.incomplete_inputs.includes("places")
        ? "Map lookup of assembly points is unavailable right now. Use the official sources below or call the emergency number."
        : "Candidate checks incomplete. No destination suggestion; use the official sources below."
    : plan.status === "assessment_incomplete"
      ? "Nearby mapped candidates · checks incomplete. No safe destination established."
      : plan.candidates.length ? "Closest mapped candidates to check · none verified safe or open."
        : "No eligible mapped assembly/shelter candidate found. Ask local authorities.";
  const results = $("destination-results");
  results.replaceChildren();
  const alternatives = node("details");
  alternatives.append(node("summary", "Other mapped candidates"));
  plan.candidates.forEach((place, index) => {
    const card = node("article", undefined, "destination-card");
    card.append(node("h3", place.name),
      node("p", `${place.kind} · ${place.distance_km.toFixed(2)} km straight line, not walking distance.`),
      node("p", "Unverified: safety, opening and access unknown.", "old small"));
    const button = node("button", "Show candidate on map");
    button.type = "button";
    button.disabled = !map;
    button.addEventListener("click", () => {
      if (!map) return;
      setMapMode("places");
      groups.evac.clearLayers();
      const popup = node("div");
      popup.append(node("strong", place.name), node("p", "Unverified candidate. Safe route, opening and capacity unknown."));
      L.circleMarker([place.lat, place.lon], {radius: 10, color: "#805008", weight: 3})
        .bindPopup(popup).addTo(groups.evac);
      map.setView([place.lat, place.lon], 15);
      $("map-workspace").scrollIntoView({block: "start", behavior: motionPreference.matches ? "auto" : "smooth"});
    });
    card.append(button, link("View map pin · not a checked route", place.map_url));
    if (index === 0) results.append(card);
    else alternatives.append(card);
  });
  if (plan.candidates.length > 1) results.append(alternatives);
  if (!plan.candidates.length) results.append(node("p", "No destination suggestion. Missing map coverage is not evidence that no shelter exists.", "old"));
  const evidence = $("destination-evidence");
  evidence.replaceChildren(node("p", plan.basis), node("p", plan.message),
    node("p", `Checked ${when(plan.generated_at)}. Excluded ${plan.excluded.length} candidate(s) in known report buffers.`),
    node("p", `Incomplete inputs: ${plan.incomplete_inputs.join(", ") || "none flagged; source coverage still bounded and delayed"}.`),
    node("p", plan.location_uncertain ? "Location accuracy is unknown or exceeds 100 m; proximity is uncertain." : "Device reports accuracy within 100 m; this does not validate hazard boundaries."),
    ...Object.values(plan.inputs).map(input => node("p",
      `${input.source} · ${input.status} · retrieved ${when(input.fetched_at)}${input.error ? " · " + input.error : ""}`)),
    ...plan.limitations.map(limit => node("p", limit)));
}
async function evacuate() {
  if (!locationReady) return;
  const requestGeneration = generation;
  const selectedLocation = {...locationPoint};
  $("evacuate").disabled = true;
  $("evac-result").replaceChildren(node("p", "Combining all sources (the flood model may need to train first, up to a minute)…", "muted small"));
  try {
    const response = await fetch(`/api/evacuate?${new URLSearchParams(selectedLocation)}`, {signal: AbortSignal.timeout(170000)});
    const plan = await response.json();
    if (!response.ok) throw new Error(plan.error || `HTTP ${response.status}`);
    if (requestGeneration === generation) renderEvacuation(plan);
  } catch (error) {
    if (requestGeneration === generation) $("evac-result").replaceChildren(node("p", `Guidance failed: ${error.message}. Follow emergency services.`, "old"));
  } finally {
    $("evacuate").disabled = false;
  }
}
function renderEvacuation(plan) {
  const urgency = {inside: "YOU ARE INSIDE A KNOWN HAZARD BUFFER", near: "A KNOWN HAZARD IS CLOSE", watch: "HAZARD SIGNALS IN THE AREA — STAY ALERT",
    none_detected: "NO HAZARD DETECTED IN AVAILABLE DATA (NOT A GUARANTEE)"}[plan.urgency];
  const head = node("div", undefined, `evac-head urgency-${plan.urgency}`);
  head.append(node("strong", urgency), node("p", `${plan.official_guidance_first} Nearest hazard buffer: ${plan.nearest_hazard_km ?? "none known"} km. Experimental high-flow score: ${plan.flood_probability == null ? "unavailable" : plan.flood_probability.toFixed(3)}. Not used to invent flooded areas.`, "small"));
  const go = node("div");
  go.append(node("h3", "Experimental direction scores · not travel instructions"));
  for (const o of plan.recommended) {
    go.append(node("div", `${o.direction} · ${o.distance_km} km · score ${o.score}`, "stat"), node("p", o.reasons.join("; "), "muted small"));
  }
  if (!plan.recommended.length) go.append(node("p", "This prototype cannot determine a safe direction. Follow emergency-service instructions.", "old"));
  go.append(node("p", `Avoid: ${plan.avoid_directions.join(", ") || "none flagged"}`, "small"));
  const places = node("div");
  places.append(node("h3", "Nearby assembly points / hospitals / fire stations"));
  for (const p of plan.places.slice(0, 6)) {
    places.append(link(`${p.name} (${p.kind}) · ${p.distance_km} km ${p.bearing}${p.in_hazard_buffer ? " · IN " + p.in_hazard_buffer.toUpperCase() + " BUFFER" : ""} ↗`, p.directions_url));
  }
  if (!plan.places.length) places.append(node("p", "No places loaded (see errors).", "muted small"));
  const info = node("div");
  info.append(node("h3", "Inputs"), ...Object.values(plan.inputs).map(i => node("div", `${i.status.toUpperCase()} · ${i.source}`, i.status === "available" ? "meta" : "meta old")),
    ...plan.errors.map(e => node("p", e, "old")), node("p", plan.basis, "muted small"));
  $("evac-result").replaceChildren(head, go, places, info);
  if (!map) return;
  groups.evac.clearLayers();
  for (const o of plan.recommended) {
    L.polyline([[locationPoint.lat, locationPoint.lon], [o.lat, o.lon]], {color: "#805008", weight: 4, dashArray: "8 6"})
      .bindPopup(`Experimental bearing ${o.direction}, not a road route. Safe travel is unknown.`).addTo(groups.evac);
  }
  for (const t of plan.fire_tracks) {
    L.polyline(t.points, {color: "#ff6b4a", weight: 1}).addTo(groups.evac);
    if (t.observed_heading) {
      L.marker([t.lat, t.lon], {title: `Fire moved ${t.observed_heading} ${t.observed_speed_kmh ?? "?"} km/h`})
        .bindPopup(`Observed shift ${t.observed_shift_km} km towards ${t.observed_heading} over ${t.hours_between_passes} h · downwind ${t.downwind ?? "unknown"}`).addTo(groups.evac);
    }
  }
}
function setMapMode(mode) {
  $("map").closest(".map-panel").dataset.mode = mode;
  const active = {rain: ["rain"], fire: ["fire"], places: ["places"]}[mode];
  for (const id of ["fire", "burns", "flood", "scenes", "water", "rain", "drainage", "river", "places", "fwi"]) {
    $(`show-${id}`).checked = active.includes(id);
  }
  for (const id of ["rain", "fire", "places"]) $(`mode-${id}`).setAttribute("aria-pressed", String(mode === id));
  $("mode-description").textContent = {
    rain: "Smoothed forecast rain—not radar or flood depth.",
    fire: "Fire reports / thermal detections only. Missing reports do not mean no fire.",
    places: "A / S = unverified assembly / shelter. H / F = medical / response. Check authorities for actual evacuation destinations."
  }[mode];
  if (mode === "places") loadFeed("places");
  if (mode !== "rain") stopRainAnimation();
  applyLayers();
}
function fireOutlook(reports, coverage, wr) {
  // Active fire evidence (satellite/EMS reports) is kept separate from weather-based danger.
  const gap = coverage ? "" : " Report coverage incomplete.";
  if (reports.length) {
    const nearest = Math.min(...reports.map(event => event.distance_km));
    return {level: "active", text: `Fire: ACTIVE fire evidence · ${reports.length} report(s), nearest ${nearest} km. Not a confirmed fire edge — follow authorities.${gap}`};
  }
  const none = coverage ? "No active fire in recent EU/NASA satellite reports." : "Active fire reports could not all be checked.";
  if (!wr) return {level: "unknown", text: `Fire: ${none} Fire danger unknown (weather unavailable).`};
  const now = wr.current_fire_weather, next = wr.fire_weather.level;
  const wet = now.rain_mm > 0.5 || now.humidity_pct >= 85 || wr.fire_weather.days_without_rain === 0;
  if (wet && now.level === "low" && ["low", "moderate"].includes(next)) {
    return {level: "very-low", text: `Fire: very unlikely now — wet conditions (${now.rain_mm} mm rain this hour, humidity ${now.humidity_pct}%). ${none}`};
  }
  const level = ["high", "very high"].includes(next) ? "high" : next === "moderate" ? "moderate" : "low";
  return {level, text: `Fire danger (ECMWF weather rule): ${next.toUpperCase()} in next 48 h. ${none}`};
}
function renderQuickStatus() {
  const context = sourceAvailable("emergency") ? feeds.emergency.data : null;
  $("quick-heading").textContent = context ? [context.city || context.region, context.country].filter(Boolean).join(" · ")
    : locationReady ? "Around your selected location" : "Location needed · allow permission or choose an area";
  const rain = canShow("rainfall") ? feeds.rainfall.data : null;
  const samples = rain?.cells.map(cell => cell.current_hour?.rain_mm);
  const complete = samples?.length && samples.every(Number.isFinite);
  const times = rain?.cells.map(cell => cell.current_hour?.valid_at);
  $("quick-rain").textContent = complete
    ? `Rain: ${Math.min(...samples).toFixed(1)}–${Math.max(...samples).toFixed(1)} mm / modelled hour nearby.${feeds.rainfall.status === "stale" ? " STALE—refresh failed." : ""} Ends ${when(times[0])}.`
    : `Rain: ${feeds.rainfall ? "unavailable—conditions unknown." : locationReady ? "loading forecast…" : "waiting for location."}`;
  const fire = eventRecords().filter(event => event.type !== "flood" && !event.older_than_24h &&
    (event.feed_id !== "gdacs" || event.source_reports_current)).concat(
    canShow("cems") ? (feeds.cems.data?.nearby || []).filter(act => act.active &&
      ["fire", "wildfire", "forest fire"].includes(act.category.toLowerCase())) : []);
  const coverage = ["firms", "gdacs", "cems"].every(sourceAvailable);
  const outlook = fireOutlook(fire, coverage, canShow("weather_risk") ? feeds.weather_risk.data : null);
  $("quick-fire").textContent = outlook.text;
  $("quick-fire").dataset.level = outlook.level;
}
function telephoneLink(label, number) {
  const element = node("a", `${label} · ${number}`);
  element.href = `tel:${number}`;
  return element;
}
function renderOfficialSources(context) {
  const box = $("official-sources");
  if (!context) {
    box.replaceChildren(node("p", "Official evacuation points are designated by national or local civil protection. Country not identified yet."),
      link("EU civil-protection authorities by country", "https://civil-protection-humanitarian-aid.ec.europa.eu/what/civil-protection/national-disaster-management-system_en"));
    return;
  }
  box.replaceChildren(node("p", `Official evacuation points in ${context.country} come from these authorities. Places listed by this app are community-mapped (OpenStreetMap), not official.`));
  if (context.primary_number) box.append(telephoneLink(`Call ${context.country} emergency`, context.primary_number));
  for (const authority of [...context.regional_authorities, ...context.national_authorities]) box.append(link(authority.name, authority.url));
}
function renderEmergencyContext() {
  const context = sourceAvailable("emergency") ? feeds.emergency.data : null;
  renderOfficialSources(context);
  const call = $("emergency-call"), authorities = $("authority-links"), details = $("emergency-details");
  authorities.replaceChildren();
  details.replaceChildren();
  call.href = "#guidance";
  call.textContent = "Emergency help";
  call.removeAttribute("aria-label");
  if (!context) {
    const message = feeds.emergency ? "Country lookup failed or is stale. No local number or authority assumed."
      : locationReady ? "Identifying country and region…" : "Allow location or choose an area to identify local contacts.";
    $("authority-location").textContent = message;
    $("emergency-notice").textContent = feeds.emergency ? "Contacts unavailable or stale; no number assumed."
      : locationReady ? "Finding country-specific contacts…" : "Choose an area to see local contacts.";
    if (feeds.emergency?.error) details.append(node("p", feeds.emergency.error, "old"));
    authorities.append(link("EU emergency-number information (not a local alert)", "https://europa.eu/youreurope/citizens/travel/security-and-emergencies/emergency/index_en.htm"));
    return;
  }
  const area = [context.city, context.region, context.country].filter((value, i, all) => value && all.indexOf(value) === i).join(" · ");
  $("area-label").textContent = area;
  $("authority-location").textContent = `${area} · approximate administrative match`;
  $("emergency-notice").textContent = context.primary_number
    ? `${context.country}: emergency ${context.primary_number}. Confirm country.`
    : `${context.country}: number not configured.`;
  if (context.primary_number) {
    call.textContent = `Call ${context.primary_number}`;
    call.href = `tel:${context.primary_number}`;
    call.setAttribute("aria-label", `Call ${context.primary_number} — emergency number for ${context.country}. Calls connect at your phone's location.`);
  }
  for (const authority of context.regional_authorities) authorities.append(link(`Local / regional · ${authority.name}`, authority.url));
  if (!context.regional_authorities.length) authorities.append(node("p", "Local authority link not configured for this region. National sources below—not a local alert feed.", "old"));
  for (const authority of context.national_authorities) authorities.append(link(`National · ${authority.name}`, authority.url));
  if (!context.national_authorities.length) authorities.append(node("p", "No curated official authority available for this country.", "old"));
  for (const contact of context.additional_numbers) {
    details.append(telephoneLink(contact.service, contact.number), link("Official number source", contact.source_url));
  }
  if (context.number_source) details.append(link("Official emergency-number reference", context.number_source));
  details.append(node("p", `Contact list reviewed ${context.contacts_reviewed}. Links are not a live warning feed.`),
    node("p", context.message), link(context.location_source, context.location_source_url));
}
function render() {
  $("location-prompt").hidden = locationReady;
  $("refresh").disabled = busy || !locationReady;
  $("fit-area").disabled = !map || !locationReady;
  renderEmergencyContext(); renderQuickStatus(); renderSummary(); renderRecords(); renderSources(); renderMapInspector(); renderMap(); renderRisk();
}
function loadFeed(id, force = false) {
  if (!locationReady) return Promise.resolve();
  if (pendingFeeds.has(id)) return pendingFeeds.get(id).promise;
  if (!force && feeds[id] && !["error", "stale"].includes(feeds[id].status)) return Promise.resolve();
  const requestGeneration = generation;
  const controller = new AbortController();
  const request = {controller, promise: null};
  const timeout = setTimeout(() => controller.abort(), id === "places" ? 25000 : coreSources.includes(id) ? 30000 : 115000);
  checkedAt[id] = new Date().toISOString();
  pendingFeeds.set(id, request);
  request.promise = (async () => {
    try {
      const response = await fetch(`/api/${id}?${new URLSearchParams(locationPoint)}`, {signal: controller.signal});
      const value = await response.json();
      if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
      if (!["available", "stale", "error", "unconfigured"].includes(value.status)) throw new Error("Unrecognized source response");
      if (requestGeneration === generation) feeds[id] = value;
    } catch (error) {
      if (requestGeneration === generation) {
        const previous = feeds[id];
        const hasData = ["available", "stale"].includes(previous?.status) && !!previous?.data;
        feeds[id] = {source: names[id], status: hasData ? "stale" : "error", data: hasData ? previous.data : null,
          fetched_at: previous?.fetched_at || null, last_attempt_at: checkedAt[id],
          error: `Feed request failed: ${error.name === "AbortError" ? "request timed out" : error.message}`};
      }
    } finally {
      clearTimeout(timeout);
      if (pendingFeeds.get(id) === request) pendingFeeds.delete(id);
      if (requestGeneration === generation) render();
    }
  })();
  render();
  return request.promise;
}
async function refresh() {
  if (busy || !locationReady) return;
  busy = true;
  $("refresh").disabled = true;
  $("refresh").setAttribute("aria-busy", "true");
  const currentGeneration = generation;
  const requested = [...new Set([...coreSources, ...Object.keys(feeds)])];
  const requests = new Map(requested.map(id => [id, loadFeed(id, true)]));
  loadDestinations();
  // Rain can paint immediately; terrain, training and Overpass never gate this refresh.
  await Promise.all([requests.get("rainfall"), requests.get("emergency")]);
  if (currentGeneration !== generation) return;
  busy = false;
  $("refresh").disabled = false;
  $("refresh").setAttribute("aria-busy", "false");
  $("refresh-clock").textContent = `Rain / location checked ${new Date().toLocaleTimeString()}; other sources load independently`;
  renderMapInspector();
}
$("place").addEventListener("change", () => {
  if ($("place").value === "custom") return;
  const [lat, lon] = $("place").value.split(",");
  $("latitude").value = lat;
  $("longitude").value = lon;
});
for (const id of ["latitude", "longitude"]) $(id).addEventListener("input", () => { $("place").value = "custom"; });
$("location-form").addEventListener("submit", event => {
  event.preventDefault();
  if (!$("latitude").value.trim() || !$("longitude").value.trim()) return;
  const lat = Number($("latitude").value), lon = Number($("longitude").value);
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat) > 80 || Math.abs(lon) > 178) return;
  if (!applyingDeviceLocation) {
    stopLocationWatch();
    locationOrigin = "manual";
    deviceAccuracy = null;
  }
  clearDestinations("Area changed. Checking candidates for the new location…");
  locationPoint = {lat, lon};
  locationReady = true;
  generation++;
  stopRainAnimation();
  rainView = null;
  for (const request of pendingFeeds.values()) request.controller.abort();
  pendingFeeds.clear();
  busy = false;
  feeds = {};
  selectedMapItem = null;
  $("rain-window").value = "hour";
  $("rain-hour").value = "0";
  $("global-error").hidden = true;
  $("evac-result").replaceChildren(node("p", "Area changed. Previous direction scores were cleared.", "muted small"));
  const place = $("place").selectedOptions[0].textContent;
  setLocationStatus("Selected area; forecasts are model estimates, not a live rain sensor.");
  $("area-label").textContent = `${place} · ${lat.toFixed(4)}, ${lon.toFixed(4)}`;
  if (map) { map.closePopup(); groups.evac.clearLayers(); map.setView([lat, lon], 10); pointMarker.setLatLng([lat, lon]).addTo(map); }
  render();
  refresh();
  if ($("show-places").checked) loadFeed("places");
  if (document.querySelector(".map-inspector").open) loadTerrain();
  if (document.querySelector(".research-details").open) loadResearch();
});
for (const id of ["rain", "fire", "places"]) $(`mode-${id}`).addEventListener("click", () => setMapMode(id));
for (const id of ["fire", "burns", "flood", "scenes", "water", "rain", "drainage", "river", "places", "fwi"]) $(`show-${id}`).addEventListener("change", () => {
  $("map").closest(".map-panel").dataset.mode = "custom";
  for (const mode of ["rain", "fire", "places"]) $(`mode-${mode}`).setAttribute("aria-pressed", "false");
  $("mode-description").textContent = "Custom layers selected. Rain and terrain scenarios are not measured flood depth.";
  if (!$("show-rain").checked) stopRainAnimation();
  if ($("show-places").checked) loadFeed("places");
  if ($("show-water").checked || $("show-drainage").checked) loadFeed("ponding");
  if ($("show-river").checked) loadFeed("glofas");
  if ($("show-burns").checked) loadFeed("effis");
  if ($("show-scenes").checked) loadFeed("satellite");
  applyLayers();
});
$("scenario-area").addEventListener("input", renderMapInspector);
$("rain-window").addEventListener("change", () => { stopRainAnimation(); renderMapInspector(); renderMap(); });
$("rain-play").addEventListener("click", playRainAnimation);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopRainAnimation();
    stopLocationWatch();
    clearDestinations("App was hidden. Recheck location and current candidates when returning.");
  } else if (locationOrigin === "device") locateUser();
  else if (locationReady) loadDestinations();
});
motionPreference.addEventListener("change", stopRainAnimation);
$("rain-hour").addEventListener("input", () => {
  stopRainAnimation();
  $("rain-window").value = "hour";
  if (map) map.closePopup();
  renderMapInspector();
  renderMap();
});
function loadTerrain() {
  for (const id of ["ponding", "glofas", "weather_risk"]) loadFeed(id);
}
function loadResearch() {
  for (const id of Object.keys(names)) loadFeed(id);
}
document.querySelector(".map-inspector").addEventListener("toggle", event => { if (event.target.open) loadTerrain(); });
document.querySelector(".research-details").addEventListener("toggle", event => { if (event.target.open) loadResearch(); });
function selectPlaceFilter(filter) {
  placeFilter = filter;
  $("places-assembly").setAttribute("aria-pressed", String(filter === "assembly"));
  $("places-services").setAttribute("aria-pressed", String(filter === "services"));
  loadFeed("places");
  renderMapInspector();
  renderMap();
}
$("places-assembly").addEventListener("click", () => selectPlaceFilter("assembly"));
$("places-services").addEventListener("click", () => selectPlaceFilter("services"));
$("runoff-factor").addEventListener("input", () => {
  $("runoff-label").textContent = $("runoff-factor").value + "%";
  if (map) map.closePopup();
  renderMapInspector();
  renderMap();
});
$("fit-area").addEventListener("click", () => {
  if (!map || !locationReady) return;
  const cells = canShow("ponding") ? feeds.ponding.data.cells : [];
  if ($("show-rain").checked && rainView?.mesh) {
    const b = rainView.mesh.bounds;
    map.fitBounds([[b.south, b.west], [b.north, b.east]], {padding: [20, 20], maxZoom: 10});
  } else if (cells.length) map.fitBounds(cells.map(c => [c.lat, c.lon]), {padding: [30, 30], maxZoom: 13});
  else map.setView([locationPoint.lat, locationPoint.lon], 12);
});
$("evacuate").addEventListener("click", evacuate);
function setLocationStatus(message) {
  $("location-status").textContent = message;
  $("location-prompt-message").textContent = message;
  $("location-prompt").hidden = locationReady;
}
function stopLocationWatch() {
  if (locationWatch !== null) navigator.geolocation?.clearWatch(locationWatch);
  locationWatch = null;
  locationEpoch++;
  $("stop-location").hidden = true;
}
function applyDevicePosition(pos, tracking = false) {
  const {latitude: lat, longitude: lon, accuracy} = pos.coords;
  if (![lat, lon, accuracy].every(Number.isFinite) || Math.abs(lat) > 80 || Math.abs(lon) > 178 ||
      accuracy < 0 || accuracy > 100000) {
    locationReady = false;
    stopLocationWatch();
    setLocationStatus("Device returned an unsupported or invalid location. Choose an area; no substitute assumed.");
    clearDestinations("Invalid device location. No current candidate assessment.");
    return false;
  }
  const now = Date.now();
  if (pos.timestamp && Math.abs(now - pos.timestamp) > 60000) {
    locationReady = false;
    stopLocationWatch();
    setLocationStatus("Device location is too old. Request a fresh location or choose an area.");
    clearDestinations("Device location is stale. No current candidate assessment.");
    return false;
  }
  const moved = Math.hypot((lat - locationPoint.lat) * 111320,
    (lon - locationPoint.lon) * 111320 * Math.cos(lat * Math.PI / 180));
  if (tracking && now - lastDeviceUpdate < 60000) return true;
  if (tracking && moved < Math.max(250, accuracy) && now - lastDeviceUpdate < 300000) return true;
  $("latitude").value = lat.toFixed(4);
  $("longitude").value = lon.toFixed(4);
  $("place").value = "custom";
  locationOrigin = "device";
  deviceAccuracy = accuracy;
  lastDeviceUpdate = now;
  applyingDeviceLocation = true;
  try { $("location-form").requestSubmit(); }
  finally { applyingDeviceLocation = false; }
  setLocationStatus(`Your device location · accuracy about ${Math.round(accuracy)} m. Coordinates are shared with data and administrative-lookup providers; cached in memory, not stored as disk-based location history.`);
  return true;
}
function locateUser() {
  stopLocationWatch();
  const requestedGeneration = generation;
  const requestedEpoch = locationEpoch;
  setLocationStatus("Requesting your location—your browser may ask permission. You can also choose an area.");
  if (!navigator.geolocation) {
    setLocationStatus("Location is unavailable. Choose a preset, enter coordinates, or click the map. No fixed area loaded.");
    return;
  }
  navigator.geolocation.getCurrentPosition(pos => {
    if (requestedGeneration !== generation || requestedEpoch !== locationEpoch || document.hidden) return;
    if (!applyDevicePosition(pos) || !navigator.geolocation.watchPosition) return;
    locationWatch = navigator.geolocation.watchPosition(update => {
      if (requestedEpoch === locationEpoch && !document.hidden) applyDevicePosition(update, true);
    }, () => {
      if (requestedEpoch !== locationEpoch) return;
      stopLocationWatch();
      locationReady = false;
      setLocationStatus("Location updates unavailable. Last position is not live; retry location or choose an area.");
      clearDestinations("Location updates failed. No current device-based candidate assessment.");
    }, {enableHighAccuracy: true, timeout: 15000, maximumAge: 30000});
    $("stop-location").hidden = false;
  }, () => {
    if (requestedGeneration === generation && requestedEpoch === locationEpoch) {
      locationReady = false;
      setLocationStatus("Location permission denied, timed out or unavailable. Choose an area manually; no fixed location was substituted.");
      clearDestinations("Device location unavailable. Choose an area; no safe destination can be determined.");
    }
  }, {enableHighAccuracy: true, timeout: 15000, maximumAge: 60000});
}
$("use-location").addEventListener("click", locateUser);
$("retry-location").addEventListener("click", locateUser);
$("stop-location").addEventListener("click", () => {
  stopLocationWatch();
  locationOrigin = "manual";
  deviceAccuracy = null;
  clearDestinations("Location updates stopped. Selected point retained, not your live position.");
  $("destination-origin").textContent = "Updates stopped. Distances refer to the last selected point, not a live device location.";
  setLocationStatus("Location updates stopped. Use Find my location to resume.");
});
$("choose-area").addEventListener("click", () => {
  $("location-settings").open = true;
  $("location-settings").scrollIntoView({block: "start", behavior: motionPreference.matches ? "auto" : "smooth"});
  $("place").focus({preventScroll: true});
});
$("nearby-action").addEventListener("click", loadDestinations);
$("location-options").addEventListener("click", () => { $("location-settings").open = true; });
$("refresh").addEventListener("click", refresh);
window.addEventListener("beforeinstallprompt", event => {
  event.preventDefault();
  installPrompt = event;
  $("install-app").hidden = false;
});
$("install-app").addEventListener("click", async () => {
  if (!installPrompt) return;
  try {
    await installPrompt.prompt();
    const choice = await installPrompt.userChoice;
    $("install-status").textContent = choice.outcome === "accepted"
      ? "Installation accepted by your browser." : "Installation dismissed. You can continue in the browser.";
  } catch (error) {
    $("install-status").textContent = `Installation could not start: ${error.message}. Use your browser's install menu.`;
  } finally {
    installPrompt = null;
    $("install-app").hidden = true;
  }
});
window.addEventListener("offline", () => {
  clearDestinations("You are offline. No current destination assessment. Follow local emergency services.");
  $("global-error").textContent = "Offline: displayed weather may be outdated. No live hazard or destination assessment.";
  $("global-error").hidden = false;
});
window.addEventListener("online", () => {
  if ($("global-error").textContent.startsWith("Offline:")) $("global-error").hidden = true;
  if (locationReady) refresh();
});
if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("/sw.js").catch(error => {
    $("install-status").textContent = `Offline help could not be enabled: ${error.message}. The online prototype remains available.`;
  });
}
$("latitude").value = "";
$("longitude").value = "";
$("place").value = "custom";
initMap();
render();
refresh();
locateUser();
setInterval(refresh, 300000);
