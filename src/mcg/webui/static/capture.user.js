// ==UserScript==
// @name         MCG Substrate Token Capture
// @namespace    https://github.com/uefi2333/m365-copilot-gateway
// @version      1.0.0
// @description  Capture ChatHub access_token from M365 Copilot / Copilot Studio WS
// @match        https://m365.cloud.microsoft/*
// @match        https://*.cloud.microsoft/*
// @match        https://copilot.microsoft.com/*
// @match        https://www.office.com/*
// @match        https://*.office.com/*
// @grant        none
// @run-at       document-start
// ==/UserScript==

(function () {
  "use strict";
  const KEY = "mcg_captured_tokens";
  const MAX = 8;

  function store(token, meta) {
    if (!token || token.length < 40) return;
    let list = [];
    try {
      list = JSON.parse(sessionStorage.getItem(KEY) || "[]");
    } catch (_) {
      list = [];
    }
    list = list.filter((x) => x.token !== token);
    list.unshift({
      token,
      ts: Date.now(),
      url: location.href,
      meta: meta || {},
    });
    list = list.slice(0, MAX);
    sessionStorage.setItem(KEY, JSON.stringify(list));
    window.__MCG_LAST_TOKEN__ = token;
    paint(token);
  }

  function fromUrl(u) {
    try {
      const url = new URL(u, location.href);
      const t =
        url.searchParams.get("access_token") ||
        url.searchParams.get("Authorization") ||
        "";
      if (t.startsWith("eyJ")) store(t, { via: "url", href: String(u).slice(0, 200) });
      // also Authorization: Bearer
      const auth = url.searchParams.get("access_token");
      if (!auth && u.includes("access_token=")) {
        const m = String(u).match(/access_token=([^&]+)/);
        if (m) store(decodeURIComponent(m[1]), { via: "url-regex" });
      }
    } catch (_) {}
  }

  // WebSocket constructor hook
  const NativeWS = window.WebSocket;
  window.WebSocket = function (url, protocols) {
    fromUrl(url);
    return protocols !== undefined
      ? new NativeWS(url, protocols)
      : new NativeWS(url);
  };
  window.WebSocket.prototype = NativeWS.prototype;
  Object.assign(window.WebSocket, NativeWS);

  // fetch hook (some paths put token in header / body)
  const nativeFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const u = typeof input === "string" ? input : input && input.url;
      if (u) fromUrl(u);
      const h = (init && init.headers) || (input && input.headers);
      if (h) {
        const get = (k) =>
          typeof h.get === "function"
            ? h.get(k)
            : h[k] || h[k.toLowerCase()];
        const auth = get("Authorization") || get("authorization") || "";
        if (String(auth).startsWith("Bearer eyJ")) {
          store(String(auth).slice(7).trim(), { via: "fetch-header" });
        }
      }
    } catch (_) {}
    return nativeFetch.apply(this, arguments);
  };

  function paint(token) {
    let el = document.getElementById("mcg-token-bar");
    if (!el) {
      el = document.createElement("div");
      el.id = "mcg-token-bar";
      el.style.cssText =
        "position:fixed;z-index:2147483647;right:12px;bottom:12px;max-width:420px;" +
        "background:#111;color:#eee;border:1px solid #333;border-radius:10px;" +
        "padding:10px 12px;font:12px/1.4 ui-sans-serif,system-ui,sans-serif;" +
        "box-shadow:0 8px 28px rgba(0,0,0,.45)";
      document.documentElement.appendChild(el);
    }
    const short = token.slice(0, 18) + "…" + token.slice(-10);
    el.innerHTML =
      '<div style="font-weight:600;margin-bottom:6px">MCG · token captured</div>' +
      '<div style="opacity:.7;word-break:break-all;margin-bottom:8px">' +
      short +
      "</div>" +
      '<div style="display:flex;gap:6px;flex-wrap:wrap">' +
      '<button id="mcg-copy" style="cursor:pointer;border:0;border-radius:6px;padding:6px 10px;background:#3d8bfd;color:#fff">Copy JWT</button>' +
      '<button id="mcg-hide" style="cursor:pointer;border:0;border-radius:6px;padding:6px 10px;background:#333;color:#ccc">Hide</button>' +
      "</div>";
    el.querySelector("#mcg-copy").onclick = () => {
      navigator.clipboard.writeText(token).then(() => {
        el.querySelector("#mcg-copy").textContent = "Copied";
      });
    };
    el.querySelector("#mcg-hide").onclick = () => el.remove();
  }

  // expose helpers for console
  window.MCG = {
    last: () => window.__MCG_LAST_TOKEN__ || null,
    list: () => {
      try {
        return JSON.parse(sessionStorage.getItem(KEY) || "[]");
      } catch (_) {
        return [];
      }
    },
    copy: async () => {
      const t = window.__MCG_LAST_TOKEN__;
      if (!t) return false;
      await navigator.clipboard.writeText(t);
      return true;
    },
  };

  console.info("[MCG] capture ready — open Copilot chat, then MCG.copy() or use floating bar");
})();
