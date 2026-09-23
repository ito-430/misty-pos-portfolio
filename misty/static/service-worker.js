// Misty POS - service worker
//
// 目的は「ホーム画面に追加」でアプリとして起動できるようにすること（PWA の要件）。
// 静的ファイルはネットワーク優先・失敗時だけキャッシュにする。キャッシュ優先にすると、
// サーバー側を更新しても iPad が古い JS を使い続け、API と画面の版がずれる。
// API はキャッシュしない（在庫や注文の古い値を見せる方が、エラーより危険）。
const CACHE_NAME = "misty-pos-static-v2";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || !url.pathname.startsWith("/static/")) return;
  event.respondWith((async () => {
    try {
      const res = await fetch(event.request);
      if (res.ok) {
        const cache = await caches.open(CACHE_NAME);
        cache.put(event.request, res.clone());
      }
      return res;
    } catch (e) {
      const cached = await caches.match(event.request);
      if (cached) return cached;
      throw e;
    }
  })());
});
