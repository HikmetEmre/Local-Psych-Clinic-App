/* Registers the service worker and handles the "turn on notifications" button. */
"use strict";

(function () {
  if (!("serviceWorker" in navigator)) return;
  navigator.serviceWorker.register("/sw.js").catch(function () {});

  // Logging out also forgets the calendar kept for offline viewing.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form && form.getAttribute("action") === "/logout" && navigator.serviceWorker.controller) {
      navigator.serviceWorker.controller.postMessage("logout");
    }
  });

  var enable = document.getElementById("push-enable");
  var disable = document.getElementById("push-disable");
  var status = document.getElementById("push-status");
  if (!enable) return;

  function say(text) {
    if (status) status.textContent = text;
  }

  function csrf() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  function keyBytes(base64url) {
    var padded = (base64url + "===".slice((base64url.length + 3) % 4)).replace(/-/g, "+").replace(/_/g, "/");
    var raw = atob(padded);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: JSON.stringify(body),
    }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
    });
  }

  if (!("PushManager" in window) || !("Notification" in window)) {
    enable.disabled = true;
    say("Bu tarayıcı anlık bildirimleri desteklemiyor. Android'de Chrome ile açın ve ana ekrana ekleyin.");
    return;
  }

  navigator.serviceWorker.ready.then(function (registration) {
    return registration.pushManager.getSubscription();
  }).then(function (existing) {
    if (existing) say("Bu cihazda anlık bildirimler açık.");
  });

  enable.addEventListener("click", function () {
    Notification.requestPermission()
      .then(function (permission) {
        if (permission !== "granted") throw new Error("denied");
        return navigator.serviceWorker.ready;
      })
      .then(function (registration) {
        return registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: keyBytes(enable.getAttribute("data-key")),
        });
      })
      .then(function (subscription) {
        return post("/push/abonelik", subscription.toJSON());
      })
      .then(function () {
        say("Bu cihazda anlık bildirimler açık.");
      })
      .catch(function (error) {
        say(error && error.message === "denied"
          ? "Bildirim izni verilmedi. Tarayıcı ayarlarından izin verebilirsiniz."
          : "Bildirimler açılamadı. Lütfen tekrar deneyin.");
      });
  });

  if (disable) {
    disable.addEventListener("click", function () {
      navigator.serviceWorker.ready
        .then(function (registration) { return registration.pushManager.getSubscription(); })
        .then(function (subscription) {
          if (!subscription) return null;
          var endpoint = subscription.endpoint;
          return subscription.unsubscribe().then(function () {
            return post("/push/abonelik/sil", { endpoint: endpoint });
          });
        })
        .then(function () { say("Bu cihazda anlık bildirimler kapalı."); })
        .catch(function () { say("İşlem tamamlanamadı."); });
    });
  }
})();
