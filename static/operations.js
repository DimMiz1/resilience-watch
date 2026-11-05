"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const fmt = (n) => (n === null || n === undefined ? "–" : Math.round(n).toLocaleString("en-GB"));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const COLORS = {open: "#777", suspected: "#c9a000", reported: "#d2691e", confirmed: "#a32822", reopened: "#1f7a3a"};
  const done = new Set();
  let map, markers = {}, state = null, loadedAt = 0;

  const table = (head, rows) => rows.length
    ? `<table><thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`
    : `<p class="muted">None.</p>`;
  const tag = (kind) => `<span class="pill tag-${kind}">${kind}</span>`;

  function statusOf(id) {
    const inc = state && state.incidents.find((i) => i.crossing === id);
    return inc ? inc.status : "open";
  }

  function paintMap() {
    for (const [id, m] of Object.entries(markers)) {
      const s = statusOf(id);
      m.setStyle({color: COLORS[s], fillColor: COLORS[s], radius: s === "open" ? 5 : 9});
    }
  }

  function render() {
    const s = state, imp = s.impact;
    const m = imp ? imp.modelled : {};
    const elapsed = (Date.now() - loadedAt) / 3.6e6;
    const isolatedNow = imp ? m.population_disconnected || 0 : 0;
    $("mode").textContent = s.operator_auth
      ? "Operator key required to confirm or reopen. Anyone may flag or report."
      : "Demonstration mode: no operator key is set, so anyone can confirm closures. Incidents reset when the server restarts.";
    $("key-label").hidden = !s.operator_auth;
    $("summary").innerHTML = [
      ["Confirmed closures", s.counts.confirmed, "observed"],
      ["Unconfirmed reports / risk flags", s.counts.reported + s.counts.suspected, "observed"],
      ["People cut off from emergency care", isolatedNow, "modelled"],
      ["People losing timely access, incl. cut off", imp ? m.population_losing_access : 0, "modelled"],
      ["Isolation person-hours so far", s.isolation_person_hours + isolatedNow * elapsed, "modelled"],
    ].map(([k, v, t]) => `<div class="figure">${tag(t)}<b>${fmt(v)}</b>${k}</div>`).join("");

    $("actions").innerHTML = s.actions.length ? s.actions.map((a, i) => {
      const key = a.text;
      return `<li class="${a.priority}${done.has(key) ? " done" : ""}"><input type="checkbox" aria-label="Mark done" data-key="${esc(key)}" ${done.has(key) ? "checked" : ""}>
        <div><span class="pill p-${a.priority}">${a.priority}</span> ${esc(a.text)}</div></li>`;
    }).join("") : `<li>No active incidents. Monitoring continues.</li>`;

    $("repair").innerHTML = table(["#", "Crossing", "Reconnects (people)", "Restores access (people)"],
      s.repair_priority.map((r, i) => [i + 1, esc(r.name), fmt(r.reconnects_people), fmt(r.restores_access_people)]));

    $("cost").innerHTML = table(["Crossing", "Closed for", "Detour", "Person-hours lost", "Rate / hour", "Projected at expected reopening"],
      s.cost.map((c) => {
        const h = c.hours_closed + elapsed;
        const ph = c.rate_per_hour ? c.rate_per_hour * h : null;
        return [esc(c.name), `${h.toFixed(1)} h`,
          c.detour_min === null ? "no route (cut off)" : `+${c.detour_min} min`,
          ph !== null ? `${fmt(ph)} ${tag("modelled")}` : c.trips_prevented ? `${fmt(c.trips_prevented)} trips prevented` : `<span class="muted">${esc(c.needs || "–")}</span>`,
          c.rate_per_hour ? fmt(c.rate_per_hour) : "–",
          c.projected_person_hours ? fmt(c.projected_person_hours) : "–"];
      }));

    $("settlements").innerHTML = imp ? table(["Settlement", "People", "Emergency care before", "After", "New route"],
      imp.settlements.map((p) => [esc(p.name), fmt(p.population), `${p.hospital_before ?? "–"} min`,
        p.disconnected ? "<b>cut off</b>" : `${p.hospital_after} min (+${p.hospital_delta})`,
        p.disconnected ? "–" : esc([p.hospital_after_name, (p.route_after || []).join(", ")].filter(Boolean).join(" via "))])) : `<p class="muted">No confirmed closures.</p>`;

    $("log").innerHTML = table(["Incident", "Crossing", "Status", "History"],
      s.incidents.map((i) => [i.id, esc(i.name), `<span class="pill s-${i.status}">${i.status}</span>`,
        i.history.map((h) => `${h.status} ${new Date(h.at).toLocaleString("en-GB")}${h.note ? `: ${esc(h.note)}` : ""}`).join("<br>")]));
    const when = (t) => (t ? new Date(t).toLocaleString("en-GB", {dateStyle: "short", timeStyle: "short"}) : "–");
    $("sources").innerHTML = table(["Source", "Status", "Last good data", "Result"],
      Object.entries(s.sources || {}).map(([name, h]) => {
        const st = h.status || "not checked yet";
        return [esc(name), `<span class="pill h-${st.split(" ")[0]}">${esc(st)}</span>`, when(h.last_ok),
          esc(h.error ? `${h.error}. Last good data kept; other sources and manual reports still work.` : h.detail || "")];
      }));
    paintMap();
  }

  async function refresh() {
    try {
      state = await (await fetch("/api/incidents")).json();
      loadedAt = Date.now();
      render();
    } catch (e) { $("status-line").textContent = "Could not load incidents. Retrying."; }
  }

  async function post(path, body) {
    const res = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    state = data; loadedAt = Date.now(); render();
  }

  async function send(status) {
    const body = {crossing: $("crossing").value, status, note: $("note").value.trim(), key: $("key").value || undefined};
    if ($("at").value) body.at = new Date($("at").value).toISOString();
    for (const [id, f] of [["expected", "expected_hours"], ["vehicles", "vehicles_per_day"], ["occupancy", "occupancy"]])
      if ($(id).value) body[f] = Number($(id).value);
    try {
      await post("/api/incidents", body);
      $("status-line").textContent = `${$("crossing").selectedOptions[0].text}: ${status}. Consequences recalculated.`;
    } catch (e) { $("status-line").textContent = `Not saved: ${e.message}`; }
  }

  async function init() {
    const data = await (await fetch("/api/infrastructure/bridges")).json();
    map = L.map("ops-map").setView([42.25, 27.7], 10);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {maxZoom: 18, attribution: "© OpenStreetMap contributors"}).addTo(map);
    $("crossing").innerHTML = data.crossings.map((c) =>
      `<option value="${c.id}">${esc(c.name)} (${c.id}) · ${fmt(c.population_losing_access)} people exposed</option>`).join("");
    for (const c of data.crossings) {
      markers[c.id] = L.circleMarker([c.lat, c.lon], {weight: 2, fillOpacity: .8}).addTo(map)
        .bindTooltip(`${c.name} (${c.id})`)
        .on("click", () => { $("crossing").value = c.id; $("crossing").scrollIntoView({behavior: "smooth", block: "center"}); });
    }
    document.querySelectorAll("button[data-status]").forEach((b) => b.addEventListener("click", () => send(b.dataset.status)));
    $("actions").addEventListener("change", (e) => {
      if (e.target.dataset.key) { e.target.checked ? done.add(e.target.dataset.key) : done.delete(e.target.dataset.key); render(); }
    });
    $("scan").addEventListener("click", () => {
      $("status-line").textContent = "Checking Copernicus EMS, GloFAS and Meteoalarm…";
      post("/api/incidents/scan", {}).then(() => { $("status-line").textContent = "Official sources checked."; })
        .catch((e) => { $("status-line").textContent = e.message; });
    });
    $("demo").addEventListener("click", () => post("/api/incidents/demo", {key: $("key").value || undefined})
      .then(() => { $("status-line").textContent = "Replay loaded: road 9901 closure confirmed 6 hours ago; crossings inside the official EMSR693 area flagged."; })
      .catch((e) => { $("status-line").textContent = e.message; }));
    $("reset").addEventListener("click", () => confirm("Clear all incidents?") && post("/api/incidents/reset", {key: $("key").value || undefined})
      .catch((e) => { $("status-line").textContent = e.message; }));
    await refresh();
    setInterval(refresh, 30000);
    setInterval(() => state && render(), 60000);
  }
  window.addEventListener("load", init);
})();
