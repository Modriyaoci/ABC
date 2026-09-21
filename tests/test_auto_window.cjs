const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function appContext(time = "2026-09-21T12:00:00+08:00") {
  let now = Date.parse(time);
  class ControlledDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  const nodes = new Map();
  const windowListeners = new Map();
  const documentListeners = new Map();
  const getElement = (key) => {
    if (!nodes.has(key)) nodes.set(key, {
      addEventListener() {}, querySelectorAll: () => [], querySelector: () => null,
      classList: { toggle() {}, add() {} }, style: { setProperty() {} },
    });
    return nodes.get(key);
  };
  const context = vm.createContext({
    Date: ControlledDate, AbortController,
    document: {
      hidden: false, querySelector: getElement, getElementById: () => null,
      addEventListener: (name, handler) => documentListeners.set(name, handler),
    },
    window: {
      setTimeout, clearTimeout, localStorage: { getItem: () => null, setItem() {} },
      addEventListener: (name, handler) => windowListeners.set(name, handler),
    },
  });
  const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
  vm.runInContext(source.replace(/void init\(\);\s*$/, ""), context);
  vm.runInContext(`
    renderView = () => {}; renderStatus = () => {}; renderSchedule = () => {};
    updateDetailPanel = () => {}; filteredRecords = () => state.records;
    state.activeSport = "TTE"; state.loadedVersion = "v1"; state.recordsLoaded = true;
    state.status = {automaticSyncAllowed: true, liveIntervalSeconds: 10};
    state.records = [{id: "tie", sport: "TTE", isLive: true}];
    state.expanded.add("tie");
  `, context);
  const requests = [];
  let status = {running: false, automaticSyncAllowed: true, liveIntervalSeconds: 10, dataVersion: "v1"};
  let syncStatus = 202;
  context.fetch = async (url, options = {}) => {
    requests.push({url, method: options.method || "GET"});
    const value = url === "/api/status" ? {...status}
      : url === "/api/schedule" ? {records: [{id: "tie", sport: "TTE", isLive: true}]}
      : url === "/api/sync" ? {accepted: syncStatus === 202, message: "同步结果"}
      : {available: true, sections: [], events: [], score: "3:2"};
    const code = url === "/api/sync" ? syncStatus : 200;
    return {ok: code >= 200 && code < 300, status: code, json: async () => value};
  };
  return {
    context, requests, windowListeners, documentListeners,
    run: (expression) => vm.runInContext(expression, context),
    setNow: (value) => { now = Date.parse(value); },
    setStatus: (value) => { status = {...status, ...value}; },
    setSyncStatus: (value) => { syncStatus = value; },
    detailRequests: () => requests.filter(({url}) => /\/api\/(match|tournament)\?/.test(url)),
  };
}

test("automatic details use Beijing 08:00 inclusive and 23:00 exclusive regardless of device timezone", () => {
  const app = appContext();
  for (const [time, expected] of [
    ["2026-09-21T07:59:59.999+08:00", false],
    ["2026-09-21T08:00:00+08:00", true],
    ["2026-09-21T22:59:59.999+08:00", true],
    ["2026-09-21T23:00:00+08:00", false],
    ["2026-09-22T00:00:00+08:00", false],
  ]) {
    app.setNow(time);
    assert.equal(app.run("automaticSyncAllowed()"), expected, time);
  }
  app.setNow("2026-09-21T12:00:00+08:00");
  app.run("state.status.automaticSyncAllowed = false");
  assert.equal(app.run("automaticSyncAllowed()"), false, "server pause must also be respected");
  app.run("state.status = {}");
  assert.equal(app.run("automaticSyncAllowed()"), true, "older servers use the Beijing window");
});

test("daytime detail and tournament polling carry the automatic marker", async () => {
  const app = appContext();
  await app.run("refreshVisibleExtras(true)");
  app.run('state.view = "groups"');
  await app.run("refreshVisibleExtras(true)");
  assert.deepEqual(app.detailRequests().map(({url}) => url), [
    "/api/match?id=tie&automatic=1", "/api/tournament?sport=TTE&automatic=1",
  ]);
});

test("crossing 23:00 blocks forced automatic details even with an old allowed status", async () => {
  const app = appContext("2026-09-21T22:59:59+08:00");
  await app.run("refreshVisibleExtras(true)");
  app.setNow("2026-09-21T23:00:00+08:00");
  await app.run("refresh(true)");
  app.run('state.view = "bracket"');
  await app.run("refresh(true)");
  assert.equal(app.detailRequests().length, 1);
  assert.equal(app.requests.filter(({url}) => url === "/api/status").length, 2);
  assert.equal(app.requests.filter(({url}) => url === "/api/schedule").length, 2);
});

test("nighttime focus, reconnection and visibility events only refresh cached status and schedule", async () => {
  const app = appContext("2026-09-21T23:10:00+08:00");
  for (const listener of [
    app.windowListeners.get("focus"), app.windowListeners.get("online"),
    app.documentListeners.get("visibilitychange"),
  ]) {
    listener();
    while (app.run("state.refreshing")) await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(app.requests.filter(({url}) => url === "/api/status").length, 3);
  assert.equal(app.detailRequests().length, 0);
});

test("direct manual detail and standings actions remain available at night", async () => {
  const app = appContext("2026-09-21T23:10:00+08:00");
  app.run("state.status.automaticSyncAllowed = false");
  await app.run('loadMatch("tie", true)');
  await app.run("loadTournament(true)");
  assert.deepEqual(app.detailRequests().map(({url}) => url), [
    "/api/match?id=tie", "/api/tournament?sport=TTE",
  ]);
});

test("manual sync refreshes open details or standings once after completion, then night polling stops", async () => {
  for (const view of ["schedule", "groups"]) {
    const app = appContext("2026-09-21T23:10:00+08:00");
    app.run(`state.view = "${view}"`);
    app.setStatus({running: true, automaticSyncAllowed: false});
    await app.run("requestSync()");
    assert.equal(app.detailRequests().length, 0, "wait for the accepted job to finish");
    app.setStatus({running: false, dataVersion: "v2", lastError: null});
    await app.run("refresh()");
    assert.equal(app.detailRequests().length, 1);
    assert.doesNotMatch(app.detailRequests()[0].url, /automatic=1/);
    assert.equal(app.run("state.manualSyncPending"), false);
    await app.run("refresh(true)");
    await app.run("refreshVisibleExtras(true)");
    assert.equal(app.detailRequests().length, 1, "manual permission must not persist");
  }
});

test("manual completion while hidden defers the one detail refresh until the page is visible", async () => {
  const app = appContext("2026-09-21T23:10:00+08:00");
  app.context.document.hidden = true;
  app.setStatus({automaticSyncAllowed: false});
  await app.run("requestSync()");
  assert.equal(app.detailRequests().length, 0);
  app.context.document.hidden = false;
  await app.run("refresh(true)");
  assert.equal(app.detailRequests().length, 1);
  await app.run("refresh(true)");
  assert.equal(app.detailRequests().length, 1);
});

test("failed manual requests and failed jobs do not start a nighttime detail retry loop", async () => {
  for (const rejected of [true, false]) {
    const app = appContext("2026-09-21T23:10:00+08:00");
    app.setStatus({automaticSyncAllowed: false, lastError: "暂不可用"});
    if (rejected) app.setSyncStatus(429);
    await app.run("requestSync()");
    await app.run("refresh(true)");
    assert.equal(app.detailRequests().length, 0);
  }
});
