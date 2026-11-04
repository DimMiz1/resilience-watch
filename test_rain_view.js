"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(__dirname + "\\static\\app.js", "utf8");
const controls = {"rain-window": {value: "hour"}, "rain-hour": {value: "0"}};
const context = vm.createContext({$: id => controls[id], Number, placeFilter: "assembly",
  feeds: {places: {data: {places: [
    {kind: "hospital"}, {kind: "assembly point"}, {kind: "shelter"}, {kind: "fire station"}
  ]}}}, canShow: () => true});
vm.runInContext(source.slice(source.indexOf("function selectedRain("), source.indexOf("function collectorDetail(")), context);
for (const [amount, colour] of [[0, "#d8f3ff"], [0.1, "#a5dcfa"], [1, "#54b2f4"],
  [3, "#337cf2"], [6, "#6654d9"], [10, "#9635c6"], [20, "#4c146d"]]) {
  assert.equal(vm.runInContext(`rainColor(${amount}, true)`, context), colour);
}
assert.equal(vm.runInContext("rainColor(150, false)", context), "#4c146d");
context.sample = {hourly: [{rain_mm: 1.5}, {rain_mm: null}], current_hour: {rain_mm: 1.5}};
assert.equal(vm.runInContext("selectedRain(sample)", context), 1.5);
controls["rain-hour"].value = "1";
assert.equal(vm.runInContext("selectedRain(sample)", context), null);
assert.equal(vm.runInContext("visiblePlaces().length", context), 2);
context.placeFilter = "services";
assert.equal(vm.runInContext("visiblePlaces().length", context), 2);
assert.equal(vm.runInContext("visiblePlaces()[0].kind", context), "hospital");
console.log("Rain thresholds, hourly unknown values and evacuation/service separation passed.");
