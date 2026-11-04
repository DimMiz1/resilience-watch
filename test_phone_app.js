"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const root = __dirname + "\\static\\";
const script = fs.readFileSync(root + "app.js", "utf8");
const manifest = JSON.parse(fs.readFileSync(root + "manifest.webmanifest", "utf8"));
assert.equal(manifest.display, "standalone");
assert.equal(manifest.start_url, "/");
assert(manifest.icons.length);
for (const size of [192, 512]) {
  assert(manifest.icons.some(icon => icon.sizes === `${size}x${size}`));
  const png = fs.readFileSync(root + `app-icon-${size}.png`);
  assert.equal(png.readUInt32BE(16), size);
  assert.equal(png.readUInt32BE(20), size);
}

function element(tag = "div", text) {
  return {tag, textContent: text || "", children: [], attrs: {},
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    setAttribute(key, value) { this.attrs[key] = value; },
    addEventListener() {}};
}
const elements = {};
const timers = new Map();
let nextTimer = 0, resolveFetch, signal, calls = 0;
const context = vm.createContext({
  locationReady: true, document: {hidden: false}, locationPoint: {lat: 42, lon: 25},
  locationOrigin: "device", deviceAccuracy: 12, lastDeviceUpdate: Date.now(), generation: 1,
  destinationRequest: null, destinationExpiry: null, map: null,
  AbortController, URLSearchParams, Date, Number, Array, Object, Math,
  $: id => elements[id] ||= element(), node: element, when: value => value,
  link: (text, url) => ({textContent: text, href: url}),
  setTimeout: (callback, delay) => { const id = ++nextTimer; timers.set(id, {callback, delay}); return id; },
  clearTimeout: id => timers.delete(id),
  fetch: (url, options) => {
    calls++;
    assert.match(url, /lat=42&lon=25&accuracy_m=12/);
    signal = options.signal;
    return new Promise(resolve => { resolveFetch = value => resolve({ok: true, json: async () => value}); });
  }
});
vm.runInContext(script.slice(script.indexOf("function clearDestinations("), script.indexOf("async function evacuate(")), context);
function plan() {
  return {schema: "resilience-destinations-v1", location: {lat: 42, lon: 25},
    safety_status: "unknown", status: "assessment_incomplete", generated_at: new Date().toISOString(),
    candidates: [{name: "Assembly", kind: "assembly point", lat: 42.1, lon: 25.1, distance_km: 2,
      safety_status: "unknown", screening: "no_known_buffer_conflict", map_url: "https://www.openstreetmap.org"}],
    excluded: [], origin_in_known_buffer: false, incomplete_inputs: ["effis"], location_uncertain: false,
    inputs: {effis: {source: "EFFIS", status: "error", fetched_at: null, error: "Offline"}},
    limitations: ["Route unknown"], basis: "Known buffers only", message: "Confirm with authorities"};
}
function allText(item) {
  return [item.textContent, ...item.children?.map(allText) || []].join(" ");
}
(async () => {
  const request = vm.runInContext("loadDestinations()", context);
  await vm.runInContext("loadDestinations()", context);
  assert.equal(calls, 1, "Candidate requests must be deduplicated.");
  assert.equal(elements["destination-panel"].attrs["aria-busy"], "true");
  resolveFetch(plan());
  await request;
  assert.match(elements["destination-status"].textContent, /checks incomplete/);
  assert.match(allText(elements["destination-results"]), /Unverified.*unknown/);
  assert.match(allText(elements["destination-evidence"]), /effis/);
  const expiry = [...timers.values()].find(timer => timer.delay <= 300000);
  assert(expiry, "An assessment must expire rather than remain a live-looking result.");
  expiry.callback();
  assert.equal(elements["destination-results"].children.length, 0);
  assert.match(elements["destination-status"].textContent, /expired/);

  const late = vm.runInContext("loadDestinations()", context);
  vm.runInContext('generation++; clearDestinations("Area changed")', context);
  assert.equal(signal.aborted, true);
  resolveFetch(plan());
  await late;
  assert.equal(elements["destination-results"].children.length, 0);

  const stale = vm.runInContext("loadDestinations()", context);
  resolveFetch({...plan(), generated_at: "2000-01-01T00:00:00Z"});
  await stale;
  assert.match(elements["destination-status"].textContent, /Invalid or expired/);
  assert.equal(elements["destination-results"].children.length, 0);

  context.lastDeviceUpdate = 1;
  await vm.runInContext("loadDestinations()", context);
  assert.match(elements["destination-status"].textContent, /Device location is stale/);

  const swEvents = {}, cached = [];
  const sw = vm.createContext({
    self: {location: {origin: "https://example.test"}, clients: {claim: async () => {}},
      addEventListener: (name, handler) => { swEvents[name] = handler; }},
    caches: {open: async () => ({addAll: async urls => cached.push(...urls)}),
      keys: async () => [], match: async path => ({offline: path})},
    URL, Promise, fetch: async () => { throw new Error("Offline"); }
  });
  vm.runInContext(fs.readFileSync(root + "sw.js", "utf8"), sw);
  let work;
  swEvents.install({waitUntil: promise => { work = promise; }});
  await work;
  assert.deepEqual(cached, ["/offline.html", "/app-icon.svg"]);
  let response;
  swEvents.fetch({request: {mode: "cors", url: "https://example.test/api/destinations"},
    respondWith: promise => { response = promise; }});
  assert.equal(response, undefined, "API data must not be intercepted or cached.");
  swEvents.fetch({request: {mode: "navigate", url: "https://example.test/"},
    respondWith: promise => { response = promise; }});
  assert.equal((await response).offline, "/offline.html");
  console.log("Phone app: automatic screening, cancellation, expiry, stale GPS and offline safety passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
