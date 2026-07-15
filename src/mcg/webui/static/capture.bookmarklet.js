/* Bookmarklet source (build minified one-liner in WebUI).
 * Hook WebSocket + fetch for access_token; copy button on capture.
 */
(function () {
  if (window.__MCG_BM__) {
    alert("MCG capture already active");
    return;
  }
  window.__MCG_BM__ = true;
  function store(t) {
    if (!t || t.length < 40 || !t.startsWith("eyJ")) return;
    window.__MCG_LAST_TOKEN__ = t;
    try {
      navigator.clipboard.writeText(t);
    } catch (_) {}
    var b = document.getElementById("mcg-bm");
    if (!b) {
      b = document.createElement("div");
      b.id = "mcg-bm";
      b.style.cssText =
        "position:fixed;z-index:2147483647;right:12px;bottom:12px;background:#111;color:#fff;padding:10px 12px;border-radius:10px;font:12px system-ui;max-width:360px";
      document.body.appendChild(b);
    }
    b.textContent = "MCG token ready (copied if allowed). Click to copy again.";
    b.onclick = function () {
      navigator.clipboard.writeText(t);
      b.textContent = "Copied.";
    };
  }
  function fromUrl(u) {
    try {
      var m = String(u).match(/access_token=([^&]+)/);
      if (m) store(decodeURIComponent(m[1]));
    } catch (_) {}
  }
  var W = window.WebSocket;
  window.WebSocket = function (u, p) {
    fromUrl(u);
    return p !== undefined ? new W(u, p) : new W(u);
  };
  window.WebSocket.prototype = W.prototype;
  Object.assign(window.WebSocket, W);
  var F = window.fetch;
  window.fetch = function (i, n) {
    try {
      fromUrl(typeof i === "string" ? i : i && i.url);
    } catch (_) {}
    return F.apply(this, arguments);
  };
  alert("MCG capture on. Open a Copilot chat, then use the floating bar.");
})();
