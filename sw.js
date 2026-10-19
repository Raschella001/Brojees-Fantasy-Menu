// Brojees Fantasy Menu service worker.
// The page itself is network-first, so a new index.html shows up on the next open;
// the cached copy is only used when the network is down. League data (Sleeper,
// ESPN, the Brojees Workers) is never cached here: it always comes live.
const CACHE = 'brojees-shell-v3.6';
const SHELL = ['./', './index.html', './manifest.webmanifest', './icons/icon-192.png', './icons/icon-512.png', './icons/apple-touch-icon.png', './icons/favicon-32.png', './icons/favicon-48.png', './icons/logo-plate.png'];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE)
      .then(cache => Promise.all(SHELL.map(url => cache.add(url).catch(() => null))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key.startsWith('brojees-') && key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    // 'no-cache' revalidates with GitHub Pages instead of reusing its 10-minute HTTP cache,
    // so a new release shows up on the very next open.
    const response = await fetch(request.url, { cache: 'no-cache', credentials: 'same-origin' });
    if (response && response.ok) cache.put('./index.html', response.clone());
    return response;
  } catch (err) {
    return (await cache.match('./index.html')) || (await cache.match('./')) || Response.error();
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  const network = fetch(request).then(response => {
    if (response && (response.ok || response.type === 'opaque')) cache.put(request, response.clone());
    return response;
  }).catch(() => cached);
  return cached || network;
}

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);

  if (request.mode === 'navigate' && url.origin === self.location.origin) {
    event.respondWith(networkFirst(request));
    return;
  }
  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }
  if (url.origin === self.location.origin && /\.(png|webmanifest|svg)$/.test(url.pathname)) {
    event.respondWith(staleWhileRevalidate(request));
  }
  // Everything else (live league data) goes straight to the network.
});
