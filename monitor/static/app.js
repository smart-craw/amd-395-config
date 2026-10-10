/* llm monitor — dashboard logic
 *
 * Plain browser JavaScript: no framework, no build step, no network asset that
 * is not served from this same directory. Every number on the page comes from
 * the same-origin proxy in monitor/serve.py, which always answers with the same
 * envelope:
 *
 *   {ok, url, status, latency_ms, content_type, body, error}
 *
 * Two consequences this file is built around:
 *   1. A dead upstream is still HTTP 200 + ok:false, so "did the poll work" is
 *      `env.ok`, never `res.ok`.
 *   2. Only /health and /v1/models have a provable schema. /cache is whatever the
 *      monitored box happens to answer, so the cache card classifies whatever it
 *      gets instead of assuming fields exist — worst case it shows the raw
 *      payload, it never renders blank.
 */

(function () {
  "use strict";

  // --- configuration -------------------------------------------------------

  const ENDPOINTS = [
    { key: "health", route: "api/health", label: "health" },
    { key: "models", route: "api/models", label: "models" },
    { key: "cache", route: "api/cache", label: "cache" },
  ];

  /** Presets taken from this repo's own launch scripts, so retargeting is one click. */
  const PRESETS = [
    {
      label: "llm.home:8001",
      url: "http://llm.home:8001",
      hint: "serve.py default (--url / LLM_MONITOR_UPSTREAM)",
    },
    {
      label: "localhost:8000",
      url: "http://localhost:8000",
      hint: "qwen3-27b/llama-start-qwen3.8-27b.sh --port 8000",
    },
    {
      label: "localhost:8080",
      url: "http://localhost:8080",
      hint: "gemma4/llama-start-gemma4-26moe.sh --port 8080",
    },
  ];

  const POLL_OPTIONS = [
    { ms: 2000, label: "2s" },
    { ms: 5000, label: "5s" },
    { ms: 15000, label: "15s" },
    { ms: 0, label: "paused" },
  ];
  const DEFAULT_POLL_MS = 5000;

  /** ~60 latency samples kept per endpoint, in memory only. */
  const HISTORY_SAMPLES = 60;
  /** A card goes "stale" once its last success is older than 3 poll intervals. */
  const STALE_FACTOR = 3;

  const LS_UPSTREAM = "llmmonitor.upstream";
  const LS_POLL_MS = "llmmonitor.pollMs";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const SPARK_W = 300;
  const SPARK_H = 60;

  // --- tiny helpers --------------------------------------------------------

  const $ = (sel) => document.querySelector(sel);
  const nowMs = () => Date.now();
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const isNum = (v) => typeof v === "number" && Number.isFinite(v);

  function clip(text, max) {
    const s = String(text == null ? "" : text);
    return s.length > max ? s.slice(0, max - 1) + "\u2026" : s;
  }

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  /** mm:ss, as used by the "next poll in" / uptime readouts. */
  function mmss(seconds) {
    const s = Math.max(0, Math.round(seconds));
    return pad2(Math.floor(s / 60)) + ":" + pad2(s % 60);
  }

  function fmtClock(date) {
    return (
      pad2(date.getHours()) + ":" + pad2(date.getMinutes()) + ":" + pad2(date.getSeconds())
    );
  }

  function fmtInt(v) {
    return Math.round(v).toLocaleString("en-US");
  }

  function fmtLatency(ms) {
    if (!isNum(ms)) return "\u2014";
    if (ms >= 1000) return (ms / 1000).toFixed(2) + " s";
    return ms.toFixed(ms < 10 ? 2 : 1) + " ms";
  }

  function fmtBytes(v) {
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let n = Math.abs(v);
    let i = 0;
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024;
      i += 1;
    }
    return (i === 0 ? n.toFixed(0) : n.toFixed(2)) + " " + units[i];
  }

  /** Unix seconds (or ms, or an ISO string) -> "2026-10-10 13:05:07 (3h ago)". */
  function fmtCreated(value) {
    if (value === null || value === undefined || value === "") return "\u2014";
    let date = null;
    if (isNum(value)) date = new Date(value > 1e11 ? value : value * 1000);
    else if (typeof value === "string") {
      const numeric = Number(value);
      date =
        Number.isFinite(numeric) && numeric > 0
          ? new Date(numeric > 1e11 ? numeric : numeric * 1000)
          : new Date(value);
    }
    if (!date || Number.isNaN(date.getTime())) return String(value);
    const stamp =
      date.getFullYear() +
      "-" + pad2(date.getMonth() + 1) +
      "-" + pad2(date.getDate()) +
      " " + fmtClock(date);
    const ago = (Date.now() - date.getTime()) / 1000;
    let rel = "";
    if (ago >= 0) {
      if (ago < 60) rel = "just now";
      else if (ago < 3600) rel = Math.floor(ago / 60) + "m ago";
      else if (ago < 86400 * 365) rel = Math.floor(ago / 3600) + "h ago";
      else rel = Math.floor(ago / 86400 / 365) + "y ago";
    }
    return rel ? stamp + " \u00b7 " + rel : stamp;
  }

  function storeGet(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (_) {
      return null; // private mode / disabled storage: run without persistence
    }
  }

  function storeSet(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (_) {
      /* ignore */
    }
  }

  function storeGetInt(key) {
    const raw = storeGet(key);
    const n = raw === null || raw === "" ? NaN : Number(raw);
    return Number.isFinite(n) ? n : null;
  }

  function makeAbort() {
    return typeof AbortController === "function" ? new AbortController() : null;
  }

  // --- state ---------------------------------------------------------------

  const state = {
    /** base URL to poll; "" means "whatever serve.py was started with" */
    upstream: "",
    /** what /api/config reports as the server-side default */
    configUpstream: "",
    pollMs: DEFAULT_POLL_MS,
    /** bumped whenever the target changes so in-flight responses can be dropped */
    generation: 0,
    timer: null,
    nextPollAt: 0,
    lastPollAt: 0,
    firstOkAt: 0, // first successful poll of the current target -> "uptime"
    startedAt: nowMs(),
    busy: false,
    cycle: null,
    retries: new Map(),
    cards: {},
  };

  ENDPOINTS.forEach((ep) => {
    state.cards[ep.key] = {
      envelope: null, // last envelope, ok or not
      history: [], // latency samples (ms)
      attempts: 0,
      lastOkAt: 0,
      lastError: null,
      errorDetail: null,
      lastGood: null, // last body from a successful poll (kept for degraded renders)
      hasGood: false,
      stale: false,
      loading: false,
    };
  });

  /** DOM handles, grouped per card. */
  const ui = {
    statusPill: $("#status-pill"),
    statusText: $("#status-text"),
    target: $("#target-upstream"),
    lastUpdated: $("#last-updated"),
    nextPoll: $("#next-poll"),
    form: $("#upstream-form"),
    input: $("#upstream-input"),
    fieldError: $("#upstream-error"),
    presets: $("#preset-row"),
    pollGroup: $("#poll-interval"),
    refresh: $("#refresh-now"),
    banner: $("#banner"),
    bannerText: $("#banner-text"),
    footUptime: $("#foot-uptime"),
    cards: {},
  };

  ENDPOINTS.forEach((ep) => {
    const k = ep.key;
    ui.cards[k] = {
      root: $("#card-" + k),
      badge: $("#" + k + "-badge"),
      spark: $("#" + k + "-spark"),
      sparkStats: $("#" + k + "-spark-stats"),
      stale: $("#" + k + "-stale"),
      error: $("#" + k + "-error"),
      errorText: $("#" + k + "-error-text"),
      status: $("#" + k + "-status"),
      code: $("#" + k + "-code"),
      latency: $("#" + k + "-latency"),
      last: $("#" + k + "-last"),
      extra: $("#" + k + "-extra"),
      uptime: $("#" + k + "-uptime"),
      tableWrap: document.querySelector("#card-" + k + " .table-wrap"),
      tableBody: $("#" + k + "-body"),
      empty: $("#" + k + "-empty"),
      gaugeLabel: $("#" + k + "-gauge-label"),
      gaugeValue: $("#" + k + "-gauge-value"),
      bar: $("#" + k + "-bar"),
      barFill: $("#" + k + "-bar-fill"),
      gaugeSub: $("#" + k + "-gauge-sub"),
      kv: $("#" + k + "-kv"),
      raw: $("#" + k + "-raw"),
      rawJson: $("#" + k + "-raw-json"),
    };
  });

  // --- transport -----------------------------------------------------------

  function apiUrl(route) {
    try {
      return new URL(route, document.baseURI).href;
    } catch (_) {
      return route;
    }
  }

  /** Coerce whatever the proxy answers into the documented envelope shape. */
  function asEnvelope(raw, httpStatus) {
    const base = {
      ok: false,
      url: null,
      status: isNum(httpStatus) ? httpStatus : null,
      latency_ms: null,
      content_type: "",
      body: null,
      error: "proxy returned an unexpected payload",
    };
    if (!raw || typeof raw !== "object" || Array.isArray(raw) || !("ok" in raw)) {
      return base;
    }
    return {
      ok: !!raw.ok,
      url: typeof raw.url === "string" ? raw.url : null,
      status: isNum(raw.status) ? raw.status : base.status,
      latency_ms: isNum(raw.latency_ms) ? raw.latency_ms : null,
      content_type: typeof raw.content_type === "string" ? raw.content_type : "",
      body: raw.body === undefined ? null : raw.body,
      error: raw.error
        ? String(raw.error)
        : raw.ok
          ? null
          : "upstream reported a failure without an error message",
    };
  }

  /**
   * GET one proxy route and return an envelope. Never throws: transport problems
   * (proxy down, non-JSON reply, aborted request) come back as an ok:false
   * envelope so the card can display them instead of the page dying.
   */
  async function fetchEnvelope(route, opts) {
    const options = opts || {};
    const signal = options.signal;
    let url = apiUrl(route);
    if (options.useUpstream && state.upstream) {
      url += "?url=" + encodeURIComponent(state.upstream);
    }
    const init = { cache: "no-store", headers: { Accept: "application/json" } };
    if (signal) init.signal = signal;

    try {
      const res = await fetch(url, init);
      let text = "";
      try {
        text = await res.text();
      } catch (_) {
        text = "";
      }
      let parsed = null;
      try {
        parsed = JSON.parse(text);
      } catch (_) {
        parsed = null;
      }
      if (parsed === null) {
        const env = asEnvelope(null, res.status);
        env.error =
          "proxy reply was not JSON (HTTP " + res.status + "): " +
          clip((text || "").trim() || "<empty body>", 140);
        return env;
      }
      return asEnvelope(parsed, res.status);
    } catch (err) {
      if (err && (err.name === "AbortError" || (signal && signal.aborted))) {
        return { aborted: true, ok: false, error: "cancelled", body: null };
      }
      const msg = (err && (err.message || err.name)) || String(err);
      return {
        ok: false,
        url: url,
        status: null,
        latency_ms: null,
        content_type: "",
        body: null,
        error: "could not reach the monitor server: " + clip(msg, 140),
      };
    }
  }

  // --- payload shape helpers (defensive: /cache has no fixed schema) --------

  function kindOf(value) {
    if (value === null || value === undefined) return "null";
    if (typeof value === "number") return Number.isFinite(value) ? "number" : "null";
    if (typeof value === "boolean") return "boolean";
    if (typeof value === "string") return "string";
    if (Array.isArray(value)) return "array";
    return "object";
  }

  /** Flatten a JSON payload one or two levels deep into {key, value, kind} rows. */
  function flatten(body, maxItems) {
    const out = [];
    const limit = maxItems || 24;

    const walk = (value, path, depth) => {
      if (out.length >= limit) return;
      const kind = kindOf(value);
      if (kind === "object" || kind === "array") {
        if (depth >= 2) {
          const shape =
            kind === "array" ? value.length + " items" : Object.keys(value).length + " keys";
          out.push({ key: path || "body", value: kind + " \u00b7 " + shape, kind: kind });
          return;
        }
        if (kind === "array") {
          value.slice(0, 4).forEach((item, i) => walk(item, path + "[" + i + "]", depth + 1));
          if (value.length > 4) {
            out.push({ key: path, value: value.length - 4 + " more", kind: "array" });
          }
          return;
        }
        Object.keys(value).forEach((k) => walk(value[k], path ? path + "." + k : k, depth + 1));
        return;
      }
      out.push({ key: path || "body", value: value, kind: kind });
    };

    walk(body, "", 0);
    return out;
  }

  /** Last path segment of a flattened key: "stats.n_ctx" -> "n_ctx". */
  function leaf(key) {
    const base = String(key).split(".").pop() || String(key);
    return base.replace(/\[\d+\]$/, "");
  }

  const RE_PCT = /(percent|pct|utiliz|occupanc|fullness)/i;
  const RE_RATIO = /(usage|ratio|load|saturation|hit[_-]?rate)/i;
  const RE_TOTAL = /(n[_-]?ctx|nctx|total|capacity|maximum|max[_-]?tokens|context[_-]?size|num[_-]?slots|size)/i;
  const RE_USED = /(used|busy|occupied|active|in[_-]?use|queued|pending|length|tokens|slots)/i;
  const RE_FREE = /(free|available|remaining|idle)/i;
  const RE_STRONG_USED = /(used|busy|occupied|active|in[_-]?use)/i;
  const RE_TOKENS = /(token|slots|length)/i;
  const RE_BYTES = /(bytes|_kb|_mb|_gb)/i;

  function asNumber(value) {
    if (typeof value === "number") return Number.isFinite(value) ? value : null;
    if (typeof value === "string") {
      const trimmed = String(value).trim().replace(/,/g, "");
      if (trimmed && /^-?\d+(\.\d+)?(e[-+]?\d+)?$/i.test(trimmed)) return Number(trimmed);
    }
    return null;
  }

  /**
   * Decide "how full is the cache" from whatever fields came back.
   *
   * Preference order (best-effort on purpose: this repo cannot prove the schema
   * of /cache, so nothing here may assume a field exists):
   *   1. an explicit percentage-ish field (utilization / percent / occupancy)
   *   2. a used + total pair -- "<something>_tokens next to n_ctx" is the usual
   *      llama.cpp-shaped answer, so tokens-vs-n_ctx becomes used/total
   *   3. a lone 0..1 ratio field
   * Returns {pct, label, sub}; pct === null means "found a candidate but no
   * total", and null means "nothing usable at all".
   */
  function deriveGauge(entries) {
    const numeric = [];
    entries.forEach((row) => {
      const value = asNumber(row.value);
      if (value !== null) numeric.push({ key: row.key, name: leaf(row.key), value: value });
    });
    if (!numeric.length) return null;

    const pctField = numeric.filter((r) => RE_PCT.test(r.name))[0];
    if (pctField) {
      const pct = pctField.value <= 1 ? pctField.value * 100 : pctField.value;
      return {
        pct: pct,
        label: pctField.name,
        sub:
          fmtInt(pct) + "% (" + (pctField.value <= 1 ? "ratio field scaled" : "reported directly") + ")",
      };
    }

    const totals = numeric.filter((r) => RE_TOTAL.test(r.name) && r.value > 0);
    const useds = numeric.filter(
      (r) => RE_USED.test(r.name) && !RE_TOTAL.test(r.name) && !RE_FREE.test(r.name)
    );
    if (totals.length && useds.length) {
      const first = (list, tests) => {
        for (const test of tests) {
          const hit = list.filter(test)[0];
          if (hit) return hit;
        }
        return list[0];
      };
      const total = first(totals, [
        (r) => /n[_-]?ctx|nctx/i.test(r.name),
        (r) => /total|capacity/i.test(r.name),
      ]);
      const used = first(useds, [
        (r) => RE_STRONG_USED.test(r.name),
        (r) => /(prompt|input|used)[_-]?tokens?/i.test(r.name),
        (r) => RE_TOKENS.test(r.name),
      ]);
      if (total && used && total.value > 0) {
        const pct = clamp((used.value / total.value) * 100, 0, 999);
        const free = total.value - used.value;
        const bytes = RE_BYTES.test(used.name) || RE_BYTES.test(total.name);
        const render = (v) => (bytes ? fmtBytes(v) : fmtInt(v));
        return {
          pct: pct,
          label: used.name + " / " + total.name,
          sub:
            render(used.value) + " / " + render(total.value) +
            (free >= 0 ? "  \u00b7  " + render(free) + " free" : ""),
        };
      }
    }

    // "free / available" instead of "used": subtract from the total instead.
    if (totals.length) {
      const freeField = numeric.filter((r) => RE_FREE.test(r.name))[0];
      const total = totals[0];
      if (
        freeField &&
        total &&
        total.value > 0 &&
        freeField.value >= 0 &&
        freeField.value <= total.value
      ) {
        const used = total.value - freeField.value;
        return {
          pct: clamp((used / total.value) * 100, 0, 100),
          label: freeField.name + " / " + total.name,
          sub:
            fmtInt(freeField.value) + " free of " + fmtInt(total.value) +
            "  \u00b7  " + fmtInt(used) + " in use",
        };
      }
    }

    const ratioField = numeric.filter(
      (r) => RE_RATIO.test(r.name) && r.value >= 0 && r.value <= 1
    )[0];
    if (ratioField) {
      return {
        pct: ratioField.value * 100,
        label: ratioField.name,
        sub: fmtInt(ratioField.value * 100) + "% of total",
      };
    }

    const lone = numeric.filter((r) => RE_USED.test(r.name) || RE_TOTAL.test(r.name))[0];
    if (lone) {
      return { pct: null, label: lone.name, sub: "no total reported \u00b7 " + fmtInt(lone.value) };
    }
    return null;
  }

  /** Does this field name belong at the top of the cache key/value grid? */
  function isPromoted(name) {
    return (
      RE_PCT.test(name) ||
      RE_RATIO.test(name) ||
      RE_TOTAL.test(name) ||
      RE_USED.test(name) ||
      RE_FREE.test(name)
    );
  }

  // --- generic render helpers ---------------------------------------------

  function renderKv(container, entries, maxItems) {
    if (!container) return;
    const limit = maxItems || 24;
    while (container.firstChild) container.removeChild(container.firstChild);
    const row = (cls, keyText, valueText, valueCls) => {
      const item = document.createElement("div");
      item.className = "kv-item" + (cls ? " " + cls : "");
      const key = document.createElement("dt");
      key.className = "kv-key";
      key.textContent = keyText;
      const val = document.createElement("dd");
      val.className = "kv-value " + valueCls;
      val.textContent = valueText;
      item.appendChild(key);
      item.appendChild(val);
      container.appendChild(item);
    };

    entries.slice(0, limit).forEach((entry) => {
      const name = leaf(entry.key);
      const numeric = asNumber(entry.value);
      let text;
      if (entry.kind === "number") {
        text = Number.isInteger(entry.value)
          ? fmtInt(entry.value)
          : String(Math.round(entry.value * 1000) / 1000);
        if (RE_BYTES.test(name) && Math.abs(numeric) > 1024) {
          text = fmtBytes(numeric) + " (" + fmtInt(numeric) + ")";
        }
      } else if (entry.kind === "string") {
        text = clip(String(entry.value), 60);
        if (text === "") text = "(empty)";
      } else if (entry.kind === "null") {
        text = "null";
      } else {
        text = String(entry.value);
      }
      const promoted = isPromoted(name);
      row(promoted ? "is-promoted" : "", entry.key, text, "k-" + entry.kind);
    });

    if (entries.length > limit) {
      row("", "\u2026", entries.length - limit + " more (see raw)", "k-null");
    }
  }

  function prettyJson(body) {
    if (body === null || body === undefined) return null;
    if (typeof body === "string") return body || null;
    try {
      return JSON.stringify(body, null, 2);
    } catch (_) {
      return String(body);
    }
  }

  /** Show the raw payload panel (the always-available fallback). */
  function setRaw(refs, env) {
    const text = prettyJson(env.body);
    if (text === null || text === "") {
      refs.raw.hidden = false;
      refs.rawJson.textContent = env.error ? "no usable body \u2014 " + env.error : "(empty body)";
      return;
    }
    refs.raw.hidden = false;
    refs.rawJson.textContent = text;
  }

  /** Inline-SVG sparkline: gridlines, area, line, last-sample tick. */
  function renderSpark(refs, samples) {
    const svg = refs.spark;
    if (!svg) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    svg.setAttribute("viewBox", "0 0 " + SPARK_W + " " + SPARK_H);

    const add = (name, attrs) => {
      const el = document.createElementNS(SVG_NS, name);
      Object.keys(attrs).forEach((k) => el.setAttribute(k, attrs[k]));
      svg.appendChild(el);
      return el;
    };

    if (!samples.length) {
      add("line", { class: "spark-empty", x1: 0, y1: SPARK_H / 2, x2: SPARK_W, y2: SPARK_H / 2 });
      refs.sparkStats.textContent = "awaiting data";
      return;
    }

    const lo0 = Math.min.apply(null, samples);
    const hi0 = Math.max.apply(null, samples);
    let lo = lo0;
    let hi = hi0;
    if (hi === lo) {
      const pad = Math.max(Math.abs(hi) * 0.2, 1);
      lo -= pad;
      hi += pad;
    } else {
      const pad = (hi - lo) * 0.15;
      lo -= pad;
      hi += pad;
    }
    const span = hi - lo || 1;
    const x = (i) => (samples.length === 1 ? SPARK_W / 2 : (i / (samples.length - 1)) * SPARK_W);
    const y = (v) => SPARK_H - ((v - lo) / span) * SPARK_H;

    [0.25, 0.5, 0.75].forEach((f) => {
      const gy = (SPARK_H * f).toFixed(1);
      add("line", { class: "spark-grid", x1: 0, y1: gy, x2: SPARK_W, y2: gy });
    });

    const pts = samples.map((v, i) => x(i).toFixed(2) + "," + y(v).toFixed(2));
    add("polygon", {
      class: "spark-fill",
      points: pts.join(" ") + " " + SPARK_W + "," + SPARK_H + " 0," + SPARK_H,
    });
    add("polyline", { class: "spark-line", points: pts.join(" ") });

    const lastX = x(samples.length - 1).toFixed(2);
    const lastY = y(samples[samples.length - 1]);
    add("line", {
      class: "spark-head",
      x1: lastX,
      y1: Math.max(2, lastY - 7).toFixed(2),
      x2: lastX,
      y2: Math.min(SPARK_H - 2, lastY + 7).toFixed(2),
    });

    const current = samples[samples.length - 1];
    refs.sparkStats.textContent =
      samples.length < 2
        ? "now " + fmtLatency(current) + " \u00b7 1 sample"
        : "min " + fmtLatency(lo0) + " \u00b7 max " + fmtLatency(hi0) +
          " \u00b7 now " + fmtLatency(current) + " \u00b7 " + samples.length + " samples";
  }

  /** Card chrome shared by all three: error strip + retry, failure pulse, badge tone. */
  function renderChrome(key, card) {
    const refs = ui.cards[key];
    if (!refs.root) return;
    const failed = !!card.lastError;
    refs.root.classList.toggle("is-error", failed);
    refs.root.classList.toggle("is-loading", card.loading);
    if (failed) {
      refs.error.hidden = false;
      const detail = card.errorDetail || card.lastError;
      refs.errorText.textContent = clip(detail, 150);
      refs.errorText.title = detail;
    } else {
      refs.error.hidden = true;
    }
    const retry = refs.error ? refs.error.querySelector(".btn-retry") : null;
    if (retry) {
      retry.disabled = card.loading;
      retry.textContent = card.loading ? "Retrying\u2026" : "Retry";
    }
  }

  function setBadge(refs, tone, text) {
    refs.badge.classList.remove("badge-ok", "badge-warn", "badge-bad", "badge-neutral");
    refs.badge.classList.add("badge-" + tone);
    refs.badge.textContent = text;
  }

  /**
   * Body to render for a card. A failed poll never blanks a card: if we ever got
   * a good payload we keep showing it (the card is marked stale/error) and only
   * fall back to the failed envelope when there is nothing better.
   */
  function bodyOf(card, env) {
    if (env.ok || !card.hasGood) return env.body;
    return card.lastGood;
  }

  // --- per-card rendering --------------------------------------------------

  function renderHealth(card) {
    const refs = ui.cards.health;
    const env = card.envelope;
    if (!env || env.aborted) return;

    const body = bodyOf(card, env);
    let label = null;
    if (body && typeof body === "object" && !Array.isArray(body)) {
      const raw =
        body.status !== undefined ? body.status : body.state !== undefined ? body.state : null;
      if (raw !== null && raw !== undefined) label = String(raw);
    } else if (typeof body === "string") {
      label = clip((body || "").trim().split("\n")[0], 32) || "empty response";
    }
    if (label === null || label === "") label = env.ok ? "reachable" : "unreachable";

    const healthy = env.ok && /^(ok|healthy|online|ready|alive|up|true|1)$/i.test(label.trim());
    refs.status.textContent = label;
    refs.status.className = "metric-value " + (!env.ok ? "t-bad" : healthy ? "t-ok" : "t-warn");

    refs.code.textContent = env.status === null || env.status === undefined ? "\u2014" : String(env.status);
    refs.latency.textContent = fmtLatency(env.latency_ms);
    refs.latency.className =
      "metric-value num" + (isNum(env.latency_ms) && env.latency_ms > 2000 ? " t-warn" : "");
    refs.last.textContent = card.lastOkAt ? fmtClock(new Date(card.lastOkAt)) : "\u2014";

    const entries = flatten(body, 6).filter((r) => !/^(status|state)$/i.test(leaf(r.key)));
    renderKv(refs.extra, entries, 6);

    setBadge(refs, env.ok ? (healthy ? "ok" : "warn") : "bad", env.ok ? "up" : "down");
    renderChrome("health", card);
    renderSpark(refs, card.history);
  }

  /** Find the model array in an OpenAI-shaped (or looser) payload. */
  function modelRows(body) {
    if (Array.isArray(body)) return { list: body, shape: "array" };
    if (body && typeof body === "object") {
      if (Array.isArray(body.data)) return { list: body.data, shape: "list" };
      if (Array.isArray(body.models)) return { list: body.models, shape: "list" };
      if (body.data && typeof body.data === "object" && Array.isArray(body.data.data)) {
        return { list: body.data.data, shape: "list" };
      }
    }
    return { list: null, shape: "unknown" };
  }

  function renderModels(card) {
    const refs = ui.cards.models;
    const env = card.envelope;
    if (!env || env.aborted) return;

    const found = modelRows(bodyOf(card, env));
    const rows = refs.tableBody;
    while (rows.firstChild) rows.removeChild(rows.firstChild);

    if (found.list && found.list.length) {
      refs.tableWrap.hidden = false;
      refs.empty.hidden = true;
      const text = (v) => (v === undefined || v === null || v === "" ? "\u2014" : String(v));
      found.list.slice(0, 200).forEach((entry, i) => {
        const tr = document.createElement("tr");
        const obj = entry && typeof entry === "object" ? entry : {};
        const id = entry && typeof entry === "object" ? text(obj.id !== undefined ? obj.id : obj.name) : text(entry);
        const cells = [
          clip(id === "\u2014" ? "#" + i : id, 96),
          entry && typeof entry === "object" ? text(obj.object) : "\u2014",
          entry && typeof entry === "object"
            ? text(obj.owned_by !== undefined ? obj.owned_by : obj.ownedBy)
            : "\u2014",
          entry && typeof entry === "object" ? fmtCreated(obj.created) : "\u2014",
        ];
        cells.forEach((value, col) => {
          const td = document.createElement("td");
          if (col >= 1) td.className = "num";
          td.textContent = value;
          tr.appendChild(td);
        });
        rows.appendChild(tr);
      });
      setBadge(refs, env.ok ? "ok" : "warn", found.list.length + (found.list.length === 1 ? " model" : " models"));
    } else {
      refs.tableWrap.hidden = true;
      refs.empty.hidden = false;
      refs.empty.textContent =
        found.shape === "unknown"
          ? "no model list in the response"
          : "no models served";
      setBadge(refs, "warn", found.shape === "unknown" ? "?" : "0 models");
    }

    renderChrome("models", card);
    renderSpark(refs, card.history);
  }

  function renderCache(card) {
    const refs = ui.cards.cache;
    const env = card.envelope;
    if (!env || env.aborted) return;

    const data = bodyOf(card, env);
    const entries = flatten(data, 24);
    const gauge = deriveGauge(entries);

    if (gauge && gauge.pct !== null && gauge.pct !== undefined) {
      const pct = clamp(gauge.pct, 0, 100);
      refs.barFill.style.width = pct.toFixed(1) + "%";
      refs.bar.setAttribute("aria-valuenow", String(Math.round(pct)));
      refs.bar.classList.remove("is-unknown");
      refs.bar.classList.toggle("is-warn", pct >= 70 && pct < 90);
      refs.bar.classList.toggle("is-bad", pct >= 90);
      const shown =
        pct.toFixed(Math.abs(pct - Math.round(pct)) < 0.05 ? 0 : 1) + "%";
      refs.gaugeValue.textContent = shown;
      refs.gaugeLabel.textContent = gauge.label;
      refs.gaugeSub.textContent = gauge.sub;
      setBadge(
        refs,
        env.ok ? (pct >= 90 ? "bad" : pct >= 70 ? "warn" : "ok") : "bad",
        shown + " used"
      );
    } else {
      refs.barFill.style.width = "0%";
      refs.bar.classList.add("is-unknown");
      refs.bar.removeAttribute("aria-valuenow");
      refs.bar.classList.remove("is-warn", "is-bad");
      refs.gaugeValue.textContent = "\u2014";
      refs.gaugeLabel.textContent = gauge ? gauge.label : "utilization";
      refs.gaugeSub.textContent = entries.length
        ? "no usage + total pair to divide \u2014 every field is listed below"
        : "no fields in the response \u2014 raw payload below";
      setBadge(refs, env.ok ? "neutral" : "bad", env.ok ? "no schema" : "down");
    }

    renderKv(refs.kv, entries, 24);
    setRaw(refs, { body: data, error: env.error });
    refs.badge.title = env.url || "";
    renderChrome("cache", card);
    renderSpark(refs, card.history);
  }

  const RENDERERS = { health: renderHealth, models: renderModels, cache: renderCache };

  /** Wipe a card's data area (used when the target changes and we are re-polling). */
  function resetCardBody(key) {
    const refs = ui.cards[key];
    setBadge(refs, "neutral", "\u2026");
    refs.stale.hidden = true;
    const clear = (node) => {
      while (node && node.firstChild) node.removeChild(node.firstChild);
    };
    if (key === "health") {
      [refs.status, refs.code, refs.latency, refs.last, refs.uptime].forEach((node) => {
        node.textContent = "\u2014";
        node.className = "metric-value num";
      });
      clear(refs.extra);
    } else if (key === "models") {
      clear(refs.tableBody);
      refs.tableWrap.hidden = true;
      refs.empty.hidden = false;
      refs.empty.textContent = "awaiting /v1/models";
    } else {
      refs.barFill.style.width = "0%";
      refs.bar.classList.add("is-unknown");
      refs.bar.classList.remove("is-warn", "is-bad");
      refs.gaugeValue.textContent = "\u2014";
      refs.gaugeLabel.textContent = "utilization";
      refs.gaugeSub.textContent = "awaiting /cache data";
      clear(refs.kv);
      refs.raw.hidden = true;
    }
  }

  function renderCard(key) {
    const card = state.cards[key];
    if (!card.envelope) {
      // Nothing polled yet (or the target just changed): keep the scaffolding
      // visible with "awaiting data" charts rather than leaving old values up.
      resetCardBody(key);
      renderSpark(ui.cards[key], []);
      renderChrome(key, card);
      return;
    }
    RENDERERS[key](card);
  }

  // --- top bar, banner, clock ---------------------------------------------

  function renderTarget() {
    const shown = state.upstream || state.configUpstream || "monitor default";
    const effective = state.upstream || state.configUpstream;
    ui.target.textContent = shown;
    ui.target.title = state.upstream
      ? "polling " + shown + " (endpoint selector)"
      : "no override set \u2014 monitor/serve.py is using its own --url";
    if (document.activeElement !== ui.input) ui.input.value = effective || "";
    Array.prototype.forEach.call(ui.presets.querySelectorAll(".btn-preset"), (btn) => {
      btn.setAttribute("aria-pressed", btn.dataset.url === effective ? "true" : "false");
    });
  }

  function statusInfo() {
    const attempted = ENDPOINTS.some((ep) => state.cards[ep.key].attempts > 0);
    if (!attempted) {
      return ["pill-pending", state.pollMs === 0 ? "paused" : "waiting for first poll"];
    }
    if (state.cards.health.lastError) {
      return ["pill-down", "down \u2014 " + clip(state.cards.health.lastError, 70)];
    }
    const failing = ENDPOINTS.filter((ep) => state.cards[ep.key].lastError).map((ep) => ep.label);
    const stale = ENDPOINTS.some((ep) => state.cards[ep.key].stale);
    if (failing.length) return ["pill-degraded", "degraded \u2014 " + failing.join(" / ") + " failing"];
    if (stale) return ["pill-degraded", "stale \u2014 no fresh poll"];
    if (state.pollMs === 0) return ["pill-healthy", "healthy \u00b7 polling paused"];
    return ["pill-healthy", "healthy"];
  }

  function renderStatus() {
    const info = statusInfo();
    ui.statusPill.className = "pill " + info[0];
    ui.statusText.textContent = info[1];
  }

  function renderBanner() {
    const polled = ENDPOINTS.filter((ep) => state.cards[ep.key].attempts > 0).length;
    const failing = ENDPOINTS.filter((ep) => state.cards[ep.key].lastError);
    if (polled === ENDPOINTS.length && failing.length === ENDPOINTS.length) {
      const first = state.cards[failing[0].key];
      ui.banner.hidden = false;
      ui.bannerText.textContent =
        " nothing answered on " +
        (state.upstream || state.configUpstream || "the configured upstream") +
        " \u2014 last error: " +
        clip(first.errorDetail || first.lastError, 130);
    } else {
      ui.banner.hidden = true;
    }

    // A rejected ?url= override belongs next to the field, not only in the banner.
    ENDPOINTS.forEach((ep) => {
      const env = state.cards[ep.key].envelope;
      if (env && env.status === 400) showFieldError(clip(env.error || "invalid upstream URL", 160));
    });
  }

  function renderClock() {
    const t = nowMs();
    ui.lastUpdated.textContent = state.lastPollAt
      ? "updated " + fmtClock(new Date(state.lastPollAt))
      : "never updated";
    if (state.pollMs === 0) {
      ui.nextPoll.textContent = "polling paused";
    } else if (state.busy) {
      ui.nextPoll.textContent = "polling\u2026";
    } else if (state.nextPollAt) {
      ui.nextPoll.textContent = "next poll in " + mmss((state.nextPollAt - t) / 1000);
    } else {
      ui.nextPoll.textContent = "\u2014";
    }
    ui.footUptime.textContent = "page up " + mmss((t - state.startedAt) / 1000);
    if (ui.cards.health.uptime) {
      ui.cards.health.uptime.textContent = state.firstOkAt
        ? mmss((t - state.firstOkAt) / 1000)
        : "\u2014";
    }
  }

  /** Stale = last success older than 3 poll intervals (never while paused). */
  function refreshStale() {
    const t = nowMs();
    ENDPOINTS.forEach((ep) => {
      const card = state.cards[ep.key];
      const refs = ui.cards[ep.key];
      card.stale =
        state.pollMs > 0 && card.lastOkAt > 0 && t - card.lastOkAt > STALE_FACTOR * state.pollMs;
      refs.root.classList.toggle("is-stale", card.stale);
      refs.root.classList.toggle("is-paused", state.pollMs === 0 && card.attempts > 0);
      refs.stale.hidden = !card.stale;
      if (card.stale) {
        refs.stale.textContent = "stale \u00b7 " + mmss((t - card.lastOkAt) / 1000) + " old";
      }
    });
  }

  function renderAll() {
    refreshStale();
    ENDPOINTS.forEach((ep) => renderCard(ep.key));
    renderTarget();
    renderStatus();
    renderBanner();
    renderClock();
  }

  // --- polling -------------------------------------------------------------

  function ingest(key, env) {
    const card = state.cards[key];
    card.loading = false;
    if (!env || env.aborted) return;
    card.attempts += 1;
    card.envelope = env;
    if (env.ok) {
      card.lastOkAt = nowMs();
      if (!state.firstOkAt) state.firstOkAt = card.lastOkAt;
      card.lastError = null;
      card.errorDetail = null;
      card.lastGood = env.body;
      card.hasGood = true;
    } else {
      card.lastError = env.error || "request failed";
      card.errorDetail = card.lastError + (env.url ? "  [" + env.url + "]" : "");
    }
    if (isNum(env.latency_ms)) {
      card.history.push(env.latency_ms);
      if (card.history.length > HISTORY_SAMPLES) card.history.shift();
    }
  }

  function scheduleNext(delay) {
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
    if (state.pollMs === 0) {
      state.nextPollAt = 0; // paused: nothing is scheduled until the interval changes
      return;
    }
    state.nextPollAt = nowMs() + delay;
    state.timer = setTimeout(() => {
      state.timer = null;
      pollAll();
    }, delay);
  }

  function abortAll(controllers) {
    controllers.forEach((ctrl) => {
      try {
        if (ctrl) ctrl.abort();
      } catch (_) {
        /* ignore */
      }
    });
  }

  /**
   * One poll of all three endpoints, used by the timer, by Apply and by
   * "Refresh now". Anything still in flight is aborted first and a generation
   * counter drops late answers, so two cycles can never overlap or interleave.
   */
  async function pollAll() {
    const generation = state.generation;
    abortAll([state.cycle].concat(Array.from(state.retries.values())));
    state.retries.clear();
    const ctrl = makeAbort();
    state.cycle = ctrl;
    state.busy = true;
    ENDPOINTS.forEach((ep) => {
      state.cards[ep.key].loading = true;
    });
    renderAll();

    const signal = ctrl ? ctrl.signal : undefined;
    const results = await Promise.all(
      ENDPOINTS.map(async (ep) => [ep.key, await fetchEnvelope(ep.route, { signal: signal, useUpstream: true })])
    );

    state.busy = false;
    if (state.generation !== generation || (signal && signal.aborted)) return;

    results.forEach((pair) => ingest(pair[0], pair[1]));
    state.lastPollAt = nowMs();
    renderAll();
    scheduleNext(state.pollMs);
  }

  /** Retry a single card without disturbing the global schedule. */
  async function pollOne(key) {
    const ep = ENDPOINTS.filter((e) => e.key === key)[0];
    if (!ep) return;
    const generation = state.generation;
    const previous = state.retries.get(key);
    if (previous) {
      try {
        previous.abort();
      } catch (_) {
        /* ignore */
      }
    }
    const ctrl = makeAbort();
    state.retries.set(key, ctrl);
    state.cards[key].loading = true;
    renderCard(key);
    renderClock();

    const env = await fetchEnvelope(ep.route, { signal: ctrl ? ctrl.signal : undefined, useUpstream: true });
    if (state.retries.get(key) === ctrl) state.retries.delete(key);
    if (state.generation !== generation || (ctrl && ctrl.signal.aborted)) return;

    ingest(key, env);
    state.lastPollAt = nowMs();
    renderCard(key);
    renderStatus();
    renderBanner();
    renderClock();
  }

  /** Point every card at a new base URL and poll it immediately (no reload). */
  function retarget(upstream) {
    state.upstream = upstream;
    state.generation += 1;
    state.firstOkAt = 0;
    storeSet(LS_UPSTREAM, upstream);
    ENDPOINTS.forEach((ep) => {
      const card = state.cards[ep.key];
      card.attempts = 0;
      card.envelope = null;
      card.history = [];
      card.lastOkAt = 0;
      card.lastError = null;
      card.errorDetail = null;
      card.lastGood = null;
      card.hasGood = false;
      card.stale = false;
    });
    abortAll([state.cycle].concat(Array.from(state.retries.values())));
    state.retries.clear();
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
    renderAll();
    pollAll();
  }

  // --- controls ------------------------------------------------------------

  /**
   * Mirror of normalize_upstream() in monitor/serve.py: absolute http(s) URL with
   * a host, query/fragment dropped, trailing slashes trimmed. "" is valid and
   * means "poll without ?url= at all, i.e. whatever serve.py was started with".
   * Returns null when the value is unusable.
   */
  function normalizeUpstream(raw) {
    const text = String(raw === null || raw === undefined ? "" : raw).trim();
    if (!text) return "";
    let url;
    try {
      url = new URL(text);
    } catch (_) {
      return null;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    if (!url.host) return null;
    const path = (url.pathname || "").replace(/\/+$/, "");
    return url.protocol + "//" + url.host + path;
  }

  function showFieldError(message) {
    ui.fieldError.textContent = message;
    ui.fieldError.hidden = false;
    ui.input.setAttribute("aria-invalid", "true");
  }

  function clearFieldError() {
    ui.fieldError.textContent = "";
    ui.fieldError.hidden = true;
    ui.input.removeAttribute("aria-invalid");
  }

  /** Validate + apply an upstream: persist it and re-poll every card at once. */
  function applyUpstream(raw) {
    const normalized = normalizeUpstream(raw);
    if (normalized === null) {
      showFieldError(
        'not a valid http(s) URL \u2014 include the scheme, e.g. http://localhost:8000'
      );
      return false;
    }
    clearFieldError();
    retarget(normalized);
    return true;
  }

  function buildPresets() {
    PRESETS.forEach((preset) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-preset";
      btn.textContent = preset.label;
      btn.dataset.url = preset.url;
      btn.title = preset.url + "\n" + preset.hint;
      btn.setAttribute("aria-pressed", "false");
      btn.addEventListener("click", () => applyUpstream(preset.url));
      ui.presets.appendChild(btn);
    });
  }

  function buildPollButtons() {
    POLL_OPTIONS.forEach((option) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn";
      btn.textContent = option.label;
      btn.dataset.ms = String(option.ms);
      btn.setAttribute("aria-pressed", "false");
      btn.setAttribute("aria-label", option.ms === 0 ? "pause polling" : "poll every " + option.label);
      btn.addEventListener("click", () => setPollMs(option.ms));
      ui.pollGroup.appendChild(btn);
    });
  }

  function renderPollButtons() {
    Array.prototype.forEach.call(ui.pollGroup.querySelectorAll("button"), (btn) => {
      btn.setAttribute("aria-pressed", Number(btn.dataset.ms) === state.pollMs ? "true" : "false");
    });
  }

  function setPollMs(ms) {
    state.pollMs = ms;
    storeSet(LS_POLL_MS, String(ms));
    renderPollButtons();
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
    if (ms === 0) {
      state.nextPollAt = 0;
      renderAll();
      return;
    }
    pollAll(); // polls now, and re-arms the timer with the new interval
  }

  // --- bootstrap -----------------------------------------------------------

  /** Read the server-side default so the endpoint selector starts pre-filled. */
  async function loadConfig() {
    const env = await fetchEnvelope("api/config", {});
    if (env.ok && env.body && typeof env.body === "object") {
      state.configUpstream = env.body.upstream || env.body.default_upstream || "";
    }
    renderTarget();
  }

  function init() {
    buildPresets();
    buildPollButtons();

    const storedMs = storeGetInt(LS_POLL_MS);
    if (storedMs !== null && POLL_OPTIONS.some((o) => o.ms === storedMs)) {
      state.pollMs = storedMs;
    }
    renderPollButtons();

    const storedUpstream = storeGet(LS_UPSTREAM);
    if (storedUpstream) {
      const normalized = normalizeUpstream(storedUpstream);
      if (normalized) state.upstream = normalized;
    }

    ui.form.addEventListener("submit", (event) => {
      event.preventDefault();
      applyUpstream(ui.input.value);
    });
    ui.input.addEventListener("input", clearFieldError);
    ui.refresh.addEventListener("click", () => pollAll());
    Array.prototype.forEach.call(document.querySelectorAll(".btn-retry"), (btn) => {
      btn.addEventListener("click", () => pollOne(btn.dataset.endpoint));
    });

    // Anything that escapes a handler should surface on the page, not only in the
    // console, and must not leave the dashboard looking frozen.
    window.addEventListener("error", (event) => {
      if (!ui.banner.hidden) return;
      ui.banner.hidden = false;
      ui.bannerText.textContent =
        " the dashboard hit an unexpected error: " + clip((event.message || "unknown error") , 140);
    });

    renderAll();
    setInterval(() => {
      refreshStale();
      renderClock();
    }, 1000);

    loadConfig().then(() => pollAll());
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
