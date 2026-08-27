// MaINbox Voice service worker — minimal offline shell.
// We deliberately do NOT cache /api/* (always live). Static shell is
// network-first so updates land immediately when the phone is on Tailscale.
const CACHE = 'mbb-voice-v13';
const SHELL = ['./', './index.html', './app.js', './manifest.webmanifest',
               './icon-192.png', './icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.includes('/api/')) return; // never intercept API
  e.respondWith(
    fetch(e.request, { cache: 'no-store' }).then((r) => {
      const copy = r.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
      return r;
    }).catch(() => caches.match(e.request).then((r) => r || caches.match('./index.html')))
  );
});
