"use strict";
// Isolate the browser location flow; no device location or permission is accessed.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const script = fs.readFileSync(__dirname + "\\static\\app.js", "utf8");
const start = script.indexOf("function setLocationStatus(");
const end = script.indexOf('$("use-location").addEventListener', start);
assert(start >= 0 && end > start);
function fixture(geolocation) {
  const elements = Object.fromEntries(["location-status", "location-prompt", "location-prompt-message", "latitude", "longitude", "place", "location-form", "stop-location"]
    .map(id => [id, {value: "", textContent: ""}]));
  let submissions = 0;
  elements["location-form"].requestSubmit = () => { submissions++; };
  const context = vm.createContext({
    navigator: {geolocation}, generation: 0, busy: false, locationReady: false,
    document: {hidden: false}, locationPoint: {lat: 42, lon: 25}, locationOrigin: "manual",
    locationWatch: null, locationEpoch: 0, deviceAccuracy: null, applyingDeviceLocation: false,
    lastDeviceUpdate: 0, clearDestinations() {},
    $: id => elements[id], Math, Date, Number
  });
  vm.runInContext(script.slice(start, end), context);
  return {context, elements, submissions: () => submissions};
}
{
  let watcher, cleared, clock = 1000000;
  const f = fixture({
    getCurrentPosition: success => success({coords: {latitude: 42, longitude: 25, accuracy: 12}}),
    watchPosition: success => { watcher = success; return 7; },
    clearWatch: id => { cleared = id; }
  });
  f.context.Date = {now: () => clock};
  vm.runInContext("locateUser()", f.context);
  assert.equal(f.context.locationWatch, 7);
  assert.equal(f.elements["stop-location"].hidden, false);
  watcher({coords: {latitude: 42.01, longitude: 25, accuracy: 12}});
  assert.equal(f.submissions(), 1, "Rapid GPS updates must not flood providers.");
  clock += 61000;
  watcher({coords: {latitude: 42.01, longitude: 25, accuracy: 12}});
  assert.equal(f.submissions(), 2, "Movement must update the chosen area automatically.");
  vm.runInContext("stopLocationWatch()", f.context);
  assert.equal(cleared, 7);
  watcher({coords: {latitude: 43, longitude: 25, accuracy: 12}});
  assert.equal(f.submissions(), 2, "Stopped GPS callbacks must not replace manual location.");
}
{
  const f = fixture({getCurrentPosition: success => success({
    coords: {latitude: NaN, longitude: 25, accuracy: 12}
  })});
  vm.runInContext("locateUser()", f.context);
  assert.equal(f.submissions(), 0);
  assert.match(f.elements["location-status"].textContent, /invalid location/);
}
{
  const f = fixture({getCurrentPosition: success => success({
    coords: {latitude: 42, longitude: 25, accuracy: 12}, timestamp: 1000
  })});
  vm.runInContext("locateUser()", f.context);
  assert.equal(f.submissions(), 0);
  assert.match(f.elements["location-status"].textContent, /too old/);
}
{
  const f = fixture(undefined);
  vm.runInContext("locateUser()", f.context);
  assert.match(f.elements["location-status"].textContent, /No fixed area/);
  assert.equal(f.elements["location-prompt"].hidden, false);
  assert.match(f.elements["location-prompt-message"].textContent, /No fixed area/);
  assert.equal(f.submissions(), 0);
}
{
  const f = fixture({getCurrentPosition: (_success, failure) => failure({code: 1})});
  vm.runInContext("locateUser()", f.context);
  assert.match(f.elements["location-status"].textContent, /no fixed location/);
  assert.match(f.elements["location-prompt-message"].textContent, /denied/);
  assert.equal(f.submissions(), 0);
}
{
  const f = fixture({getCurrentPosition: success => success({
    coords: {latitude: 45.12345, longitude: 9.98765, accuracy: 12}
  })});
  vm.runInContext("locateUser()", f.context);
  assert.equal(f.elements.latitude.value, "45.1234");
  assert.equal(f.elements.longitude.value, "9.9877");
  assert.equal(f.submissions(), 1);
  assert.match(f.elements["location-status"].textContent, /accuracy about 12 m/);
}
{
  let delayed;
  const f = fixture({getCurrentPosition: success => { delayed = success; }});
  vm.runInContext("locateUser(); generation++;", f.context);
  delayed({coords: {latitude: 45, longitude: 9, accuracy: 20}});
  assert.equal(f.submissions(), 0);
}
assert.match(script, /refresh\(\);\s*locateUser\(\);/);
assert.match(script, /if \(busy \|\| !locationReady\) return;/);
console.log("Location-first flow: success, denial, unavailable and late callback checks passed.");
