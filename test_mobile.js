"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const script = fs.readFileSync(__dirname + "\\static\\app.js", "utf8");
const start = script.indexOf("function setMapMode(");
const end = script.indexOf("function render() {", start);
const elements = {};
function element() {
  return {textContent: "", children: [], attrs: {}, href: "", checked: false, dataset: {},
    closest() {return this;},
    append(...items) {this.children.push(...items);},
    replaceChildren(...items) {this.children = items;},
    setAttribute(key, value) {this.attrs[key] = value;},
    removeAttribute(key) {delete this.attrs[key];}};
}
const context = vm.createContext({
  $: id => elements[id] ||= element(),
  node: (_tag, text) => Object.assign(element(), {textContent: text}),
  link: (text, href) => Object.assign(element(), {textContent: text, href}),
  feeds: {}, locationReady: false, Number, Math,
  sourceAvailable: id => context.feeds[id]?.status === "available",
  canShow: id => ["available", "stale"].includes(context.feeds[id]?.status),
  eventRecords: () => [], when: value => value,
  loadFeed: () => {},
  stopRainAnimation: () => {},
  applyLayers: () => {}
});
vm.runInContext(script.slice(start, end), context);
for (const mode of ["rain", "fire", "places"]) {
  vm.runInContext(`setMapMode("${mode}")`, context);
  for (const id of ["rain", "fire", "places", "water", "drainage", "burns", "river", "fwi", "scenes", "flood"]) {
    assert.equal(elements[`show-${id}`].checked, id === mode);
  }
  assert.equal(elements[`mode-${mode}`].attrs["aria-pressed"], "true");
}
vm.runInContext("renderQuickStatus();renderEmergencyContext()", context);
assert.match(elements["quick-fire"].textContent, /unknown/);
assert.equal(elements["emergency-call"].href, "#guidance");
function contact(country, number) {
  return {country, primary_number: number, regional_authorities: [], national_authorities: [],
    additional_numbers: [], contacts_reviewed: "2026-11-03", message: "approximate",
    location_source: "OSM", location_source_url: "https://www.openstreetmap.org/copyright"};
}
for (const [country, number] of [["United States", "911"], ["United Kingdom", "999"], ["Bulgaria", "112"]]) {
  context.feeds.emergency = {status: "available", data: contact(country, number)};
  vm.runInContext("renderEmergencyContext()", context);
  assert.equal(elements["emergency-call"].href, `tel:${number}`);
  assert.match(elements["authority-location"].textContent, new RegExp(country));
}
context.feeds.emergency = {status: "stale", data: contact("Bulgaria", "112"), error: "Lookup unavailable"};
vm.runInContext("renderEmergencyContext()", context);
assert.equal(elements["emergency-call"].href, "#guidance");
assert.match(elements["emergency-notice"].textContent, /unavailable or stale/);
context.feeds.emergency = {status: "available", data: contact("Unconfigured", null)};
vm.runInContext("renderEmergencyContext()", context);
assert.equal(elements["emergency-call"].href, "#guidance");
assert.match(elements["emergency-notice"].textContent, /not configured/);
context.locationReady = true;
context.feeds.rainfall = {status: "available", data: {cells: [
  {current_hour: {rain_mm: 2, valid_at: "2026-11-03T12:00"}},
  {current_hour: {rain_mm: 4, valid_at: "2026-11-03T12:00"}}
]}};
context.feeds.firms = {status: "available"};
context.feeds.gdacs = {status: "available"};
context.feeds.cems = {status: "available", data: {nearby: []}};
vm.runInContext("renderQuickStatus()", context);
assert.match(elements["quick-rain"].textContent, /2.0–4.0 mm/);
assert.match(elements["quick-fire"].textContent, /No active fire.*danger unknown/);
const risk = (rain, humidity, next) => ({status: "available", data: {
  current_fire_weather: {level: "low", rain_mm: rain, humidity_pct: humidity},
  fire_weather: {level: next, days_without_rain: 0}}});
context.feeds.weather_risk = risk(3.2, 96, "low");
vm.runInContext("renderQuickStatus()", context);
assert.match(elements["quick-fire"].textContent, /very unlikely now — wet/);
assert.equal(elements["quick-fire"].dataset.level, "very-low");
context.feeds.weather_risk = risk(0, 20, "high");
context.feeds.weather_risk.data.fire_weather.days_without_rain = 25;
vm.runInContext("renderQuickStatus()", context);
assert.match(elements["quick-fire"].textContent, /HIGH in next 48 h/);
context.feeds.weather_risk = risk(3.2, 96, "low");
context.eventRecords = () => [{type: "fire", distance_km: 4.2, feed_id: "firms"}];
vm.runInContext("renderQuickStatus()", context);
assert.match(elements["quick-fire"].textContent, /ACTIVE fire evidence.*4.2 km/);
assert.equal(elements["quick-fire"].dataset.level, "active");
context.eventRecords = () => [];
context.feeds.rainfall.data.cells[0].current_hour.rain_mm = null;
vm.runInContext("renderQuickStatus()", context);
assert.match(elements["quick-rain"].textContent, /unknown/);
console.log("Mobile presets, unknown conditions, country contacts and stale-contact clearing passed.");
