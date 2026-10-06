// 앱 껍데기(HTML/아이콘)만 캐시합니다. 오디오는 캐시하지 않습니다.
const CACHE = "news-shell-v1";
const SHELL = ["./", "index.html", "manifest.json", "icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.endsWith(".mp3") || url.pathname.endsWith("episodes.json")) return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
