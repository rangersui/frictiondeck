const CDNS = ['esm.sh', 'cdn.jsdelivr.net', 'unpkg.com', 'cdnjs.cloudflare.com'];
self.addEventListener('fetch', e => {
  const h = new URL(e.request.url).hostname;
  if (!CDNS.some(c => h.includes(c))) return;
  e.respondWith(
    caches.open('elastik-cdn').then(c =>
      c.match(e.request).then(r => r || fetch(e.request).then(res => {
        c.put(e.request, res.clone()); return res;
      }))
    )
  );
});
