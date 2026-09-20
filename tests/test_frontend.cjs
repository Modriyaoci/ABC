const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");

test("schedule updates without observing running state and retains expanded details", async () => {
  const browser = await chromium.launch({ headless: true, executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let version = 1;
    let statusRequests = 0;
    const match = { id: "VVO:test", sport: "VVO", date: "2026-09-17", time: "12:00", category: "女子", eventCode: "women", stage: "小组赛 A组", matchup: "中国 vs 日本", score: "2 : 1", venue: "体育馆", isLive: true };
    await page.route("http://127.0.0.1:4173/**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/status") {
        statusRequests += 1;
        return route.fulfill({ json: {
          running: false, dataVersion: String(version), lastSuccess: "2026-09-17T12:00:00+08:00",
          liveEnabled: true, liveIntervalSeconds: 5, nextAutomaticSync: "2026-09-18T08:00:00+08:00",
          // Simulate a transient response that omits the persisted notice on
          // later status polls.  The banner must remain visible.
          scheduleChanged: statusRequests === 1,
          scheduleChangeCount: statusRequests === 1 ? 2 : 0,
          scheduleChangeAt: "2026-09-17T11:59:00+08:00",
        } });
      }
      if (url.pathname === "/api/schedule") return route.fulfill({ json: { records: [{ ...match, score: version === 1 ? "2 : 1" : "3 : 1" }] } });
      if (url.pathname === "/api/match") return route.fulfill({ json: { available: true, stale: version > 1, updatedAt: "2026-09-17T12:00:00+08:00", message: version > 1 ? "官网暂时连接失败，显示上次成功数据" : "", home: "中国", away: "日本", sections: [{ title: "各局小分", columns: ["队伍", "第一局", "第二局"], rows: [["中国", "25", "25"], ["日本<script>throw Error('unsafe')</script>", "20", "21"]] }] } });
      if (url.pathname === "/api/tournament") return route.fulfill({ json: { stale: true, updatedAt: "2026-09-17T12:00:00+08:00", message: "官网暂时连接失败，显示上次成功数据", events: [{ id: "women", name: "女子", groups: [{ name: "A组", columns: ["排名", "国家", "积分"], rows: [["1", "中国", "6"]] }], rounds: [{ id: "semifinal", name: "半决赛", matches: [{ id: "a", home: "中国", away: "日本", homeScore: "3", awayScore: "1", winner: "home", nextMatchId: "b" }] }, { id: "final", name: "决赛", matches: [{ id: "b", home: "中国", away: "韩国", homeScore: "", awayScore: null }] }] }] } });
      if (url.pathname.endsWith(".png")) return route.fulfill({ status: 204 });
      const filename = ["/", "/volleyball"].includes(url.pathname) ? "index.html" : path.basename(url.pathname);
      const body = await fs.readFile(path.join(__dirname, "../static", filename));
      const contentType = filename.endsWith(".css") ? "text/css" : filename.endsWith(".js") ? "application/javascript" : "text/html";
      return route.fulfill({ body, contentType });
    });
    await page.goto("http://127.0.0.1:4173/");
    await page.locator(".score-toggle").waitFor();
    assert.match(await page.locator("#automatic-sync").innerText(), /每 5 秒/);
    const scheduleChangeBanner = page.locator("#schedule-change-banner");
    await scheduleChangeBanner.waitFor({ state: "visible" });
    assert.match(await scheduleChangeBanner.innerText(), /官网赛程有变动：2场/);
    await page.waitForTimeout(1300);
    assert.equal(await scheduleChangeBanner.isVisible(), true);
    await page.reload();
    await page.locator(".score-toggle").waitFor();
    assert.equal(await scheduleChangeBanner.isVisible(), true);
    assert.match(await scheduleChangeBanner.innerText(), /官网赛程有变动：2场/);
    assert.equal(await page.locator(".schedule-table > thead th").count(), 6);
    await page.locator(".score-toggle").focus();
    await page.keyboard.press("Enter");
    await page.locator(".match-detail .data-table").waitFor();
    assert.equal(await page.locator(".score-toggle").getAttribute("aria-expanded"), "true");
    assert.equal(await page.locator(".match-detail script").count(), 0);
    assert.match(await page.locator(".match-detail").innerText(), /日本<script>/);
    version = 2;
    await page.waitForFunction(() => document.querySelector(".score-toggle").textContent.includes("3 : 1"));
    assert.equal(await page.locator(".score-toggle").getAttribute("aria-expanded"), "true");
    await page.waitForFunction(() => document.querySelector(".match-detail").textContent.includes("小分暂未更新"));
    assert.match(await page.locator(".match-detail").innerText(), /上次更新.*12:00:00/);
    assert.ok(statusRequests >= 2);
    await page.getByRole("tab", { name: "小组积分", exact: true }).click();
    await page.locator(".group-card").waitFor();
    assert.match(await page.locator("#tournament-view").innerText(), /中国/);
    assert.match(await page.locator("#tournament-view").innerText(), /小组积分暂未更新.*上次更新.*12:00:00/);
    assert.equal(await page.locator("#category-filter").isVisible(), true);
    assert.deepEqual(await page.locator("#category-filter option").allTextContents(), ["女子"]);
    await page.getByRole("tab", { name: "淘汰赛对阵图", exact: true }).click();
    await page.locator(".bracket-match").first().waitFor();
    assert.match(await page.locator(".bracket-team.is-winner").innerText(), /中国/);
    assert.match(await page.locator("#tournament-view").innerText(), /对阵图暂未更新.*上次更新.*12:00:00/);
    assert.equal(await page.locator(".advancement-link").count(), 1);
    await page.getByRole("button", { name: "胜者进入：决赛 · 对阵 1" }).click();
    assert.equal(await page.evaluate(() => document.activeElement.id), "bracket-b");
    assert.deepEqual(await page.locator("#bracket-b strong").allTextContents(), ["—", "—"]);
    for (const width of [320, 390, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 844 });
      for (const name of ["小组积分", "淘汰赛对阵图", "赛程赛果"]) {
        await page.getByRole("tab", { name, exact: true }).click();
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, `${width}px ${name} should not overflow`);
      }
      assert.equal(await page.locator(".score-toggle").getAttribute("aria-expanded"), "true");
    }
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});
