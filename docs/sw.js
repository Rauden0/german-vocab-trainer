// Offline support: cache the app and word list. Bumped automatically by tools/build_web.py.
const CACHE = "wortschatz-ec43559e65c4";
const FILES = ["./", "index.html", "engine.js", "words.json", "manifest.webmanifest",
               "icon-180.png", "icon-192.png", "icon-512.png"];
self.addEventListener("install", e => e.waitUntil(
  caches.open(CACHE).then(c => c.addAll(FILES)).then(() => self.skipWaiting())));
self.addEventListener("activate", e => e.waitUntil(
  caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim())));
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  e.respondWith(caches.match(e.request, {ignoreSearch: true}).then(r => r || fetch(e.request)));
});
