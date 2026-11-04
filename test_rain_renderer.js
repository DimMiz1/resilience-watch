"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(__dirname + "\\static\\rain_renderer.js", "utf8"), context);
const renderer = context.RainRaster;
const samples = [];
for (let row = 0; row < 3; row++) for (let col = 0; col < 3; col++) {
  samples.push({row, col, model_cell: {lat: 44 - row, lon: 23 + col}});
}
const mesh = renderer.build({samples, mesh_rows: 3, mesh_cols: 3}, 64);
assert.equal(mesh.triangles.length, 8);
const constant = samples.map(() => 5);
assert(Math.abs(renderer.valueAt(mesh, constant, 43.4, 23.7) - 5) < 1e-6);
assert.equal(renderer.valueAt(mesh, constant, 43, 30), null);
const dry = renderer.pixels(mesh, samples.map(() => 0));
assert(dry.every((value, index) => index % 4 !== 3 || value === 0));
const unknown = renderer.pixels(mesh, samples.map(() => null));
assert(unknown.every(value => value === 0));
assert.equal(renderer.valueAt(mesh, samples.map(() => null), 43.4, 23.7), null);
const partial = [...constant];
partial[0] = null;
assert.equal(renderer.valueAt(mesh, partial, 43.8, 23.2), null);
assert(Math.abs(renderer.valueAt(mesh, partial, 42.2, 24.8) - 5) < 1e-6);
const left = samples.map(sample => sample.col === 0 ? 10 : 0);
const right = samples.map(sample => sample.col === 2 ? 10 : 0);
function centre(pixels) {
  let mass = 0, weighted = 0;
  for (let pixel = 0; pixel < 64 * 64; pixel++) {
    const alpha = pixels[pixel * 4 + 3];
    mass += alpha; weighted += (pixel % 64) * alpha;
  }
  return weighted / mass;
}
assert(centre(renderer.pixels(mesh, right)) > centre(renderer.pixels(mesh, left)) + 20);
assert.deepEqual(Array.from(renderer.colour(20)).slice(0, 3), [76, 20, 109]);
assert.throws(() => renderer.build({samples: [], mesh_rows: 1, mesh_cols: 1}), /unavailable/);
const duplicateFirstRow = samples.map(sample => ({
  ...sample, model_cell: {...sample.model_cell, lat: sample.row < 2 ? 44 : 42}
}));
assert.equal(renderer.build({samples: duplicateFirstRow, mesh_rows: 3, mesh_cols: 3}, 64).triangles.length, 4);
const collinear = samples.map(sample => ({
  ...sample, model_cell: {lat: 44 - sample.row, lon: 23 + sample.row}
}));
assert.throws(() => renderer.build({samples: collinear, mesh_rows: 3, mesh_cols: 3}, 64), /interpolation mesh/);
const invalidCoordinates = samples.map(sample => ({...sample, model_cell: {lat: null, lon: 23}}));
assert.throws(() => renderer.build({samples: invalidCoordinates, mesh_rows: 3, mesh_cols: 3}, 64), /coordinates/);
// Native sample values remain exact; between-centre estimates stay within source bounds.
const ramp = samples.map(sample => sample.col * 5);
assert(Math.abs(renderer.valueAt(mesh, ramp, 43, 24) - 5) < 1e-6);
assert(Math.abs(renderer.valueAt(mesh, ramp, 43.4, 23.7) - 3.5) < 1e-6);
console.log("Forecast raster: linear interpolation, moving frames, native values, dry/unknown masks and no extrapolation passed.");
