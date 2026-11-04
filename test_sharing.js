"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(__dirname + "\\static\\share.js", "utf8");
const html = fs.readFileSync(__dirname + "\\static\\review.html", "utf8");
function fixture(href, clipboard) {
  const elements = {};
  const context = vm.createContext({
    URL, encodeURIComponent, window: {location: {href}}, navigator: {clipboard},
    document: {getElementById: id => elements[id] ||= {
      attrs: {}, value: "", handlers: {}, textContent: "",
      setAttribute(name, value) { this.attrs[name] = value; },
      addEventListener(name, handler) { this.handlers[name] = handler; },
      focus() { this.focused = true; }, select() { this.selected = true; }
    }}
  });
  vm.runInContext(source, context);
  return {context, elements};
}
const hosted = "https://resilience-watch.onrender.com/map?lat=42.123456&lon=25.987654&token=private#position";
const f = fixture(hosted);
assert.equal(f.elements["share-url"].value, "https://resilience-watch.onrender.com/review");
const email = new URL(f.elements["email-app"].href);
assert.equal(email.protocol, "mailto:");
assert.match(email.searchParams.get("body"), /research prototype, not an official warning/);
assert.match(email.searchParams.get("body"), /https:\/\/resilience-watch.onrender.com\/review/);
for (const privateValue of ["42.123456", "25.987654", "private", "position"]) {
  assert(!email.searchParams.get("body").includes(privateValue));
}
for (const url of [
  "http://127.0.0.1:8088/", "https://127.0.0.1/", "https://192.168.1.2/",
  "https://10.0.0.1/", "https://[::1]/", "https://[::ffff:127.0.0.1]/",
  "https://localhost/", "https://localhost./", "https://app.local/",
  "https://app.localhost/", "https://app.internal/", "https://app.test/",
  "http://resilience-watch.onrender.com/", "file:///C:/app/index.html", "invalid"
]) {
  const guard = fixture(url);
  assert.equal(guard.elements["copy-app-link"].disabled, true, `Must not share ${url}`);
  assert.equal(guard.elements["email-app"].attrs["aria-disabled"], "true");
  assert.equal(guard.elements["share-url"].value, "");
  assert.match(guard.elements["sharing-status"].textContent, /not a shareable link/);
}
assert.equal(vm.runInContext('publicReviewURL("https://user:password@resilience-watch.onrender.com/?lat=1")', f.context),
  "https://resilience-watch.onrender.com/review");
assert(!/geolocation|app\.js|leaflet|rain_renderer/.test(html), "Introduction must not run GPS or live-map code.");
assert(html.includes('href="/"'), "Mentors need a clear path into the prototype.");
(async () => {
  let copied;
  const copy = fixture(hosted, {writeText: async value => { copied = value; }});
  await copy.elements["copy-app-link"].handlers.click();
  assert.equal(copied, "https://resilience-watch.onrender.com/review");
  assert.match(copy.elements["sharing-status"].textContent, /copied/);
  const failure = fixture(hosted, {writeText: async () => { throw new Error("Permission denied"); }});
  await failure.elements["copy-app-link"].handlers.click();
  assert.match(failure.elements["sharing-status"].textContent, /Could not copy.*Permission denied/);
  assert(failure.elements["share-url"].selected);
  await f.elements["copy-app-link"].handlers.click();
  assert.match(f.elements["sharing-status"].textContent, /Clipboard is unavailable/);
  assert(f.elements["share-url"].selected);
  console.log("Mentor sharing: hosted email, GPS/credential privacy, local guards and clipboard recovery passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
