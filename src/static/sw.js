// Bump per release: old caches are deleted on activate, so stale JS/CSS
// can't outlive a deploy.
const CACHE_VERSION = 'v2';
const cacheName = `offline_cache_${CACHE_VERSION}`;


self.addEventListener("install", (event) =>
{
    console.log('[Service Worker] Install event: Service worker installed.');
    self.skipWaiting();
});


self.addEventListener("activate", (event) =>
{
    event.waitUntil((async () =>
    {
        const keys = await caches.keys();
        await Promise.all(keys.filter(k => k !== cacheName).map(k => caches.delete(k)));
        await self.clients.claim();
    })());
});


// Cache-first is only safe for immutable-ish static assets. Everything else
// — navigations (the server-rendered library page changes after a rebuild),
// the JSON API (saved searches), /logs, /t/ thumbnails — must hit the network.
function isCacheable(request)
{
    if (request.method !== 'GET') return false;
    if (request.mode === 'navigate' || request.destination === 'document') return false;
    if (!['script', 'style', 'image', 'font'].includes(request.destination)) return false;
    const path = new URL(request.url).pathname;
    if (path.startsWith('/t/') || path.startsWith('/api/') || path.startsWith('/logs')) return false;
    return !request.url.includes("hls_stream");
}

async function cacheFirstWithRefresh(request)
{
    const fetchResponsePromise = fetch(request).then(async (networkResponse) =>
    {
        // Only full 200 responses are cacheable; cache.put() throws on 206.
        if (networkResponse.ok && networkResponse.status === 200)
        {
            try
            {
                const cache = await caches.open(cacheName);
                await cache.put(request, networkResponse.clone());
            }
            catch (e)
            {
                console.warn('[Service Worker] cache.put failed:', e);
            }
        }
        return networkResponse;
    });

    return (await caches.match(request)) || (await fetchResponsePromise);
}

self.addEventListener("fetch", (event) =>
{
    if (isCacheable(event.request))
    {
        event.respondWith(cacheFirstWithRefresh(event.request));
    }
});
