const SPORTS = {
  TEN: "网球", BBL: "棒球", CKT: "板球", VVO: "排球",
  TTE: "乒乓球", BDM: "羽毛球", HBL: "手球",
};
// Keep a stable, human-readable URL for every sport. The path is part of the
// page state so a refresh (or a shared link) opens the same sport instead of
// falling back to whichever sport happens to have a live match.
const SPORT_PATHS = {
  TEN: "tennis", BBL: "baseball", CKT: "cricket", VVO: "volleyball",
  TTE: "table-tennis", BDM: "badminton", HBL: "handball",
};
const PATH_SPORTS = Object.fromEntries(Object.entries(SPORT_PATHS).map(([sport, path]) => [path, sport]));
const GITHUB_PAGES = window.location.hostname.endsWith(".github.io");
const SITE_BASE = GITHUB_PAGES && window.location.pathname.startsWith("/ABC") ? "/ABC" : "";
const API_BASE = GITHUB_PAGES ? "https://two026asiagames-abc.onrender.com" : "";
const apiUrl = (path) => `${API_BASE}${path}`;
const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
const state = {
  records: [], activeSport: null, view: "schedule", selections: new Map(),
  dateFilter: [], statusFilter: [], sportFilter: [], courtSelections: new Map(),
  status: null, statusTimer: null, refreshing: false, refreshAgain: false,
  manualSyncPending: false, manualExtrasPending: false, completionExtrasPending: false,
  completionExtrasRetryAt: 0,
  teamScheduleChanges: savedTeamScheduleChanges(),
  recordsLoaded: false, loadedVersion: null, connectionError: "",
  expanded: new Set(), details: new Map(), tournaments: new Map(),
  selectedSubMatches: new Map(), lineupVisibility: savedLineupVisibility(), layout: savedLayout(),
};
// Status is deliberately polled at five-second intervals.  The status
// response is small and carries the dataVersion; the full schedule is only
// fetched when that version changes (see refresh()), so a live score update
// does not repeatedly download an unchanged schedule document.
const STATUS_POLL_INTERVAL_MS = 5000;
const elements = {
  tabs: document.querySelector("#sport-tabs"),
  title: document.querySelector("#active-sport-title"),
  count: document.querySelector("#record-count"),
  body: document.querySelector("#schedule-body"),
  table: document.querySelector(".schedule-table"),
  cards: document.querySelector("#schedule-cards"),
  layout: document.querySelector("#layout-filter"),
  empty: document.querySelector("#empty-state"),
  syncButton: document.querySelector("#sync-button"),
  syncStatus: document.querySelector("#sync-status"),
  automaticSync: document.querySelector("#automatic-sync"),
  statusDot: document.querySelector("#status-dot"),
  errorBanner: document.querySelector("#error-banner"),
  scheduleChangeBanner: document.querySelector("#schedule-change-banner"),
  viewTabs: document.querySelector("#view-tabs"),
  scheduleFilters: document.querySelector("#schedule-filters"),
  dateFilter: document.querySelector("#date-filter"),
  statusFilter: document.querySelector("#status-filter"),
  sportFilter: document.querySelector("#sport-filter"),
  courtFilter: document.querySelector("#court-filter"),
  category: document.querySelector("#category-filter"),
  scheduleView: document.querySelector("#schedule-view"),
  tournamentView: document.querySelector("#tournament-view"),
};

// Schedule filters use a compact checkbox menu. Tournament views keep the
// native single-choice event selector, so the two kinds of filtering never
// share a control or selection state.
const checkboxMenus = new WeakMap();
function renderCheckboxMenu(select, options, selected, onChange) {
  if (!select || !select.multiple) return;
  let menu = checkboxMenus.get(select);
  if (!menu) {
    menu = document.createElement("details");
    menu.className = "checkbox-filter-menu";
    menu.addEventListener("toggle", () => {
      if (menu.open) document.querySelectorAll(".checkbox-filter-menu[open]").forEach((other) => { if (other !== menu) other.open = false; });
    });
    select.hidden = true;
    select.parentElement.appendChild(menu);
    checkboxMenus.set(select, menu);
  }
  const selectedLabels = options.filter(([value]) => selected.includes(value)).map(([, label]) => String(label));
  const summary = selectedLabels.length ? (selectedLabels.length <= 2 ? selectedLabels.join("、") : `已选 ${selectedLabels.length} 项`) : "全部";
  menu.innerHTML = `<summary>${escapeHtml(summary)}</summary><div class="checkbox-filter-options">${options.map(([value, label]) => `<label><input type="checkbox" value="${escapeHtml(value)}" ${selected.includes(value) ? "checked" : ""}> <span>${escapeHtml(label)}</span></label>`).join("")}</div>`;
  menu.querySelectorAll("input").forEach((input) => input.addEventListener("change", () => {
    onChange([...menu.querySelectorAll("input:checked")].map((item) => item.value));
  }));
}

function savedLayout() {
  try {
    const columns = Number(window.localStorage?.getItem("schedule-layout"));
    return [2, 3, 4, 5].includes(columns) ? columns : 0;
  } catch { return 0; }
}

function savedTeamScheduleChanges() {
  try {
    const value = JSON.parse(window.localStorage?.getItem("team-schedule-changes") || "[]");
    if (Array.isArray(value)) return new Set(value.map(String));
    if (value && typeof value === "object") return new Set(Object.keys(value));
  } catch {}
  return new Set();
}

function teamScheduleChangeIds(status) {
  const changes = status?.teamScheduleChanges;
  if (Array.isArray(changes)) return changes.map(String);
  if (changes && typeof changes === "object") return Object.keys(changes);
  return [];
}

function retainTeamScheduleChanges(status) {
  const ids = teamScheduleChangeIds(status);
  if (!ids.length) return false;
  let changed = false;
  for (const id of ids) {
    if (!state.teamScheduleChanges.has(id)) {
      state.teamScheduleChanges.add(id);
      changed = true;
    }
  }
  if (changed) {
    try { window.localStorage?.setItem("team-schedule-changes", JSON.stringify([...state.teamScheduleChanges])); } catch {}
  }
  return changed;
}

function hasTeamScheduleChange(id) {
  return state.teamScheduleChanges.has(String(id));
}

function isTeamRecord(record) {
  const sport = String(record?.sport || "").toUpperCase();
  const category = String(record?.category || "");
  const eventCode = String(record?.eventCode || "").toUpperCase();
  return (sport === "TTE" || sport === "BDM") && (category.includes("团体") || eventCode.includes("TEAM"));
}

function savedLineupVisibility() {
  try {
    const value = JSON.parse(window.localStorage?.getItem("lineup-visibility") || "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) return new Map();
    return new Map(Object.entries(value).map(([key, visible]) => [key, visible !== false]));
  } catch { return new Map(); }
}

function lineupIsVisible(key) {
  return state.lineupVisibility.get(String(key)) !== false;
}

function setLineupVisibility(key, visible) {
  state.lineupVisibility.set(String(key), Boolean(visible));
  try {
    window.localStorage?.setItem("lineup-visibility", JSON.stringify(Object.fromEntries(state.lineupVisibility)));
  } catch {}
}

function sportFromPath(pathname = window.location.pathname) {
  const path = String(pathname || "").replace(new RegExp(`^${SITE_BASE}\\/?`), "").replace(/^\/+|\/+$/g, "").toLowerCase();
  return PATH_SPORTS[path] || null;
}

function sportPath(sport) {
  const path = SPORT_PATHS[sport];
  return path ? `${SITE_BASE}/${path}` : `${SITE_BASE}/`;
}

function updateSportPath(sport, { replace = false } = {}) {
  const path = sportPath(sport);
  if (window.location.pathname === path) return;
  const url = `${path}${window.location.search}${window.location.hash}`;
  try {
    window.history[replace ? "replaceState" : "pushState"]({ sport }, "", url);
  } catch {
    // Browsers that do not expose History API still retain the in-memory
    // selection; the rest of the page remains usable.
  }
}

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function formatDateTime(record) {
  const date = new Date(`${record.date}T12:00:00Z`);
  const [, month, day] = String(record.date || "").split("-");
  return {
    date: Number.isNaN(date.getTime()) ? "日期待定" : `${month}月${day}日 ${WEEKDAYS[date.getUTCDay()]}`,
    time: record.time || "时间待定",
  };
}

function formatSyncTime(value) {
  if (!value) return "尚未完成同步";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间待定";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(date);
}

async function fetchJson(url, options = {}) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 25000);
  const cached = fetchJson.cache.get(url);
  const headers = new Headers(options.headers || {});
  if (cached?.etag && !headers.has("If-None-Match")) headers.set("If-None-Match", cached.etag);
  try {
    const response = await fetch(url, { cache: "no-store", ...options, headers, signal: controller.signal });
    if (response.status === 304 && cached) return cached.data;
    if (!response.ok) throw new Error(response.status === 503 ? "正在准备官方数据，请稍后再试" : "暂时无法读取数据");
    const data = await response.json();
    const etag = response.headers.get("ETag");
    if (etag) fetchJson.cache.set(url, { etag, data });
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("读取超时，将自动重试");
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}
fetchJson.cache = new Map();

function renderTabs() {
  const tabs = [["TODAY", "今日赛程"], ...Object.entries(SPORTS)];
  elements.tabs.innerHTML = tabs.map(([code, name]) => `
    <button class="sport-tab" type="button" role="tab" data-sport="${code}"
      aria-selected="${state.activeSport === code}">${name}</button>`).join("");
}

function selectionKey() { return `${state.activeSport}:${state.view === "schedule" ? "schedule" : "tournament"}`; }
function recordCategory(record) { return String(record.eventCode || record.category || ""); }
function isByeValue(value) {
  const text = String(value ?? "").trim().toUpperCase().replace(/[()（）\[\]【】]/g, "");
  return text === "BYE" || text === "轮空";
}
function recordHasBye(record) {
  if (!record || typeof record !== "object") return false;
  const values = [record.matchup, record.home, record.away, record.homeName, record.awayName,
    ...(Array.isArray(record.homePlayers) ? record.homePlayers : []),
    ...(Array.isArray(record.awayPlayers) ? record.awayPlayers : [])];
  return values.some((value) => {
    if (isByeValue(value)) return true;
    if (!value || typeof value !== "object") return false;
    return [value.Name, value.NameS, value.name, value.shortName, value.org, value.Org, value.country]
      .some(isByeValue);
  });
}
function sportRecords() {
  if (!state.activeSport) return state.records.filter((record) => !state.sportFilter.length || state.sportFilter.includes(record.sport));
  return state.records.filter((record) => record.sport === state.activeSport);
}
function recordStatus(record) {
  if (record.isLive || ["LIVE", "RUNNING", "IN_PROGRESS"].includes(String(record.status || "").toUpperCase())) return "live";
  if (["OFFICIAL", "UNOFFICIAL", "FINISHED", "COMPLETED", "CANCELED", "CANCELLED"].includes(String(record.status || "").toUpperCase())) return "completed";
  return "upcoming";
}
function dateLabel(value) {
  const [, month, day] = String(value || "").split("-");
  return month && day ? `${month}月${day}日` : value || "日期待定";
}
function formatScore(record) {
  const raw = String(record.score || "").trim();
  if (record.sport !== "CKT" || !raw) return raw || "—";
  const sides = raw.split(/\s*:\s*/);
  if (sides.length !== 2) return raw;
  const runs = sides.map((side) => (side.match(/^\s*(\d+)/) || ["", side.trim()])[1]);
  return `${runs[0]} : ${runs[1]}`;
}
function courtFilterKey() { return state.activeSport || "TODAY"; }
function recordCourtKey(record) { return record.court ? `${record.sport}:${String(record.court).trim()}` : ""; }
function courtLabel(court) {
  return String(court || "").replace(/^Court\s+(\d+)$/i, "$1号场").replace(/^Table\s+(\d+)$/i, "$1号台");
}
function recordVenue(record) {
  return [record.venue, courtLabel(record.court)].filter(Boolean).join(" · ");
}
function renderCourtFilter() {
  elements.courtFilter.multiple = true;
  const options = [...new Map(sportRecords()
    .filter((record) => ["TEN", "TTE", "BDM"].includes(record.sport) && !recordHasBye(record) && record.court)
    .map((record) => [recordCourtKey(record), `${state.activeSport ? "" : SPORTS[record.sport] + " · "}${courtLabel(record.court)}`])).entries()]
    .sort((a, b) => a[1].localeCompare(b[1], "zh-CN", { numeric: true }));
  const selected = state.courtSelections.get(courtFilterKey()) || [];
  elements.courtFilter.innerHTML = options.map(([value, label]) => `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`).join("");
  for (const option of elements.courtFilter.options) option.selected = selected.includes(option.value);
  renderCheckboxMenu(elements.courtFilter, options, selected, (values) => {
    state.courtSelections.set(courtFilterKey(), values);
    renderView();
    void refreshVisibleExtras(false, { manual: true });
  });
}
function filteredRecords() {
  const categories = state.selections.get(selectionKey()) || [];
  const courts = state.courtSelections.get(courtFilterKey()) || [];
  return sportRecords()
    // A bye is a bracket advancement, not a played match. Keep it available
    // to the official bracket data, but never show it as a schedule fixture.
    .filter((record) => !recordHasBye(record))
    .filter((record) => !categories.length || categories.includes(recordCategory(record)))
    .filter((record) => !state.dateFilter.length || state.dateFilter.includes(record.date))
    .filter((record) => !state.statusFilter.length || state.statusFilter.includes(recordStatus(record)))
    .filter((record) => !courts.length || courts.includes(recordCourtKey(record)))
    .sort((left, right) => {
      const liveOrder = Number(rightStatusIsLive(right) - rightStatusIsLive(left));
      if (liveOrder) return liveOrder;
      return `${left.date || ""}T${left.time || ""}`.localeCompare(`${right.date || ""}T${right.time || ""}`) || String(left.id).localeCompare(String(right.id));
    });
}
function rightStatusIsLive(record) { return recordStatus(record) === "live" ? 1 : 0; }

function renderDateFilter() {
  const dates = [...new Set(sportRecords().map((record) => record.date).filter(Boolean))].sort();
  const current = state.dateFilter;
  elements.dateFilter.innerHTML = dates.map((date) => `<option value="${escapeHtml(date)}">${escapeHtml(dateLabel(date))}</option>`).join("");
  for (const option of elements.dateFilter.options) option.selected = current.includes(option.value);
  state.dateFilter = current.filter((date) => dates.includes(date));
  renderCheckboxMenu(elements.dateFilter, dates.map((date) => [date, dateLabel(date)]), state.dateFilter, (values) => { state.dateFilter = values; renderView(); void refreshVisibleExtras(false, { manual: true }); });
}

function renderSportFilter() {
  const options = Object.entries(SPORTS).filter(([code]) => state.records.some((record) => record.sport === code));
  elements.sportFilter.innerHTML = options.map(([value, label]) => `<option value="${value}">${escapeHtml(label)}</option>`).join("");
  for (const option of elements.sportFilter.options) option.selected = state.sportFilter.includes(option.value);
  renderCheckboxMenu(elements.sportFilter, options, state.sportFilter, (values) => { state.sportFilter = values; renderView(); void refreshVisibleExtras(false, { manual: true }); });
}

function renderCategoryFilter() {
  // The schedule is the source of truth for available categories. This keeps
  // mixed doubles and any newly published event visible even if the separate
  // standings/bracket endpoint lags behind.
  const scheduleOptions = [...new Map(sportRecords()
    .filter((record) => recordCategory(record))
    .map((record) => [recordCategory(record), record.category])).entries()];
  let options = state.view === "schedule" || scheduleOptions.length
    ? scheduleOptions
    : (state.tournaments.get(state.activeSport)?.data?.events || []).map((event) => [String(event.id), event.name]);
  if (!options.length) options = [["", "暂无类别"]];
  const key = selectionKey();
  const current = state.selections.get(key) || [];
  const values = options.map(([value]) => value);
  // Tournament views need one event selected so their standings/bracket can
  // render. The today overview remains unselected by default to show all
  // categories.
  const selected = state.view === "schedule" ? current.filter((value) => values.includes(value)) : (current.filter((value) => values.includes(value)).length ? current.filter((value) => values.includes(value)) : [options[0][0]]);
  state.selections.set(key, selected);
  elements.category.innerHTML = options.map(([value, label]) => `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`).join("");
  for (const option of elements.category.options) option.selected = state.selections.get(key).includes(option.value);
  elements.category.disabled = options.length <= 1;
  if (state.view === "schedule") {
    renderCheckboxMenu(elements.category, options, state.selections.get(key), (values) => { state.selections.set(key, values); renderView(); void refreshVisibleExtras(false, { manual: true }); });
  }
}

function transposeScoreSection(section) {
  const columns = Array.isArray(section?.columns) ? section.columns : [];
  const rows = Array.isArray(section?.rows) ? section.rows : [];
  if (!columns.length || !rows.length) return section;

  const first = String(columns[0] || "").trim();
  const values = (row, index) => Array.isArray(row) ? (row[index] ?? "—") : "—";

  // Official set/game feeds put the period in the first column and the
  // competitors in the column headings.  Present the same data in the
  // orientation used by the official detail view: one competitor per row,
  // with each period's score in its own column.
  if (/^(局|盘|节)(?:\/|、)?(?:局|盘|节)?/.test(first) && columns.length > 1) {
    const labels = rows.map((row, index) => String(Array.isArray(row) && row[0] ? row[0] : `第${index + 1}局`));
    const competitors = columns.slice(1);
    return {
      ...section,
      columns: [section.entityLabel || "姓名", ...labels],
      rows: competitors.map((name, competitorIndex) => [name, ...rows.map((row) => values(row, competitorIndex + 1))]),
    };
  }

  // Cricket's Runs/Wickets/Overs block has the same axis inversion, but its
  // first column is labelled “项目” rather than “局/节”.
  if (first === "项目" && columns.length > 2 && rows.every((row) => /^(Runs|Wickets|Overs)$/i.test(String(row?.[0] || "")))) {
    const competitors = columns.slice(1);
    const labels = rows.map((row) => String(row[0]));
    return {
      ...section,
      columns: [section.entityLabel || "队伍", ...labels],
      rows: competitors.map((name, competitorIndex) => [name, ...rows.map((row) => values(row, competitorIndex + 1))]),
    };
  }

  // A one-row current-score section (for example tennis' live points) has no
  // period label. Give it the same left-name/right-score treatment.
  if (rows.length === 1 && columns.length >= 2 && (section.entityLabel === "姓名" || section.title === "当前局")) {
    const row = rows[0];
    return {
      ...section,
      columns: [section.entityLabel || "姓名", section.title || "当前比分"],
      rows: columns.map((name, index) => [name, values(row, index)]),
    };
  }

  // Baseball's current at-bat counters have no competitors; keeping them as
  // a compact vertical key/value table is clearer on narrow screens.
  if (rows.length === 1 && columns.length && columns.every((label) => ["坏球", "好球", "出局"].includes(String(label)))) {
    return {
      ...section,
      columns: [section.entityLabel || "项目", "数量"],
      rows: columns.map((label, index) => [label, values(rows[0], index)]),
    };
  }
  return section;
}

function renderDataTable(section, options = {}) {
  const display = options.transpose ? transposeScoreSection(section) : section;
  const columns = Array.isArray(display.columns) ? display.columns : [];
  const rows = Array.isArray(display.rows) ? display.rows : [];
  if (!rows.length) return "";
  const tableClass = options.transpose ? "data-table score-orientation-table" : "data-table";
  return `<div class="data-table-scroll"><table class="${tableClass}">
    ${columns.length ? `<thead><tr>${columns.map((value) => `<th scope="col">${escapeHtml(value)}</th>`).join("")}</tr></thead>` : ""}
    <tbody>${rows.map((row) => `<tr>${(Array.isArray(row) ? row : []).map((value, index) => `<${index === 0 ? "th scope=\"row\"" : "td"}>${escapeHtml(value)}</${index === 0 ? "th" : "td"}>`).join("")}</tr>`).join("")}</tbody>
  </table></div>`;
}

function detailContent(id) {
  const detail = state.details.get(id);
  if (!detail || (detail.loading && !detail.data)) return '<p class="panel-message" role="status">正在读取小分…</p>';
  if (!detail.data) return `<p class="panel-message">${escapeHtml(detail.error || "官网尚未公布小分")}</p><button class="text-button" type="button" data-retry-match="${escapeHtml(id)}">重试</button>`;
  const sections = (detail.data.sections || []).filter((section) => Array.isArray(section.rows) && section.rows.length);
  const subMatches = Array.isArray(detail.data.subMatches) ? detail.data.subMatches : [];
  const hasLineup = lineupPlayers(detail.data, "home").length || lineupPlayers(detail.data, "away").length;
  const hasContent = sections.length || subMatches.length || hasLineup;
  return `${staleNotice(detail, "小分")}
    ${renderLineup(detail.data, id, id)}${renderScoreSections(sections)}${renderSubMatches(subMatches, id, id)}
    ${hasContent ? "" : `<p class="panel-message">${escapeHtml(detail.data.message || "官网尚未公布小分")}</p>`}`;
}

function lineupPlayers(match, side) {
  const players = match && match[`${side}Players`];
  if (!Array.isArray(players)) return [];
  return players.filter((player) => player && typeof player === "object");
}

function lineupPhoto(player) {
  const reg = String(player?.reg || "").trim();
  // Prefer a checked-in static asset.  This keeps GitHub Pages and Render
  // from requesting the official photo host for every Line-up render.  The
  // image's error handler falls back to our API only when this registration
  // has not yet been harvested into static/player-photos.
  if (reg && /^[A-Za-z0-9_.-]+$/.test(reg)) {
    const base = SITE_BASE || "";
    return `${base}/player-photos/${encodeURIComponent(reg)}.jpg`;
  }
  const value = String(player?.photo || player?.avatar || "").trim();
  return /^https?:\/\//i.test(value) ? value : "";
}

function lineupInitials(player) {
  const name = String(player?.name || "?").trim();
  const words = name.split(/\s+/).filter(Boolean);
  if (words.length > 1) return `${words[0][0] || ""}${words[words.length - 1][0] || ""}`.toUpperCase();
  return [...name].slice(0, 2).join("").toUpperCase() || "?";
}

function lineupCountry(player) {
  return String(player?.country || player?.orgName || player?.org || "").trim();
}

function flagMarkup(value, label = "") {
  const code = String(value?.Org || value?.org || value?.countryCode || value || "").trim().toUpperCase();
  if (!/^[A-Z]{3}$/.test(code)) return "";
  const src = `${SITE_BASE}/flags/${encodeURIComponent(code)}.png`;
  return `<img class="country-flag" src="${escapeHtml(src)}" alt="${escapeHtml(label || code)}" loading="lazy" onerror="this.hidden=true">`;
}

function renderMatchup(record) {
  const text = String(record?.matchup || "对阵待定");
  const home = record?.home || {};
  const away = record?.away || {};
  return `<span class="matchup-with-flags">${flagMarkup(home, home.Name || home.NameS || "")}${escapeHtml(text)}${flagMarkup(away, away.Name || away.NameS || "")}</span>`;
}

function renderLineupPlayer(player) {
  const name = String(player.name || "待定").trim() || "待定";
  const photo = lineupPhoto(player);
  const initials = lineupInitials(player);
  const country = lineupCountry(player);
  const role = player.substitute ? " · 替补" : "";
  const reg = String(player?.reg || "").trim();
  const fallback = reg && /^[A-Za-z0-9_.-]+$/.test(reg)
    ? apiUrl(`/api/player-photo?reg=${encodeURIComponent(reg)}`) : "";
  const photoMarkup = photo
    ? `<img class="lineup-player-photo" src="${escapeHtml(photo)}" alt="" loading="lazy" onerror="${fallback ? `this.onerror=function(){this.hidden=true;this.nextElementSibling.hidden=false};this.src='${escapeHtml(fallback)}';` : "this.hidden=true;this.nextElementSibling.hidden=false;"}" />`
    : "";
  return `<li class="lineup-player">
    <span class="lineup-player-avatar">${photoMarkup}<span class="lineup-player-initials"${photo ? " hidden" : ""} aria-hidden="true">${escapeHtml(initials)}</span></span>
    <span class="lineup-player-copy"><strong>${flagMarkup(player, country)}${escapeHtml(name)}</strong>${country || role ? `<span>${escapeHtml(country)}${escapeHtml(role)}</span>` : ""}</span>
  </li>`;
}

function renderLineup(match, lineupKey = "lineup", rootId = lineupKey) {
  const home = lineupPlayers(match, "home");
  const away = lineupPlayers(match, "away");
  if (!home.length && !away.length) return "";
  const key = String(lineupKey);
  const visible = lineupIsVisible(key);
  const panelId = `lineup-panel-${encodeURIComponent(key)}`;
  const team = (label, players, side) => `<div class="lineup-team lineup-team-${side}">
    <h4>${escapeHtml(label || (side === "home" ? "主队" : "客队"))}</h4>
    <ul>${players.map(renderLineupPlayer).join("")}</ul>
  </div>`;
  return `<section class="lineup-section" aria-label="Line-up"><div class="lineup-heading"><div><h3>Line-up</h3><span>球员名单</span></div><button class="lineup-toggle" type="button" data-toggle-lineup="${escapeHtml(key)}" data-lineup-root="${escapeHtml(rootId)}" aria-expanded="${visible}" aria-controls="${escapeHtml(panelId)}">${visible ? "隐藏阵容" : "显示阵容"}</button></div><div class="lineup-grid" id="${escapeHtml(panelId)}"${visible ? "" : " hidden"}>${team(match.home, home, "home")}${team(match.away, away, "away")}</div></section>`;
}

function renderScoreSections(sections) {
  return (sections || []).map((section) => `<section class="score-section">${section.title ? `<h3>${escapeHtml(section.title)}</h3>` : ""}${renderDataTable(section, { transpose: true })}</section>`).join("");
}

function subMatchStatus(match) {
  if (match.isLive) return "进行中";
  const status = String(match.status || "").toUpperCase();
  if (["LIVE", "RUNNING", "IN_PROGRESS"].includes(status)) return "进行中";
  if (["OFFICIAL", "FINISHED", "COMPLETED"].includes(status)) return "完场";
  if (["CANCELED", "CANCELLED"].includes(status)) return "已取消";
  if (status === "NOT_PLAYED") return "未进行";
  if (status === "POSTPONED") return "推迟";
  if (status === "SUSPENDED" || status === "INTERRUPTED") return "暂停";
  if (status === "UNOFFICIAL") return "成绩待确认";
  if (["SCHEDULED", "UNSCHEDULED", "START_LIST", "PROVISIONAL", "GETTING_READY", "RESCHEDULED"].includes(status)) return "未开赛";
  return "状态待定";
}

function renderSubMatches(matches, parentKey, rootId = parentKey) {
  if (!Array.isArray(matches) || !matches.length) return "";
  const keyOf = (match, index) => String(match.id || index + 1);
  let index = matches.findIndex((match, i) => keyOf(match, i) === state.selectedSubMatches.get(parentKey));
  if (index < 0) {
    index = Math.max(0, matches.findIndex((match) => subMatchStatus(match) === "进行中"));
    state.selectedSubMatches.set(parentKey, keyOf(matches[index], index));
  }
  const match = matches[index];
  const status = subMatchStatus(match);
  const score = (value) => value === null || value === undefined || value === "" ? "—" : escapeHtml(value);
  const sections = (match.sections || []).filter((section) => Array.isArray(section.rows) && section.rows.length);
  const panelId = `submatch-panel-${encodeURIComponent(parentKey)}`;
  return `<div class="submatch-list" aria-label="团体赛子比赛">
    <div class="submatch-tabs" role="group" aria-label="选择子比赛">${matches.map((child, i) => `<button class="submatch-tab" type="button" aria-pressed="${i === index}" aria-controls="${escapeHtml(panelId)}" data-select-submatch="${escapeHtml(keyOf(child, i))}" data-submatch-parent="${escapeHtml(parentKey)}" data-submatch-root="${escapeHtml(rootId)}">第${escapeHtml(child.number || i + 1)}场${child.type ? ` · ${escapeHtml(child.type)}` : ""}${subMatchStatus(child) === "进行中" ? '<span class="submatch-live-dot" aria-label="进行中"></span>' : ""}</button>`).join("")}</div>
    <section class="submatch-card" id="${escapeHtml(panelId)}" data-submatch-id="${escapeHtml(keyOf(match, index))}">
      <div class="submatch-heading"><h3>第${escapeHtml(match.number || index + 1)}场${match.type ? ` · ${escapeHtml(match.type)}` : ""}</h3><span class="submatch-status${status === "进行中" ? " is-live" : ""}">${status}</span></div>
      <div class="submatch-players" aria-label="选手与比分">
        <div><span>${escapeHtml(match.home || "待定")}</span><strong>${score(match.homeScore)}</strong></div>
        <div><span>${escapeHtml(match.away || "待定")}</span><strong>${score(match.awayScore)}</strong></div>
      </div>
      ${renderLineup(match, `${parentKey}/${keyOf(match, index)}`, rootId)}${renderScoreSections(sections)}${renderSubMatches(match.subMatches, `${parentKey}/${keyOf(match, index)}`, rootId)}
    </section></div>`;
}

function restoreSubmatchFocus(container, focus) {
  if (!focus?.selectSubmatch) return;
  [...container.querySelectorAll("[data-select-submatch]")].find((button) => button.dataset.selectSubmatch === focus.selectSubmatch && button.dataset.submatchParent === focus.submatchParent)?.focus({ preventScroll: true });
}

function updateDetailPanel(id) {
  const panel = document.getElementById(`detail-${id}`);
  if (!panel) return;
  const focus = { ...document.activeElement?.dataset };
  panel.innerHTML = detailContent(id);
  restoreSubmatchFocus(panel, focus);
  if (focus?.toggleLineup) {
    [...panel.querySelectorAll("[data-toggle-lineup]")]
      .find((button) => button.dataset.toggleLineup === focus.toggleLineup)?.focus({ preventScroll: true });
  }
}

function staleNotice(entry, label) {
  if (!entry?.error && !entry?.data?.stale) return "";
  const reason = entry.error || entry.data.message || "官网暂时连接失败";
  const updated = entry.data?.updatedAt ? `上次更新 ${formatSyncTime(entry.data.updatedAt)}（北京时间）` : "上次更新时间未提供";
  return `<p class="panel-message is-error">${escapeHtml(label)}暂未更新，显示上次成功数据。${escapeHtml(reason)} · ${escapeHtml(updated)}</p>`;
}

function renderSchedule() {
  const records = filteredRecords();
  elements.count.textContent = `${records.length} 场`;
  elements.empty.hidden = records.length !== 0;
  const focusedMatch = document.activeElement?.dataset?.toggleMatch;
  const submatchFocus = { ...document.activeElement?.dataset };
  elements.table.hidden = Boolean(state.layout);
  elements.cards.hidden = !state.layout;
  elements.scheduleView.classList.toggle("is-card-layout", Boolean(state.layout));
  elements.layout.value = String(state.layout);
  elements.cards.style.setProperty("--match-columns", state.layout || 1);
  if (state.layout) {
    elements.body.innerHTML = "";
    elements.cards.innerHTML = records.map((record) => {
      const dateTime = formatDateTime(record);
      const open = state.expanded.has(record.id);
      const status = recordStatus(record);
      const categoryLabel = state.activeSport ? record.category : `${record.sportName || SPORTS[record.sport] || ""} · ${record.category || ""}`;
      const scheduleNotice = isTeamRecord(record) && hasTeamScheduleChange(record.id) ? '<span class="schedule-change-inline">官网赛程有变动</span>' : "";
      return `<article class="match-card ${status === "live" ? "is-live" : ""} ${open ? "is-expanded" : ""}" data-match-id="${escapeHtml(record.id)}">
        <div class="card-content">
          <header class="card-header"><div class="card-date"><strong>${escapeHtml(dateTime.date)}</strong><span>${escapeHtml(dateTime.time)}</span></div><span class="card-category">${escapeHtml(categoryLabel)}</span></header>
          <p class="card-stage">${escapeHtml(record.stage)}</p>
          <h3 class="card-matchup">${renderMatchup(record)}${scheduleNotice}</h3>
          <div class="card-score"><button class="score-toggle" type="button" data-toggle-match="${escapeHtml(record.id)}" aria-expanded="${open}" aria-controls="detail-${escapeHtml(record.id)}" aria-label="${open ? "收起" : "查看"}${escapeHtml(record.matchup)}的小分"><span>${escapeHtml(formatScore(record))}</span><span class="disclosure-arrow" aria-hidden="true">⌄</span></button></div>
          <p class="card-venue">${escapeHtml(recordVenue(record))}</p>
        </div>
        ${open ? `<div class="match-detail" id="detail-${escapeHtml(record.id)}" aria-label="${escapeHtml(record.matchup)}的小分">${detailContent(record.id)}</div>` : ""}
      </article>`;
    }).join("");
    if (focusedMatch) [...elements.cards.querySelectorAll("[data-toggle-match]")].find((button) => button.dataset.toggleMatch === focusedMatch)?.focus({ preventScroll: true });
    restoreSubmatchFocus(elements.cards, submatchFocus);
    return;
  }
  elements.cards.innerHTML = "";
  elements.body.innerHTML = records.map((record) => {
    const dateTime = formatDateTime(record);
    const cancelled = ["CANCELED", "CANCELLED", "POSTPONED"].includes(record.status);
    const open = state.expanded.has(record.id);
    const categoryLabel = state.activeSport ? record.category : `${record.sportName || SPORTS[record.sport] || ""} · ${record.category || ""}`;
    const rowClass = record.isLive ? "is-live" : cancelled ? "is-cancelled" : "";
    const scheduleNotice = isTeamRecord(record) && hasTeamScheduleChange(record.id) ? '<span class="schedule-change-inline">官网赛程有变动</span>' : "";
    return `<tr class="match-row ${rowClass} ${open ? "is-expanded" : ""}" data-match-id="${escapeHtml(record.id)}">
      <td class="date-cell" data-label="日期时间"><span><strong>${escapeHtml(dateTime.date)}</strong>${escapeHtml(dateTime.time)}</span></td>
      <td class="category-cell" data-label="类别"><span>${escapeHtml(categoryLabel)}</span></td>
      <td data-label="阶段"><span>${escapeHtml(record.stage)}</span></td>
      <td class="matchup-cell" data-label="对阵">${renderMatchup(record)}${scheduleNotice}</td>
      <td class="score-cell" data-label="比分"><button class="score-toggle" type="button" data-toggle-match="${escapeHtml(record.id)}"
        aria-expanded="${open}" aria-controls="detail-${escapeHtml(record.id)}" aria-label="${open ? "收起" : "查看"}${escapeHtml(record.matchup)}的小分">
        <span>${escapeHtml(formatScore(record))}</span><span class="disclosure-arrow" aria-hidden="true">⌄</span></button></td>
      <td data-label="场馆"><span>${escapeHtml(recordVenue(record))}</span></td>
    </tr>${open ? `<tr class="detail-row"><td colspan="6"><div class="match-detail" id="detail-${escapeHtml(record.id)}" aria-label="${escapeHtml(record.matchup)}的小分">${detailContent(record.id)}</div></td></tr>` : ""}`;
  }).join("");
  if (focusedMatch) [...elements.body.querySelectorAll("[data-toggle-match]")].find((button) => button.dataset.toggleMatch === focusedMatch)?.focus({ preventScroll: true });
  restoreSubmatchFocus(elements.body, submatchFocus);
}

function renderTournament() {
  const entry = state.tournaments.get(state.activeSport);
  const previousScroll = elements.tournamentView.querySelector(".rounds-board")?.scrollLeft || 0;
  elements.count.textContent = "";
  if (!entry || (entry.loading && !entry.data)) {
    elements.tournamentView.innerHTML = '<div class="view-message" role="status">正在读取官方数据…</div>';
    return;
  }
  if (!entry.data) {
    elements.tournamentView.innerHTML = `<div class="view-message"><p>${escapeHtml(entry.error || "官网尚未公布")}</p><button class="text-button" type="button" data-retry-tournament>重试</button></div>`;
    return;
  }
  const selectedEvents = state.selections.get(selectionKey()) || [];
  const event = (entry.data.events || []).find((item) => selectedEvents.includes(String(item.id)));
  const errorNote = staleNotice(entry, state.view === "groups" ? "小组积分" : "对阵图");
  if (!event) {
    elements.tournamentView.innerHTML = `${errorNote}<div class="view-message">${escapeHtml(entry.data.message || "官网尚未公布")}</div>`;
    return;
  }
  if (state.view === "groups") {
    const groups = (event.groups || []).filter((group) => Array.isArray(group.rows) && group.rows.length);
    elements.tournamentView.innerHTML = `${errorNote}${groups.length ? `<div class="group-grid">${groups.map((group) => `<section class="group-card"><h3>${escapeHtml(group.name)}</h3>${renderDataTable(group)}</section>`).join("")}</div>` : `<div class="view-message">${escapeHtml(event.message || "官网尚未公布此类别的小组积分")}</div>`}`;
    elements.count.textContent = groups.length ? `${groups.length} 个小组` : "";
  } else {
    const rounds = (event.rounds || []).filter((round) => Array.isArray(round.matches) && round.matches.length);
    const matchLocations = new Map(rounds.flatMap((round) => round.matches.map((match, index) => [String(match.id), { round: round.name, number: index + 1 }])));
    elements.tournamentView.innerHTML = `${errorNote}${rounds.length ? `<div class="rounds-board" aria-label="淘汰赛各轮对阵">${rounds.map((round) => `<section class="round-column"><h3>${escapeHtml(round.name)}</h3><div class="round-matches">${round.matches.map((match, index) => bracketMatch(match, index + 1, matchLocations)).join("")}</div></section>`).join("")}</div>` : `<div class="view-message">${escapeHtml(event.message || "官网尚未公布此类别的淘汰赛对阵")}</div>`}`;
    elements.count.textContent = rounds.length ? `${rounds.length} 轮` : "";
    const board = elements.tournamentView.querySelector(".rounds-board");
    if (board) board.scrollLeft = previousScroll;
  }
}

function bracketMatch(match, number, locations) {
  const winner = String(match.winner ?? "").toUpperCase();
  const homeWins = winner && ["HOME", "1", String(match.home).toUpperCase()].includes(winner);
  const awayWins = winner && ["AWAY", "2", String(match.away).toUpperCase()].includes(winner);
  const destination = match.nextMatchId ? locations.get(String(match.nextMatchId)) : null;
  const score = (value) => String(value ?? "").trim() || "—";
  const status = String(match.status || "").toUpperCase();
  const statusLabel = status === "CANCELED" || status === "CANCELLED" ? "已取消" : "";
  return `<article class="bracket-match" id="bracket-${escapeHtml(encodeURIComponent(String(match.id)))}" tabindex="-1">
    <p class="bracket-match-label">对阵 ${number}${statusLabel ? ` · ${statusLabel}` : ""}</p>
    <div class="bracket-team ${homeWins ? "is-winner" : ""}"><span>${escapeHtml(match.home || "待定")}</span><strong>${escapeHtml(score(match.homeScore))}</strong></div>
    <div class="bracket-team ${awayWins ? "is-winner" : ""}"><span>${escapeHtml(match.away || "待定")}</span><strong>${escapeHtml(score(match.awayScore))}</strong></div>
    ${destination ? `<button class="advancement-link" type="button" data-advance-to="${escapeHtml(match.nextMatchId)}">胜者进入：${escapeHtml(destination.round)} · 对阵 ${destination.number} <span aria-hidden="true">→</span></button>` : ""}
  </article>`;
}

function renderView() {
  elements.title.textContent = SPORTS[state.activeSport] || "今日赛程";
  elements.scheduleView.hidden = state.view !== "schedule";
  elements.tournamentView.hidden = state.view === "schedule";
  // Keep the event/category selector available for standings and bracket
  // views. Date and status only apply to the schedule table.
  elements.scheduleFilters.hidden = false;
  elements.dateFilter.parentElement.hidden = state.view !== "schedule";
  elements.statusFilter.parentElement.hidden = state.view !== "schedule";
  elements.sportFilter.parentElement.hidden = state.view !== "schedule" || Boolean(state.activeSport);
  elements.layout.parentElement.hidden = state.view !== "schedule";
  elements.courtFilter.parentElement.hidden = state.view !== "schedule" || (Boolean(state.activeSport) && !["TEN", "TTE", "BDM"].includes(state.activeSport));
  if (state.view === "schedule") renderCourtFilter();
  elements.category.parentElement.hidden = false;
  elements.category.multiple = state.view === "schedule";
  elements.category.size = state.view === "schedule" ? 1 : 1;
  elements.category.hidden = state.view === "schedule";
  const categoryMenu = checkboxMenus.get(elements.category);
  if (categoryMenu) categoryMenu.hidden = state.view !== "schedule";
  elements.viewTabs.hidden = !state.activeSport;
  for (const button of elements.viewTabs.querySelectorAll("[data-view]")) button.setAttribute("aria-selected", String(button.dataset.view === state.view));
  renderCategoryFilter();
  if (state.view === "schedule") {
    renderSportFilter();
    renderDateFilter();
    renderCheckboxMenu(elements.statusFilter, [["live", "进行中"], ["completed", "完场"], ["upcoming", "未开赛"]], state.statusFilter, (values) => { state.statusFilter = values; renderView(); void refreshVisibleExtras(false, { manual: true }); });
  }
  if (state.view === "schedule") renderSchedule(); else renderTournament();
}

function renderStatus() {
  const status = state.status || {};
  const teamChangesAdded = retainTeamScheduleChanges(status);
  if (teamChangesAdded && state.recordsLoaded && state.view === "schedule") renderSchedule();
  elements.statusDot.className = "status-dot";
  elements.syncButton.disabled = Boolean(status.running);
  elements.syncButton.classList.toggle("is-running", Boolean(status.running));
  const interval = Number(status.liveIntervalSeconds) || 30;
  elements.automaticSync.textContent = todayCompleted()
    ? status.resultsPendingConfirmation
      ? status.resultConfirmationExpired
        ? "待确认赛果自动核对已结束 · 请手动同步"
        : "今日比赛已结束 · 每 5 分钟核对待确认赛果"
      : "今日比赛已全部完场 · 自动同步已停止"
    : !automaticSyncAllowed()
      ? "北京时间 08:00–23:00 自动更新 · 当前仅手动同步"
      : status.liveEnabled
        ? `北京时间 08:00–23:00 自动更新 · 比赛日约每 ${interval} 秒更新比分`
        : "北京时间 08:00–23:00 自动更新 · 每天 08:00 全量同步";
  elements.automaticSync.title = todayCompleted() && status.resultsPendingConfirmation && status.resultConfirmationDeadline
    ? `北京时间，待确认赛果自动核对截止 ${formatSyncTime(status.resultConfirmationDeadline)}`
    : status.nextAutomaticSync ? `北京时间，下次全量同步 ${formatSyncTime(status.nextAutomaticSync)}` : "北京时间";
  const updated = [status.lastLiveSuccess, status.lastSuccess].filter(Boolean).sort().at(-1);
  if (state.connectionError) {
    elements.statusDot.classList.add("is-error");
    elements.syncStatus.textContent = "连接中断，正在自动重试";
  } else if (status.running) {
    elements.statusDot.classList.add("is-running");
    elements.syncStatus.textContent = `正在同步${status.progressTotal ? ` ${status.progressDone}/${status.progressTotal}` : ""}`;
  } else if (status.lastError || status.lastLiveError) {
    elements.statusDot.classList.add("is-error");
    elements.syncStatus.textContent = `等待重试 · 上次更新 ${formatSyncTime(updated)}`;
  } else if (updated) {
    elements.statusDot.classList.add("is-success");
    elements.syncStatus.textContent = `更新于 ${formatSyncTime(updated)}`;
  } else elements.syncStatus.textContent = "等待首次同步";
  const error = state.connectionError || status.lastLiveError || status.lastError;
  elements.errorBanner.hidden = !error;
  elements.errorBanner.textContent = error ? `暂未更新，已保留现有赛程。${error}` : "";
  // Schedule edits are shown beside the affected team-tie row. A global
  // banner is intentionally kept hidden because it cannot identify which
  // of the many matches changed.
  elements.scheduleChangeBanner.hidden = true;
  elements.scheduleChangeBanner.textContent = "";
}

function statusVersion(status) {
  return String(status?.scheduleVersion ?? status?.dataVersion ?? `${status?.lastSuccess || ""}`);
}

function applyLiveDelta(status) {
  const delta = Array.isArray(status?.liveDelta) ? status.liveDelta : [];
  if (!delta.length || !state.recordsLoaded) return;
  const byId = new Map(state.records.map((record) => [String(record.id), record]));
  for (const update of delta) {
    const current = byId.get(String(update?.id));
    if (current && update && typeof update === "object") Object.assign(current, update);
  }
  renderView();
}

async function loadSchedule(version) {
  // Bind the full schedule request to the server version so a browser or
  // proxy cannot reuse an older snapshot that lacked court locations.
  const cacheKey = version ? `?schema=2&v=${encodeURIComponent(version)}` : "?schema=2";
  const payload = await fetchJson(apiUrl(`/api/schedule${cacheKey}`));
  state.records = Array.isArray(payload.records) ? payload.records : [];
  state.recordsLoaded = true;
  state.loadedVersion = version;
  if (!state.activeSport && !state.dateFilter.length) {
    const today = new Date(Date.now() + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
    if (state.records.some((record) => record.date === today)) state.dateFilter = [today];
  }
  // The root path is the all-sports “今日赛程” view. Sport paths opt into a
  // single sport explicitly and remain stable across refreshes.
  if (state.activeSport && !SPORTS[state.activeSport]) state.activeSport = null;
  renderTabs();
  renderView();
}

async function loadMatch(id, force = false, { automatic = false } = {}) {
  const current = state.details.get(id);
  if (current?.loading || (!force && current?.data)) return;
  const entry = { ...current, loading: true, lastRequested: Date.now(), error: "" };
  state.details.set(id, entry);
  updateDetailPanel(id);
  try {
    entry.data = await fetchJson(apiUrl(`/api/match?id=${encodeURIComponent(id)}${automatic ? "&automatic=1" : ""}`));
    // The schedule feed can publish a provisional “对阵待定” row while the
    // official results page already exposes the selected doubles players.
    // Promote that confirmed Line-up into the visible matchup immediately.
    const record = state.records.find((item) => String(item.id) === String(id));
    if (record && /待定/.test(String(record.matchup || ""))) {
      const side = (label, players) => {
        if (label && !/待定/.test(String(label))) return String(label);
        if (!Array.isArray(players) || !players.length || players.length > 2) return "";
        const names = players.map((player) => String(player?.name || player?.nameS || "").trim()).filter(Boolean);
        return names.length ? names.join(" / ") : "";
      };
      // Mixed/team results may publish the lineup only on a child unit.
      // Walk those units as well; the first child with both sides confirmed
      // is the official opponent pair for the provisional parent row.
      const sources = [entry.data, ...(Array.isArray(entry.data?.subMatches) ? entry.data.subMatches : [])];
      let home = "";
      let away = "";
      for (const source of sources) {
        home = side(source?.home, source?.homePlayers);
        away = side(source?.away, source?.awayPlayers);
        if (home && away) break;
      }
      if (home && away) {
        record.matchup = `${home} vs ${away}`;
        renderView();
      }
    }
  }
  catch (error) { entry.error = error.message || "无法读取小分"; }
  finally {
    entry.loading = false;
    updateDetailPanel(id);
  }
}

async function loadTournament(force = false, { automatic = false } = {}) {
  const sport = state.activeSport;
  if (!sport) return;
  const current = state.tournaments.get(sport);
  if (current?.loading || (!force && current?.data && current.version === state.loadedVersion)) return;
  const entry = { ...current, loading: true, lastRequested: Date.now(), error: "", version: state.loadedVersion };
  state.tournaments.set(sport, entry);
  if (state.view !== "schedule") renderView();
  try { entry.data = await fetchJson(apiUrl(`/api/tournament?sport=${encodeURIComponent(sport)}${automatic ? "&automatic=1" : ""}`)); }
  catch (error) { entry.error = error.message || "无法读取积分和对阵"; }
  finally {
    entry.loading = false;
    if (state.activeSport === sport && state.view !== "schedule") renderView();
  }
}

function todayCompleted(now = Date.now()) {
  const beijingDate = new Date(now + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
  return state.status?.todayCompleted === true && state.status.completionDate === beijingDate;
}

function automaticSyncAllowed(now = Date.now()) {
  // Use a fixed UTC+8 offset so device timezone and an old status response
  // cannot leave automatic detail requests running after 23:00 Beijing time.
  const beijingHour = new Date(now + 8 * 60 * 60 * 1000).getUTCHours();
  return beijingHour >= 8 && beijingHour < 23 && !todayCompleted(now) && state.status?.automaticSyncAllowed !== false;
}

async function refreshVisibleExtras(force = false, { manual = false, completionRefresh = false } = {}) {
  // Completion refreshes only read finalized server caches, including versions
  // updated by the low-frequency confirmation checks.
  if (!manual && todayCompleted() && state.status?.resultConfirmationExpired) return;
  if (!manual && !automaticSyncAllowed() && !(completionRefresh && todayCompleted())) return;
  const interval = (Number(state.status?.liveIntervalSeconds) || 30) * 1000;
  if (state.view !== "schedule") {
    const entry = state.tournaments.get(state.activeSport);
    if (force || !entry || Date.now() - entry.lastRequested >= interval) await loadTournament(true, { automatic: !manual });
    return;
  }
  const visibleOpen = filteredRecords().filter((record) => state.expanded.has(record.id));
  await Promise.allSettled(visibleOpen.map((record) => {
    const entry = state.details.get(record.id);
    return force || !entry || Date.now() - entry.lastRequested >= interval ? loadMatch(record.id, true, { automatic: !manual }) : undefined;
  }));
}

function visibleExtrasNeedRetry() {
  const entries = state.view !== "schedule"
    ? [state.tournaments.get(state.activeSport)]
    : filteredRecords().filter((record) => state.expanded.has(record.id)).map((record) => state.details.get(record.id));
  return entries.some((entry) => !entry || entry.loading || entry.error);
}

async function refresh(force = false) {
  if (state.refreshing) { state.refreshAgain ||= force; return; }
  state.refreshing = true;
  try {
    const status = await fetchJson(apiUrl("/api/status"));
    const wasCompleted = todayCompleted();
    state.status = status;
    applyLiveDelta(status);
    const version = statusVersion(status);
    const changed = version !== state.loadedVersion;
    if (todayCompleted() && !status.resultConfirmationExpired && (!wasCompleted || changed)) {
      state.completionExtrasPending = true;
      state.completionExtrasRetryAt = 0;
    }
    if (!todayCompleted() || status.resultConfirmationExpired) {
      state.completionExtrasPending = false;
      state.completionExtrasRetryAt = 0;
    }
    if (force || !state.recordsLoaded || changed) await loadSchedule(version);
    state.connectionError = "";
    renderStatus();
    if (state.manualSyncPending && !status.running) {
      state.manualSyncPending = false;
      state.manualExtrasPending = !status.lastError;
    }
    if (!document.hidden) {
      const manual = state.manualExtrasPending;
      const completionRefresh = state.completionExtrasPending && Date.now() >= state.completionExtrasRetryAt;
      state.manualExtrasPending = false;
      if (completionRefresh) {
        const retrySeconds = status.resultsPendingConfirmation ? 300 : (Number(status.liveIntervalSeconds) || 30);
        state.completionExtrasRetryAt = Date.now() + retrySeconds * 1000;
      }
      await refreshVisibleExtras(force || changed || manual || completionRefresh, { manual, completionRefresh });
      // A failed final cache read must not lose the final score. Subsequent
      // attempts still use automatic=1 (cache only after completion), at the
      // confirmation interval while results are pending, even on focus.
      if (completionRefresh) state.completionExtrasPending = visibleExtrasNeedRetry();
    }
  } catch (error) {
    state.connectionError = error.message || "无法连接本地服务";
    renderStatus();
  } finally {
    state.refreshing = false;
    if (state.refreshAgain) {
      state.refreshAgain = false;
      void refresh(true);
    }
  }
}

async function requestSync() {
  elements.syncButton.disabled = true;
  try {
    const response = await fetch(apiUrl("/api/sync"), { method: "POST" });
    if (!response.ok && response.status !== 409) throw new Error("无法启动同步");
    state.manualSyncPending = true;
    await refresh();
  } catch (error) {
    state.connectionError = error.message;
    renderStatus();
  }
}

function toggleMatch(id) {
  if (state.expanded.has(id)) state.expanded.delete(id);
  else state.expanded.add(id);
  renderSchedule();
  if (state.expanded.has(id)) void loadMatch(id);
}

elements.tabs.addEventListener("click", (event) => {
  const button = event.target.closest("[data-sport]");
  if (!button) return;
  state.activeSport = button.dataset.sport === "TODAY" ? null : button.dataset.sport;
  updateSportPath(state.activeSport);
  renderTabs();
  renderView();
  if (state.view !== "schedule") void loadTournament();
  else void refreshVisibleExtras(false, { manual: true });
});
elements.viewTabs.addEventListener("click", (event) => {
  const button = event.target.closest("[data-view]");
  if (!button) return;
  state.view = button.dataset.view;
  renderView();
  if (state.view !== "schedule") void loadTournament();
  else void refreshVisibleExtras(false, { manual: true });
});
elements.category.addEventListener("change", () => {
  state.selections.set(selectionKey(), [...elements.category.selectedOptions].map((option) => option.value));
  renderView();
  void refreshVisibleExtras(false, { manual: true });
});
elements.dateFilter.addEventListener("change", () => {
  state.dateFilter = [...elements.dateFilter.selectedOptions].map((option) => option.value);
  renderView();
  void refreshVisibleExtras(false, { manual: true });
});
elements.statusFilter.addEventListener("change", () => {
  state.statusFilter = [...elements.statusFilter.selectedOptions].map((option) => option.value);
  renderView();
  void refreshVisibleExtras(false, { manual: true });
});
elements.sportFilter.addEventListener("change", () => {
  state.sportFilter = [...elements.sportFilter.selectedOptions].map((option) => option.value);
  renderView();
  void refreshVisibleExtras(false, { manual: true });
});
elements.layout.addEventListener("change", () => {
  const columns = Number(elements.layout.value);
  state.layout = [2, 3, 4, 5].includes(columns) ? columns : 0;
  try { window.localStorage?.setItem("schedule-layout", String(state.layout)); } catch {}
  renderSchedule();
});
elements.scheduleView.addEventListener("click", (event) => {
  const submatch = event.target.closest("[data-select-submatch]");
  if (submatch) {
    state.selectedSubMatches.set(submatch.dataset.submatchParent, submatch.dataset.selectSubmatch);
    updateDetailPanel(submatch.dataset.submatchRoot);
    return;
  }
  const lineupToggle = event.target.closest("[data-toggle-lineup]");
  if (lineupToggle) {
    const key = lineupToggle.dataset.toggleLineup;
    setLineupVisibility(key, lineupToggle.getAttribute("aria-expanded") !== "true");
    updateDetailPanel(lineupToggle.dataset.lineupRoot);
    return;
  }
  const retry = event.target.closest("[data-retry-match]");
  if (retry) { void loadMatch(retry.dataset.retryMatch, true); return; }
  if (event.target.closest(".match-detail")) return;
  const row = event.target.closest("[data-match-id]");
  if (row && !window.getSelection()?.toString()) toggleMatch(row.dataset.matchId);
});
elements.tournamentView.addEventListener("click", (event) => {
  if (event.target.closest("[data-retry-tournament]")) void loadTournament(true);
  const advancement = event.target.closest("[data-advance-to]");
  if (advancement) {
    const destination = document.getElementById(`bracket-${encodeURIComponent(advancement.dataset.advanceTo)}`);
    if (destination) {
      destination.focus({ preventScroll: true });
      destination.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
    }
  }
});
elements.syncButton.addEventListener("click", () => { void requestSync(); });
window.addEventListener("focus", () => { void refresh(true); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) void refresh(true); });
window.addEventListener("online", () => { void refresh(true); });
window.addEventListener("popstate", () => {
  const sport = sportFromPath();
  if (sport === state.activeSport) return;
  state.activeSport = sport;
  renderTabs();
  renderView();
  if (state.view !== "schedule") void loadTournament();
  else void refreshVisibleExtras(false, { manual: true });
});

async function init() {
  state.activeSport = sportFromPath();
  renderTabs();
  if (window.lucide) window.lucide.createIcons();
  await refresh();
  state.statusTimer = window.setInterval(() => { void refresh(); }, STATUS_POLL_INTERVAL_MS);
}
void init();
