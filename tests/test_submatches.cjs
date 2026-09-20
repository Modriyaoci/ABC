const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function appContext() {
  const elements = new Map();
  const storage = new Map();
  const elementFor = (selector) => {
    if (!elements.has(selector)) elements.set(selector, {
      listeners: {}, addEventListener(type, listener) { this.listeners[type] = listener; },
      style: { setProperty() {} }, classList: { toggle() {} }, querySelectorAll: () => [],
    });
    return elements.get(selector);
  };
  const context = vm.createContext({
    document: { querySelector: elementFor, addEventListener() {}, getElementById: () => null },
    window: { addEventListener() {}, setTimeout, clearTimeout, localStorage: { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value) } },
    AbortController,
  });
  const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
  vm.runInContext(source.replace(/void init\(\);\s*$/, ""), context);
  return context;
}

test("team lineups render without game scores, preserve zero and escape player names", () => {
  const context = appContext();
  context.payload = { sections: [], subMatches: [
    { id: "tie-1", number: 1, type: "单打", home: "WARDANI Putri Kusuma（印度尼西亚）", away: "TSELMEG-OD Enkhlen（蒙古）", homeScore: 0, awayScore: 0, status: "RUNNING", sections: [] },
    { id: "tie-2", number: 2, type: "双打", home: "ROSE Rachel Allesya / SETIANINGRUM Febi（印度尼西亚）", away: "A<script> / B（蒙古）", homeScore: "", awayScore: "", status: "START_LIST", sections: [] },
  ] };
  const html = vm.runInContext('state.details.set("team", {data: payload}); detailContent("team")', context);
  assert.equal((html.match(/class="submatch-card"/g) || []).length, 1);
  assert.equal((html.match(/class="submatch-tab"/g) || []).length, 2);
  assert.match(html, /第1场 · 单打/);
  assert.match(html, /第2场 · 双打/);
  assert.match(html, /进行中/);
  assert.equal((html.match(/<strong>0<\/strong>/g) || []).length, 2);
  assert.doesNotMatch(html, /<script>|官网尚未公布小分/);
  const doubles = vm.runInContext('state.selectedSubMatches.set("team", "tie-2"); detailContent("team")', context);
  assert.match(doubles, /ROSE Rachel Allesya \/ SETIANINGRUM Febi/);
  assert.match(doubles, /未开赛/);
  assert.equal((doubles.match(/<strong>—<\/strong>/g) || []).length, 2);
  assert.match(doubles, /A&lt;script&gt;/);
  assert.doesNotMatch(doubles, /WARDANI|<script>|官网尚未公布小分/);
});

test("table-tennis and badminton line-ups render official player photos with a fallback", () => {
  const context = appContext();
  const html = vm.runInContext(`renderLineup({
    home: "中国", away: "印度尼西亚",
    homePlayers: [{name: "FAN Shuhan", org: "CHN", reg: "14244548", photo: "https://results.asiangames2026.org/ag2026/photos/14244548.jpg"}],
    awayPlayers: [{name: "WARDANI Putri Kusuma", org: "INA", reg: "7819131"}, {name: "SETIANINGRUM Febi", org: "INA", substitute: true}]
  })`, context);
  assert.match(html, /aria-label="Line-up"/);
  assert.match(html, /Line-up/);
  assert.match(html, /FAN Shuhan/);
  assert.match(html, /CHN/);
  assert.match(html, /photos\/14244548\.jpg/);
  assert.match(html, /photos\/7819131\.jpg/);
  assert.match(html, /替补/);
  assert.match(html, /lineup-player-initials/);
  assert.doesNotMatch(html, /<script>/);
});

test("published line-up remains visible when scores are not available yet", () => {
  const context = appContext();
  const html = vm.runInContext(`state.details.set("upcoming", {data: {
    sections: [], subMatches: [], message: "官网尚未公布该场小分",
    home: "中国", away: "日本",
    homePlayers: [{name: "CHEN Yi", org: "CHN", reg: "16276085"}],
    awayPlayers: [{name: "HARIMOTO Miwa", org: "JPN", reg: "380921"}]
  }}); detailContent("upcoming")`, context);
  assert.match(html, /Line-up/);
  assert.doesNotMatch(html, /官网尚未公布该场小分/);
});

test("line-up can be hidden and shown independently", () => {
  const context = appContext();
  const match = {
    home: "中国", away: "日本",
    homePlayers: [{name: "CHEN Yi", org: "CHN", reg: "16276085"}],
    awayPlayers: [{name: "HARIMOTO Miwa", org: "JPN", reg: "380921"}],
  };
  const hidden = vm.runInContext(`setLineupVisibility("match", false); renderLineup(${JSON.stringify(match)}, "match", "match")`, context);
  assert.match(hidden, /aria-expanded="false"/);
  assert.match(hidden, /显示阵容/);
  assert.match(hidden, /class="lineup-grid"[^>]* hidden/);
  assert.doesNotMatch(hidden, /aria-expanded="true"/);

  const shown = vm.runInContext(`setLineupVisibility("match", true); renderLineup(${JSON.stringify(match)}, "match", "match")`, context);
  assert.match(shown, /aria-expanded="true"/);
  assert.match(shown, /隐藏阵容/);
  assert.doesNotMatch(shown, /class="lineup-grid"[^>]* hidden/);
});

test("small-score tables put names on the left and periods across the score columns", () => {
  const context = appContext();
  const html = vm.runInContext(`renderDataTable({
    title: "小分",
    columns: ["局/节", "FAN Shuhan（中国）", "POUDELY Yonggi（尼泊尔）"],
    rows: [["第1局", "11", "1"], ["第2局", "11", "5"], ["第3局", "3", "0"]]
  }, {transpose: true})`, context);
  assert.match(html, /<th scope="col">姓名<\/th>.*第1局.*第2局.*第3局/s);
  assert.match(html, /<th scope="row">FAN Shuhan（中国）<\/th><td>11<\/td><td>11<\/td><td>3<\/td>/);
  assert.match(html, /<th scope="row">POUDELY Yonggi（尼泊尔）<\/th><td>1<\/td><td>5<\/td><td>0<\/td>/);
  assert.doesNotMatch(html, /<th scope="col">局\/节<\/th>/);
});

test("five-second polling refreshes expanded child scores without a schedule change", async () => {
  const context = appContext();
  context.payload = { sections: [], subMatches: [
    { id: "one", number: 1, home: "A", away: "B", homeScore: "1", awayScore: "0", status: "RUNNING", sections: [] },
    { id: "two", number: 2, home: "C", away: "D", status: "START_LIST", sections: [] },
  ] };
  vm.runInContext(`
    state.details.set("team", {data: payload, lastRequested: Date.now() - 6000});
    state.expanded.add("team");
    state.status = {liveIntervalSeconds: 5};
    state.activeSport = "TTE";
    state.records = [{id: "team", sport: "TTE", isLive: true}];
  `, context);
  context.fetch = async () => ({ ok: true, json: async () => ({
    sections: [], subMatches: [
      { ...context.payload.subMatches[0], homeScore: "3", status: "OFFICIAL", sections: [{ title: "小分", columns: ["局", "A", "B"], rows: [["第1局", "11", "9"]] }] },
      context.payload.subMatches[1],
    ],
  }) });
  await vm.runInContext('refreshVisibleExtras()', context);
  const html = vm.runInContext('detailContent("team")', context);
  assert.match(html, /<strong>3<\/strong>/);
  assert.match(html, /完场/);
  assert.match(html, /第1局/);
  assert.equal((html.match(/class="submatch-tab"/g) || []).length, 2);
  assert.equal(vm.runInContext('state.expanded.has("team")', context), true);
  vm.runInContext('state.selectedSubMatches.set("team", "two")', context);
  await vm.runInContext('loadMatch("team", true)', context);
  const retained = vm.runInContext('detailContent("team")', context);
  assert.match(retained, /data-submatch-id="two"/);
  assert.doesNotMatch(retained, /data-submatch-id="one"/);
});

test("column layouts retain live-first row order, open details and saved preference", () => {
  const context = appContext();
  vm.runInContext(`
    state.activeSport = "TTE";
    state.records = [
      {id:"early",sport:"TTE",date:"2026-09-20",time:"08:00",matchup:"A vs B"},
      {id:"live",sport:"TTE",date:"2026-09-20",time:"09:00",matchup:"C vs D",isLive:true},
      {id:"later",sport:"TTE",date:"2026-09-20",time:"12:00",matchup:"E vs F"},
    ];
    state.expanded.add("live");
    state.details.set("live", {data:{sections:[],subMatches:[{id:"child",number:1,home:"Player C",away:"Player D"}]}});
  `, context);
  for (const columns of [2, 3, 4, 5]) {
    vm.runInContext(`elements.layout.value = "${columns}"; elements.layout.listeners.change()`, context);
    const html = vm.runInContext('elements.cards.innerHTML', context);
    assert.deepEqual([...html.matchAll(/data-match-id="([^"]+)"/g)].map((match) => match[1]), ["live", "early", "later"]);
    assert.match(html, /Player C/);
    assert.equal(vm.runInContext('elements.table.hidden', context), true);
    assert.equal(vm.runInContext('savedLayout()', context), columns);
  }
  vm.runInContext('elements.layout.value = "0"; elements.layout.listeners.change()', context);
  assert.equal(vm.runInContext('elements.cards.hidden', context), true);
  assert.match(vm.runInContext('elements.body.innerHTML', context), /Player C/);
});
