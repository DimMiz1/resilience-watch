"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const root = __dirname + "\\static\\";
const html = fs.readFileSync(root + "index.html", "utf8");
const source = fs.readFileSync(root + "app.js", "utf8");
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
assert.equal(new Set(ids).size, ids.length, "Controls must have unique IDs.");
for (const match of source.matchAll(/\$\("([^"]+)"\)/g)) {
  assert(ids.includes(match[1]), `Missing wired control: ${match[1]}`);
}
for (const layer of ["rain", "fire", "places", "water", "drainage", "burns", "river", "fwi", "scenes", "flood"]) {
  assert(ids.includes("show-" + layer));
}
assert(html.indexOf('<div id="map"') < html.indexOf('class="rain-playback"'),
  "The map must appear before forecast controls on a phone.");
assert(html.includes('class="skip-link"'), "Keyboard users need a skip link.");
assert(html.includes("<noscript>"), "Disabled JavaScript needs an honest explanation.");
assert(ids.includes("location-prompt"), "Location recovery must not be trapped in a closed section.");

const start = source.indexOf("function forecastPosition(");
const end = source.indexOf("function playRainAnimation(", start);
assert(start >= 0 && end > start, "Forecast timing needs a reduced-motion path.");
const context = vm.createContext({});
vm.runInContext(source.slice(start, end), context);
assert.equal(vm.runInContext("forecastPosition(1500, 0, false)", context), 1.5);
assert.equal(vm.runInContext("forecastPosition(1500, 0, true)", context), 0);
assert.equal(vm.runInContext("forecastPosition(4000, 0, true)", context), 2);
assert.equal(vm.runInContext("forecastPosition(90000, 0, false)", context), 23);
assert.equal(vm.runInContext("forecastPosition(2000, 20, true)", context), 21);
const controls = {"fit-area": {disabled: false}};
const view = vm.createContext({
  $: id => controls[id] ||= {},
  locationReady: false, busy: false, map: {},
  renderEmergencyContext() {}, renderQuickStatus() {}, renderSummary() {},
  renderRecords() {}, renderSources() {}, renderMapInspector() {}, renderMap() {}, renderRisk() {}
});
vm.runInContext(source.slice(source.indexOf("function render() {"), source.indexOf("function loadFeed(")), view);
vm.runInContext("render()", view);
assert.equal(controls["fit-area"].disabled, true, "An unknown location must not centre on a fixed fallback.");
view.locationReady = true;
vm.runInContext("render()", view);
assert.equal(controls["fit-area"].disabled, false);
console.log("Product UI: preserved wiring, map-first order, location recovery and reduced-motion timing passed.");
