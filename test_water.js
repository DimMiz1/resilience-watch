"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const script = fs.readFileSync(__dirname + "\\static\\app.js", "utf8");
const start = script.indexOf("function renderDepthScenario(");
const end = script.indexOf("function renderMapInspector()", start);
assert(start >= 0 && end > start);
function node(tag, text = "", className) {
  return {tag, text, className, style: {}, children: [], append(...items) {this.children.push(...items);}};
}
const elements = {
  "scenario-area": {value: "10000"},
  "runoff-factor": {value: "30"},
  "water-scenario": {textContent: "", children: [], replaceChildren(...items) {this.children = items;}}
};
const context = vm.createContext({$: id => elements[id], node,
  stat: (title, value) => ({title, value}), Number, Math});
vm.runInContext(script.slice(start, end), context);
vm.runInContext("renderDepthScenario({inflow_m3_if_all_runs_off:10000})", context);
const box = elements["water-scenario"];
assert.match(box.children[0].text, /NOT PREDICTED DEPTH/);
assert.equal(box.children[1].children[1].text, "0.30 m");
assert.equal(box.children[1].children[0].style.height, "15%");
elements["runoff-factor"].value = "0";
vm.runInContext("renderDepthScenario({inflow_m3_if_all_runs_off:10000})", context);
assert.equal(box.children[1].children[1].text, "0.00 m");
for (const area of ["0", "", "-1", "99", "10000001", "NaN", "Infinity"]) {
  elements["scenario-area"].value = area;
  vm.runInContext("renderDepthScenario({inflow_m3_if_all_runs_off:10000})", context);
  assert.match(box.textContent, /No scenario calculated/);
}
elements["scenario-area"].value = "100";
elements["runoff-factor"].value = "100";
vm.runInContext("renderDepthScenario({inflow_m3_if_all_runs_off:10000})", context);
assert.equal(box.children[1].children[1].text, "100.00 m");
assert.equal(box.children[1].children[0].style.height, "100%");
vm.runInContext("renderDepthScenario(null)", context);
assert.match(box.textContent, /Select a terrain collector/);
console.log("Water scenario: arithmetic, zero runoff, area bounds, visual cap and unknown depth passed.");
