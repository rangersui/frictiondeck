const CDNS = ['esm.sh', 'cdn.jsdelivr.net', 'unpkg.com', 'cdnjs.cloudflare.com'];
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (u.pathname.endsWith('/raw')) return;
  // Navigation requests — network first (needed for PWA installability)
  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).catch(() => caches.match('/')));
    return;
  }
  const h = u.hostname;
  if (!CDNS.some(c => h.includes(c))) return;
  e.respondWith(
    caches.open('elastik-cdn').then(c =>
      c.match(e.request).then(r => r || fetch(e.request).then(res => {
        c.put(e.request, res.clone()); return res;
      }))
    )
  );
});
