"use strict";
// Piecewise-linear interpolation within the supplied model mesh, never outside it.
globalThis.RainRaster = (() => {
  const mercator = lat => Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360));
  const latitude = value => Math.atan(Math.sinh(value)) * 180 / Math.PI;
  function barycentric(triangle, x, y) {
    const [a, b, c] = triangle;
    const denominator = (b.y - c.y) * (a.x - c.x) + (c.x - b.x) * (a.y - c.y);
    if (Math.abs(denominator) < 1e-12) return null;
    const u = ((b.y - c.y) * (x - c.x) + (c.x - b.x) * (y - c.y)) / denominator;
    const v = ((c.y - a.y) * (x - c.x) + (a.x - c.x) * (y - c.y)) / denominator;
    const w = 1 - u - v;
    return Math.min(u, v, w) >= -1e-7 ? [u, v, w] : null;
  }
  function build(data, size = 192) {
    const samples = data.samples;
    const rows = data.mesh_rows, cols = data.mesh_cols;
    if (!Array.isArray(samples) || !Number.isInteger(rows) || !Number.isInteger(cols) ||
        rows < 2 || cols < 2 || samples.length !== rows * cols) {
      throw new Error("Forecast mesh is unavailable; reload the rain feed.");
    }
    if (samples.some(sample => !Number.isFinite(sample.model_cell?.lon) ||
        !Number.isFinite(sample.model_cell?.lat) || Math.abs(sample.model_cell.lon) > 180 ||
        Math.abs(sample.model_cell.lat) >= 85.051129)) {
      throw new Error("Forecast centres have invalid or unsupported map coordinates.");
    }
    const grid = new Map(samples.map((sample, index) => [`${sample.row}-${sample.col}`, {
      x: sample.model_cell.lon, y: sample.model_cell.lat, index
    }]));
    const points = [...grid.values()];
    const bounds = {
      west: Math.min(...points.map(p => p.x)), east: Math.max(...points.map(p => p.x)),
      south: Math.min(...points.map(p => p.y)), north: Math.max(...points.map(p => p.y))
    };
    if (bounds.west === bounds.east || bounds.south === bounds.north) throw new Error("Forecast centres do not span an area.");
    const triangles = [];
    for (let row = 0; row < rows - 1; row++) {
      for (let col = 0; col < cols - 1; col++) {
        const a = grid.get(`${row}-${col}`), b = grid.get(`${row}-${col + 1}`);
        const c = grid.get(`${row + 1}-${col}`), d = grid.get(`${row + 1}-${col + 1}`);
        if (![a, b, c, d].every(Boolean)) throw new Error("Forecast mesh has missing locations.");
        for (const t of [[a, b, c], [b, d, c]]) {
          if (barycentric(t, t[0].x, t[0].y)) triangles.push(t);
        }
      }
    }
    if (!triangles.length) throw new Error("Forecast centres cannot form an interpolation mesh.");
    const northM = mercator(bounds.north), southM = mercator(bounds.south);
    const weights = new Float32Array(size * size * 3);
    const indices = new Int16Array(size * size * 3).fill(-1);
    // Precompute spatial weights once; every subsequent forecast frame is O(pixels).
    for (const triangle of triangles) {
      const xs = triangle.map(p => (p.x - bounds.west) / (bounds.east - bounds.west) * size);
      const ys = triangle.map(p => (northM - mercator(p.y)) / (northM - southM) * size);
      const left = Math.max(0, Math.floor(Math.min(...xs)));
      const right = Math.min(size - 1, Math.ceil(Math.max(...xs)));
      const top = Math.max(0, Math.floor(Math.min(...ys)));
      const bottom = Math.min(size - 1, Math.ceil(Math.max(...ys)));
      for (let py = top; py <= bottom; py++) {
        const y = latitude(northM - (py + 0.5) / size * (northM - southM));
        for (let px = left; px <= right; px++) {
          const x = bounds.west + (px + 0.5) / size * (bounds.east - bounds.west);
          const w = barycentric(triangle, x, y);
          if (!w) continue;
          const offset = (py * size + px) * 3;
          for (let k = 0; k < 3; k++) {indices[offset + k] = triangle[k].index; weights[offset + k] = w[k];}
        }
      }
    }
    return {bounds, size, triangles, indices, weights};
  }
  function weighted(values, indices, weights, offset) {
    let amount = 0;
    for (let k = 0; k < 3; k++) {
      const value = values[indices[offset + k]];
      if (!Number.isFinite(value) || value < 0) return null;
      amount += value * weights[offset + k];
    }
    return amount;
  }
  function colour(amount, hourly = true) {
    const bins = hourly ? [0, 0.1, 1, 3, 6, 10, 20] : [0, 1, 10, 30, 60, 100, 150];
    const palette = [[216,243,255], [165,220,250], [84,178,244], [51,124,242], [102,84,217], [150,53,198], [76,20,109]];
    let high = bins.findIndex(value => amount <= value);
    if (high < 0) return [...palette[6], 190];
    if (high === 0) return [...palette[0], 0];
    const f = (amount - bins[high - 1]) / (bins[high] - bins[high - 1]);
    const rgb = palette[high - 1].map((value, k) => Math.round(value + f * (palette[high][k] - value)));
    // Fade only rain intensity near zero, never invent rain beyond the sampled mesh.
    return [...rgb, Math.round(Math.min(1, amount / bins[1]) * 190)];
  }
  function pixels(mesh, values, hourly = true) {
    const output = new Uint8ClampedArray(mesh.size * mesh.size * 4);
    for (let pixel = 0; pixel < mesh.size * mesh.size; pixel++) {
      if (mesh.indices[pixel * 3] < 0) continue;
      const amount = weighted(values, mesh.indices, mesh.weights, pixel * 3);
      if (amount !== null) output.set(colour(amount, hourly), pixel * 4);
    }
    return output;
  }
  function valueAt(mesh, values, lat, lon) {
    for (const triangle of mesh.triangles) {
      const w = barycentric(triangle, lon, lat);
      if (w) return weighted(values, triangle.map(p => p.index), w, 0);
    }
    return null;
  }
  return {build, pixels, valueAt, colour};
})();
