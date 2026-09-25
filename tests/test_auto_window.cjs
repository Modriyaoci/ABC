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
    const renderRealStatus = renderStatus;
    renderView = () => {}; renderStatus = () => {}; renderSchedule = () => {};
    updateDetailPanel = () => {}; filteredRecords = () => state.records;
    state.activeSport = "TTE"; state.loadedVersion = "v1"; state.recordsLoaded = true;
    state.status = {automaticSyncAllowed: true, liveIntervalSeconds: 5};
    state.records = [{id: "tie", sport: "TTE", isLive: true}];
    state.expanded.add("tie");
  `, context);
  const requests = [];
  let status = {running: false, automaticSyncAllowed: true, liveIntervalSeconds: 5, dataVersion: "v1"};
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
    context, requests, windowListeners, documentListeners, getElement,
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

test("confirmed completion is displayed for its Beijing date, including after 23:00", () => {
  const app = appContext();
  app.run('state.status = {todayCompleted: true, completionDate: "2026-09-21", automaticSyncAllowed: false}');
  for (const time of ["2026-09-21T12:00:00+08:00", "2026-09-21T23:30:00+08:00"]) {
    app.setNow(time);
    app.run("renderRealStatus()");
    assert.equal(app.getElement("#automatic-sync").textContent, "今日比赛已全部完场 · 自动同步已停止");
  }
  app.setNow("2026-09-22T00:00:00+08:00");
  app.run("renderRealStatus()");
  assert.doesNotMatch(app.getElement("#automatic-sync").textContent, /全部完场/);
});

test("unofficial results are completed only when no longer live", () => {
  const app = appContext();
  assert.equal(app.run('recordStatus({status: "UNOFFICIAL", isLive: false})'), "completed");
  assert.equal(app.run('recordStatus({status: "UNOFFICIAL", isLive: true})'), "live");
  assert.equal(app.run('recordStatus({status: "SCHEDULED", isLive: false})'), "upcoming");
});

test("pending confirmation displays low-frequency checks while automatic details stay paused", async () => {
  const app = appContext();
  app.setStatus({todayCompleted: true, resultsPendingConfirmation: true, completionDate: "2026-09-21", automaticSyncAllowed: false});
  await app.run("refresh()");
  app.run("renderRealStatus()");
  assert.equal(app.getElement("#automatic-sync").textContent, "今日比赛已结束 · 每 5 分钟核对待确认赛果");
  assert.equal(app.run("automaticSyncAllowed()"), false);
  assert.equal(app.detailRequests().length, 1, "final cached details are read once");
  app.setNow("2026-09-21T12:00:05+08:00");
  await app.run("refresh(true)");
  await app.run("refreshVisibleExtras(true)");
  assert.equal(app.detailRequests().length, 1, "pending results do not restart high-frequency detail polling");
  app.setStatus({resultsPendingConfirmation: false});
  await app.run("refresh()");
  app.run("renderRealStatus()");
  assert.equal(app.getElement("#automatic-sync").textContent, "今日比赛已全部完场 · 自动同步已停止");
});

test("expired confirmation window asks for manual sync and keeps automatic details paused", async () => {
  const app = appContext();
  app.setStatus({
    todayCompleted: true, resultsPendingConfirmation: true,
    resultConfirmationExpired: true, resultConfirmationDeadline: "2026-09-21T11:59:00+08:00",
    completionDate: "2026-09-21", automaticSyncAllowed: false,
  });
  await app.run("refresh()");
  app.run("renderRealStatus()");
  assert.equal(app.getElement("#automatic-sync").textContent, "待确认赛果自动核对已结束 · 请手动同步");
  assert.match(app.getElement("#automatic-sync").title, /自动核对截止/);
  app.setNow("2026-09-21T12:00:05+08:00");
  await app.run("refresh(true)");
  await app.run("refreshVisibleExtras(true)");
  assert.equal(app.detailRequests().length, 0, "expired confirmation does not read automatic details");
  await app.run('loadMatch("tie", true)');
  assert.equal(app.detailRequests().length, 1, "manual detail reads remain available");
  assert.doesNotMatch(app.detailRequests()[0].url, /automatic=1/);
});

test("late confirmation versions refresh cached details and failed reads use a five-minute interval", async () => {
  const app = appContext();
  app.setStatus({todayCompleted: true, resultsPendingConfirmation: true, completionDate: "2026-09-21", automaticSyncAllowed: false});
  await app.run("refresh()");
  assert.equal(app.detailRequests().length, 1);
  const originalFetch = app.context.fetch;
  let fail = true;
  app.context.fetch = async (url, options) => {
    const response = await originalFetch(url, options);
    if (url.startsWith("/api/match?") && fail) throw new Error("temporary error");
    return response;
  };
  app.setStatus({dataVersion: "v2"});
  app.setNow("2026-09-21T12:05:00+08:00");
  await app.run("refresh()");
  assert.equal(app.detailRequests().length, 2, "new confirmation version updates final cached details");
  app.setNow("2026-09-21T12:05:05+08:00");
  await app.run("refresh(true)");
  assert.equal(app.detailRequests().length, 2, "failed reads do not retry after five seconds");
  fail = false;
  app.setNow("2026-09-21T12:10:00+08:00");
  await app.run("refresh()");
  assert.equal(app.detailRequests().length, 3);
  assert.equal(app.run("state.completionExtrasPending"), false);
  app.setStatus({dataVersion: "v3", resultConfirmationExpired: true});
  app.setNow("2026-09-21T13:00:00+08:00");
  await app.run("refresh(true)");
  assert.equal(app.detailRequests().length, 3, "expired window suppresses reads even for a new version");
});

test("completion reads final cached details once, then forced refresh and focus do not poll them", async () => {
  for (const view of ["schedule", "groups", "bracket"]) {
    const app = appContext();
    app.run(`state.view = "${view}"`);
    app.setStatus({todayCompleted: true, completionDate: "2026-09-21", automaticSyncAllowed: false});
    await app.run("refresh()");
    assert.equal(app.detailRequests().length, 1, "the final cached score/standings must reach the open view");
    assert.match(app.detailRequests()[0].url, /automatic=1/);
    await app.run("refresh(true)");
    await app.run("refreshVisibleExtras(true)");
    for (const listener of [
      app.windowListeners.get("focus"), app.windowListeners.get("online"),
      app.documentListeners.get("visibilitychange"),
    ]) {
      listener();
      while (app.run("state.refreshing")) await new Promise((resolve) => setImmediate(resolve));
    }
    assert.equal(app.detailRequests().length, 1, view);
    assert.equal(app.requests.filter(({url}) => url === "/api/status").length, 5);
  }
});

test("hidden completion defers its final cached detail refresh until visible", async () => {
  const app = appContext();
  app.context.document.hidden = true;
  app.setStatus({todayCompleted: true, completionDate: "2026-09-21", automaticSyncAllowed: false});
  await app.run("refresh()");
  assert.equal(app.detailRequests().length, 0);
  app.context.document.hidden = false;
  await app.run("refresh(true)");
  assert.equal(app.detailRequests().length, 1);
  await app.run("refresh(true)");
  assert.equal(app.detailRequests().length, 1);
});

test("manual details and sync remain usable after all matches complete", async () => {
  const app = appContext();
  app.setStatus({todayCompleted: true, completionDate: "2026-09-21", automaticSyncAllowed: false});
  await app.run("refresh()");
  app.requests.length = 0;
  await app.run('loadMatch("tie", true)');
  await app.run("loadTournament(true)");
  await app.run("requestSync()");
  assert.equal(app.detailRequests().length, 3);
  assert.ok(app.detailRequests().every(({url}) => !url.includes("automatic=1")));
  assert.equal(app.requests.filter(({url, method}) => url === "/api/sync" && method === "POST").length, 1);
  await app.run("refresh(true)");
  assert.equal(app.detailRequests().length, 3, "a manual action does not re-enable automatic polling");
});

test("new server status reopens automatic details when a match is no longer complete", async () => {
  const app = appContext();
  app.setStatus({todayCompleted: true, completionDate: "2026-09-21", automaticSyncAllowed: false});
  await app.run("refresh()");
  app.setStatus({todayCompleted: false, completionDate: null, automaticSyncAllowed: true});
  app.setNow("2026-09-21T12:00:05+08:00");
  await app.run("refresh()");
  assert.equal(app.run("automaticSyncAllowed()"), true);
  assert.equal(app.detailRequests().length, 2);
  assert.match(app.detailRequests()[1].url, /automatic=1/);
});

test("previous-day completion does not survive the next 08:00 status response", async () => {
  const app = appContext();
  app.setStatus({todayCompleted: true, completionDate: "2026-09-21", automaticSyncAllowed: false});
  await app.run("refresh()");
  app.setNow("2026-09-22T07:59:59+08:00");
  assert.equal(app.run("todayCompleted()"), false);
  assert.equal(app.run("automaticSyncAllowed()"), false);
  app.setNow("2026-09-22T08:00:00+08:00");
  app.setStatus({todayCompleted: false, completionDate: null, automaticSyncAllowed: true});
  await app.run("refresh()");
  assert.equal(app.run("automaticSyncAllowed()"), true);
  assert.equal(app.detailRequests().length, 2);
});

test("failed final cache reads retry at the score interval and stop after success", async () => {
  for (const view of ["schedule", "groups", "bracket"]) {
    const app = appContext();
    app.run(`state.view = "${view}"`);
    const originalFetch = app.context.fetch;
    let failures = 1;
    app.context.fetch = async (url, options) => {
      const response = await originalFetch(url, options);
      if (/\/api\/(match|tournament)\?/.test(url) && failures > 0) {
        failures -= 1;
        throw new Error("短暂连接中断");
      }
      return response;
    };
    app.setStatus({todayCompleted: true, completionDate: "2026-09-21", automaticSyncAllowed: false});
    await app.run("refresh()");
    assert.equal(app.detailRequests().length, 1);
    assert.equal(app.run("state.completionExtrasPending"), true);
    app.setNow("2026-09-21T12:00:01+08:00");
    await app.run("refresh(true)");
    assert.equal(app.detailRequests().length, 1, "forced status updates must not accelerate cache retries");
    app.setNow("2026-09-21T12:00:05+08:00");
    await app.run("refresh()");
    assert.equal(app.detailRequests().length, 2);
    assert.equal(app.run("state.completionExtrasPending"), false);
    assert.ok(app.detailRequests().every(({url}) => url.includes("automatic=1")));
    app.setNow("2026-09-21T12:00:10+08:00");
    await app.run("refresh(true)");
    assert.equal(app.detailRequests().length, 2);
    failures = 1;
    await app.run('state.view === "schedule" ? loadMatch("tie", true) : loadTournament(true)');
    app.setNow("2026-09-21T12:00:15+08:00");
    await app.run("refresh(true)");
    assert.equal(app.detailRequests().length, 3, "a later failed manual read must not start an automatic retry loop");
    assert.doesNotMatch(app.detailRequests()[2].url, /automatic=1/);
  }
});
