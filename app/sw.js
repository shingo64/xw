// Service Worker: オフラインキャッシュ + 自動アップデート
const VERSION = "v0.7.2";
const CACHE = `xw100-${VERSION}`;
const APP_FILES = [
  "./",
  "./index.html",
  "./app.js",
  "./style.css",
  "./manifest.json",
  "./data/course.json",
  "./data/cutoffs.json",
  "./data/conveni.json",
  "./data/sento.json",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(APP_FILES.map((u) => new Request(u, { cache: "reload" }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);

  // OSMタイルはキャッシュ・ファースト（古くなっても良い、オフライン優先）
  if (url.host.endsWith("tile.openstreetmap.org")) {
    e.respondWith(
      caches.match(req).then((c) => c || fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((cache) => cache.put(req, copy));
        return res;
      }).catch(() => c))
    );
    return;
  }

  // アプリファイル: ネットワーク優先 → キャッシュにフォールバック
  if (req.method === "GET") {
    e.respondWith(
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((cache) => cache.put(req, copy));
        return res;
      }).catch(() => caches.match(req).then((c) => c || caches.match("./index.html")))
    );
  }
});
