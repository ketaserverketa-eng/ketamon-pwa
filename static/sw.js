const CACHE = 'ketamon-v__PWA_VERSION__';
const CORE_ASSETS = [
  '/offline'
];

const NETWORK_ONLY_PREFIXES = [
  '/api/',
  '/abonnement',
  '/reseau/',
  '/hotspot/',
  '/parametres/',
  '/settings/',
  '/concepteur/',
  '/journaux/',
  '/logs/',
  '/system/',
  '/systeme/',
  '/dhcp/',
  '/traffic',
  '/report',
  '/impression-rapide',
  '/bons',
  '/login',
  '/logout',
  '/pwa-reset',
  '/health'
];

function offlineFallback() {
  return caches.match('/offline').then(cached => {
    if (cached) return cached;
    return new Response(
      '<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>KetaMon hors ligne</title></head><body style="margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f172a;color:#f8fafc;font-family:Arial,sans-serif"><main style="max-width:420px;padding:24px;text-align:center"><h1>KetaMon</h1><p>Connexion au serveur indisponible.</p><p><a href="/pwa-reset" style="color:#38bdf8">Reparer le cache PWA</a></p></main></body></html>',
      { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
    );
  });
}

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(CORE_ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('message', e => {
  if (e.data && e.data.type === 'CLEAR_KETAMON_CACHE') {
    e.waitUntil(caches.keys().then(keys => Promise.all(keys.map(k => caches.delete(k)))));
  }
});

function pathMatchesRule(pathname, rule) {
  if (pathname === rule) return true;
  return pathname.startsWith(rule.endsWith('/') ? rule : rule + '/');
}

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  if (e.request.method !== 'GET') {
    e.respondWith(fetch(e.request));
    return;
  }

  // Les pages de gestion et API doivent toujours venir du serveur.
  if (url.origin === self.location.origin && NETWORK_ONLY_PREFIXES.some(path => pathMatchesRule(url.pathname, path))) {
    if (e.request.mode === 'navigate') {
      e.respondWith(fetch(e.request).catch(() => offlineFallback()));
    } else {
      e.respondWith(fetch(e.request));
    }
    return;
  }

  // Assets statiques en network-first pour eviter les anciens JS/CSS bloques en PWA.
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      fetch(e.request).then(res => {
        if (res && res.ok) {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  // Network-first pour les pages HTML avec fallback offline
  if (e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request).catch(() => offlineFallback())
    );
    return;
  }

  // Network-first pour tout le reste
  e.respondWith(fetch(e.request));
});
