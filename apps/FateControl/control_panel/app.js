"use strict";

const visualThemes = [
  { key: "amoled_neon", name: "AMOLED Neon", description: "True black • cyan, violet, and pink glow" },
  { key: "classic", name: "Classic", description: "Lime garden • warm golden light" },
  { key: "clockwork", name: "Clockwork", description: "Copper mechanisms • turning gears" },
  { key: "galaxy", name: "Galaxy", description: "Deep space • stars and comet trails" },
  { key: "music", name: "Music", description: "Orchestral color • floating notes" },
  { key: "nature", name: "Nature", description: "Living woodland • leaves and fireflies" },
  { key: "underwater", name: "Underwater", description: "Ocean light • rising bubbles" },
  { key: "waterfall", name: "Waterfall", description: "Cool cascades • drifting mist and ripples" },
];
const themeStorageKey = "fate-control.visual-theme.v1";
const themeEffectsStorageKey = "fate-control.theme-effects.v1";
const metricColumnStorageKey = "fate-control.metric-columns.v1";

function storedTheme() {
  const value = localStorage.getItem(themeStorageKey) || "classic";
  return visualThemes.some((theme) => theme.key === value) ? value : "classic";
}

function effectsEnabled() {
  return localStorage.getItem(themeEffectsStorageKey) !== "off";
}

function activeArtworkSection() {
  const active = document.querySelector(".panel-section.active")?.id?.replace("section-", "");
  if (["overview", "metrics", "config", "backups", "console", "settings"].includes(active)) return active;
  const hashSection = new URLSearchParams(location.hash.replace(/^#/, "")).get("section");
  return ["overview", "metrics", "config", "backups", "console", "settings"].includes(hashSection) ? hashSection : "overview";
}

function applySectionArtwork(section = activeArtworkSection()) {
  const theme = document.documentElement.dataset.theme || storedTheme();
  const role = section === "settings" ? "config" : (["overview", "metrics", "config", "backups", "console"].includes(section) ? section : "overview");
  document.documentElement.style.setProperty("--section-art", `url("/control/assets/assets/themes/menus/${theme}_${role}.webp")`);
  document.querySelectorAll(".panel-section").forEach((panel) => {
    let banner = panel.querySelector(":scope > .native-theme-banner");
    if (!banner) {
      banner = document.createElement("div");
      banner.className = "native-theme-banner";
      banner.setAttribute("aria-hidden", "true");
      panel.prepend(banner);
    }
  });
}

function applyVisualTheme(themeKey = storedTheme()) {
  const theme = visualThemes.find((item) => item.key === themeKey) || visualThemes[1];
  document.documentElement.dataset.theme = theme.key;
  document.documentElement.dataset.effects = effectsEnabled() ? "on" : "off";
  applySectionArtwork();
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", getComputedStyle(document.documentElement).getPropertyValue("--shell").trim());
  if (document.getElementById("theme-button")) document.getElementById("theme-button").textContent = theme.name;
  document.querySelectorAll(".theme-option").forEach((option) => option.setAttribute("aria-checked", String(option.dataset.theme === theme.key)));
}

function storedMetricColumns() {
  const value = localStorage.getItem(metricColumnStorageKey) || "auto";
  return ["auto", "2", "3", "4", "5"].includes(value) ? value : "auto";
}

function applyMetricColumns(value = storedMetricColumns()) {
  const selected = ["auto", "2", "3", "4", "5"].includes(value) ? value : "auto";
  document.documentElement.dataset.metricColumns = selected;
  if (selected === "auto") document.documentElement.style.removeProperty("--metric-column-count");
  else document.documentElement.style.setProperty("--metric-column-count", selected);
  if (document.getElementById("metrics-columns")) document.getElementById("metrics-columns").value = selected;
}

applyVisualTheme();
applyMetricColumns();

const state = {
  authenticated: false,
  manifest: null,
  status: null,
  config: null,
  configRevision: "",
  configDirty: false,
  unsafeIntegerPaths: [],
  backups: null,
  backupRevision: "",
  redactionMarker: "__FATE_SECRET__",
  metricsLoaded: false,
  metricsLoadedAt: 0,
  metricLoadGeneration: 0,
  metricsPromise: null,
  metricsReloadRequested: false,
  metricLayoutLoaded: false,
  metricOrder: [],
  metricHidden: new Set(),
  showHiddenMetrics: false,
  metricLayoutDraft: null,
  metricSettings: {},
  editingMetric: null,
  metricEditorFromCustomizer: false,
  metricEditorScroll: null,
  systemMetricPoints: [],
  systemMetricHistoryLoaded: false,
  activeSection: "overview",
  consoleTimer: null,
  statusTimer: null,
  statusPromise: null,
  consolePromise: null,
  configLoadedAt: 0,
  backupsLoadedAt: 0,
  backupDirty: false,
  processActionPending: false,
  statusServerActionPending: false,
  hostActionPending: false,
  folderStack: [{ id: "root", name: "Drive" }],
};

const byId = (id) => document.getElementById(id);
const panelSections = new Set(["overview", "metrics", "config", "backups", "console", "settings"]);
const svgNamespace = "http://www.w3.org/2000/svg";
const metricLayoutStorageKey = "fate-control.metric-layout.v1";
const metricSettingsStorageKey = "fate-control.metric-settings.v1";
const systemMetricHistoryStorageKey = "fate-control.system-metric-history.v2";
const metricCardResizeObserver = typeof ResizeObserver === "function"
  ? new ResizeObserver((entries) => entries.forEach((entry) => sizeMetricCard(entry.target)))
  : null;
const preferredMetricOrder = [
  "servers", "active_servers", "users", "logger_events", "dashboard_signins", "commands", "host_load", "messages_sent",
  "discord_rate_limits", "discord_global_rate_limits", "discord_invalid_requests", "discord_api_blocks", "mysql_calls", "mongo_calls",
  "selfrole_activity", "welcome_leave_messages", "autorole_activity",
  "antispam_triggers", "chatfilter_triggers", "uno_games", "uno_players",
  "connect_four_games", "tictactoe_games",
];
const moduleMetricKeys = new Set([
  "selfrole_activity", "welcome_leave_messages", "autorole_activity",
  "antispam_triggers", "chatfilter_triggers", "uno_games", "uno_players",
  "connect_four_games", "tictactoe_games",
]);

function renderThemeOptions(containerId = "theme-options") {
  const container = byId(containerId);
  if (!container) return;
  container.replaceChildren();
  visualThemes.forEach((theme) => {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "theme-option";
    option.dataset.theme = theme.key;
    option.setAttribute("role", "radio");
    const artwork = document.createElement("img");
    artwork.src = `/control/assets/assets/themes/${theme.key}.webp`;
    artwork.alt = "";
    const copy = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = theme.name;
    const description = document.createElement("small");
    description.textContent = theme.description;
    copy.append(name, description);
    option.append(artwork, copy);
    option.addEventListener("click", () => {
      localStorage.setItem(themeStorageKey, theme.key);
      applyVisualTheme(theme.key);
      if (containerId !== "theme-options") renderThemeOptions("theme-options");
      if (containerId !== "settings-theme-options") renderThemeOptions("settings-theme-options");
      if (window.FateAndroid && typeof window.FateAndroid.setTheme === "function") window.FateAndroid.setTheme(theme.key);
    });
    container.append(option);
  });
  applyVisualTheme();
}

function openThemeDialog() {
  renderThemeOptions("theme-options");
  byId("theme-effects").checked = effectsEnabled();
  byId("theme-dialog").showModal();
}

function setThemeEffects(enabled) {
  localStorage.setItem(themeEffectsStorageKey, enabled ? "on" : "off");
  byId("theme-effects").checked = enabled;
  byId("settings-theme-effects").checked = enabled;
  applyVisualTheme();
  if (window.FateAndroid && typeof window.FateAndroid.setThemeEffects === "function") window.FateAndroid.setThemeEffects(enabled);
}

function registeredMetrics() {
  const metrics = state.manifest?.metrics || [];
  return [...metrics, ...(!metrics.includes("host_load") ? ["host_load"] : [])];
}

function humanize(value) {
  return String(value || "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function metricLabel(metric) {
  if (metric === "active_servers") return "Active servers";
  if (metric === "dashboard_signins") return "Dashboard sign-ins";
  if (metric === "logger_events") return "Logger activity";
  if (metric === "discord_rate_limits") return "Discord rate limits";
  if (metric === "discord_global_rate_limits") return "Global Discord limits";
  if (metric === "discord_invalid_requests") return "Invalid Discord requests";
  if (metric === "discord_api_blocks") return "Discord API blocks";
  if (metric === "selfrole_activity") return "Self-role activity";
  if (metric === "welcome_leave_messages") return "Welcome / leave messages";
  if (metric === "autorole_activity") return "Auto-role activity";
  if (metric === "host_load") return "System activity";
  return humanize(metric);
}

function formatNumber(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(value);
}

function formatBytes(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatDuration(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "—";
  const totalMinutes = Math.floor(value / 60);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function showToast(message, isError = false) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.style.borderColor = isError ? "rgba(255,129,127,.6)" : "";
  toast.classList.add("visible");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("visible"), 3500);
}

function setConnection(label, kind) {
  const pill = byId("connection-pill");
  pill.textContent = label;
  pill.dataset.state = kind;
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store" });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : { error: await response.text() };
  if (!response.ok) {
    if (response.status === 401 && !path.includes("/control/session/")) showLogin();
    throw new Error(payload.error || `Request failed with HTTP ${response.status}`);
  }
  return payload;
}

function showLogin(message = "") {
  state.authenticated = false;
  clearInterval(state.consoleTimer);
  state.consoleTimer = null;
  setConnection("Key required", "error");
  byId("login-error").textContent = message;
  const dialog = byId("login-dialog");
  if (!dialog.open) dialog.showModal();
  setTimeout(() => byId("login-key").focus(), 50);
}

function dataIsStale(loadedAt, maximumAge = 30_000) {
  return !loadedAt || Date.now() - loadedAt > maximumAge;
}

async function exchangeLaunchTicket() {
  const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
  const ticket = hash.get("ticket");
  const requestedSection = hash.get("section");
  if (requestedSection && panelSections.has(requestedSection)) state.activeSection = requestedSection;
  history.replaceState(null, "", `${location.pathname}${location.search}`);
  if (!ticket) return;
  await request("/api/v1/control/session/exchange", {
    method: "POST",
    body: JSON.stringify({ ticket }),
  });
}

async function authenticate() {
  try {
    await exchangeLaunchTicket();
    const result = await request("/api/v1/auth/check");
    state.authenticated = true;
    byId("instance-title").textContent = result.instance_name || "Mission control";
    setConnection("Connected", "online");
    if (byId("login-dialog").open) byId("login-dialog").close();
    return true;
  } catch (error) {
    showLogin(error.message);
    return false;
  }
}

async function loginWithKey(event) {
  event.preventDefault();
  const key = byId("login-key").value.trim();
  byId("login-error").textContent = "";
  try {
    const response = await fetch("/api/v1/control/session/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ control_key: key }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "The control key was rejected.");
    byId("login-key").value = "";
    byId("login-dialog").close();
    state.authenticated = true;
    byId("instance-title").textContent = payload.instance_name || "Mission control";
    setConnection("Connected", "online");
    await loadInitialData();
  } catch (error) {
    byId("login-error").textContent = error.message;
  }
}

function notifyNativeSection(section) {
  try {
    if (window.FateAndroid && typeof window.FateAndroid.sectionChanged === "function") {
      window.FateAndroid.sectionChanged(section);
    }
  } catch {
    // Native section synchronization is optional; browser navigation remains local.
  }
}

function switchSection(section, updateHistory = true) {
  if (!panelSections.has(section)) section = "overview";
  state.activeSection = section;
  document.documentElement.dataset.activeSection = section;
  document.querySelectorAll(".panel-section").forEach((element) => {
    element.classList.toggle("active", element.id === `section-${section}`);
  });
  document.querySelectorAll(".section-nav button").forEach((button) => {
    if (button.dataset.section === section) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  applySectionArtwork(section);
  if (updateHistory) history.replaceState(null, "", `${location.pathname}${location.search}#section=${section}`);
  notifyNativeSection(section);
  if (!state.authenticated) return;
  if (section === "metrics" && dataIsStale(state.metricsLoadedAt)) loadAllMetrics();
  if (section === "config" && (!state.config || (!state.configDirty && dataIsStale(state.configLoadedAt)))) loadConfig();
  if (section === "backups" && (!state.backups || (!state.backupDirty && dataIsStale(state.backupsLoadedAt)))) loadBackups();
  manageConsoleTimer();
}

window.FatePanel = {
  openSection(section) { switchSection(section); },
  refresh() { refreshActive(); },
};

function statusCard(label, value, detail) {
  const card = document.createElement("article");
  card.className = "card status-card";
  const caption = document.createElement("p");
  caption.className = "eyebrow";
  caption.textContent = label;
  const strong = document.createElement("strong");
  strong.textContent = value;
  const small = document.createElement("small");
  small.textContent = detail;
  card.append(caption, strong, small);
  return card;
}

function renderStatus(payload) {
  state.status = payload;
  recordHostLoadSample(payload);
  byId("instance-title").textContent = payload.instance_name || "Mission control";
  const discord = payload.discord || {};
  const system = payload.system || {};
  const cpu = system.cpu || {};
  const memory = system.memory || payload.resources?.memory || {};
  const storage = system.storage || payload.resources?.storage || {};
  const discordOnline = discord.online === true
    || (discord.online == null && payload.online === true);
  const controllerOnline = payload.controller_online === true
    || (payload.controller_online == null && payload.service === "fate-control");
  const latency = payload.latency_ms ?? discord.latency_ms;
  const uptimeSeconds = payload.uptime_seconds ?? discord.uptime_seconds;
  const grid = byId("status-grid");
  grid.replaceChildren(
    statusCard("DISCORD", discordOnline ? "Connected" : "Offline", `${formatNumber(payload.servers)} servers · ${formatNumber(payload.users)} users`),
    statusCard("LATENCY", latency == null ? "—" : `${formatNumber(latency)} ms`, payload.health?.summary || "Awaiting health"),
    statusCard("CPU", cpu.used_percent == null ? "—" : `${formatNumber(cpu.used_percent)}%`, system.hostname || "Host"),
    statusCard("MEMORY", memory.used_percent == null ? "—" : `${formatNumber(memory.used_percent)}%`, memory.used_bytes ? `${formatBytes(memory.used_bytes)} used` : "Live host memory"),
    statusCard("STORAGE", storage.used_percent == null ? "—" : `${formatNumber(storage.used_percent)}%`, storage.free_bytes ? `${formatBytes(storage.free_bytes)} free` : "Live host storage"),
    statusCard("COMMANDS", formatNumber(payload.commands_this_month), "This month"),
    statusCard("UPTIME", formatDuration(uptimeSeconds), "Fate runtime"),
    statusCard("CONTROLLER", controllerOnline ? "Online" : "Offline", payload.updated_at ? new Date(payload.updated_at).toLocaleTimeString() : "Just now"),
  );

  const process = payload.bot || {};
  const running = Boolean(process.process_running);
  const owned = Boolean(process.owned);
  byId("process-copy").textContent = running
    ? owned ? `Running under FateControl${process.pid ? ` · PID ${process.pid}` : ""}.` : "Running externally. Restart transfers supervision to FateControl."
    : "Stopped and ready to start.";
  updateActionButtons();
  byId("overview-updated").textContent = payload.updated_at ? `Updated ${new Date(payload.updated_at).toLocaleTimeString()}` : "Updated now";
  setConnection("Connected", "online");
  const hostLoadCard = byId("metric-grid")?.querySelector('[data-metric="host_load"]');
  if (state.metricsLoaded && hostLoadCard) renderHostLoad(hostLoadCard);
}

async function loadStatus(silent = false) {
  if (state.statusPromise) return state.statusPromise;
  const pending = (async () => {
    try {
      renderStatus(await request("/api/v1/status"));
    } catch (error) {
      setConnection("Unreachable", "error");
      if (!silent) showToast(error.message, true);
    }
  })();
  state.statusPromise = pending;
  try {
    return await pending;
  } finally {
    if (state.statusPromise === pending) state.statusPromise = null;
  }
}

function updateActionButtons() {
  const process = state.status?.bot || {};
  const running = Boolean(process.process_running);
  const owned = Boolean(process.owned);
  const processBusy = state.processActionPending;
  byId("start-button").disabled = processBusy || running;
  byId("stop-button").disabled = processBusy || !running || !owned;
  byId("restart-button").disabled = processBusy || !running;
  byId("restart-status-server-button").disabled = state.statusServerActionPending || !running;
  byId("reboot-button").disabled = state.hostActionPending || !state.status?.system?.capabilities?.reboot;
}

async function processAction(action) {
  if (state.processActionPending) return;
  state.processActionPending = true;
  updateActionButtons();
  try {
    let path = "/api/v1/bot/action";
    let body = { action };
    if (action === "start" || action === "stop") {
      path = "/api/v1/bot/state";
      body = { running: action === "start" };
    }
    const result = await request(path, { method: "POST", body: JSON.stringify(body) });
    if (result.status) renderStatus(result.status);
    showToast(result.message || `Fate ${action} request accepted.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.processActionPending = false;
    updateActionButtons();
  }
}

async function restartStatusServer() {
  if (state.statusServerActionPending) return;
  state.statusServerActionPending = true;
  updateActionButtons();
  try {
    const payload = await request("/api/v1/status-server/action", {
      method: "POST",
      body: JSON.stringify({ action: "restart" }),
    });
    showToast(payload.message || "Fate Status server restarted.");
    await loadStatus(true);
  } catch (error) {
    showToast(error.message, true);
    await loadStatus(true);
  } finally {
    state.statusServerActionPending = false;
    updateActionButtons();
  }
}

async function rebootHost() {
  if (state.hostActionPending) return;
  const instance = state.status?.instance_name || byId("instance-title").textContent;
  const required = `REBOOT ${instance}`;
  const supplied = window.prompt(`This reboots the entire computer. Type ${required} to continue.`);
  if (supplied !== required) return;
  state.hostActionPending = true;
  updateActionButtons();
  try {
    const result = await request("/api/v1/host/action", {
      method: "POST",
      body: JSON.stringify({ action: "reboot", confirm: required }),
    });
    showToast(result.message || "Host reboot scheduled.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.hostActionPending = false;
    updateActionButtons();
  }
}

function createMetricCard(metric) {
  const card = document.createElement("article");
  card.className = "card metric-card";
  card.dataset.metric = metric;
  const header = document.createElement("div");
  header.className = "metric-header";
  const heading = document.createElement("div");
  const eyebrow = document.createElement("p");
  eyebrow.className = "eyebrow";
  eyebrow.textContent = metric === "host_load" ? "SYSTEM" : moduleMetricKeys.has(metric) ? "MODULE" : "GENERAL";
  const title = document.createElement("h3");
  title.textContent = metricLabel(metric);
  const hiddenBadge = document.createElement("span");
  hiddenBadge.className = "metric-hidden-badge";
  hiddenBadge.textContent = "Hidden";
  hiddenBadge.hidden = true;
  heading.append(eyebrow, title, hiddenBadge);
  const value = document.createElement("span");
  value.className = "metric-value";
  value.textContent = "—";
  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "button metric-edit-button";
  edit.textContent = "Edit";
  edit.addEventListener("click", () => openMetricSettings(metric));
  const dragHandle = document.createElement("button");
  dragHandle.type = "button";
  dragHandle.className = "metric-drag-handle";
  dragHandle.textContent = "⠿";
  dragHandle.title = `Drag ${metricLabel(metric)} to rearrange`;
  dragHandle.setAttribute("aria-label", dragHandle.title);
  const actions = document.createElement("div");
  actions.className = "metric-header-actions";
  actions.append(value, edit, dragHandle);
  header.append(heading, actions);
  const chart = document.createElement("div");
  chart.className = "metric-empty";
  chart.textContent = "Loading samples…";
  const status = document.createElement("p");
  status.className = "metric-card-status muted";
  status.textContent = metric === "host_load" ? "Building live system history on this device" : "Tap to customize this graph";
  card.append(header, chart, status);
  card.addEventListener("click", (event) => {
    const chartTarget = event.target.closest(".metric-chart, .metric-empty");
    if (!chartTarget) return;
    if (chartTarget.dataset.suppressClick === "true") {
      delete chartTarget.dataset.suppressClick;
      event.preventDefault();
      return;
    }
    openMetricSettings(metric);
  });
  enableMetricCardReordering(card);
  return card;
}

function visibleMetricOrder(grid = byId("metric-grid")) {
  return Array.from(grid?.querySelectorAll(":scope > .metric-card") || [], (card) => card.dataset.metric);
}

function persistVisibleMetricOrder(grid = byId("metric-grid")) {
  const visible = visibleMetricOrder(grid);
  const visibleSet = new Set(visible);
  let visibleIndex = 0;
  state.metricOrder = state.metricOrder.map((metric) => visibleSet.has(metric) ? visible[visibleIndex++] : metric);
  saveMetricLayout();
  updateMetricLayoutSummary(registeredMetrics(), visible);
}

function updateMetricLayoutSummary(metrics, displayedMetrics) {
  const hiddenCount = state.metricHidden.size;
  const revealed = state.showHiddenMetrics && hiddenCount
    ? ` · ${hiddenCount} hidden ${hiddenCount === 1 ? "graph" : "graphs"} revealed`
    : hiddenCount ? ` · ${hiddenCount} hidden` : "";
  byId("metrics-layout-summary").textContent = `${displayedMetrics.length} of ${metrics.length} graphs displayed${revealed} · drag cards to rearrange · layout saved on this device`;
}

function moveVisibleMetric(card, offset) {
  const grid = card.closest(".metric-grid");
  if (!grid) return;
  const cards = Array.from(grid.querySelectorAll(":scope > .metric-card"));
  const from = cards.indexOf(card);
  const to = from + offset;
  if (from < 0 || to < 0 || to >= cards.length) return;
  if (offset < 0) grid.insertBefore(card, cards[to]);
  else grid.insertBefore(cards[to], card);
  persistVisibleMetricOrder(grid);
  card.querySelector(".metric-drag-handle")?.focus();
  window.requestAnimationFrame(() => cards.forEach(sizeMetricCard));
}

function enableMetricCardReordering(card) {
  const header = card.querySelector(".metric-header");
  const handle = card.querySelector(".metric-drag-handle");
  if (!header || !handle) return;
  let pointerId = null;
  let startX = 0;
  let startY = 0;
  let dragging = false;
  let originalOrder = [];
  let placeholder = null;
  let dragFrame = null;
  let pendingPoint = null;

  const clearDraggingStyles = () => {
    card.classList.remove("metric-card-dragging");
    ["left", "top", "width", "height", "margin", "--metric-drag-x", "--metric-drag-y"].forEach((property) => {
      card.style.removeProperty(property);
    });
  };

  const updatePlaceholder = () => {
    dragFrame = null;
    if (!dragging || !pendingPoint) return;
    const { x, y } = pendingPoint;
    card.style.setProperty("--metric-drag-x", `${x - startX}px`);
    card.style.setProperty("--metric-drag-y", `${y - startY}px`);
    const grid = card.closest(".metric-grid");
    const target = Array.from(grid?.querySelectorAll(":scope > .metric-card") || [])
      .filter((candidate) => candidate !== card)
      .find((candidate) => {
        const bounds = candidate.getBoundingClientRect();
        return x >= bounds.left && x <= bounds.right && y >= bounds.top && y <= bounds.bottom;
      });
    if (!target || !placeholder || target.parentElement !== grid) return;
    const bounds = target.getBoundingClientRect();
    const centerX = bounds.left + bounds.width / 2;
    const centerY = bounds.top + bounds.height / 2;
    const before = Math.abs(y - centerY) < bounds.height * .25 ? x < centerX : y < centerY;
    const reference = before ? target : target.nextSibling;
    if (reference === placeholder || (!reference && placeholder === grid.lastElementChild)) return;
    grid.insertBefore(placeholder, reference);
  };

  const finish = (event, cancelled = false) => {
    if (pointerId !== event.pointerId) return;
    const grid = card.closest(".metric-grid");
    if (card.hasPointerCapture?.(event.pointerId)) card.releasePointerCapture(event.pointerId);
    if (dragging && grid) {
      if (dragFrame !== null) window.cancelAnimationFrame(dragFrame);
      dragFrame = null;
      pendingPoint = null;
      if (!cancelled && placeholder) grid.insertBefore(card, placeholder);
      placeholder?.remove();
      placeholder = null;
      clearDraggingStyles();
      if (cancelled) {
        originalOrder.forEach((metric) => {
          const originalCard = grid.querySelector(`[data-metric="${CSS.escape(metric)}"]`);
          if (originalCard) grid.append(originalCard);
        });
      } else {
        persistVisibleMetricOrder(grid);
        showToast(`${metricLabel(card.dataset.metric)} moved.`);
      }
      grid.classList.remove("metric-grid-reordering");
      window.requestAnimationFrame(() => Array.from(grid.children).forEach(sizeMetricCard));
    }
    pointerId = null;
    dragging = false;
  };

  card.addEventListener("pointerdown", (event) => {
    if (pointerId !== null || (event.pointerType === "mouse" && event.button !== 0)) return;
    const handleTarget = event.target.closest(".metric-drag-handle");
    if (event.target.closest("button,input,select,textarea,a,.metric-chart,.metric-empty") && !handleTarget) return;
    if (event.pointerType === "touch" && !handleTarget) return;
    event.preventDefault();
    pointerId = event.pointerId;
    startX = event.clientX;
    startY = event.clientY;
    originalOrder = visibleMetricOrder(card.closest(".metric-grid"));
    card.setPointerCapture?.(event.pointerId);
  });
  card.addEventListener("pointermove", (event) => {
    if (pointerId !== event.pointerId) return;
    const distance = Math.hypot(event.clientX - startX, event.clientY - startY);
    if (!dragging && distance >= 7) {
      dragging = true;
      const bounds = card.getBoundingClientRect();
      placeholder = document.createElement("div");
      placeholder.className = "metric-card-placeholder";
      placeholder.setAttribute("aria-hidden", "true");
      placeholder.style.height = `${bounds.height}px`;
      placeholder.style.gridRowEnd = window.getComputedStyle(card).gridRowEnd;
      card.parentElement?.insertBefore(placeholder, card);
      card.style.left = `${bounds.left}px`;
      card.style.top = `${bounds.top}px`;
      card.style.width = `${bounds.width}px`;
      card.style.height = `${bounds.height}px`;
      card.style.margin = "0";
      card.classList.add("metric-card-dragging");
      card.closest(".metric-grid")?.classList.add("metric-grid-reordering");
    }
    if (!dragging) return;
    event.preventDefault();
    pendingPoint = { x: event.clientX, y: event.clientY };
    if (dragFrame === null) dragFrame = window.requestAnimationFrame(updatePlaceholder);
  });
  card.addEventListener("pointerup", (event) => finish(event));
  card.addEventListener("pointercancel", (event) => finish(event, true));
  handle.addEventListener("click", (event) => event.preventDefault());
  handle.addEventListener("keydown", (event) => {
    if (["ArrowUp", "ArrowLeft"].includes(event.key)) {
      event.preventDefault();
      moveVisibleMetric(card, -1);
    } else if (["ArrowDown", "ArrowRight"].includes(event.key)) {
      event.preventDefault();
      moveVisibleMetric(card, 1);
    }
  });
}

function sizeMetricCard(card) {
  const grid = card.closest(".metric-grid");
  if (!grid || window.matchMedia("(max-width: 760px)").matches) {
    card.style.removeProperty("grid-row-end");
    return;
  }
  const gridStyle = window.getComputedStyle(grid);
  const rowHeight = Number.parseFloat(gridStyle.gridAutoRows) || 1;
  const rowGap = Number.parseFloat(gridStyle.rowGap) || 0;
  const height = card.getBoundingClientRect().height;
  card.style.gridRowEnd = `span ${Math.max(1, Math.ceil((height + rowGap) / (rowHeight + rowGap)))}`;
}

function observeMetricCard(card) {
  metricCardResizeObserver?.observe(card);
  window.requestAnimationFrame(() => sizeMetricCard(card));
}

function formatMetricTimestamp(point) {
  const timestamp = Number(point?.timestamp);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return "";
  return new Date(timestamp * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

function enableMetricScrubbing(svg, points, coordinates, width, height, pad) {
  const scrub = document.createElementNS(svgNamespace, "g");
  scrub.setAttribute("class", "metric-scrub");
  scrub.style.display = "none";
  const guide = document.createElementNS(svgNamespace, "line");
  guide.setAttribute("class", "metric-scrub-guide");
  guide.setAttribute("y1", pad);
  guide.setAttribute("y2", height - pad);
  const marker = document.createElementNS(svgNamespace, "circle");
  marker.setAttribute("class", "metric-scrub-marker");
  marker.setAttribute("r", 4.5);
  const bubble = document.createElementNS(svgNamespace, "rect");
  bubble.setAttribute("class", "metric-scrub-bubble");
  bubble.setAttribute("width", 210);
  bubble.setAttribute("height", 30);
  bubble.setAttribute("rx", 8);
  bubble.setAttribute("y", pad + 5);
  const label = document.createElementNS(svgNamespace, "text");
  label.setAttribute("class", "metric-scrub-label");
  label.setAttribute("y", pad + 24);
  scrub.append(guide, marker, bubble, label);
  svg.append(scrub);

  let activePointer = null;
  let activeTouch = null;
  let startX = 0;
  let startY = 0;
  let dragged = false;

  const update = (clientX) => {
    const bounds = svg.getBoundingClientRect();
    if (!bounds.width) return;
    const viewX = ((clientX - bounds.left) / bounds.width) * width;
    const fraction = Math.max(0, Math.min(1, (viewX - pad) / (width - pad * 2)));
    const index = Math.round(fraction * Math.max(0, points.length - 1));
    const point = points[index];
    const [x, y] = coordinates[index];
    const bubbleX = Math.max(pad, Math.min(x - 105, width - pad - 210));
    const timestamp = formatMetricTimestamp(point);
    guide.setAttribute("x1", x);
    guide.setAttribute("x2", x);
    marker.setAttribute("cx", x);
    marker.setAttribute("cy", y);
    bubble.setAttribute("x", bubbleX);
    label.setAttribute("x", bubbleX + 105);
    const displayValue = formatNumber(point.value);
    label.textContent = timestamp ? `${displayValue}  ·  ${timestamp}` : displayValue;
    svg.setAttribute("aria-label", `${metricLabel(svg.dataset.metric)}: ${label.textContent}`);
    scrub.style.display = "inline";
  };

  const suppressChartClick = () => {
    svg.dataset.suppressClick = "true";
    window.setTimeout(() => delete svg.dataset.suppressClick, 500);
  };

  const finishPointer = (event, cancelled = false) => {
    if (activePointer !== event.pointerId) return;
    if (!cancelled && dragged) {
      update(event.clientX);
      suppressChartClick();
    }
    if (svg.hasPointerCapture?.(event.pointerId)) svg.releasePointerCapture(event.pointerId);
    activePointer = null;
    scrub.style.display = "none";
  };

  const matchingTouch = (touches) => Array.from(touches || []).find((touch) => touch.identifier === activeTouch);

  svg.addEventListener("pointerdown", (event) => {
    // Android WebView has a more reliable Touch Events path below. Avoid
    // handling the same finger gesture twice when it also emits Pointer Events.
    if (event.pointerType === "touch") return;
    if (event.pointerType === "mouse" && event.button !== 0) return;
    activePointer = event.pointerId;
    startX = event.clientX;
    dragged = false;
    svg.setPointerCapture?.(event.pointerId);
  });
  svg.addEventListener("pointermove", (event) => {
    if (event.pointerType === "touch") return;
    if (activePointer !== event.pointerId) return;
    if (!dragged && Math.abs(event.clientX - startX) >= 5) dragged = true;
    if (!dragged) return;
    event.preventDefault();
    update(event.clientX);
  });
  svg.addEventListener("pointerup", (event) => finishPointer(event));
  svg.addEventListener("pointercancel", (event) => finishPointer(event, true));

  svg.addEventListener("touchstart", (event) => {
    if (activeTouch !== null || event.changedTouches.length !== 1) return;
    const touch = event.changedTouches[0];
    activeTouch = touch.identifier;
    startX = touch.clientX;
    startY = touch.clientY;
    dragged = false;
  }, { passive: true });
  svg.addEventListener("touchmove", (event) => {
    const touch = matchingTouch(event.touches);
    if (!touch) return;
    const deltaX = Math.abs(touch.clientX - startX);
    const deltaY = Math.abs(touch.clientY - startY);
    if (!dragged && deltaX >= 5 && deltaX >= deltaY) dragged = true;
    if (!dragged) return;
    event.preventDefault();
    update(touch.clientX);
  }, { passive: false });
  const finishTouch = (event, cancelled = false) => {
    const touch = matchingTouch(event.changedTouches);
    if (!touch && !cancelled) return;
    if (!cancelled && dragged && touch) {
      update(touch.clientX);
      suppressChartClick();
    }
    activeTouch = null;
    scrub.style.display = "none";
  };
  svg.addEventListener("touchend", (event) => finishTouch(event));
  svg.addEventListener("touchcancel", (event) => finishTouch(event, true));
}

function renderMetricChart(card, payload, setting, includeCommands = true) {
  const points = Array.isArray(payload.points) ? payload.points.filter((point) => Number.isFinite(point.value)) : [];
  const metricValue = payload.summary?.current ?? payload.summary?.total;
  card.querySelector(".metric-value").textContent = formatNumber(metricValue);
  const oldChart = card.querySelector(".metric-chart, .metric-empty");
  const status = card.querySelector(".metric-card-status");
  if (status && setting) {
    status.textContent = `${setting.window} · ${formatMetricBucket(setting.bucket)} buckets · Drag graph for details`;
  }
  if (!points.length) {
    const empty = document.createElement("div");
    empty.className = "metric-empty";
    empty.textContent = payload.available === false ? "Metric store unavailable" : "No samples in this window";
    oldChart.replaceWith(empty);
    if (includeCommands) renderTopCommands(card, payload, setting);
    renderTopRateLimitRoutes(card, payload);
    return;
  }
  const width = 520;
  const height = 150;
  const pad = 10;
  const values = points.map((point) => point.value);
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (Math.abs(max - min) < 0.000001) {
    const padding = Math.max(1, Math.abs(max) * 0.08);
    min -= padding;
    max += padding;
  } else {
    const padding = (max - min) * 0.1;
    min -= padding;
    max += padding;
  }
  const span = max - min || 1;
  const coordinates = points.map((point, index) => {
    const x = pad + (index / Math.max(1, points.length - 1)) * (width - pad * 2);
    const y = height - pad - ((point.value - min) / span) * (height - pad * 2);
    return [x, y];
  });
  const svg = document.createElementNS(svgNamespace, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "metric-chart");
  svg.setAttribute("role", "img");
  svg.dataset.metric = payload.metric;
  svg.setAttribute("aria-label", `${humanize(payload.metric)} chart with ${points.length} points`);
  [0.25, 0.5, 0.75].forEach((fraction) => {
    const line = document.createElementNS(svgNamespace, "line");
    line.setAttribute("x1", pad);
    line.setAttribute("x2", width - pad);
    line.setAttribute("y1", height * fraction);
    line.setAttribute("y2", height * fraction);
    line.setAttribute("class", "grid");
    svg.append(line);
  });
  const path = document.createElementNS(svgNamespace, "path");
  path.setAttribute("d", coordinates.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(2)} ${y.toFixed(2)}`).join(" "));
  path.setAttribute("class", "line");
  svg.append(path);
  enableMetricScrubbing(svg, points, coordinates, width, height, pad);
  oldChart.replaceWith(svg);
  if (includeCommands) renderTopCommands(card, payload, setting);
  renderTopRateLimitRoutes(card, payload);
}

function nullableNumber(value, minimum = 0) {
  const number = Number(value);
  return Number.isFinite(number) && number >= minimum ? number : null;
}

function totalRate(...values) {
  const rates = values.map((value) => nullableNumber(value)).filter((value) => value !== null);
  return rates.length ? rates.reduce((total, value) => total + value, 0) : null;
}

function loadSystemMetricHistory() {
  if (state.systemMetricHistoryLoaded) return;
  state.systemMetricHistoryLoaded = true;
  try {
    const saved = JSON.parse(localStorage.getItem(systemMetricHistoryStorageKey) || "[]");
    if (!Array.isArray(saved)) return;
    const cutoff = Math.floor(Date.now() / 1000) - 30 * 24 * 60 * 60;
    state.systemMetricPoints = saved
      .filter((point) => Number.isFinite(point?.timestamp) && point.timestamp >= cutoff)
      .map((point) => ({
        timestamp: Number(point.timestamp),
        ram: nullableNumber(point.ram) === null ? null : Math.min(100, Number(point.ram)),
        network: nullableNumber(point.network),
        disk: nullableNumber(point.disk),
      }))
      .filter((point) => [point.ram, point.network, point.disk].some((value) => value !== null))
      .sort((left, right) => left.timestamp - right.timestamp)
      .slice(-15000);
  } catch (_error) {
    state.systemMetricPoints = [];
  }
}

function recordHostLoadSample(status) {
  loadSystemMetricHistory();
  const system = status?.system || {};
  const memory = system.memory || status?.resources?.memory || {};
  const network = system.network || {};
  const disk = system.disk_io || {};
  const ramValue = nullableNumber(memory.used_percent);
  const point = {
    timestamp: 0,
    ram: ramValue === null ? null : Math.min(100, ramValue),
    network: totalRate(network.receive_bytes_per_second, network.send_bytes_per_second),
    disk: totalRate(disk.read_bytes_per_second, disk.write_bytes_per_second),
  };
  if ([point.ram, point.network, point.disk].every((value) => value === null)) return;
  const suppliedTime = Date.parse(status?.updated_at || "") / 1000;
  point.timestamp = Math.floor((Number.isFinite(suppliedTime) ? suppliedTime : Date.now() / 1000) / 15) * 15;
  const last = state.systemMetricPoints[state.systemMetricPoints.length - 1];
  if (last?.timestamp === point.timestamp) state.systemMetricPoints[state.systemMetricPoints.length - 1] = point;
  else state.systemMetricPoints.push(point);
  const cutoff = point.timestamp - 30 * 24 * 60 * 60;
  state.systemMetricPoints = state.systemMetricPoints.filter((item) => item.timestamp >= cutoff).slice(-15000);
  try { localStorage.setItem(systemMetricHistoryStorageKey, JSON.stringify(state.systemMetricPoints)); }
  catch (_error) { /* The graph still works for this session. */ }
}

function hostLoadChartPoints(setting) {
  loadSystemMetricHistory();
  const duration = metricWindowSpec(setting.window).duration;
  const cutoff = Math.floor(Date.now() / 1000) - duration;
  const buckets = new Map();
  state.systemMetricPoints.forEach((point) => {
    if (point.timestamp < cutoff) return;
    const timestamp = point.timestamp - (point.timestamp % setting.bucket);
    buckets.set(timestamp, { ...point, timestamp });
  });
  return Array.from(buckets.values()).sort((left, right) => left.timestamp - right.timestamp);
}

function systemRateLabel(value) {
  return value === null || !Number.isFinite(value) ? "—" : `${formatBytes(value)}/s`;
}

function renderSystemMetricLegend(card, point) {
  card.querySelector(".system-metric-legend")?.remove();
  const legend = document.createElement("div");
  legend.className = "system-metric-legend";
  [
    ["ram", "RAM", point?.ram === null || !Number.isFinite(point?.ram) ? "—" : `${formatNumber(point.ram)}%`],
    ["network", "Network", systemRateLabel(point?.network)],
    ["disk", "Disk", systemRateLabel(point?.disk)],
  ].forEach(([series, label, value]) => {
    const item = document.createElement("span");
    item.className = `system-legend-item system-${series}`;
    const swatch = document.createElement("i");
    swatch.setAttribute("aria-hidden", "true");
    const name = document.createElement("strong");
    name.textContent = label;
    const reading = document.createElement("small");
    reading.textContent = value;
    item.append(swatch, name, reading);
    legend.append(item);
  });
  const chart = card.querySelector(".metric-chart, .metric-empty");
  card.insertBefore(legend, chart);
}

function enableSystemMetricScrubbing(svg, points, coordinates, width, height, pad) {
  const scrub = document.createElementNS(svgNamespace, "g");
  scrub.setAttribute("class", "metric-scrub system-metric-scrub");
  scrub.style.display = "none";
  const guide = document.createElementNS(svgNamespace, "line");
  guide.setAttribute("class", "metric-scrub-guide");
  guide.setAttribute("y1", pad);
  guide.setAttribute("y2", height - pad);
  const markers = {};
  ["ram", "network", "disk"].forEach((series) => {
    const marker = document.createElementNS(svgNamespace, "circle");
    marker.setAttribute("class", `metric-scrub-marker system-${series}-marker`);
    marker.setAttribute("r", 4.5);
    markers[series] = marker;
  });
  const bubble = document.createElementNS(svgNamespace, "rect");
  bubble.setAttribute("class", "metric-scrub-bubble");
  bubble.setAttribute("width", 390);
  bubble.setAttribute("height", 43);
  bubble.setAttribute("rx", 8);
  bubble.setAttribute("y", pad + 5);
  const timeLabel = document.createElementNS(svgNamespace, "text");
  timeLabel.setAttribute("class", "metric-scrub-label system-scrub-time");
  timeLabel.setAttribute("y", pad + 20);
  const valuesLabel = document.createElementNS(svgNamespace, "text");
  valuesLabel.setAttribute("class", "metric-scrub-label system-scrub-values");
  valuesLabel.setAttribute("y", pad + 38);
  scrub.append(guide, markers.ram, markers.network, markers.disk, bubble, timeLabel, valuesLabel);
  svg.append(scrub);

  let activePointer = null;
  let activeTouch = null;
  let startX = 0;
  let startY = 0;
  let dragged = false;
  const update = (clientX) => {
    const bounds = svg.getBoundingClientRect();
    if (!bounds.width) return;
    const viewX = ((clientX - bounds.left) / bounds.width) * width;
    const fraction = Math.max(0, Math.min(1, (viewX - pad) / (width - pad * 2)));
    const index = Math.round(fraction * Math.max(0, points.length - 1));
    const point = points[index];
    const x = pad + (index / Math.max(1, points.length - 1)) * (width - pad * 2);
    const bubbleX = Math.max(pad, Math.min(x - 195, width - pad - 390));
    guide.setAttribute("x1", x);
    guide.setAttribute("x2", x);
    ["ram", "network", "disk"].forEach((series) => {
      const coordinate = coordinates[series][index];
      markers[series].style.display = coordinate ? "inline" : "none";
      if (coordinate) {
        markers[series].setAttribute("cx", coordinate[0]);
        markers[series].setAttribute("cy", coordinate[1]);
      }
    });
    bubble.setAttribute("x", bubbleX);
    timeLabel.setAttribute("x", bubbleX + 195);
    valuesLabel.setAttribute("x", bubbleX + 195);
    timeLabel.textContent = formatMetricTimestamp(point);
    const ram = point.ram === null ? "—" : `${formatNumber(point.ram)}%`;
    valuesLabel.textContent = `RAM ${ram}  ·  NET ${systemRateLabel(point.network)}  ·  DISK ${systemRateLabel(point.disk)}`;
    svg.setAttribute("aria-label", `System activity: ${valuesLabel.textContent} at ${timeLabel.textContent}`);
    scrub.style.display = "inline";
  };
  const suppressChartClick = () => {
    svg.dataset.suppressClick = "true";
    window.setTimeout(() => delete svg.dataset.suppressClick, 500);
  };
  const finishPointer = (event, cancelled = false) => {
    if (activePointer !== event.pointerId) return;
    if (!cancelled && dragged) {
      update(event.clientX);
      suppressChartClick();
    }
    if (svg.hasPointerCapture?.(event.pointerId)) svg.releasePointerCapture(event.pointerId);
    activePointer = null;
    scrub.style.display = "none";
  };
  const matchingTouch = (touches) => Array.from(touches || []).find((touch) => touch.identifier === activeTouch);
  svg.addEventListener("pointerdown", (event) => {
    if (event.pointerType === "touch" || (event.pointerType === "mouse" && event.button !== 0)) return;
    activePointer = event.pointerId;
    startX = event.clientX;
    dragged = false;
    svg.setPointerCapture?.(event.pointerId);
  });
  svg.addEventListener("pointermove", (event) => {
    if (event.pointerType === "touch" || activePointer !== event.pointerId) return;
    if (!dragged && Math.abs(event.clientX - startX) >= 5) dragged = true;
    if (!dragged) return;
    event.preventDefault();
    update(event.clientX);
  });
  svg.addEventListener("pointerup", (event) => finishPointer(event));
  svg.addEventListener("pointercancel", (event) => finishPointer(event, true));
  svg.addEventListener("touchstart", (event) => {
    if (activeTouch !== null || event.changedTouches.length !== 1) return;
    const touch = event.changedTouches[0];
    activeTouch = touch.identifier;
    startX = touch.clientX;
    startY = touch.clientY;
    dragged = false;
  }, { passive: true });
  svg.addEventListener("touchmove", (event) => {
    const touch = matchingTouch(event.touches);
    if (!touch) return;
    const deltaX = Math.abs(touch.clientX - startX);
    const deltaY = Math.abs(touch.clientY - startY);
    if (!dragged && deltaX >= 5 && deltaX >= deltaY) dragged = true;
    if (!dragged) return;
    event.preventDefault();
    update(touch.clientX);
  }, { passive: false });
  const finishTouch = (event, cancelled = false) => {
    const touch = matchingTouch(event.changedTouches);
    if (!touch && !cancelled) return;
    if (!cancelled && dragged && touch) {
      update(touch.clientX);
      suppressChartClick();
    }
    activeTouch = null;
    scrub.style.display = "none";
  };
  svg.addEventListener("touchend", (event) => finishTouch(event));
  svg.addEventListener("touchcancel", (event) => finishTouch(event, true));
}

function renderSystemMetricChart(card, points, setting) {
  const latest = points.at(-1) || state.systemMetricPoints.at(-1);
  card.querySelector(".metric-value").textContent = Number.isFinite(latest?.ram) ? `${formatNumber(latest.ram)}% RAM` : "3 signals";
  renderSystemMetricLegend(card, latest);
  const oldChart = card.querySelector(".metric-chart, .metric-empty");
  const status = card.querySelector(".metric-card-status");
  status.textContent = `${setting.window} · ${formatMetricBucket(setting.bucket)} buckets · Each line uses its own activity range · Drag for details`;
  if (!points.length) {
    const empty = document.createElement("div");
    empty.className = "metric-empty";
    empty.textContent = "Waiting for live RAM, network, and disk samples";
    oldChart.replaceWith(empty);
    return;
  }
  const width = 520;
  const height = 150;
  const pad = 10;
  const svg = document.createElementNS(svgNamespace, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "metric-chart system-metric-chart");
  svg.setAttribute("role", "img");
  svg.dataset.metric = "host_load";
  svg.setAttribute("aria-label", `Combined RAM, network, and disk activity chart with ${points.length} points`);
  [0.25, 0.5, 0.75].forEach((fraction) => {
    const line = document.createElementNS(svgNamespace, "line");
    line.setAttribute("x1", pad);
    line.setAttribute("x2", width - pad);
    line.setAttribute("y1", height * fraction);
    line.setAttribute("y2", height * fraction);
    line.setAttribute("class", "grid");
    svg.append(line);
  });
  const maxima = {
    ram: 100,
    network: Math.max(1, ...points.map((point) => point.network ?? 0)),
    disk: Math.max(1, ...points.map((point) => point.disk ?? 0)),
  };
  const coordinates = {};
  ["ram", "network", "disk"].forEach((series) => {
    coordinates[series] = points.map((point, index) => {
      if (!Number.isFinite(point[series])) return null;
      const x = pad + (index / Math.max(1, points.length - 1)) * (width - pad * 2);
      const normalized = Math.max(0, Math.min(1, point[series] / maxima[series]));
      const y = height - pad - normalized * (height - pad * 2);
      return [x, y];
    });
    let drawing = false;
    const commands = coordinates[series].map((coordinate) => {
      if (!coordinate) {
        drawing = false;
        return "";
      }
      const command = `${drawing ? "L" : "M"}${coordinate[0].toFixed(2)} ${coordinate[1].toFixed(2)}`;
      drawing = true;
      return command;
    }).filter(Boolean);
    const path = document.createElementNS(svgNamespace, "path");
    path.setAttribute("d", commands.join(" "));
    path.setAttribute("class", `line system-line system-${series}-line`);
    svg.append(path);
  });
  enableSystemMetricScrubbing(svg, points, coordinates, width, height, pad);
  oldChart.replaceWith(svg);
}

function renderHostLoad(card) {
  const setting = metricSetting("host_load");
  renderSystemMetricChart(card, hostLoadChartPoints(setting), setting);
}

function formatMetricBucket(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "default";
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

function metricWindowSpec(windowValue) {
  const fallback = {
    "1m": [60, 5], "5m": [300, 15], "15m": [900, 30], "1h": [3600, 60],
    "6h": [21600, 300], "12h": [43200, 600], "24h": [86400, 900],
    "7d": [604800, 3600], "2w": [1209600, 7200], "30d": [2592000, 14400],
    "2mo": [5184000, 28800], "3mo": [7776000, 43200],
    "6mo": [15552000, 86400], "1y": [31536000, 172800],
  };
  const supplied = state.manifest?.metric_windows?.[windowValue];
  if (supplied) return {
    duration: supplied.duration_seconds,
    defaultBucket: supplied.default_bucket_seconds,
  };
  const values = fallback[windowValue] || fallback["1h"];
  return { duration: values[0], defaultBucket: values[1] };
}

function minimumMetricBucket(windowValue) {
  const maximumPoints = state.manifest?.max_chart_points || 720;
  return Math.max(1, Math.ceil(metricWindowSpec(windowValue).duration / maximumPoints));
}

function validMetricBucket(windowValue, bucket) {
  const spec = metricWindowSpec(windowValue);
  return Number.isInteger(bucket) && bucket >= minimumMetricBucket(windowValue) && bucket <= spec.duration;
}

function metricBucketRule(windowValue) {
  const spec = metricWindowSpec(windowValue);
  return `Use a whole number from ${minimumMetricBucket(windowValue)} to ${spec.duration} seconds.`;
}

function loadMetricSettings() {
  try { state.metricSettings = JSON.parse(localStorage.getItem(metricSettingsStorageKey) || "{}") || {}; }
  catch (_error) { state.metricSettings = {}; }
}

function saveMetricSettings() {
  try { localStorage.setItem(metricSettingsStorageKey, JSON.stringify(state.metricSettings)); }
  catch (_error) { showToast("This browser could not persist graph settings.", true); }
}

function metricSetting(metric) {
  const windows = state.manifest?.windows || ["1h"];
  const saved = state.metricSettings[metric] || {};
  const windowValue = windows.includes(saved.window) ? saved.window : "1h";
  const suppliedBucket = Number(saved.bucket);
  return {
    window: windowValue,
    bucket: validMetricBucket(windowValue, suppliedBucket)
      ? suppliedBucket : metricWindowSpec(windowValue).defaultBucket,
  };
}

function updateMetricEditorHint() {
  const windowValue = byId("metric-editor-window").value;
  byId("metric-editor-bucket-hint").textContent = metricBucketRule(windowValue);
}

function updateMetricMoveButtons() {
  const index = state.metricOrder.indexOf(state.editingMetric);
  byId("metric-editor-up").disabled = index <= 0;
  byId("metric-editor-down").disabled = index < 0 || index >= state.metricOrder.length - 1;
}

function openMetricSettings(metric, fromCustomizer = false) {
  state.editingMetric = metric;
  state.metricEditorFromCustomizer = fromCustomizer;
  state.metricEditorScroll = { left: window.scrollX, top: window.scrollY };
  const setting = metricSetting(metric);
  byId("metric-editor-title").textContent = `${metricLabel(metric)} graph`;
  const windowSelect = byId("metric-editor-window");
  windowSelect.replaceChildren();
  (state.manifest?.windows || ["1h"]).forEach((windowValue) => {
    const option = new Option(windowValue, windowValue);
    option.selected = windowValue === setting.window;
    windowSelect.append(option);
  });
  byId("metric-editor-bucket").value = String(setting.bucket);
  byId("metric-editor-error").textContent = "";
  byId("metric-editor-position").hidden = fromCustomizer;
  updateMetricEditorVisibilityControl();
  updateMetricEditorHint();
  updateMetricMoveButtons();
  byId("metric-editor-dialog").showModal();
}

function updateMetricEditorVisibilityControl() {
  const metric = state.editingMetric;
  const hidden = state.metricEditorFromCustomizer
    ? state.metricLayoutDraft?.hidden.has(metric)
    : state.metricHidden.has(metric);
  const button = byId("toggle-metric-hidden");
  button.textContent = hidden ? "Unhide metric" : "Hide metric";
  button.classList.toggle("danger", !hidden);
  byId("metric-editor-visibility-copy").textContent = hidden
    ? "Return this graph to the normal Metrics view."
    : "Hide this graph while keeping its settings and saved position.";
}

function moveEditingMetric(offset) {
  const metric = state.editingMetric;
  const from = state.metricOrder.indexOf(metric);
  const to = from + offset;
  if (from < 0 || to < 0 || to >= state.metricOrder.length) return;
  [state.metricOrder[from], state.metricOrder[to]] = [state.metricOrder[to], state.metricOrder[from]];
  saveMetricLayout();
  updateMetricMoveButtons();
  loadAllMetrics(true);
}

function restoreMetricScroll(position) {
  if (!position) return;
  window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
    window.scrollTo(position.left, position.top);
  }));
}

async function commitMetricSettings(toggleVisibility = false) {
  const metric = state.editingMetric;
  if (!metric) return;
  const returnToCustomizer = state.metricEditorFromCustomizer;
  const scrollPosition = state.metricEditorScroll;
  const windowValue = byId("metric-editor-window").value;
  const bucket = Number(byId("metric-editor-bucket").value.trim());
  if (!validMetricBucket(windowValue, bucket)) {
    byId("metric-editor-error").textContent = metricBucketRule(windowValue);
    return;
  }
  if (toggleVisibility === true) {
    const hidden = returnToCustomizer ? state.metricLayoutDraft?.hidden : state.metricHidden;
    const order = returnToCustomizer ? state.metricLayoutDraft?.order : state.metricOrder;
    if (!hidden || !order) return;
    const isHidden = hidden.has(metric);
    if (!isHidden && !order.some((item) => item !== metric && !hidden.has(item))) {
      byId("metric-editor-error").textContent = "Keep at least one graph visible.";
      return;
    }
    if (isHidden) hidden.delete(metric);
    else hidden.add(metric);
    if (!returnToCustomizer) saveMetricLayout();
  }
  state.metricSettings[metric] = { window: windowValue, bucket };
  saveMetricSettings();
  byId("metric-editor-dialog").close();
  state.editingMetric = null;
  state.metricEditorFromCustomizer = false;
  state.metricEditorScroll = null;
  if (returnToCustomizer && byId("metrics-customizer-dialog").open) {
    renderMetricLayoutDraft();
  }
  await loadAllMetrics(true);
  restoreMetricScroll(scrollPosition);
}

function applyWindowToAllMetrics() {
  const windowValue = byId("metrics-window").value || "1h";
  registeredMetrics().forEach((metric) => {
    state.metricSettings[metric] = {
      window: windowValue,
      bucket: metricWindowSpec(windowValue).defaultBucket,
    };
  });
  saveMetricSettings();
  loadAllMetrics(true);
}

function renderTopCommands(card, payload, setting) {
  card.querySelector(".top-commands")?.remove();
  if (payload.metric !== "commands" || !Array.isArray(payload.top_commands) || !payload.top_commands.length) return;
  const section = document.createElement("div");
  section.className = "top-commands";
  const heading = document.createElement("h4");
  heading.textContent = "Top commands";
  section.append(heading);
  payload.top_commands.forEach((command) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "top-command-button";
    const name = document.createElement("span");
    name.textContent = command.name;
    const count = document.createElement("strong");
    count.textContent = `${formatNumber(command.count)} · ${formatNumber(command.share)}%`;
    button.append(name, count);
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openCommandBreakdown(command, setting);
    });
    section.append(button);
  });
  card.append(section);
}

function renderTopRateLimitRoutes(card, payload) {
  card.querySelector(".top-rate-limit-routes")?.remove();
  if (!payload.metric.startsWith("discord_") || !Array.isArray(payload.top_discord_routes) || !payload.top_discord_routes.length) return;
  const section = document.createElement("div");
  section.className = "top-rate-limit-routes";
  const heading = document.createElement("h4");
  heading.textContent = "Affected Discord routes";
  section.append(heading);
  payload.top_discord_routes.forEach((item) => {
    const row = document.createElement("div");
    row.className = "rate-limit-route-row";
    const route = document.createElement("code");
    route.textContent = item.route;
    const count = document.createElement("strong");
    count.textContent = `${formatNumber(item.count)} · ${formatNumber(item.share)}%`;
    row.append(route, count);
    section.append(row);
  });
  card.append(section);
}

async function openCommandBreakdown(command, setting) {
  byId("command-detail-title").textContent = command.name;
  byId("command-detail-summary").textContent = `${formatNumber(command.count)} uses in ${setting.window} · ${formatNumber(command.share)}% of commands`;
  const host = byId("command-detail-chart");
  host.replaceChildren();
  const chart = document.createElement("div");
  chart.className = "metric-empty";
  chart.textContent = "Loading command samples…";
  host.append(chart);
  byId("command-detail-dialog").showModal();
  try {
    const payload = await request(`/api/v1/metrics/commands/${encodeURIComponent(command.name)}?window=${encodeURIComponent(setting.window)}&bucket_seconds=${setting.bucket}`);
    if (!byId("command-detail-dialog").open) return;
    const fakeCard = document.createElement("div");
    const fakeValue = document.createElement("span");
    fakeValue.className = "metric-value";
    const fakeChart = document.createElement("div");
    fakeChart.className = "metric-empty";
    fakeCard.append(fakeValue, fakeChart);
    renderMetricChart(fakeCard, payload, null, false);
    host.replaceChildren(fakeCard.querySelector(".metric-chart, .metric-empty"));
    byId("command-detail-summary").textContent = `${formatNumber(payload.summary?.total)} uses in ${setting.window} · ${formatMetricBucket(payload.bucket_seconds)} buckets`;
  } catch (error) {
    host.textContent = error.message;
  }
}

function defaultMetricLayout(metrics) {
  const available = new Set(metrics);
  return [
    ...preferredMetricOrder.filter((metric) => available.has(metric)),
    ...metrics.filter((metric) => !preferredMetricOrder.includes(metric)),
  ];
}

function loadMetricLayout(metrics) {
  const defaults = defaultMetricLayout(metrics);
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(metricLayoutStorageKey) || "null"); }
  catch (_error) { saved = null; }
  const savedOrder = Array.isArray(saved?.order) ? saved.order : [];
  const seen = new Set();
  state.metricOrder = [...savedOrder, ...defaults].filter((metric) => {
    if (!metrics.includes(metric) || seen.has(metric)) return false;
    seen.add(metric);
    return true;
  });
  const hidden = Array.isArray(saved?.hidden) ? saved.hidden : [];
  state.metricHidden = new Set(hidden.filter((metric) => metrics.includes(metric)));
  if (metrics.length && state.metricHidden.size === metrics.length) state.metricHidden.clear();
  state.metricLayoutLoaded = true;
}

function saveMetricLayout() {
  try {
    localStorage.setItem(metricLayoutStorageKey, JSON.stringify({
      order: state.metricOrder,
      hidden: Array.from(state.metricHidden),
    }));
  } catch (_error) {
    showToast("This browser could not persist the metric layout.", true);
  }
}

function renderMetricLayoutDraft() {
  const draft = state.metricLayoutDraft;
  const list = byId("metrics-customizer-list");
  list.replaceChildren();
  if (!draft) return;
  draft.order.forEach((metric, index) => {
    const row = document.createElement("div");
    row.className = "metric-layout-row";
    const toggle = document.createElement("label");
    toggle.className = "metric-layout-toggle";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = !draft.hidden.has(metric);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) draft.hidden.delete(metric);
      else draft.hidden.add(metric);
      byId("metrics-customizer-error").textContent = "";
    });
    const copy = document.createElement("span");
    copy.className = "metric-layout-copy";
    const label = document.createElement("strong");
    label.textContent = metricLabel(metric);
    const detail = document.createElement("small");
    const setting = metricSetting(metric);
    detail.textContent = `${setting.window} · ${formatMetricBucket(setting.bucket)} buckets`;
    copy.append(label, detail);
    toggle.append(checkbox, copy);

    const actions = document.createElement("div");
    actions.className = "metric-layout-actions";
    const edit = document.createElement("button");
    edit.className = "button metric-layout-edit";
    edit.type = "button";
    edit.textContent = "Edit";
    edit.addEventListener("click", () => openMetricSettings(metric, true));
    actions.append(edit);

    const moveUp = document.createElement("button");
    moveUp.className = "button metric-layout-move";
    moveUp.type = "button";
    moveUp.textContent = "↑";
    moveUp.title = `Move ${metricLabel(metric)} earlier`;
    moveUp.setAttribute("aria-label", moveUp.title);
    moveUp.disabled = index === 0;
    moveUp.addEventListener("click", () => {
      [draft.order[index - 1], draft.order[index]] = [draft.order[index], draft.order[index - 1]];
      renderMetricLayoutDraft();
    });

    const moveDown = document.createElement("button");
    moveDown.className = "button metric-layout-move";
    moveDown.type = "button";
    moveDown.textContent = "↓";
    moveDown.title = `Move ${metricLabel(metric)} later`;
    moveDown.setAttribute("aria-label", moveDown.title);
    moveDown.disabled = index === draft.order.length - 1;
    moveDown.addEventListener("click", () => {
      [draft.order[index], draft.order[index + 1]] = [draft.order[index + 1], draft.order[index]];
      renderMetricLayoutDraft();
    });
    actions.append(moveUp, moveDown);
    row.append(toggle, actions);
    list.append(row);
  });
}

function openMetricCustomizer() {
  const metrics = registeredMetrics();
  if (!metrics.length) {
    showToast("No metric registry is available yet.", true);
    return;
  }
  if (!state.metricLayoutLoaded) loadMetricLayout(metrics);
  state.metricLayoutDraft = {
    order: [...state.metricOrder],
    hidden: new Set(state.metricHidden),
  };
  byId("metrics-customizer-error").textContent = "";
  renderMetricLayoutDraft();
  byId("metrics-customizer-dialog").showModal();
}

function resetMetricLayoutDraft() {
  const metrics = registeredMetrics();
  state.metricLayoutDraft = { order: defaultMetricLayout(metrics), hidden: new Set() };
  byId("metrics-customizer-error").textContent = "";
  renderMetricLayoutDraft();
}

function commitMetricLayout() {
  const draft = state.metricLayoutDraft;
  if (!draft) return;
  if (!draft.order.some((metric) => !draft.hidden.has(metric))) {
    byId("metrics-customizer-error").textContent = "Keep at least one graph visible.";
    return;
  }
  state.metricOrder = [...draft.order];
  state.metricHidden = new Set(draft.hidden);
  state.metricLayoutDraft = null;
  saveMetricLayout();
  byId("metrics-customizer-dialog").close();
  loadAllMetrics(true);
}

function saveDialogOnBackdrop(dialog, save) {
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) save();
  });
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    save();
  });
}

async function mapLimit(items, limit, worker) {
  let index = 0;
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (index < items.length) {
      const current = items[index++];
      await worker(current);
    }
  });
  await Promise.all(runners);
}

async function loadAllMetrics(force = false) {
  if (state.metricsPromise) {
    if (force) state.metricsReloadRequested = true;
    return state.metricsPromise;
  }
  const pending = (async () => {
    do {
      state.metricsReloadRequested = false;
      await loadAllMetricsOnce();
    } while (state.metricsReloadRequested);
  })();
  state.metricsPromise = pending;
  try {
    return await pending;
  } finally {
    if (state.metricsPromise === pending) state.metricsPromise = null;
  }
}

async function loadAllMetricsOnce() {
  if (!state.manifest) return;
  const generation = ++state.metricLoadGeneration;
  state.metricsLoaded = true;
  const grid = byId("metric-grid");
  const metrics = registeredMetrics();
  if (!state.metricLayoutLoaded) loadMetricLayout(metrics);
  else {
    const knownOrder = new Set(state.metricOrder);
    defaultMetricLayout(metrics).forEach((metric) => {
      if (!knownOrder.has(metric)) state.metricOrder.push(metric);
    });
    state.metricOrder = state.metricOrder.filter((metric) => metrics.includes(metric));
    state.metricHidden = new Set(Array.from(state.metricHidden).filter((metric) => metrics.includes(metric)));
  }
  const displayedMetrics = state.metricOrder.filter((metric) => state.showHiddenMetrics || !state.metricHidden.has(metric));
  Array.from(grid.children).forEach((card) => {
    if (!displayedMetrics.includes(card.dataset.metric)) {
      metricCardResizeObserver?.unobserve(card);
      card.remove();
    }
  });
  displayedMetrics.forEach((metric, index) => {
    const card = grid.querySelector(`[data-metric="${CSS.escape(metric)}"]`) || createMetricCard(metric);
    const isHidden = state.metricHidden.has(metric);
    card.classList.toggle("metric-card-hidden-preview", isHidden);
    card.querySelector(".metric-hidden-badge").hidden = !isHidden;
    const cardAtIndex = grid.children[index];
    if (cardAtIndex !== card) grid.insertBefore(card, cardAtIndex || null);
    observeMetricCard(card);
  });
  updateMetricLayoutSummary(metrics, displayedMetrics);
  await mapLimit(displayedMetrics, 4, async (metric) => {
    const card = grid.querySelector(`[data-metric="${CSS.escape(metric)}"]`);
    if (metric === "host_load") {
      renderHostLoad(card);
      return;
    }
    const setting = metricSetting(metric);
    try {
      const payload = await request(`/api/v1/metrics?metric=${encodeURIComponent(metric)}&window=${encodeURIComponent(setting.window)}&bucket_seconds=${setting.bucket}`);
      if (generation !== state.metricLoadGeneration) return;
      renderMetricChart(card, payload, setting);
    } catch (error) {
      if (generation !== state.metricLoadGeneration) return;
      const chart = card.querySelector(".metric-chart, .metric-empty");
      chart.className = "metric-empty";
      chart.textContent = error.message;
    }
  });
  if (generation === state.metricLoadGeneration) state.metricsLoadedAt = Date.now();
}

function cloneJson(value) {
  return JSON.parse(JSON.stringify(value));
}

function editorInput(path, value, redactionMarker) {
  const row = document.createElement("label");
  row.className = "field-row";
  const key = path[path.length - 1];
  const title = document.createElement("span");
  title.textContent = humanize(key);
  const pathLabel = document.createElement("span");
  pathLabel.className = "field-path";
  pathLabel.textContent = path.join(" › ");
  let input;
  let type;
  if (typeof value === "boolean") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = value;
    type = "boolean";
    row.classList.add("toggle-row");
  } else if (typeof value === "number") {
    input = document.createElement("input");
    input.type = "number";
    input.step = "any";
    input.value = String(value);
    type = "number";
  } else if (value === null) {
    input = document.createElement("input");
    const numeric = key === "max_storage_gb" || key === "max_backups";
    input.type = numeric ? "number" : "text";
    if (numeric) input.step = "any";
    input.value = "";
    input.placeholder = numeric ? "No limit" : "Not set";
    type = numeric ? "nullable-number" : "nullable-string";
  } else if (value && typeof value === "object") {
    input = document.createElement("textarea");
    input.value = JSON.stringify(value, null, 2);
    type = "json";
    row.classList.add("wide");
  } else {
    const secret = value === redactionMarker;
    input = document.createElement(String(value).length > 100 ? "textarea" : "input");
    if (input instanceof HTMLInputElement) input.type = secret ? "password" : "text";
    input.value = secret ? "" : String(value);
    input.placeholder = secret ? "Stored secret — leave blank to preserve" : "";
    input.dataset.redacted = secret ? "true" : "false";
    type = "string";
    if (input instanceof HTMLTextAreaElement) row.classList.add("wide");
  }
  input.dataset.path = JSON.stringify(path);
  input.dataset.valueType = type;
  if (type === "boolean") row.append(input, title, pathLabel);
  else row.append(title, pathLabel, input);
  return row;
}

function collectLeaves(value, path, output, redactionMarker) {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    Object.entries(value).forEach(([key, child]) => collectLeaves(child, [...path, key], output, redactionMarker));
    if (!Object.keys(value).length) output.push(editorInput(path, value, redactionMarker));
    return;
  }
  output.push(editorInput(path, value, redactionMarker));
}

function renderEditor(container, value, redactionMarker) {
  container.replaceChildren();
  const general = document.createElement("article");
  general.className = "card backup-settings-card";
  const generalTitle = document.createElement("h3");
  generalTitle.textContent = "Backup settings";
  const generalFields = document.createElement("div");
  generalFields.className = "editor-fields";
  general.append(generalTitle, generalFields);
  Object.entries(value || {}).forEach(([sectionName, sectionValue]) => {
    if (!sectionValue || typeof sectionValue !== "object" || Array.isArray(sectionValue)) {
      generalFields.append(editorInput([sectionName], sectionValue, redactionMarker));
      return;
    }
    const section = document.createElement("article");
    section.className = "card backup-settings-card";
    const title = document.createElement("h3");
    title.textContent = humanize(sectionName);
    const fields = document.createElement("div");
    fields.className = "editor-fields";
    const rows = [];
    collectLeaves(sectionValue, [sectionName], rows, redactionMarker);
    fields.append(...rows);
    section.append(title, fields);
    container.append(section);
  });
  if (generalFields.childElementCount) container.prepend(general);
}

const configSectionDescriptions = {
  backups: "Automatic complete archives, oldest-first retention limits, dump tools, and Google Drive destination.",
  mongodb: "Database connection and compression settings.",
  mysql: "SQL host, account, database, transaction behavior, and connection-pool limits. The password stays in encrypted auth storage.",
  local_databases: "Automatic startup policy for local MySQL and MongoDB services.",
  translation: "Outbound translation provider and fail-fast request timeout.",
  website: "Dashboard identity, address, cookies, proxy trust, and development behavior.",
  control_panel: "FateControl listener, bot status connection, startup, and host-action policy. Listener changes apply after FateControl restarts.",
  organism: "Local Organism state, bounded work queues, and disabled-by-default configured-advisor discovery.",
  bot_invite_permissions: "Permissions requested when Fate is invited to a server.",
  extensions: "Loaded modules by category. Enter one module name per line.",
};

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function pathEquals(left, right) {
  return left.length === right.length && left.every((part, index) => part === right[index]);
}

function hasUnsafeIntegerAt(path) {
  return state.unsafeIntegerPaths.some((candidate) => pathEquals(candidate, path));
}

function hasUnsafeIntegerInside(path) {
  return state.unsafeIntegerPaths.some((candidate) =>
    candidate.length === path.length + 1 && path.every((part, index) => candidate[index] === part));
}

function isProtectedConfigValue(key, path, value) {
  if (value === state.redactionMarker) return true;
  const lower = String(key).toLowerCase();
  if (lower.includes("secret") || lower === "token" || lower === "token_id" || lower === "top.gg") return true;
  return path.length === 2 && path[0] === "mongodb" && lower === "url";
}

function configSearchKeys(value, includeObjects = true) {
  if (!isPlainObject(value)) return "";
  const output = [];
  Object.entries(value).forEach(([key, child]) => {
    if (isPlainObject(child)) {
      if (includeObjects) output.push(key, configSearchKeys(child, true));
    } else {
      output.push(key, humanize(key));
    }
  });
  return output.join(" ");
}

function configInput(path, key, value) {
  if (isProtectedConfigValue(key, path, value)) {
    const protectedRow = document.createElement("div");
    protectedRow.className = "config-secret";
    const title = document.createElement("div");
    title.className = "config-field-title";
    title.textContent = humanize(key);
    const protectedValue = document.createElement("div");
    protectedValue.className = "config-secret-value";
    protectedValue.textContent = "Protected secret • kept unchanged on the bot computer";
    protectedRow.append(title, protectedValue);
    return protectedRow;
  }

  const titleText = humanize(key);
  let input;
  let type;
  if (typeof value === "boolean") {
    const row = document.createElement("label");
    row.className = "config-switch";
    const title = document.createElement("span");
    title.textContent = titleText;
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = value;
    input.setAttribute("role", "switch");
    type = "boolean";
    input.dataset.path = JSON.stringify(path);
    input.dataset.valueType = type;
    row.append(title, input);
    return row;
  }

  const row = document.createElement("label");
  row.className = "config-field";
  const title = document.createElement("span");
  title.className = "config-field-title";
  title.textContent = titleText;

  if (Array.isArray(value)) {
    input = document.createElement("textarea");
    const numbers = key.endsWith("_ids") || value.some((item) => typeof item === "number") || hasUnsafeIntegerInside(path);
    type = numbers ? "number-list" : "string-list";
    input.value = value.map(String).join("\n");
    input.placeholder = "One item per line";
  } else if (typeof value === "number") {
    input = document.createElement("input");
    input.type = "number";
    input.step = Number.isInteger(value) ? "1" : "any";
    input.value = String(value);
    type = Number.isInteger(value) ? "integer" : "decimal";
  } else if (hasUnsafeIntegerAt(path)) {
    input = document.createElement("input");
    input.type = "number";
    input.step = "1";
    input.value = String(value);
    type = "unsafe-integer";
  } else {
    input = document.createElement("input");
    input.type = "text";
    input.value = value == null ? "" : String(value);
    type = value == null ? "nullable-string" : "string";
  }
  input.dataset.path = JSON.stringify(path);
  input.dataset.valueType = type;
  row.append(title, input);
  return row;
}

function appendConfigValues(container, values, parentPath, includeObjects) {
  Object.entries(values || {}).forEach(([key, value]) => {
    if (isPlainObject(value)) {
      if (!includeObjects) return;
      const heading = document.createElement("div");
      heading.className = "config-subsection-title";
      heading.textContent = humanize(key);
      container.append(heading);
      appendConfigValues(container, value, [...parentPath, key], true);
      return;
    }
    container.append(configInput([...parentPath, key], key, value));
  });
}

function createConfigSection(label, description, values, path, includeObjects, searchText) {
  const card = document.createElement("article");
  card.className = "card config-section-card";
  card.dataset.configSearch = searchText.toLowerCase();
  const title = document.createElement("h3");
  title.textContent = label;
  const copy = document.createElement("p");
  copy.className = "config-section-description";
  copy.textContent = description;
  card.append(title, copy);
  appendConfigValues(card, values, path, includeObjects);
  return card;
}

function renderConfigEditor(config) {
  const container = byId("config-editor");
  container.replaceChildren();
  const panels = [];
  const core = {};
  Object.entries(config || {}).forEach(([key, value]) => {
    if (!isPlainObject(value)) core[key] = value;
  });
  panels.push(createConfigSection(
    "Core bot settings",
    "Identity, storage, logging channels, runtime behavior, and general limits.",
    core,
    [],
    false,
    `Core bot settings ${configSearchKeys(config, false)}`,
  ));
  Object.entries(config || {}).forEach(([key, value]) => {
    if (!isPlainObject(value) || key === "backups") return;
    const label = humanize(key);
    panels.push(createConfigSection(
      label,
      configSectionDescriptions[key] || "Related Fate settings.",
      value,
      [key],
      true,
      `${label} ${key} ${configSearchKeys(value, true)}`,
    ));
  });
  container.append(...panels);

  const filter = byId("config-section-filter");
  filter.replaceChildren(new Option("All settings", "all"));
  panels.forEach((panel, index) => filter.append(new Option(panel.querySelector("h3").textContent, String(index))));
  filter.value = "all";
  byId("config-search").value = "";
  applyConfigFilters();
}

function applyConfigFilters() {
  const panels = Array.from(byId("config-editor").querySelectorAll(".config-section-card"));
  const selected = byId("config-section-filter").value;
  const query = byId("config-search").value.trim().toLowerCase();
  let visible = 0;
  panels.forEach((panel, index) => {
    const sectionMatches = selected === "all" || Number(selected) === index;
    const searchMatches = !query || panel.dataset.configSearch.includes(query);
    panel.hidden = !(sectionMatches && searchMatches);
    if (!panel.hidden) visible += 1;
  });
  const summary = byId("config-filter-summary");
  summary.textContent = visible === 0
    ? "No sections match this filter. Try a setting key such as logging, prefix, or extensions."
    : `${visible} ${visible === 1 ? "section" : "sections"} shown${query ? ` for “${query}”` : ""}`;
  summary.classList.toggle("empty", visible === 0);
}

function assignPath(target, path, value) {
  let cursor = target;
  path.slice(0, -1).forEach((key) => { cursor = cursor[key]; });
  cursor[path[path.length - 1]] = value;
}

function collectEditor(container, baseline, redactionMarker) {
  const result = cloneJson(baseline);
  container.querySelectorAll("[data-path]").forEach((input) => {
    const path = JSON.parse(input.dataset.path);
    let value;
    if (input.dataset.valueType === "boolean") value = input.checked;
    else if (input.dataset.valueType === "number" || input.dataset.valueType === "nullable-number") {
      if (input.dataset.valueType === "nullable-number" && !input.value.trim()) {
        value = null;
        assignPath(result, path, value);
        return;
      }
      value = Number(input.value);
      if (!Number.isFinite(value)) throw new Error(`${path.join(".")} must be a number.`);
    } else if (input.dataset.valueType === "nullable-string") {
      value = input.value.trim() || null;
    } else if (input.dataset.valueType === "json") {
      try { value = JSON.parse(input.value); }
      catch (_error) { throw new Error(`${path.join(".")} must contain valid JSON.`); }
    } else if (input.dataset.redacted === "true" && !input.value) value = redactionMarker;
    else value = input.value;
    assignPath(result, path, value);
  });
  return result;
}

function setConfigResult(message, resultState = "") {
  const result = byId("config-result");
  result.textContent = message;
  result.dataset.state = resultState;
}

function updateConfigControls() {
  const canSave = state.configDirty && Boolean(state.configRevision);
  const save = byId("save-config");
  save.disabled = !canSave;
  save.textContent = !state.configRevision ? "Reload first" : state.configDirty ? "Save changes" : "Saved";
  byId("save-restart-config").disabled = !canSave;
  byId("discard-config").disabled = !state.configDirty;
}

function markConfigDirty() {
  if (!state.config) return;
  state.configDirty = true;
  updateConfigControls();
  setConfigResult("Unsaved setting changes. Review them, then save when ready.", "dirty");
}

function splitConfigList(text, numbers, path, unsafePaths) {
  const items = text.trim() ? text.split(/[,\n]/).map((item) => item.trim()).filter(Boolean) : [];
  if (!numbers) return items;
  return items.map((item, index) => {
    if (!/^-?\d+$/.test(item)) throw new Error(`${path.join(".")} must contain whole numbers.`);
    const value = Number(item);
    if (Number.isSafeInteger(value)) return value;
    unsafePaths.push([...path, String(index)]);
    return item;
  });
}

function collectConfigEditor() {
  const result = cloneJson(state.config);
  const unsafeIntegerPaths = [];
  byId("config-editor").querySelectorAll("[data-path]").forEach((input) => {
    const path = JSON.parse(input.dataset.path);
    const textValue = input.type === "checkbox" ? "" : input.value.trim();
    let value;
    switch (input.dataset.valueType) {
      case "boolean":
        value = input.checked;
        break;
      case "integer":
        if (!/^-?\d+$/.test(textValue)) throw new Error(`${path.join(".")} must be a whole number.`);
        value = Number(textValue);
        if (!Number.isSafeInteger(value)) throw new Error(`${path.join(".")} must be a safe whole number.`);
        break;
      case "unsafe-integer":
        if (!/^-?\d+$/.test(textValue)) throw new Error(`${path.join(".")} must be a whole number.`);
        if (Number.isSafeInteger(Number(textValue))) value = Number(textValue);
        else {
          value = textValue;
          unsafeIntegerPaths.push(path);
        }
        break;
      case "decimal":
        value = Number(textValue);
        if (!textValue || !Number.isFinite(value)) throw new Error(`${path.join(".")} must be a number.`);
        break;
      case "number-list":
        value = splitConfigList(textValue, true, path, unsafeIntegerPaths);
        break;
      case "string-list":
        value = splitConfigList(textValue, false, path, unsafeIntegerPaths);
        break;
      case "nullable-string":
        value = textValue || null;
        break;
      default:
        value = textValue;
    }
    assignPath(result, path, value);
  });
  return { config: result, unsafeIntegerPaths };
}

async function loadConfig() {
  setConfigResult("Loading config.json…");
  try {
    const payload = await request("/api/v1/config");
    state.config = payload.control_config || payload.config;
    state.configRevision = payload.revision;
    state.unsafeIntegerPaths = payload.unsafe_integer_paths || [];
    state.redactionMarker = payload.redaction_marker || state.redactionMarker;
    state.configDirty = false;
    state.configLoadedAt = Date.now();
    renderConfigEditor(state.config);
    updateConfigControls();
    setConfigResult("Loaded. Masked secrets remain unchanged when saved.", "success");
  } catch (error) {
    state.configRevision = "";
    updateConfigControls();
    setConfigResult(error.message, "error");
    showToast(error.message, true);
  }
}

function discardConfigEdits() {
  if (!state.config || !state.configDirty) return;
  renderConfigEditor(state.config);
  state.configDirty = false;
  updateConfigControls();
  setConfigResult("Unsaved edits discarded. The last loaded values are restored.", "success");
}

async function saveConfig(restart = false) {
  if (!state.configDirty) return;
  try {
    if (!state.configRevision) throw new Error("Reload settings before saving.");
    const collected = collectConfigEditor();
    const config = collected.config;
    setConfigResult(restart ? "Saving and restarting Fate…" : "Validating and saving…", "warning");
    const payload = await request("/api/v1/config", {
      method: "PUT",
      body: JSON.stringify({
        config,
        revision: state.configRevision,
        restart,
        unsafe_integer_paths: collected.unsafeIntegerPaths,
      }),
    });
    state.config = config;
    state.unsafeIntegerPaths = collected.unsafeIntegerPaths;
    state.configRevision = payload.revision || state.configRevision;
    state.configDirty = false;
    state.configLoadedAt = Date.now();
    renderConfigEditor(state.config);
    updateConfigControls();
    let message = restart
      ? "Saved. Fate is restarting with the new config."
      : "Saved. Restart Fate when you want the changes applied.";
    if (payload.controller_restart_required) {
      message += " FateControl listener and host-policy changes apply the next time the Fate Control Service restarts.";
    }
    setConfigResult(message, "success");
    showToast("config.json backed up and saved.");
  } catch (error) {
    setConfigResult(error.message, "error");
    showToast(error.message, true);
  }
}

async function loadBackups() {
  try {
    const payload = await request("/api/v1/backups/settings");
    state.backups = payload.settings;
    state.backupRevision = payload.revision;
    state.backupsLoadedAt = Date.now();
    state.backupDirty = false;
    renderEditor(byId("backups-editor"), state.backups, state.redactionMarker);
    const drive = payload.drive || {};
    const pill = byId("drive-pill");
    pill.textContent = drive.linked ? "Drive linked" : drive.configured ? "Drive ready" : "Drive not configured";
    pill.dataset.state = drive.linked ? "online" : drive.configured ? "loading" : "error";
    byId("link-drive").disabled = !drive.configured;
    byId("choose-drive-folder").disabled = !drive.linked;
  } catch (error) {
    showToast(error.message, true);
  }
}

async function saveBackups(event) {
  event.preventDefault();
  try {
    const settings = collectEditor(byId("backups-editor"), state.backups, state.redactionMarker);
    const payload = await request("/api/v1/backups/settings", {
      method: "PUT",
      body: JSON.stringify({ settings, revision: state.backupRevision }),
    });
    state.backups = payload.settings || settings;
    state.backupRevision = payload.revision || state.backupRevision;
    state.backupsLoadedAt = Date.now();
    state.backupDirty = false;
    renderEditor(byId("backups-editor"), state.backups, state.redactionMarker);
    showToast("Backup policy saved.");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function linkDrive() {
  try {
    const payload = await request("/api/v1/backups/google/authorize", { method: "POST" });
    window.open(payload.authorization_url, "_blank", "noopener,noreferrer");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function loadFolder(folder) {
  try {
    const payload = await request(`/api/v1/backups/google/folders?parent_id=${encodeURIComponent(folder.id)}`);
    byId("folder-path").textContent = state.folderStack.map((item) => item.name).join(" / ");
    const list = byId("folder-list");
    list.replaceChildren();
    const folders = payload.folders || [];
    if (!folders.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No child folders.";
      list.append(empty);
    }
    folders.forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = item.name || "Untitled folder";
      button.addEventListener("click", () => {
        const next = { id: item.id, name: item.name || "Untitled folder" };
        state.folderStack.push(next);
        loadFolder(next);
      });
      list.append(button);
    });
    byId("folder-up").disabled = state.folderStack.length <= 1;
  } catch (error) {
    showToast(error.message, true);
  }
}

function openFolderDialog() {
  state.folderStack = [{ id: "root", name: "Drive" }];
  byId("folder-dialog").showModal();
  loadFolder(state.folderStack[0]);
}

async function selectCurrentFolder() {
  const current = state.folderStack[state.folderStack.length - 1];
  try {
    await request("/api/v1/backups/google/folder", {
      method: "POST",
      body: JSON.stringify({ folder_id: current.id }),
    });
    byId("folder-dialog").close();
    showToast(`Using ${state.folderStack.map((item) => item.name).join(" / ")}.`);
    await loadBackups();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function loadConsole(silent = false) {
  if (state.consolePromise) return state.consolePromise;
  const pending = (async () => {
    try {
      const payload = await request("/api/v1/console?lines=400");
      const output = byId("console-output");
      const nearBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 80;
      output.textContent = payload.text || "No console output yet.";
      if (nearBottom) output.scrollTop = output.scrollHeight;
    } catch (error) {
      if (!silent) showToast(error.message, true);
    }
  })();
  state.consolePromise = pending;
  try {
    return await pending;
  } finally {
    if (state.consolePromise === pending) state.consolePromise = null;
  }
}

async function copyConsole() {
  const output = byId("console-output").textContent || "";
  if (!output || output === "No console output yet." || output === "Waiting for output…") {
    showToast("There is no console output to copy yet.", true);
    return;
  }
  try {
    if (!navigator.clipboard || !window.isSecureContext) throw new Error("Clipboard fallback required");
    await navigator.clipboard.writeText(output);
  } catch (_error) {
    const field = document.createElement("textarea");
    field.value = output;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.append(field);
    field.select();
    const copied = document.execCommand("copy");
    field.remove();
    if (!copied) {
      showToast("Could not copy the console on this device.", true);
      return;
    }
  }
  showToast("Console copied.");
}

function manageConsoleTimer() {
  clearInterval(state.consoleTimer);
  state.consoleTimer = null;
  if (state.authenticated && state.activeSection === "console") {
    loadConsole();
    if (byId("console-auto").checked) state.consoleTimer = setInterval(() => loadConsole(true), 2000);
  }
}

async function refreshActive() {
  if (!state.authenticated) return showLogin();
  if (state.activeSection === "overview") return loadStatus();
  if (state.activeSection === "metrics") return loadAllMetrics(true);
  if (state.activeSection === "config") return loadConfig();
  if (state.activeSection === "backups") return loadBackups();
  return loadConsole();
}

async function loadInitialData() {
  try {
    state.manifest = await request("/api/v1/control/manifest");
    loadMetricSettings();
    state.redactionMarker = state.manifest.redaction_marker || state.redactionMarker;
    byId("instance-title").textContent = state.manifest.instance_name || "Mission control";
    const windowSelect = byId("metrics-window");
    windowSelect.replaceChildren();
    (state.manifest.windows || ["1h"]).forEach((windowValue) => {
      const option = document.createElement("option");
      option.value = windowValue;
      option.textContent = windowValue;
      option.selected = windowValue === "1h";
      windowSelect.append(option);
    });
    await loadStatus();
    switchSection(state.activeSection, false);
    clearInterval(state.statusTimer);
    state.statusTimer = setInterval(() => {
      if (!document.hidden && state.authenticated) loadStatus(true);
    }, (state.manifest.refresh_seconds || 15) * 1000);
  } catch (error) {
    showToast(error.message, true);
  }
}

function bindEvents() {
  byId("theme-button").addEventListener("click", openThemeDialog);
  byId("close-theme-dialog").addEventListener("click", () => byId("theme-dialog").close());
  byId("theme-effects").addEventListener("change", (event) => {
    setThemeEffects(event.target.checked);
  });
  byId("settings-theme-effects").addEventListener("change", (event) => setThemeEffects(event.target.checked));
  document.querySelectorAll(".section-nav button[data-section]").forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.section === "settings" && window.FateAndroid && typeof window.FateAndroid.openSettings === "function") window.FateAndroid.openSettings();
    else switchSection(button.dataset.section);
  }));
  byId("refresh-button").addEventListener("click", refreshActive);
  byId("login-form").addEventListener("submit", loginWithKey);
  byId("start-button").addEventListener("click", () => processAction("start"));
  byId("stop-button").addEventListener("click", () => processAction("stop"));
  byId("restart-button").addEventListener("click", () => processAction("restart"));
  byId("restart-status-server-button").addEventListener("click", restartStatusServer);
  byId("reboot-button").addEventListener("click", rebootHost);
  byId("metrics-window").addEventListener("change", applyWindowToAllMetrics);
  byId("metrics-columns").addEventListener("change", (event) => {
    localStorage.setItem(metricColumnStorageKey, event.target.value);
    applyMetricColumns(event.target.value);
    window.requestAnimationFrame(() => document.querySelectorAll(".metric-card").forEach(sizeMetricCard));
  });
  byId("metrics-show-hidden").addEventListener("change", (event) => {
    state.showHiddenMetrics = event.target.checked;
    loadAllMetrics(true);
  });
  byId("customize-metrics").addEventListener("click", openMetricCustomizer);
  byId("reset-metrics-layout").addEventListener("click", resetMetricLayoutDraft);
  byId("cancel-metrics-layout").addEventListener("click", () => {
    state.metricLayoutDraft = null;
    byId("metrics-customizer-dialog").close();
  });
  byId("save-metrics-layout").addEventListener("click", commitMetricLayout);
  saveDialogOnBackdrop(byId("metrics-customizer-dialog"), commitMetricLayout);
  byId("metric-editor-window").addEventListener("change", () => {
    const windowValue = byId("metric-editor-window").value;
    byId("metric-editor-bucket").value = String(metricWindowSpec(windowValue).defaultBucket);
    byId("metric-editor-error").textContent = "";
    updateMetricEditorHint();
  });
  byId("metric-editor-up").addEventListener("click", () => moveEditingMetric(-1));
  byId("metric-editor-down").addEventListener("click", () => moveEditingMetric(1));
  byId("cancel-metric-editor").addEventListener("click", () => {
    state.editingMetric = null;
    state.metricEditorFromCustomizer = false;
    state.metricEditorScroll = null;
    byId("metric-editor-dialog").close();
  });
  byId("toggle-metric-hidden").addEventListener("click", () => commitMetricSettings(true));
  byId("save-metric-editor").addEventListener("click", () => commitMetricSettings());
  saveDialogOnBackdrop(byId("metric-editor-dialog"), commitMetricSettings);
  byId("close-command-detail").addEventListener("click", () => byId("command-detail-dialog").close());
  byId("config-editor").addEventListener("input", markConfigDirty);
  byId("config-editor").addEventListener("change", markConfigDirty);
  byId("config-form").addEventListener("submit", (event) => event.preventDefault());
  byId("save-config").addEventListener("click", () => saveConfig(false));
  byId("save-restart-config").addEventListener("click", () => saveConfig(true));
  byId("reload-config").addEventListener("click", loadConfig);
  byId("discard-config").addEventListener("click", discardConfigEdits);
  byId("config-search").addEventListener("input", () => {
    if (byId("config-search").value && byId("config-section-filter").value !== "all") {
      byId("config-section-filter").value = "all";
    }
    applyConfigFilters();
  });
  byId("config-section-filter").addEventListener("change", applyConfigFilters);
  byId("backups-form").addEventListener("submit", saveBackups);
  byId("backups-editor").addEventListener("input", () => { if (state.backups) state.backupDirty = true; });
  byId("backups-editor").addEventListener("change", () => { if (state.backups) state.backupDirty = true; });
  byId("link-drive").addEventListener("click", linkDrive);
  byId("choose-drive-folder").addEventListener("click", openFolderDialog);
  byId("close-folder-dialog").addEventListener("click", () => byId("folder-dialog").close());
  byId("select-current-folder").addEventListener("click", selectCurrentFolder);
  byId("folder-up").addEventListener("click", () => {
    if (state.folderStack.length > 1) state.folderStack.pop();
    loadFolder(state.folderStack[state.folderStack.length - 1]);
  });
  byId("console-auto").addEventListener("change", manageConsoleTimer);
  byId("copy-console").addEventListener("click", copyConsole);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.authenticated) refreshActive();
  });
}

async function boot() {
  const launch = new URL(location.href);
  const launchTheme = launch.searchParams.get("theme");
  const launchEffects = launch.searchParams.get("effects");
  if (visualThemes.some((theme) => theme.key === launchTheme)) localStorage.setItem(themeStorageKey, launchTheme);
  if (["on", "off"].includes(launchEffects)) localStorage.setItem(themeEffectsStorageKey, launchEffects);
  applyVisualTheme();
  bindEvents();
  renderThemeOptions("settings-theme-options");
  byId("settings-theme-effects").checked = effectsEnabled();
  const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
  const requested = hash.get("section");
  if (requested && panelSections.has(requested)) state.activeSection = requested;
  switchSection(state.activeSection, false);
  if (await authenticate()) await loadInitialData();
}

boot();
