const CACHE = 'dallas-leads-v1';
const STATIC = [
  '/dallas-leads/leads.html',
  '/dallas-leads/manifest.json',
  'https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Syne:wght@600;700;800&family=Inter:wght@300;400;500;600&display=swap'
];

// Install — cache static assets
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(STATIC)).then(() => self.skipWaiting())
  );
});

// Activate — clean old caches
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Fetch strategy:
// - records.json: network first (always want fresh leads), fall back to cache
// - leads.html: network first too -- this is the app code itself (bug
//   fixes, feature changes), and CACHE never changes version between
//   deploys, so a pure cache-first strategy here meant a phone's
//   home-screen install could keep serving stale/buggy JS indefinitely
//   with no way to pick up a fix short of manually clearing site data.
//   Network-first fixes that without needing a version bump on every push.
// - everything else (fonts, manifest): cache first, fall back to network
self.addEventListener('fetch', e => {
  const url = e.request.url;

  if (url.includes('records.json') || url.includes('leads.html')) {
    // Network first
    e.respondWith(
      fetch(e.request)
        .then(res => {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
          return res;
        })
        .catch(() => caches.match(e.request))
    );
    return;
  }

  // Cache first for everything else
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(res => {
        if (!res || res.status !== 200 || res.type === 'opaque') return res;
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return res;
      });
    })
  );
});
