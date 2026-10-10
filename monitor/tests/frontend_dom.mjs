/**
 * A miniature DOM, just big enough to run monitor/static/app.js under node.
 *
 * There is no browser (and no npm) in this repo, so the dashboard's logic is
 * exercised here instead: index.html is parsed into a real element tree, app.js
 * runs unmodified in a vm context against it, and the test drives the page
 * through DOM events while its polls hit a real monitor/serve.py over HTTP.
 *
 * That is enough to check the things only the front end can check: that the page
 * boots without a single uncaught error, that every id it looks up exists, that
 * a failed poll renders text instead of a blank card, and that an unrecognised
 * /cache shape still produces a populated grid.
 *
 * Only the DOM surface app.js actually touches is implemented -- anything else
 * throws loudly rather than silently doing nothing.
 */

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const VOID_TAGS = new Set([
  "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
  "meta", "param", "source", "track", "wbr",
]);

const ATTR_RE = /([:@.\-0-9a-zA-Z_]+)(?:\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+)))?/g;

/** One page's worth of element bookkeeping (several pages can be loaded). */
class Registry {
  constructor() {
    this.byId = new Map();
    this.all = [];
    this.document = null;
  }
}

let current = new Registry();

class ClassList {
  constructor(el) { this.el = el; }
  add(...names) { names.forEach((n) => n && this.el._classes.add(n)); }
  remove(...names) { names.forEach((n) => this.el._classes.delete(n)); }
  contains(name) { return this.el._classes.has(name); }
  toggle(name, force) {
    const want = force === undefined ? !this.el._classes.has(name) : !!force;
    if (want) this.el._classes.add(name);
    else this.el._classes.delete(name);
    return want;
  }
  toString() { return [...this.el._classes].join(" "); }
}

export class El {
  constructor(tag, attrs = {}) {
    this.registry = current;
    this.tagName = String(tag).toUpperCase();
    this.attributes = new Map();
    this._classes = new Set();
    this.classList = new ClassList(this);
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = {
      _props: {},
      setProperty(name, value) { this._props[name] = String(value); },
      getPropertyValue(name) { return this._props[name] || ""; },
    };
    this._text = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.title = "";
    this._listeners = new Map();
    for (const [name, value] of Object.entries(attrs)) this.setAttribute(name, value);
  }

  // -- attributes ----------------------------------------------------------

  setAttribute(name, value) {
    const key = String(name);
    this.attributes.set(key, String(value));
    if (key === "class") this.className = String(value);
    else if (key === "id") this.id = String(value);
    else if (key.startsWith("data-")) this.dataset[camel(key.slice(5))] = String(value);
  }
  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }
  removeAttribute(name) { this.attributes.delete(String(name)); }
  hasAttribute(name) { return this.attributes.has(name); }

  set className(value) {
    this._classes = new Set(String(value).split(/\s+/).filter(Boolean));
    this.attributes.set("class", this.classList.toString());
  }
  get className() { return this.classList.toString(); }

  set id(value) {
    this._id = String(value);
    this.attributes.set("id", this._id);
    this.registry.byId.set(this._id, this);
  }
  get id() { return this._id || ""; }

  // -- text / children -----------------------------------------------------

  set textContent(value) {
    this._text = value === null || value === undefined ? "" : String(value);
    this.children = [];
  }
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join("");
    return this._text;
  }
  get innerHTML() {
    return this.children.length ? this.children.map((c) => c.innerHTML).join("") : this._text;
  }
  set innerHTML(value) {
    // app.js never assigns innerHTML; if that ever changes, say so loudly
    // instead of quietly rendering nothing.
    throw new Error(`innerHTML is not supported by the test DOM (got ${JSON.stringify(value)})`);
  }
  insertAdjacentHTML() { throw new Error("insertAdjacentHTML is not supported by the test DOM"); }

  appendChild(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.children.push(node);
    this.registry.all.push(node);
    return node;
  }
  removeChild(node) {
    const i = this.children.indexOf(node);
    if (i >= 0) this.children.splice(i, 1);
    node.parentNode = null;
    return node;
  }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get childElementCount() { return this.children.length; }

  // -- events --------------------------------------------------------------

  addEventListener(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(fn);
  }
  removeEventListener(type, fn) {
    const list = this._listeners.get(type) || [];
    const i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
  }
  dispatch(type, extra = {}) {
    const event = {
      type,
      target: this,
      currentTarget: this,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() {},
      ...extra,
    };
    for (const fn of [...(this._listeners.get(type) || [])]) fn(event);
    return event;
  }

  // -- queries -------------------------------------------------------------

  descendants() {
    const out = [];
    const walk = (node) => node.children.forEach((c) => { out.push(c); walk(c); });
    walk(this);
    return out;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const isDocument = this === this.registry.document;
    let pool = isDocument ? this.registry.document.body.descendants() : this.descendants();
    for (const part of String(selector).split(",")) {
      const steps = part.trim().split(/\s+/);
      let found = pool;
      for (const step of steps) {
        found = found.filter((el) => matchesSimple(el, step));
        if (steps.length > 1) found = dedupe(found).flatMap((el) => el.descendants());
      }
      return dedupe(found);
    }
    return [];
  }
  closest(selector) {
    let node = this;
    while (node) {
      if (matchesSimple(node, selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
  matches(selector) { return matchesSimple(this, selector); }
  focus() {}
  blur() {}
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  scrollIntoView() {}
  getBoundingClientRect() { return { width: 300, height: 60, top: 0, left: 0 }; }
}

function dedupe(list) { return [...new Set(list)]; }

function camel(name) {
  return name.replace(/-([a-z])/g, (_m, c) => c.toUpperCase());
}

/** Supports "#id", ".class", "tag" and compounds like "#id.cls". */
function matchesSimple(el, selector) {
  const tokens = String(selector).match(/[.#]?[A-Za-z0-9_-]+/g) || [];
  return tokens.every((token) => {
    if (token.startsWith("#")) return el.id === token.slice(1);
    if (token.startsWith(".")) return el.classList.contains(token.slice(1));
    return el.tagName === token.toUpperCase();
  });
}

// --- HTML parsing (the subset needed to build index.html) -------------------

export function parseHtml(html) {
  current = new Registry();
  const body = new El("body");
  current.all.push(body);
  const stack = [body];
  const tokenRe = /<!--[\s\S]*?-->|<(\/?)([a-zA-Z][\w:-]*)((?:[^>"']|"[^"]*"|'[^']*')*?)(\/?)>/g;
  let token;
  while ((token = tokenRe.exec(html))) {
    const whole = token[0];
    if (whole.startsWith("<!--")) continue; // comments carry no groups
    const [, closing, rawTag, rawAttrs, selfClosing] = token;
    const tag = rawTag.toLowerCase();
    if (tag === "html" || tag === "head" || tag === "script" || tag === "style") {
      if (closing) {
        while (stack.length > 1 && stack[stack.length - 1].tagName !== tag.toUpperCase()) stack.pop();
        if (stack.length > 1) stack.pop();
      }
      continue;
    }
    if (closing) {
      while (stack.length > 1 && stack[stack.length - 1].tagName !== tag.toUpperCase()) stack.pop();
      if (stack.length > 1) stack.pop();
      continue;
    }
    const el = new El(tag, parseAttrs(rawAttrs || ""));
    stack[stack.length - 1].appendChild(el);
    if (!selfClosing && !VOID_TAGS.has(tag)) stack.push(el);
  }
  return body;
}

function parseAttrs(raw) {
  const attrs = {};
  let match;
  ATTR_RE.lastIndex = 0;
  while ((match = ATTR_RE.exec(raw))) {
    const name = match[1];
    const value = match[3] ?? match[4] ?? match[5] ?? "";
    if (name) attrs[name] = decodeEntities(value);
  }
  return attrs;
}

function decodeEntities(text) {
  return String(text)
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&");
}

// --- the window the page runs inside ----------------------------------------

export function memoryStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => { data.set(k, String(v)); },
    removeItem: (k) => { data.delete(k); },
    clear: () => data.clear(),
    key: (i) => [...data.keys()][i] ?? null,
    get length() { return data.size; },
    _dump: () => Object.fromEntries(data),
  };
}

class FakeWindow {
  constructor({ baseURI, localStorage }) {
    this.baseURI = baseURI;
    this.localStorage = localStorage || memoryStorage();
    this.navigator = { userAgent: "node-test-dom" };
    this.location = { href: baseURI };
    this.errors = [];
    this.warnings = [];
    this._listeners = new Map();
    this._timers = new Set();
  }

  addEventListener(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(fn);
  }
  removeEventListener() {}
  /** Fire a window-level listener (used for the "error" escape hatch). */
  fire(type, event = {}) { for (const fn of this._listeners.get(type) || []) fn(event); }

  // Timers are deliberately *not* unref'd: the page under test polls on a timer,
  // and an unref'd timer never fires while the loop idles. dispose() clears them
  // instead, so the test process can still exit.
  setTimeout(fn, ms) { const t = setTimeout(fn, ms); this._timers.add(t); return t; }
  setInterval(fn, ms) { const t = setInterval(fn, ms); this._timers.add(t); return t; }
  clearTimeout(t) { this._timers.delete(t); clearTimeout(t); }
  clearInterval(t) { this._timers.delete(t); clearInterval(t); }
  /** Stop everything this page scheduled; without it the runner never exits. */
  dispose() {
    for (const t of this._timers) {
      clearTimeout(t);
      clearInterval(t);
    }
    this._timers.clear();
    this.disposed = true;
  }
}

/**
 * Load monitor/static/app.js against a parsed index.html. The page polls for
 * real, so point baseURI at a running monitor/serve.py.
 *
 * @returns {{window: FakeWindow, document: object, byId: (id: string) => El}}
 */
export function loadPage({ staticDir, baseURI, storage, htmlOverride }) {
  const html = htmlOverride ?? fs.readFileSync(path.join(staticDir, "index.html"), "utf8");
  const script = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");

  const body = parseHtml(html);
  // This page's own registry: several pages can be loaded in one process, and
  // `current` moves on as soon as the next one is parsed.
  const reg = current;
  const document = {
    baseURI,
    readyState: "complete",
    documentElement: body,
    body,
    activeElement: null,
    addEventListener() {},
    removeEventListener() {},
    getElementById: (id) => reg.byId.get(id) || null,
    querySelector: (sel) => document.body.querySelectorAll(sel)[0] || null,
    querySelectorAll: (sel) => document.body.querySelectorAll(sel),
    createElement: (tag) => new El(tag),
    createElementNS: (_ns, tag) => new El(tag),
    createTextNode: (text) => { const el = new El("#text"); el.textContent = text; return el; },
  };
  current.document = document;

  const window = new FakeWindow({ baseURI, localStorage: storage || memoryStorage() });
  const sandbox = {
    window,
    document,
    navigator: window.navigator,
    location: window.location,
    localStorage: window.localStorage,
    console: {
      log: () => {},
      info: () => {},
      debug: () => {},
      trace: () => {},
      warn: (...a) => window.warnings.push(a.join(" ")),
      error: (...a) => window.errors.push(a.join(" ")),
    },
    fetch: globalThis.fetch,
    AbortController: globalThis.AbortController,
    URL,
    URLSearchParams,
    TextDecoder: globalThis.TextDecoder,
    setTimeout: (fn, ms) => window.setTimeout(fn, ms),
    clearTimeout: (t) => window.clearTimeout(t),
    setInterval: (fn, ms) => window.setInterval(fn, ms),
    clearInterval: (t) => window.clearInterval(t),
    Date,
    Math,
    JSON,
    Promise,
    Number,
    String,
    Array,
    Object,
    Boolean,
    RegExp,
    Error,
    TypeError,
    RangeError,
    Infinity,
    NaN,
    isFinite,
    isNaN,
    parseFloat,
    parseInt,
    encodeURIComponent,
    decodeURIComponent,
    Intl,
  };

  vm.createContext(sandbox);
  vm.runInContext(script, sandbox, { filename: "app.js" });

  return {
    window,
    document,
    registry: reg,
    byId: (id) => reg.byId.get(id) || null,
    dispose: () => window.dispose(),
  };
}

/** Wait (up to `timeout` ms) for `predicate` to hold; rejects with a message. */
export async function waitFor(predicate, { timeout = 8000, interval = 25, what = "condition" } = {}) {
  const deadline = Date.now() + timeout;
  for (;;) {
    let value;
    try {
      value = predicate();
    } catch (err) {
      value = false;
      what = `${what} (predicate threw: ${err.message})`;
    }
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`timed out after ${timeout}ms waiting for ${what}`);
    await new Promise((resolve) => setTimeout(resolve, interval));
  }
}

export function textOf(el) {
  return el ? el.textContent : null;
}
