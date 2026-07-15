/* Bookmarklet source — only accepts substrate.office.com JWT */
(function () {
  if (window.__MCG_BM__) {
    alert("MCG capture already active");
    return;
  }
  window.__MCG_BM__ = true;

  function b64urlJson(seg) {
    try {
      var s = seg.replace(/-/g, "+").replace(/_/g, "/");
      var pad = s + "===".slice((s.length + 3) % 4);
      return JSON.parse(atob(pad));
    } catch (e) {
      return null;
    }
  }
  function isSubstrate(t) {
    if (!t || t.indexOf("eyJ") !== 0) return false;
    var c = b64urlJson(t.split(".")[1] || "");
    if (!c) return false;
    var aud = String(c.aud || "");
    return aud.indexOf("substrate.office.com") >= 0;
  }
  function store(t) {
    if (!t || t.length < 40 || t.indexOf("eyJ") !== 0) return;
    if (!isSubstrate(t)) {
      console.warn("[MCG] ignored non-substrate token aud=", (b64urlJson(t.split(".")[1]) || {}).aud);
      return;
    }
    window.__MCG_LAST_TOKEN__ = t;
    try {
      navigator.clipboard.writeText(t);
    } catch (e) {}
    var b = document.getElementById("mcg-bm");
    if (!b) {
      b = document.createElement("div");
      b.id = "mcg-bm";
      b.style.cssText =
        "position:fixed;z-index:2147483647;right:12px;bottom:12px;background:#111;color:#fff;padding:10px 12px;border-radius:10px;font:12px system-ui;max-width:360px;border:1px solid #2f9e44";
      document.body.appendChild(b);
    }
    b.textContent = "MCG SUBSTRATE JWT ready — click to copy";
    b.onclick = function () {
      navigator.clipboard.writeText(t);
      b.textContent = "Copied substrate JWT.";
    };
  }
  function fromUrl(u) {
    try {
      var s = String(u);
      if (s.indexOf("substrate.office.com") < 0 && s.toLowerCase().indexOf("chathub") < 0) return;
      var m = s.match(/access_token=([^&]+)/i);
      if (m) store(decodeURIComponent(m[1]));
    } catch (e) {}
  }
  var W = window.WebSocket;
  window.WebSocket = function (u, p) {
    fromUrl(u);
    return p !== undefined ? new W(u, p) : new W(u);
  };
  window.WebSocket.prototype = W.prototype;
  Object.assign(window.WebSocket, W);
  alert("MCG capture on — open Copilot chat and SEND a message. Green panel = substrate JWT.");
})();
