"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  const fmt = (n) => (n === null || n === undefined ? "unknown" : Number(n).toLocaleString("en-GB"));
  const tag = (kind) => `<span class="tag ${kind}">${kind}</span>`;
  let map, markers = {}, overlay, timer;

  function figure(kind, value, label) {
    return `<div class="figure">${tag(kind)}<b>${esc(value)}</b>${esc(label)}</div>`;
  }

  function render(r) {
    const m = r.modelled, c = r.closed[0];
    $("wording").textContent = `${r.wording} ${c.name} (${c.id}).`;
    const detour = c.detour_min === null ? "No main-road alternative" : `+${c.detour_min} min`;
    $("figures").innerHTML = [
      figure("modelled", fmt(m.population_losing_access), `people lose hospital access within ${m.threshold_min} min`),
      figure("modelled", m.disconnected_settlements.length, `settlements cut off (${fmt(m.population_disconnected)} people)`),
      figure("modelled", detour, "detour for a vehicle using this bridge"),
      figure(m.person_hours_lost === null ? "assumed" : "modelled", m.person_hours_lost === null ? "Needs traffic" : fmt(m.person_hours_lost), "person-hours lost"),
      ...(m.value_of_time_eur === null ? [] : [figure("modelled", `€${fmt(m.value_of_time_eur)}`, "value of lost time")]),
    ].join("");
    const rows = r.settlements.map((s) => `<tr><td>${esc(s.name)}</td><td>${fmt(s.population)}</td>
      <td>${s.disconnected ? '<span class="cut">Cut off</span>' : `${s.hospital_before} → ${s.hospital_after} min`}</td>
      <td>${s.regional_after === null ? '<span class="cut">Cut off</span>' : s.regional_delta ? `+${s.regional_delta} min` : "—"}</td></tr>`).join("");
    $("table").innerHTML = r.settlements.length
      ? `<table><caption class="muted">${tag("modelled")} Settlements whose access changes · population ${tag("observed")} OSM tags</caption>
        <thead><tr><th>Settlement</th><th>People</th><th>Nearest hospital</th><th>To UMBAL Burgas</th></tr></thead><tbody>${rows}</tbody></table>`
      : `<p>${tag("modelled")} No settlement changes access by a minute or more: the network has a nearby alternative.</p>`;
    $("notes").innerHTML = r.assumed.notes.map((n) => `<li>${esc(n)}</li>`).join("") +
      `<li>Speeds (km/h): ${esc(Object.entries(r.assumed.speeds_kmh).map(([k, v]) => `${k} ${v}`).join(", "))}</li>`;
    if (overlay) overlay.remove();
    overlay = L.layerGroup(r.settlements.map((s) => L.circleMarker([s.lat, s.lon], {
      radius: 7, color: s.disconnected ? "#a32822" : "#61509c", fillOpacity: 0.8,
    }).bindTooltip(`${s.name}: ${s.disconnected ? "cut off" : `+${s.hospital_delta ?? 0} min to hospital`}`))).addTo(map);
    Object.entries(markers).forEach(([id, mk]) => mk.setStyle({color: id === c.id ? "#a32822" : "#0d4d7c", radius: id === c.id ? 10 : 6}));
  }

  async function run() {
    const q = new URLSearchParams({bridge: $("bridge").value, hours: $("hours").value || 24, threshold: $("threshold").value || 30});
    [["journeys", "journeys"], ["occupancy", "occupancy"], ["vot", "value_of_time"]].forEach(([id, key]) => { if ($(id).value) q.set(key, $(id).value); });
    $("wording").textContent = "Computing…";
    const res = await fetch(`/api/infrastructure/scenario?${q}`);
    const body = await res.json();
    if (!res.ok) { $("wording").textContent = body.error || "Scenario failed."; return; }
    render(body);
  }

  function select(id) { $("bridge").value = id; run(); }

  async function init() {
    const data = await (await fetch("/api/infrastructure/bridges")).json();
    $("region").textContent = `${data.region}. ${data.note}`;
    map = L.map("impact-map").setView([42.25, 27.7], 10);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {maxZoom: 18, attribution: "© OpenStreetMap contributors"}).addTo(map);
    data.crossings.forEach((c) => {
      const cut = c.population_disconnected, lost = c.population_losing_access;
      const hint = cut ? ` – cuts off ${fmt(cut)}` : lost ? ` – ${fmt(lost)} lose access` : "";
      $("bridge").add(new Option(`${c.name} (${c.id})${hint}`, c.id));
      markers[c.id] = L.circleMarker([c.lat, c.lon], {radius: 6, color: "#0d4d7c", fillOpacity: 0.9})
        .bindTooltip(`${c.name} (${c.id})${hint}`).on("click", () => select(c.id)).addTo(map);
    });
    const first = data.crossings[0];
    $("bridge").addEventListener("change", run);
    document.querySelectorAll("input").forEach((i) => i.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 400); }));
    select(first.id);
  }

  window.addEventListener("load", () => init().catch(() => { $("region").textContent = "Road network could not be loaded."; }));
})();
