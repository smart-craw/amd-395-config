/**
 * Front-end tests for monitor/static/app.js, run with `node --test`.
 *
 *   node --test "monitor/tests/*.test.mjs"
 *
 * There is no browser in this repo, so these tests drive the real app.js inside
 * a small DOM (see frontend_dom.mjs) against a real monitor/serve.py and mock
 * model servers, then assert on what the page actually renders. This is the
 * part no Python test can reach: the first paint, the degraded paths (dead
 * upstream, unexpected /cache shape, invalid input), and the "zero uncaught
 * errors" rule.
 */

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import net from "node:net";
import path from "node:path";
import { after, before, describe, test } from "node:test";
import { fileURLToPath } from "node:url";

import { loadPage, memoryStorage, textOf, waitFor } from "./frontend_dom.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATIC_DIR = path.join(HERE, "..", "static");
const REPO_ROOT = path.join(HERE, "..", "..");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

/** Every child we start; killed in the root after() so node can exit. */
const children = [];

/** Spawn a python helper and resolve with the URL it announces. */
function spawnPython(args, { name, port }) {
  const child = spawn("python3", args, { cwd: REPO_ROOT });
  children.push(child);
  const seen = [];
  let timer;
  const started = new Promise((resolve, reject) => {
    let buffer = "";
    let settled = false;
    const consume = (chunk) => {
      buffer += String(chunk);
      let index;
      while ((index = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 1);
        if (!line) continue;
        seen.push(line);
        // serve.py announces on stderr, mock_upstream.py on stdout; watch both.
        const match = line.match(/https?:\/\/\S+/);
        if (match && !settled) {
          settled = true;
          clearTimeout(timer);
          resolve(match[0].replace(/\)$/, ""));
        }
      }
    };
    child.stdout.on("data", consume);
    child.stderr.on("data", consume);
    child.once("exit", (code) => {
      if (!settled) {
        settled = true;
        reject(new Error(`${name} exited early (${code}):\n${seen.join("\n")}`));
      }
    });
    timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new Error(`${name} did not report a URL within 20s:\n${seen.join("\n")}`));
      }
    }, 20000);
    if (timer.unref) timer.unref();
  });
  return { child, started, port };
}

const ENV = {};

async function startMock(mode) {
  const port = await freePort();
  return spawnPython(
    [path.join("monitor", "tests", "mock_upstream.py"), "--port", String(port), "--mode", mode],
    { name: `mock ${mode}`, port }
  );
}

before(async () => {
  const healthy = await startMock("healthy");
  const weird = await startMock("weird");
  const port = await freePort();
  const monitor = spawnPython(
    [
      path.join("monitor", "serve.py"),
      "--host", "127.0.0.1",
      "--port", String(port),
      "--url", await healthy.started,
    ],
    { name: "monitor/serve.py", port }
  );

  ENV.healthy = await healthy.started;
  ENV.weird = await weird.started;
  ENV.monitor = await monitor.started;
  for (const [name, url] of Object.entries({ ...ENV })) {
    assert.match(String(url), /^http:\/\/[\d.]+:\d+$/, `${name} URL looks wrong`);
  }
  ENV.dead = "http://127.0.0.1:9"; // nothing listens on the discard port
  // Keep the pipes from holding the event loop open; the after() hook kills all.
  for (const child of children) child.unref();
});

after(() => {
  for (const child of children) {
    try {
      child.kill();
    } catch (_) {
      /* already gone */
    }
  }
});

/** A freshly parsed page, pointed at the running monitor. */
function boot({ storage } = {}) {
  return loadPage({
    staticDir: STATIC_DIR,
    baseURI: `${ENV.monitor}/`,
    storage: storage || memoryStorage(),
  });
}

function card(page, key) {
  const id = (suffix) => page.byId(`${key}-${suffix}`);
  return {
    root: page.byId(`card-${key}`),
    badge: id("badge"),
    status: id("status"),
    code: id("code"),
    latency: id("latency"),
    last: id("last"),
    extra: id("extra"),
    spark: id("spark"),
    sparkStats: id("spark-stats"),
    error: id("error"),
    errorText: id("error-text"),
    stale: id("stale"),
    kv: id("kv"),
    tableBody: id("body"),
    tableWrap: page.byId(`card-${key}`).querySelector(".table-wrap"),
    empty: id("empty"),
    bar: id("bar"),
    barFill: id("bar-fill"),
    gaugeLabel: id("gauge-label"),
    gaugeValue: id("gauge-value"),
    gaugeSub: id("gauge-sub"),
    raw: id("raw"),
    rawJson: id("raw-json"),
  };
}

function kvRows(cacheCard) {
  return cacheCard.kv.children.map((row) => [
    row.children[0].textContent,
    row.children[1].textContent,
  ]);
}

/** Point every card at a new upstream, exactly as the endpoint selector does. */
function retarget(page, url) {
  const input = page.byId("upstream-input");
  input.value = url;
  page.byId("upstream-form").dispatch("submit");
  return input;
}

/** The first poll cycle is over (healthy / degraded / paused, anything but pending). */
function settled(page) {
  return waitFor(
    () => /^(healthy|down|degraded|stale)/.test(String(textOf(page.byId("status-text")))),
    { what: "the first poll to finish" }
  );
}

/** Two completed polls, so the sparklines have a trend to draw. */
async function twoPolls(page) {
  await settled(page);
  // "Refresh now" instead of waiting 5 s for the next scheduled tick.
  page.byId("refresh-now").dispatch("click");
  return waitFor(
    () => /min .* max .* now .* samples/.test(String(textOf(page.byId("health-spark-stats")))),
    { what: "a second latency sample" }
  );
}

describe("dashboard: healthy upstream", () => {
  let page;

  before(async () => {
    page = boot();
    await twoPolls(page);
  });

  after(() => page.dispose());

  test("boots and polls without a single uncaught error", () => {
    assert.deepEqual(page.window.errors, []);
    assert.deepEqual(page.window.warnings, []);
  });

  test("the status pill reads healthy", () => {
    assert.match(page.byId("status-pill").className, /pill-healthy/);
    assert.equal(textOf(page.byId("status-text")), "healthy");
  });

  test("the endpoint selector is pre-filled from /api/config", () => {
    assert.equal(page.byId("upstream-input").value, ENV.healthy);
    assert.equal(textOf(page.byId("target-upstream")), ENV.healthy);
  });

  test("health card shows status, http code, latency and last check", () => {
    const health = card(page, "health");
    assert.equal(textOf(health.status), "ok");
    assert.match(health.status.className, /t-ok/);
    assert.equal(textOf(health.code), "200");
    assert.match(textOf(health.latency), /\d+(\.\d+)? (ms|s)/);
    assert.match(textOf(health.last), /\d\d:\d\d:\d\d/);
    assert.equal(textOf(health.badge), "up");
    // /health fields beyond status are listed rather than dropped.
    assert.match(health.extra.textContent, /load/);
  });

  test("models card lists every model the server reports", () => {
    const models = card(page, "models");
    const rows = models.tableBody.children;
    assert.equal(rows.length, 2);
    assert.equal(rows[0].children[0].textContent, "qwen3-27b");
    assert.equal(rows[1].children[0].textContent, "gemma4-26b");
    assert.equal(rows[0].children[1].textContent, "model");
    assert.equal(rows[0].children[2].textContent, "local");
    // `created` is rendered human-readable, not as a raw unix timestamp.
    assert.match(rows[0].children[3].textContent, /^\d{4}-\d{2}-\d{2} \d\d:\d\d:\d\d/);
    assert.match(textOf(models.badge), /^2 models$/);
    assert.equal(models.empty.hidden, true);
  });

  test("cache card turns tokens + n_ctx into a labelled gauge", () => {
    const cache = card(page, "cache");
    assert.equal(textOf(cache.gaugeLabel), "tokens / n_ctx");
    assert.equal(textOf(cache.gaugeValue), "62.5%");
    assert.equal(cache.barFill.style.width, "62.5%");
    assert.equal(cache.bar.getAttribute("aria-valuenow"), "63");
    assert.match(textOf(cache.gaugeSub), /5,123 \/ 8,192/);
    assert.match(textOf(cache.badge), /62\.5% used/);
    assert.ok(cache.kv.children.length >= 4);
    assert.match(cache.kv.textContent, /n_ctx/);
  });

  test("every card draws a sparkline with min/max/current", () => {
    for (const key of ["health", "models", "cache"]) {
      const c = card(page, key);
      const drawn = c.spark.children.map((el) => el.tagName);
      assert.ok(drawn.length > 0, `${key} sparkline is empty`);
      assert.ok(drawn.includes("POLYLINE"), `${key} sparkline has no line`);
      assert.ok(drawn.includes("POLYGON"), `${key} sparkline has no area`);
      assert.match(textOf(c.sparkStats), /min .* max .* now .* samples/);
    }
  });

  test("nothing is in an error or stale state", () => {
    for (const key of ["health", "models", "cache"]) {
      const c = card(page, key);
      assert.equal(c.error.hidden, true, `${key} shows an error strip`);
      assert.equal(c.stale.hidden, true, `${key} is marked stale`);
      assert.equal(c.root.classList.contains("is-error"), false, `${key} is marked failed`);
    }
    assert.equal(page.byId("banner").hidden, true);
  });

  test("poll controls are built, one is pressed, presets exist", () => {
    const buttons = page.byId("poll-interval").children;
    assert.deepEqual(buttons.map((b) => b.textContent), ["2s", "5s", "15s", "paused"]);
    assert.equal(
      buttons.filter((b) => b.getAttribute("aria-pressed") === "true").length,
      1,
      "exactly one poll interval is selected"
    );
    const presets = page.byId("preset-row").children;
    assert.equal(presets.length, 3);
    assert.deepEqual(
      presets.map((b) => b.dataset.url),
      ["http://llm.home:8001", "http://localhost:8000", "http://localhost:8080"]
    );
  });

  test("the header counts down to the next poll", () => {
    assert.match(textOf(page.byId("last-updated")), /^updated \d\d:\d\d:\d\d$/);
    assert.match(textOf(page.byId("next-poll")), /^next poll in \d\d:\d\d$/);
    assert.match(textOf(page.byId("foot-uptime")), /^page up \d\d:\d\d$/);
  });

  test("pausing polling is reflected in the header and persisted", async () => {
    const storage = memoryStorage();
    const paused = boot({ storage });
    try {
      await twoPolls(paused);
      const pause = paused
        .byId("poll-interval")
        .children.find((button) => button.textContent === "paused");
      pause.dispatch("click");
      await waitFor(() => /paused/.test(String(textOf(paused.byId("status-text")))), {
        what: "the paused status",
      });
      assert.match(textOf(paused.byId("next-poll")), /polling paused/);
      assert.equal(storage._dump()["llmmonitor.pollMs"], "0", "interval must persist");
    } finally {
      paused.dispose();
    }
  });
});

describe("dashboard: retargeting from the endpoint selector", () => {
  let page;
  let storage;

  before(async () => {
    storage = memoryStorage();
    page = boot({ storage });
    await twoPolls(page);
    assert.match(card(page, "cache").kv.textContent, /n_ctx/);
    retarget(page, `${ENV.weird}/`); // a trailing slash must be tolerated
    await waitFor(() => /usage_percent/.test(card(page, "cache").kv.textContent), {
      what: "every card to follow the new upstream",
    });
  });

  after(() => page.dispose());

  test("the switch happens without a reload and survives one", () => {
    assert.equal(textOf(page.byId("target-upstream")), ENV.weird);
    assert.equal(page.byId("upstream-input").value, ENV.weird);
    assert.equal(storage._dump()["llmmonitor.upstream"], ENV.weird, "must survive a reload");
    assert.equal(page.byId("upstream-error").hidden, true);
    assert.match(page.byId("status-pill").className, /pill-healthy/);
    assert.deepEqual(page.window.errors, []);
  });

  test("an unexpected /cache shape renders a populated grid, not a blank card", () => {
    const cache = card(page, "cache");
    const rows = kvRows(cache);
    assert.ok(rows.length >= 5, `expected a populated grid, got ${rows.length} rows`);
    assert.deepEqual(rows.map(([key]) => key), [
      "usage_percent",
      "slots[0]",
      "slots[1]",
      "nested.deep",
      "note",
      "flag",
    ]);
    // nested and odd-typed values are described rather than hidden
    assert.equal(rows.find(([key]) => key === "note")[1], "null");
    assert.equal(rows.find(([key]) => key === "flag")[1], "true");
    assert.match(rows.find(([key]) => key === "nested.deep")[1], /object/);
  });

  test("a percentage-shaped field is promoted to the progress bar", () => {
    const cache = card(page, "cache");
    assert.equal(textOf(cache.gaugeLabel), "usage_percent");
    assert.equal(textOf(cache.gaugeValue), "62.5%");
    assert.equal(cache.barFill.style.width, "62.5%");
    assert.match(textOf(cache.gaugeSub), /reported directly/);
    assert.equal(cache.raw.hidden, false, "the raw payload stays available too");
    assert.match(textOf(cache.rawJson), /usage_percent/);
  });

  test("the other cards follow the new upstream as well", () => {
    assert.equal(card(page, "models").tableBody.children.length, 2);
    assert.equal(textOf(card(page, "health").badge), "up");
  });
});

describe("dashboard: unreachable upstream", () => {
  let page;

  before(async () => {
    page = boot();
    await twoPolls(page);
    retarget(page, ENV.dead);
    await waitFor(() => card(page, "health").error.hidden === false, {
      what: "the health card to report the failure",
    });
  });

  after(() => page.dispose());

  test("the failure path throws nothing", () => {
    assert.deepEqual(page.window.errors, []);
  });

  test("the pill reads down and the all-failure banner appears", () => {
    assert.match(page.byId("status-pill").className, /pill-down/);
    assert.match(textOf(page.byId("status-text")), /^down/);
    assert.equal(page.byId("banner").hidden, false);
    assert.match(textOf(page.byId("banner-text")), /nothing answered on http:\/\/127\.0\.0\.1:9/);
  });

  test("every card explains itself instead of going blank", () => {
    for (const key of ["health", "models", "cache"]) {
      const c = card(page, key);
      assert.equal(c.error.hidden, false, `${key} hides its error`);
      assert.ok(textOf(c.errorText).length > 10, `${key} has no error text`);
      assert.match(c.errorText.textContent, /unreachable|refused/);
      assert.match(c.root.className, /is-error/, `${key} is not marked as failed`);
    }
    assert.equal(textOf(card(page, "health").badge), "down");
    assert.match(textOf(card(page, "health").status), /unreachable/);
  });

  test("the cache card keeps a usable fallback", () => {
    const cache = card(page, "cache");
    assert.equal(cache.raw.hidden, false);
    assert.match(textOf(cache.rawJson), /no usable body/);
    assert.equal(textOf(cache.gaugeValue), "\u2014");
    assert.match(textOf(cache.gaugeSub), /no usage \+ total|no fields/);
    assert.match(textOf(cache.badge), /down/);
  });

  test("the retry button re-polls just that card", () => {
    const retry = card(page, "health").error.querySelector(".btn-retry");
    assert.ok(retry, "the error strip has no retry button");
    retry.dispatch("click");
    assert.equal(retry.textContent, "Retrying\u2026");
  });
});

describe("dashboard: input validation", () => {
  test("a value that is not an http(s) URL is refused with a field error", async () => {
    const page = boot();
    try {
      await twoPolls(page);
      retarget(page, "llm.home:8001"); // no scheme
      const field = page.byId("upstream-error");
      assert.equal(field.hidden, false);
      assert.match(textOf(field), /not a valid http\(s\) URL/);
      assert.equal(page.byId("upstream-input").getAttribute("aria-invalid"), "true");
      // The cards keep polling the previous target rather than going blank.
      assert.equal(card(page, "cache").error.hidden, true);
      // ...and typing again clears the message.
      page.byId("upstream-input").dispatch("input");
      await sleep(10);
      assert.equal(page.byId("upstream-error").hidden, true);
    } finally {
      page.dispose();
    }
  });
});
