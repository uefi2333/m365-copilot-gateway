// ==UserScript==
// @name         MCG Substrate Token Capture
// @namespace    https://github.com/uefi2333/m365-copilot-gateway
// @version      1.1.0
// @description  Capture ONLY Substrate ChatHub JWT (aud substrate.office.com) — ignores config.office tokens
// @match        https://m365.cloud.microsoft/*
// @match        https://*.cloud.microsoft/*
// @match        https://copilot.cloud.microsoft/*
// @match        https://www.office.com/*
// @match        https://*.office.com/*
// @match        https://outlook.office.com/*
// @match        https://teams.microsoft.com/*
// @grant        none
// @run-at       document-start
// ==/UserScript==

(function () {
  "use strict";
  const KEY = "mcg_captured_tokens";
  const MAX = 12;

  function b64urlJson(seg) {
    try {
      const s = seg.replace(/-/g, "+").replace(/_/g, "/");
      const pad = s + "===".slice((s.length + 3) % 4);
      return JSON.parse(atob(pad));
    } catch (_) {
      return null;
    }
  }

  function decodeJwt(token) {
    if (!token || !token.startsWith("eyJ")) return null;
    const parts = token.split(".");
    if (parts.length < 2) return null;
    return b64urlJson(parts[1]);
  }

  function isSubstrateToken(token) {
    const c = decodeJwt(token);
    if (!c) return false;
    const aud = String(c.aud || "");
    // Accept sydney / substrate office audiences only
    if (aud.includes("substrate.office.com")) return true;
    // some builds put resource in scp / only
    const scp = String(c.scp || "");
    if (scp.includes("sydney") || scp.includes("M365Chat")) return true;
    return false;
  }

  function classify(token) {
    const c = decodeJwt(token) || {};
    const aud = String(c.aud || "?");
    if (isSubstrateToken(token)) return { ok: true, label: "SUBSTRATE OK", aud, claims: c };
    return {
      ok: false,
      label: "WRONG (not ChatHub)",
      aud,
      claims: c,
    };
  }

  function store(token, meta) {
    if (!token || token.length < 40 || !token.startsWith("eyJ")) return;
    // Always remember last raw for debug, but only promote substrate
    const info = classify(token);
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
      ok: info.ok,
      aud: info.aud,
      meta: meta || {},
    });
    list = list.slice(0, MAX);
    sessionStorage.setItem(KEY, JSON.stringify(list));
    window.__MCG_LAST_RAW__ = token;
    if (info.ok) {
      window.__MCG_LAST_TOKEN__ = token;
      paint(token, info, true);
    } else {
      // show warning but do not set LAST_TOKEN (Copy uses LAST_TOKEN)
      paint(token, info, false);
    }
  }

  function fromUrl(u) {
    try {
      const s = String(u);
      // ChatHub classic: access_token= in WS URL
      if (/substrate\.office\.com/i.test(s) || /Chathub/i.test(s) || /m365Copilot/i.test(s)) {
        const m = s.match(/access_token=([^&]+)/i);
        if (m) {
          store(decodeURIComponent(m[1]), { via: "url-chathub", href: s.slice(0, 180) });
          return;
        }
      }
      // generic access_token — still classify
      const m2 = s.match(/access_token=([^&]+)/i);
      if (m2) {
        const t = decodeURIComponent(m2[1]);
        if (t.startsWith("eyJ")) store(t, { via: "url-generic", href: s.slice(0, 180) });
      }
    } catch (_) {}
  }

  function fromAuthHeader(auth, via) {
    const a = String(auth || "");
    if (a.startsWith("Bearer eyJ")) store(a.slice(7).trim(), { via });
    else if (a.startsWith("eyJ")) store(a.trim(), { via });
  }

  // WebSocket constructor hook
  const NativeWS = window.WebSocket;
  window.WebSocket = function (url, protocols) {
    try {
      fromUrl(url);
    } catch (_) {}
    return protocols !== undefined
      ? new NativeWS(url, protocols)
      : new NativeWS(url);
  };
  window.WebSocket.prototype = NativeWS.prototype;
  Object.assign(window.WebSocket, NativeWS);

  // fetch hook
  const nativeFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const u = typeof input === "string" ? input : input && input.url;
      if (u) fromUrl(u);
      const h = (init && init.headers) || (input && input.headers);
      if (h) {
        const get = (k) =>
          typeof h.get === "function" ? h.get(k) : h[k] || h[String(k).toLowerCase()];
        fromAuthHeader(get("Authorization") || get("authorization"), "fetch-header");
      }
    } catch (_) {}
    return nativeFetch.apply(this, arguments);
  };

  // XHR open hook (some shells still use XHR)
  const xo = XMLHttpRequest.prototype.open;
  const xs = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__mcg_url = url;
    try {
      fromUrl(url);
    } catch (_) {}
    return xo.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    try {
      if (this.__mcg_url) fromUrl(this.__mcg_url);
    } catch (_) {}
    return xs.apply(this, arguments);
  };

  function paint(token, info, ok) {
    let el = document.getElementById("mcg-token-bar");
    if (!el) {
      el = document.createElement("div");
      el.id = "mcg-token-bar";
      el.style.cssText =
        "position:fixed;z-index:2147483647;right:12px;bottom:12px;max-width:440px;" +
        "background:#111;color:#eee;border:1px solid #333;border-radius:10px;" +
        "padding:10px 12px;font:12px/1.4 ui-sans-serif,system-ui,sans-serif;" +
        "box-shadow:0 8px 28px rgba(0,0,0,.45)";
      document.documentElement.appendChild(el);
    }
    const short = token.slice(0, 16) + "…" + token.slice(-8);
    const border = ok ? "#2f9e44" : "#e03131";
    el.style.borderColor = border;
    el.innerHTML =
      '<div style="font-weight:700;margin-bottom:4px;color:' +
      border +
      '">MCG · ' +
      info.label +
      "</div>" +
      '<div style="opacity:.85;margin-bottom:4px;word-break:break-all">aud: ' +
      (info.aud || "?") +
      "</div>" +
      '<div style="opacity:.55;word-break:break-all;margin-bottom:8px">' +
      short +
      "</div>" +
      (ok
        ? ""
        : '<div style="color:#ffa8a8;margin-bottom:8px">这不是 ChatHub token（常见误抓 clients.config.office.net）。请打开 m365.cloud.microsoft/chat 并<strong>发一条消息</strong>，等出现 green SUBSTRATE OK 再 Copy。</div>') +
      '<div style="display:flex;gap:6px;flex-wrap:wrap">' +
      (ok
        ? '<button id="mcg-copy" style="cursor:pointer;border:0;border-radius:6px;padding:6px 10px;background:#3d8bfd;color:#fff">Copy Substrate JWT</button>'
        : '<button id="mcg-copy" style="cursor:pointer;border:0;border-radius:6px;padding:6px 10px;background:#555;color:#ccc" disabled>Copy disabled</button>') +
      '<button id="mcg-list" style="cursor:pointer;border:0;border-radius:6px;padding:6px 10px;background:#333;color:#ccc">List</button>' +
      '<button id="mcg-hide" style="cursor:pointer;border:0;border-radius:6px;padding:6px 10px;background:#333;color:#ccc">Hide</button>' +
      "</div>";
    const copyBtn = el.querySelector("#mcg-copy");
    if (ok && copyBtn) {
      copyBtn.onclick = () => {
        navigator.clipboard.writeText(token).then(() => {
          copyBtn.textContent = "Copied";
        });
      };
    }
    el.querySelector("#mcg-hide").onclick = () => el.remove();
    el.querySelector("#mcg-list").onclick = () => {
      const items = window.MCG.list();
      console.table(
        items.map((x) => ({
          ok: x.ok,
          aud: x.aud,
          ts: new Date(x.ts).toISOString(),
          via: (x.meta && x.meta.via) || "",
          head: (x.token || "").slice(0, 24),
        }))
      );
      alert("已在控制台 console.table 列出 " + items.length + " 条；只复制 ok=true 的。");
    };
  }

  window.MCG = {
    last: () => window.__MCG_LAST_TOKEN__ || null,
    lastRaw: () => window.__MCG_LAST_RAW__ || null,
    list: () => {
      try {
        return JSON.parse(sessionStorage.getItem(KEY) || "[]");
      } catch (_) {
        return [];
      }
    },
    good: () => window.MCG.list().filter((x) => x.ok),
    copy: async () => {
      const t = window.__MCG_LAST_TOKEN__;
      if (!t) {
        alert("还没有 Substrate JWT。请在 Copilot 聊天里发一条消息。");
        return false;
      }
      await navigator.clipboard.writeText(t);
      return true;
    },
    explain: (token) => classify(token || window.__MCG_LAST_RAW__),
  };

  console.info(
    "[MCG] v1.1 capture ready — only aud=substrate.office.com is copyable. Open chat and send a message."
  );
})();
