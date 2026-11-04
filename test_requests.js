"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(__dirname + "\\static\\app.js", "utf8");
const requests = new Map(), elements = {};
let painted = 0;
const context = vm.createContext({
  feeds: {}, names: {rainfall: "Rain", emergency: "Contacts", firms: "FIRMS", gdacs: "GDACS", cems: "CEMS"},
  pendingFeeds: new Map(), coreSources: ["rainfall", "emergency", "firms", "gdacs", "cems"],
  locationReady: true, locationPoint: {lat: 42.87, lon: 25.32}, generation: 1, busy: false, checkedAt: {},
  AbortController, URLSearchParams, Date, Promise, Map, Set,
  setTimeout: () => 1, clearTimeout: () => {},
  $: id => elements[id] ||= {disabled: false, textContent: "", attrs: {},
    setAttribute(key, value) {this.attrs[key] = value;}},
  render: () => {painted++;}, renderMapInspector: () => {}, loadDestinations: () => {},
  fetch: (url, options) => new Promise((resolve, reject) => {
    const id = url.match(/\/api\/([^?]+)/)[1];
    requests.set(id, {resolve: value => resolve({ok: true, json: async () => value}), reject});
    options.signal.addEventListener("abort", () => reject(Object.assign(new Error("Aborted"), {name: "AbortError"})));
  })
});
vm.runInContext(source.slice(source.indexOf("function loadFeed("), source.indexOf('$("place").addEventListener')), context);
(async () => {
  const refresh = vm.runInContext("refresh()", context);
  assert.equal(elements.refresh.attrs["aria-busy"], "true");
  assert.deepEqual([...requests.keys()], ["rainfall", "emergency", "firms", "gdacs", "cems"]);
  assert.equal(requests.has("ponding"), false);
  assert.equal(requests.has("map"), false);
  const rain = vm.runInContext('loadFeed("rainfall")', context);
  assert.equal(rain, context.pendingFeeds.get("rainfall").promise);
  requests.get("rainfall").resolve({status: "available", data: {cells: []}});
  await rain;
  assert.equal(context.feeds.rainfall.status, "available");
  assert(painted > 0);
  assert.equal(context.pendingFeeds.has("cems"), true);
  requests.get("emergency").resolve({status: "available", data: {}});
  await refresh;
  assert.equal(context.busy, false);
  assert.equal(elements.refresh.disabled, false);
  assert.equal(elements.refresh.attrs["aria-busy"], "false");
  // Old area requests cannot publish old observations after an area change.
  vm.runInContext("generation++;feeds={};for(const request of pendingFeeds.values())request.controller.abort();pendingFeeds.clear()", context);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(Object.keys(context.feeds), []);
  const places = vm.runInContext('loadFeed("places")', context);
  requests.get("places").reject(new Error("Provider failed"));
  await places;
  assert.equal(context.feeds.places.status, "error");
  assert.match(context.feeds.places.error, /Provider failed/);
  console.log("Fast rain paint, independent evidence, request deduplication, area aborts and errors passed.");
})().catch(error => {console.error(error);process.exitCode = 1;});
