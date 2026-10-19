// Service Worker fuer die installierbare EVO-Auslastung-App.
// Strategie: immer zuerst das Netz fragen, offline den letzten gecachten Stand zeigen.
// Nur GET und nur same-origin; Datenrequests der App bleiben unangetastet
// (status.json laeuft mit cache: "no-store" und wird hier nicht veraendert).

const CACHE = "evo-v1";

// Dateien relativ zur Scope-URL vorab cachen.
function precache() {
  const base = new URL("./", self.registration.scope);
  const paths = ["./", "manifest.webmanifest", "fonts/archivo.woff2", "icons/icon-192.png"];
  return caches.open(CACHE).then((c) => c.addAll(paths.map((p) => new URL(p, base).href)));
}

self.addEventListener("install", (event) => {
  event.waitUntil(precache().then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

const ASSET_RE = /\.(json|woff2|png|svg|webmanifest)$/;

// Netzwerk zuerst, bei Erfolg unter key in den Cache, offline key aus dem Cache.
function networkFirst(req, key) {
  return fetch(req).then((response) => {
    if (response.ok) {
      const copy = response.clone();
      caches.open(CACHE).then((c) => c.put(key, copy));
    }
    return response;
  }).catch(() => caches.match(key).then((hit) => hit || Response.error()));
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const path = url.pathname;

  if (req.mode === "navigate") {
    // Seite selbst: frisch aus dem Netz, offline die zuletzt gesehene Seite
    const scope = new URL("./", self.registration.scope);
    event.respondWith(networkFirst(req, scope));
    return;
  }
  if (ASSET_RE.test(path)) {
    // Die App haengt ?t=/?v= an; cachen unter der URL ohne Query, sonst laeuft der Cache voll
    const clean = url.origin + path;
    event.respondWith(networkFirst(req, clean));
  }
  // alles andere (z. B. api/export.csv, POST /api/collect) bleibt unberuehrt
});
