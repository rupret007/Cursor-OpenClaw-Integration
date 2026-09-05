"use strict";

// Run the actual Python-rendered script. The fixture has no network, browser,
// package dependency, live credentials, or product-only testing hooks.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const html = fs.readFileSync(0, "utf8");
const inline = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)]
  .map((match) => match[1]).join("\n");
assert.ok(inline, "rendered dashboard must include its executable script");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

class Element {
  constructor(id, attributes = {}) {
    this.id = id;
    this.attributes = { ...attributes };
    this.listeners = new Map();
    this._innerHTML = "";
    this.innerHTMLWrites = 0;
    this.textContent = "";
    this.className = attributes.class || "";
    this.hidden = Object.hasOwn(attributes, "hidden");
    this.disabled = false;
    this.style = {};
    this.dataset = {};
    this.classList = {
      add: (...names) => { this.className = `${this.className} ${names.join(" ")}`; },
      remove: (...names) => {
        this.className = this.className.split(/\s+/).filter((n) => !names.includes(n)).join(" ");
      },
      toggle: (name, force) => {
        const present = this.className.split(/\s+/).includes(name);
        const wanted = force === undefined ? !present : force;
        if (wanted && !present) this.classList.add(name);
        if (!wanted && present) this.classList.remove(name);
        return wanted;
      },
    };
  }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(value) {
    this._innerHTML = value;
    this.innerHTMLWrites += 1;
  }
  focus() { if (this.onFocus) this.onFocus(this); }
  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  click() {
    if (!this.disabled) {
      for (const listener of this.listeners.get("click") || []) listener({ target: this });
    }
  }
}

function task(id) {
  return { task_id: id, status: "pending", channel: "fixture", summary: `${id} summary`, updated_at: 1 };
}

function summary(ids = []) {
  return {
    ok: true,
    service: { kill_switch: { engaged: false }, repair_safe_roots: [] },
    webhook: { status: "healthy", reason: "offline fixture" },
    tasks: { items: ids.map(task), count: ids.length, limit: 30 },
    capabilities: { summary: {}, blocked_critical: [], attention: [], acpx: null },
    operator_readiness: {
      receipt_state: "current", trusted_receipt: true, receipt_verified: true,
      overall_status: "ready", grade: "A", who_acts_first: "coding_agent",
      next_action: "FRESH_ACTION_ONLY", age_seconds: 60, failed_stages: [],
    },
  };
}

function detail(id) {
  return { ok: true, task: task(id), events: [{ event_type: `${id}_EVENT`, seq: 1, ts: 1 }] };
}

async function settle() {
  for (let i = 0; i < 30; i += 1) await Promise.resolve();
}

function browser() {
  let now = 1700000000000;
  let nextTimer = 1;
  const timers = new Map();
  const elements = new Map();
  for (const tag of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
    const attributes = {};
    for (const attribute of tag[0].matchAll(/([\w-]+)="([^"]*)"/g)) {
      attributes[attribute[1]] = attribute[2];
    }
    if (/\shidden(?:[\s=>])/.test(tag[0])) attributes.hidden = "";
    elements.set(tag[1], new Element(tag[1], attributes));
  }
  const element = (id) => {
    assert.ok(elements.has(id), `actual HTML must define #${id}`);
    return elements.get(id);
  };
  const requests = [];
  const documentListeners = new Map();
  const document = {
    hidden: false,
    visibilityState: "visible",
    activeElement: null,
    getElementById: element,
    taskButtons: [],
    addEventListener(name, listener) {
      const listeners = documentListeners.get(name) || [];
      listeners.push(listener);
      documentListeners.set(name, listeners);
    },
    dispatch(name) {
      for (const listener of documentListeners.get(name) || []) listener();
    },
    querySelectorAll(selector) {
      if (!selector.includes("task")) return [];
      this.taskButtons = [...element("tasks").innerHTML.matchAll(/<button\b[^>]*data-task-id="([^"]+)"[^>]*>/g)]
        .map((match) => {
          const button = new Element("", { "data-task-id": match[1] });
          button.onFocus = (focused) => { this.activeElement = focused; };
          return button;
        });
      return this.taskButtons;
    },
  };
  const timer = (fn, delay, repeat) => {
    const id = nextTimer++;
    timers.set(id, { fn, at: now + Number(delay || 0), repeat });
    return id;
  };
  class FixtureDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  const context = vm.createContext({
    document, Date: FixtureDate, AbortController, DOMException, console,
    URL, URLSearchParams,
    setTimeout: (fn, delay) => timer(fn, delay, 0),
    clearTimeout: (id) => timers.delete(id),
    setInterval: (fn, delay) => timer(fn, delay, Number(delay)),
    clearInterval: (id) => timers.delete(id),
    fetch: (url, options = {}) => {
      const pending = deferred();
      const request = {
        url, options, pending,
        resolve: (body) => pending.resolve({ ok: true, status: 200, json: async () => body }),
        reject: (message = "fixture offline") => pending.reject(new Error(message)),
      };
      requests.push(request);
      // Deliberately ignore abort to prove stale responses cannot commit even
      // if the transport or a cached JSON decoder finishes after cancellation.
      return pending.promise;
    },
  });
  context.window = context;
  context.addEventListener = () => {};
  vm.runInContext(inline, context, { filename: "rendered-dashboard.js" });
  return {
    context, requests, element, timers, document,
    call: (source) => vm.runInContext(source, context),
    elapseWithoutTimers(milliseconds) { now += milliseconds; },
    async advance(milliseconds) {
      const target = now + milliseconds;
      let executions = 0;
      while (true) {
        const due = [...timers.entries()].filter(([, entry]) => entry.at <= target)
          .sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        assert.ok(executions++ < 1000, "fake timer loop must remain bounded");
        const [id, entry] = due;
        now = entry.at;
        timers.delete(id);
        if (entry.repeat) timers.set(id, { ...entry, at: now + entry.repeat });
        entry.fn();
        await settle();
      }
      now = target;
      await settle();
    },
  };
}

async function ready(ids = []) {
  const b = browser();
  await settle();
  assert.equal(b.requests.length, 1, "first paint starts one summary request");
  b.requests[0].resolve(summary(ids));
  await settle();
  assert.equal(b.element("connectionState").textContent, "Current");
  assert.equal(b.element("snapshotContent").hidden, false);
  return b;
}

function assertUnavailable(b) {
  assert.notEqual(b.element("connectionState").textContent, "Current");
  assert.equal(b.element("snapshotContent").hidden, true);
  assert.equal(b.element("operatorReadinessPill").textContent.toLowerCase(), "unavailable");
  assert.doesNotMatch(b.element("operatorReadiness").innerHTML, /FRESH_ACTION_ONLY|coding_agent/);
}

const scenarios = {
  async "concurrent refreshes coalesce and the next poll waits for completion"() {
    const b = browser();
    b.call("loadSummary()");
    b.call("loadSummary()");
    b.element("refreshBtn").click();
    await b.advance(4000);
    assert.equal(b.requests.length, 1);
    b.requests[0].resolve(summary());
    await settle();
    await b.advance(4999);
    assert.equal(b.requests.length, 1);
    await b.advance(1);
    assert.equal(b.requests.length, 2);
  },
  async "hung fetch releases refresh and can recover"() {
    const b = browser();
    await settle();
    await b.advance(10001);
    assertUnavailable(b);
    assert.equal(b.requests[0].options.signal.aborted, true);
    b.element("refreshBtn").click();
    await settle();
    assert.equal(b.requests.length, 2);
    b.requests[1].resolve(summary());
    await settle();
    assert.equal(b.element("connectionState").textContent, "Current");
    assert.equal(b.element("snapshotContent").hidden, false);
    b.requests[0].resolve(summary(["LATE"]));
    await settle();
    assert.doesNotMatch(b.element("tasks").innerHTML, /LATE/);
  },
  async "timeout also bounds a hung JSON decoder"() {
    const b = browser();
    await settle();
    const body = deferred();
    b.requests[0].pending.resolve({ ok: true, status: 200, json: () => body.promise });
    await settle();
    await b.advance(10001);
    assertUnavailable(b);
    b.element("refreshBtn").click();
    await settle();
    b.requests[1].resolve(summary());
    await settle();
    body.resolve(summary(["OLD_BODY"]));
    await settle();
    assert.equal(b.element("connectionState").textContent, "Current");
    assert.doesNotMatch(b.element("tasks").innerHTML, /OLD_BODY/);
  },
  async "late completion after timer suspension cannot reset freshness"() {
    const b = browser();
    await settle();
    b.elapseWithoutTimers(20000);
    b.requests[0].resolve(summary());
    await settle();
    assertUnavailable(b);
  },
  async "failed summary revokes readiness and preserves last-success evidence"() {
    const b = await ready();
    const lastSuccess = b.element("lastUpdated").textContent;
    b.call("loadSummary()");
    await settle();
    b.requests.at(-1).reject();
    await settle();
    assertUnavailable(b);
    assert.equal(b.element("lastUpdated").textContent, lastSuccess);
    assert.ok(b.element("connectionNote").textContent.length > 0);
  },
  async "malformed summary cannot preserve or publish green status"() {
    const b = await ready();
    b.call("loadSummary()");
    await settle();
    const malformed = summary();
    delete malformed.service.kill_switch;
    b.requests.at(-1).resolve(malformed);
    await settle();
    assertUnavailable(b);
  },
  async "renderer failure revokes partially rendered evidence"() {
    const b = await ready();
    b.call("loadSummary()");
    await settle();
    const broken = summary();
    broken.optimization = { dominant_categories: 1 };
    b.requests.at(-1).resolve(broken);
    await settle();
    assertUnavailable(b);
  },
  async "old evidence expires while a refresh is still pending"() {
    const b = await ready();
    await b.advance(15001);
    assertUnavailable(b);
  },
  async "returning from a suspended tab revokes stale evidence before refresh"() {
    const b = await ready();
    b.document.hidden = true;
    b.document.dispatch("visibilitychange");
    await settle();
    assert.equal(b.requests.length, 1);
    b.elapseWithoutTimers(20000);
    b.document.hidden = false;
    b.document.dispatch("visibilitychange");
    assertUnavailable(b);
    await settle();
    assert.equal(b.requests.length, 2);
    b.requests[1].resolve(summary());
    await settle();
    assert.equal(b.element("connectionState").textContent, "Current");
  },
  async "HTTP errors never parse or publish response bodies"() {
    const b = await ready();
    b.call("loadSummary()");
    await settle();
    let parsed = false;
    b.requests.at(-1).pending.resolve({
      ok: false, status: 503,
      json: async () => { parsed = true; return summary(); },
    });
    await settle();
    assertUnavailable(b);
    assert.equal(parsed, false);
  },
  async "missing receipt age is unknown rather than zero minutes"() {
    const b = browser();
    await settle();
    const data = summary();
    data.operator_readiness = { receipt_state: "missing", age_seconds: null };
    b.requests[0].resolve(data);
    await settle();
    assert.doesNotMatch(b.element("operatorReadiness").innerHTML, /0m old/);
    assert.match(b.element("operatorReadiness").innerHTML, /no current receipt|unknown|unavailable/i);
  },
  async "switching tasks rejects late success from the previous selection"() {
    const b = await ready(["A", "B"]);
    const first = b.requests.find((r) => r.url.endsWith("/A"));
    assert.ok(first);
    assert.match(b.element("tasks").innerHTML, /<button\b[^>]*data-task-id="B"/);
    const button = b.document.taskButtons.find((item) => item.getAttribute("data-task-id") === "B");
    assert.ok(button, "task selection must be a native button with a wired click handler");
    button.click();
    await settle();
    assert.equal(first.options.signal.aborted, true);
    const second = b.requests.find((r) => r.url.endsWith("/B"));
    second.resolve(detail("B"));
    await settle();
    first.resolve(detail("A"));
    await settle();
    assert.match(b.element("taskMeta").innerHTML, /B summary/);
    assert.doesNotMatch(b.element("taskMeta").innerHTML, /A summary/);
    assert.match(b.element("timeline").innerHTML, /B_EVENT/);
  },
  async "late failure cannot replace the current task or freshness"() {
    const b = await ready(["A", "B"]);
    const first = b.requests.find((r) => r.url.endsWith("/A"));
    b.call('selectTask("B")');
    await settle();
    b.requests.find((r) => r.url.endsWith("/B")).resolve(detail("B"));
    await settle();
    first.reject("A_LATE_ERROR");
    await settle();
    assert.match(b.element("taskMeta").innerHTML, /B summary/);
    assert.doesNotMatch(b.element("detailSummary").textContent, /A_LATE_ERROR/);
    assert.equal(b.element("connectionState").textContent, "Current");
  },
  async "duplicate loads for the selected task coalesce"() {
    const b = await ready(["A"]);
    b.call('loadTask("A")');
    b.call('loadTask("A")');
    await settle();
    assert.equal(b.requests.filter((r) => r.url.endsWith("/A")).length, 1);
  },
  async "polling preserves selected details and does not rebuild unchanged content"() {
    const b = await ready(["A"]);
    b.requests.find((r) => r.url.endsWith("/A")).resolve(detail("A"));
    await settle();
    const metadata = b.element("taskMeta");
    const timeline = b.element("timeline");
    const metadataWrites = metadata.innerHTMLWrites;
    const timelineWrites = timeline.innerHTMLWrites;
    b.call("loadSummary()");
    await settle();
    b.requests.at(-1).resolve(summary(["A"]));
    await settle();
    assert.match(metadata.innerHTML, /A summary/);
    assert.match(timeline.innerHTML, /A_EVENT/);
    assert.match(b.element("detailSummary").textContent, /refreshing.*last received/i);
    assert.equal(metadata.innerHTMLWrites, metadataWrites);
    assert.equal(timeline.innerHTMLWrites, timelineWrites);
    b.requests.at(-1).resolve(detail("A"));
    await settle();
    assert.equal(metadata.innerHTMLWrites, metadataWrites);
    assert.equal(timeline.innerHTMLWrites, timelineWrites);
    assert.doesNotMatch(b.element("detailSummary").textContent, /refreshing/i);
  },
  async "failed detail refresh clears previous facts while summary stays current"() {
    const b = await ready(["A"]);
    b.requests.find((r) => r.url.endsWith("/A")).resolve(detail("A"));
    await settle();
    b.call('loadTask("A")');
    await settle();
    b.requests.at(-1).reject();
    await settle();
    assert.equal(b.element("taskMeta").innerHTML, "");
    assert.equal(b.element("timeline").innerHTML, "");
    assert.equal(b.element("detailRetryBtn").hidden, false);
    assert.equal(b.element("connectionState").textContent, "Current");
  },
  async "task button keyboard focus survives summary rerender"() {
    const b = await ready(["A", "B"]);
    const focusedBefore = b.document.taskButtons.find((item) => item.getAttribute("data-task-id") === "B");
    focusedBefore.focus();
    assert.equal(b.document.activeElement, focusedBefore);
    b.call("loadSummary()");
    await settle();
    b.requests.at(-1).resolve(summary(["A", "B"]));
    await settle();
    assert.equal(b.document.activeElement.getAttribute("data-task-id"), "B");
    assert.notEqual(b.document.activeElement, focusedBefore);
    assert.equal(b.element("connectionState").textContent, "Current");
  },
  async "mismatched detail fails locally and retry restores the task"() {
    const b = await ready(["A"]);
    b.requests.find((r) => r.url.endsWith("/A")).resolve(detail("OTHER"));
    await settle();
    assert.doesNotMatch(b.element("taskMeta").innerHTML, /OTHER summary/);
    assert.equal(b.element("detailRetryBtn").hidden, false);
    assert.equal(b.element("connectionState").textContent, "Current");
    assert.equal(b.element("snapshotContent").hidden, false);
    b.element("detailRetryBtn").click();
    await settle();
    b.requests.at(-1).resolve(detail("A"));
    await settle();
    assert.match(b.element("taskMeta").innerHTML, /A summary/);
    assert.equal(b.element("detailRetryBtn").hidden, true);
  },
  async "detail network failure does not invalidate current summary"() {
    const b = await ready(["A"]);
    b.requests.find((r) => r.url.endsWith("/A")).reject();
    await settle();
    assert.equal(b.element("connectionState").textContent, "Current");
    assert.equal(b.element("snapshotContent").hidden, false);
    assert.equal(b.element("detailRetryBtn").hidden, false);
  },
  async "empty summary clears details and invalidates pending completion"() {
    const b = await ready(["A"]);
    const first = b.requests.find((r) => r.url.endsWith("/A"));
    b.call("loadSummary()");
    await settle();
    b.requests.at(-1).resolve(summary());
    await settle();
    first.resolve(detail("A"));
    await settle();
    assert.doesNotMatch(b.element("taskMeta").innerHTML, /A summary/);
    assert.doesNotMatch(b.element("timeline").innerHTML, /A_EVENT/);
    assert.equal(b.element("detailRetryBtn").hidden, true);
    assert.equal(b.call("selectedTaskId"), "");
  },
  async "summary loss invalidates in-flight details"() {
    const b = await ready(["A"]);
    const first = b.requests.find((r) => r.url.endsWith("/A"));
    b.call("loadSummary()");
    await settle();
    b.requests.at(-1).reject();
    await settle();
    first.resolve(detail("A"));
    await settle();
    assertUnavailable(b);
    assert.doesNotMatch(b.element("taskMeta").innerHTML, /A summary/);
  },
};

(async () => {
  for (const [name, run] of Object.entries(scenarios)) {
    try {
      await run();
      process.stdout.write(`PASS ${name}\n`);
    } catch (error) {
      process.stderr.write(`FAIL ${name}\n${error.stack}\n`);
      process.exitCode = 1;
    }
  }
  if (!process.exitCode) {
    process.stdout.write(`${Object.keys(scenarios).length} dashboard runtime scenarios passed\n`);
  }
})();
