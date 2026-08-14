const cacheName = 'offline_cache';


self.addEventListener("install", (event) =>
{
    console.log('[Service Worker] Install event: Service worker installed.');
});


function isCacheable(request)
{
    // /t/<hash> thumbnails must stay uncached: the .mp4 variants answer 206
    // (which cache.put() rejects with a TypeError), and a cache-first still
    // would keep serving a stale image forever after the server regenerates
    // the thumbnail.
    const path = new URL(request.url).pathname;
    return !request.url.includes("?")
        && !request.url.includes("hls_stream")
        && !path.startsWith("/t/");
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
