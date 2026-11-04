"use strict";
const OFFLINE_CACHE = "resilience-offline-v1";
const OFFLINE_FILES = ["/offline.html", "/app-icon.svg"];
self.addEventListener("install", event => {
  event.waitUntil(caches.open(OFFLINE_CACHE).then(cache => cache.addAll(OFFLINE_FILES)));
});
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys
    .filter(key => key.startsWith("resilience-offline-") && key !== OFFLINE_CACHE)
    .map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", event => {
  if (event.request.mode !== "navigate" || new URL(event.request.url).origin !== self.location.origin) return;
  // Never cache API responses or a dashboard containing stale hazard/location results.
  event.respondWith(fetch(event.request).catch(() => caches.match("/offline.html")));
});
