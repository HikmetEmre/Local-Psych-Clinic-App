/* Klinik Web service worker.
 * - shows Web Push notifications (text already free of names: date, time, client code);
 * - keeps the last calendar page for read-only viewing without a connection;
 * - forgets that copy at logout.
 */
"use strict";

const CACHE = "klinik-takvim-v1";
const LAST = "/takvim#son";
const OFFLINE = "/offline";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.add(OFFLINE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function remember(request, response) {
  const cache = await caches.open(CACHE);
  // Only the most recent calendar page is kept.
  for (const key of await cache.keys()) {
    if (new URL(key.url).pathname === "/takvim") await cache.delete(key);
  }
  await cache.put(request, response.clone());
  await cache.put(LAST, response.clone());
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET" || request.mode !== "navigate") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname === "/takvim") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok && !response.redirected) {
            event.waitUntil(remember(request, response.clone()));
          }
          return response;
        })
        .catch(async () => {
          const cache = await caches.open(CACHE);
          return (await cache.match(request)) || (await cache.match(LAST)) || cache.match(OFFLINE);
        })
    );
    return;
  }

  event.respondWith(
    fetch(request).catch(async () => (await caches.open(CACHE)).match(OFFLINE))
  );
});

self.addEventListener("message", (event) => {
  if (event.data === "logout") {
    event.waitUntil(caches.delete(CACHE));
  }
});

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = {};
  }
  const title = data.title || "Nil Psikoloji";
  event.waitUntil(
    self.registration.showNotification(title, {
      body: data.body || "",
      tag: data.tag || undefined,
      renotify: Boolean(data.tag),
      icon: "/static/ui/img/icon-192.png",
      badge: "/static/ui/img/icon-192.png",
      lang: "tr",
      data: { url: data.url || "/bildirimler" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin);
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      for (const client of windows) {
        if (new URL(client.url).origin === target.origin && "focus" in client) {
          client.navigate(target.href);
          return client.focus();
        }
      }
      return self.clients.openWindow(target.href);
    })
  );
});
