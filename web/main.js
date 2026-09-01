function readStorage(key, fallback = "") {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function maskTelegramToken(token) {
  if (!token || token.length < 8) {
    return "•••";
  }
  return `${token.slice(0, 4)}...${token.slice(-4)}`;
}

function writeStorage(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Ignore storage failures in restricted browser contexts.
  }
}

const state = {
  proxies: [],
  tasks: {},
  taskSockets: {},
  selectedTaskId: readStorage("selectedTaskId", ""),
  taskFilter: readStorage("taskFilter", "all"),
  taskSearch: readStorage("taskSearch", ""),
  taskRowsView: {
    task: null,
    rows: [],
    loading: false,
    error: "",
    page: 1,
    pageSize: 25,
    status: "all",
    search: "",
    total: 0,
    totalPages: 1,
    statusFilters: [],
    summary: {
      total: 0,
      processed: 0,
      success: 0,
      failed: 0,
      visible: 0,
    },
  },
  taskRowsRequestId: 0,
};

const steps = document.querySelectorAll(".step");
const tabButtons = document.querySelectorAll(".tab-btn");
const tabPanels = {
  proxies: document.getElementById("proxies-tab"),
  tasks: document.getElementById("tasks-tab"),
  "telegram-settings": document.getElementById("telegram-settings-tab"),
};

const taskSearchInput = document.getElementById("taskSearchInput");
const refreshTasksBtn = document.getElementById("refreshTasksBtn");
const taskFilterButtons = document.querySelectorAll("[data-task-filter]");
const runningTasksList = document.getElementById("runningTasksList");
const archiveTasksList = document.getElementById("archiveTasksList");
const runningCountLabel = document.getElementById("runningCountLabel");
const archiveCountLabel = document.getElementById("archiveCountLabel");
const tasksTotalCount = document.getElementById("tasksTotalCount");
const tasksActiveCount = document.getElementById("tasksActiveCount");
const tasksFinishedCount = document.getElementById("tasksFinishedCount");
const tasksFailedCount = document.getElementById("tasksFailedCount");
const taskDetailEmpty = document.getElementById("taskDetailEmpty");
const taskDetailBody = document.getElementById("taskDetailBody");
const selectedTaskName = document.getElementById("selectedTaskName");
const selectedTaskFile = document.getElementById("selectedTaskFile");
const selectedTaskStatus = document.getElementById("selectedTaskStatus");
const selectedTaskMeta = document.getElementById("selectedTaskMeta");
const taskRowsTotal = document.getElementById("taskRowsTotal");
const taskRowsProcessed = document.getElementById("taskRowsProcessed");
const taskRowsSuccess = document.getElementById("taskRowsSuccess");
const taskRowsFailed = document.getElementById("taskRowsFailed");
const taskRowsVisible = document.getElementById("taskRowsVisible");
const taskRowsPage = document.getElementById("taskRowsPage");
const taskRowsPageLabel = document.getElementById("taskRowsPageLabel");
const taskRowsPageSizeLabel = document.getElementById("taskRowsPageSizeLabel");
const taskVisibleRowsLabel = document.getElementById("taskVisibleRowsLabel");
const taskRowsSearchInput = document.getElementById("taskRowsSearchInput");
const taskStatusBreakdown = document.getElementById("taskStatusBreakdown");
const taskRowsTableBody = document.getElementById("taskRowsTableBody");
const taskRowsPrevBtn = document.getElementById("taskRowsPrevBtn");
const taskRowsNextBtn = document.getElementById("taskRowsNextBtn");
const taskJsonLink = document.getElementById("taskJsonLink");
const taskXlsxLink = document.getElementById("taskXlsxLink");
const telegramSettingsForm = document.getElementById("telegramSettingsForm");
const telegramSettingsChatIdInput = document.getElementById("telegramSettingsChatIdInput");
const telegramSettingsBotTokenInput = document.getElementById("telegramSettingsBotTokenInput");
const telegramSettingsStatus = document.getElementById("telegramSettingsStatus");
const taskRefreshDetailBtn = document.getElementById("taskRefreshDetailBtn");
const taskCopyIdBtn = document.getElementById("taskCopyIdBtn");
const taskCardTemplate = document.getElementById("task-card-template");

let activeTab = "proxies";
let taskRowsSearchDebounce = null;

if (telegramSettingsChatIdInput) {
  telegramSettingsChatIdInput.value = readStorage("telegramChatId", "");
}
if (telegramSettingsBotTokenInput) {
  telegramSettingsBotTokenInput.value = readStorage("telegramBotToken", "");
}

function hydrateTelegramDefaultsFromTask(task) {
  if (!task) return;
  if (telegramSettingsChatIdInput && !telegramSettingsChatIdInput.value && task.telegram_chat_id) {
    telegramSettingsChatIdInput.value = task.telegram_chat_id;
    writeStorage("telegramChatId", task.telegram_chat_id);
  }
  if (telegramSettingsBotTokenInput && !telegramSettingsBotTokenInput.value && task.telegram_bot_token) {
    telegramSettingsBotTokenInput.value = task.telegram_bot_token;
    writeStorage("telegramBotToken", task.telegram_bot_token);
  }
}

function setTelegramStatus(message, isError = false) {
  if (!telegramSettingsStatus) return;
  telegramSettingsStatus.textContent = message;
  telegramSettingsStatus.style.color = isError ? "#fca5a5" : "";
}

function setActiveTab(tabName) {
  activeTab = tabName;
  tabButtons.forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tabName));
  Object.entries(tabPanels).forEach(([key, panel]) => {
    panel.classList.toggle("active", key === tabName);
  });
}

tabButtons.forEach((btn) => {
  btn.addEventListener("click", () => setActiveTab(btn.dataset.tab));
});

function setStep(stepNumber) {
  steps.forEach((step) => {
    step.classList.toggle("active", Number(step.dataset.step) <= stepNumber);
  });
}

function isActiveTask(task) {
  return task.status === "queued" || task.status === "running";
}

function taskGroup(task) {
  if (isActiveTask(task)) return "active";
  if (task.status === "finished") return "finished";
  if (task.status === "failed") return "failed";
  return task.status || "queued";
}

function taskTimeValue(task) {
  const raw = task.updated_at || task.completed_at || task.started_at || task.created_at || "";
  const parsed = Date.parse(raw);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function formatDate(value) {
  if (!value) return "Unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("en-US", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function shortTaskId(taskId) {
  if (!taskId) return "";
  return taskId.length > 8 ? `${taskId.slice(0, 8)}…` : taskId;
}

function taskStatusLabel(status) {
  if (status === "queued") return "Queued";
  if (status === "running") return "Running";
  if (status === "finished") return "Finished";
  if (status === "failed") return "Failed";
  return status || "Queued";
}

function safeText(value, fallback = "—") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function taskMatchesSearch(task) {
  const query = state.taskSearch.trim().toLowerCase();
  if (!query) return true;
  const haystack = [
    task.name,
    task.filename,
    task.id,
    task.status,
    task.last_message,
    task.last_error,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return haystack.includes(query);
}

function taskMatchesFilter(task) {
  if (state.taskFilter === "all") return true;
  if (state.taskFilter === "active") return isActiveTask(task);
  if (state.taskFilter === "finished") return task.status === "finished";
  if (state.taskFilter === "failed") return task.status === "failed";
  return true;
}

function createEmptyState(title, description) {
  const empty = document.createElement("div");
  empty.className = "task-empty";

  const heading = document.createElement("strong");
  heading.textContent = title;

  const text = document.createElement("p");
  text.textContent = description;

  empty.appendChild(heading);
  empty.appendChild(text);
  return empty;
}

function syncTaskFilterButtons() {
  taskFilterButtons.forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.taskFilter === state.taskFilter);
  });
}

function setTaskFilter(filter) {
  state.taskFilter = filter;
  writeStorage("taskFilter", filter);
  syncTaskFilterButtons();
  renderTasks();
}

function setTaskSearch(value) {
  state.taskSearch = value;
  writeStorage("taskSearch", value);
  renderTasks();
}

function ensureSelectedTask() {
  if (state.selectedTaskId && state.tasks[state.selectedTaskId]) {
    return state.selectedTaskId;
  }

  const tasks = Object.values(state.tasks).sort((a, b) => taskTimeValue(b) - taskTimeValue(a));
  const fallback = tasks.find(isActiveTask) || tasks[0] || null;
  state.selectedTaskId = fallback ? fallback.id : "";

  writeStorage("selectedTaskId", state.selectedTaskId);

  return state.selectedTaskId;
}

function resetTaskRowsView() {
  state.taskRowsView.page = 1;
  state.taskRowsView.status = "all";
  state.taskRowsView.search = "";
  state.taskRowsView.rows = [];
  state.taskRowsView.total = 0;
  state.taskRowsView.totalPages = 1;
  state.taskRowsView.statusFilters = [];
  state.taskRowsView.summary = {
    total: 0,
    processed: 0,
    success: 0,
    failed: 0,
    visible: 0,
  };
  state.taskRowsView.loading = false;
  state.taskRowsView.error = "";

  if (taskRowsSearchInput) {
    taskRowsSearchInput.value = "";
  }

  if (taskStatusBreakdown) {
    taskStatusBreakdown.innerHTML = "";
  }
}

function selectTask(taskId, { resetView = true } = {}) {
  if (!taskId || !state.tasks[taskId]) return Promise.resolve();
  const changed = state.selectedTaskId !== taskId;
  state.selectedTaskId = taskId;
  writeStorage("selectedTaskId", taskId);
  if (resetView && changed) {
    resetTaskRowsView();
  }
  renderTasks();
  return loadTaskRows(taskId);
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `Request failed: ${response.status}`);
  }
  return response.json();
}

function renderProxyList() {
  const list = document.getElementById("proxiesList");
  const tpl = document.getElementById("proxy-row-template");
  list.innerHTML = "";

  state.proxies.forEach((proxy) => {
    const node = tpl.content.firstElementChild.cloneNode(true);
    node.querySelector(".proxy-raw").textContent = proxy.raw;
    node.querySelector(".proxy-meta").textContent = `${proxy.last_status || "unknown"}${proxy.last_ip ? ` | ${proxy.last_ip}` : ""}${proxy.last_checked_at ? ` | ${proxy.last_checked_at}` : ""}`;

    const checkBtn = node.querySelector(".check-btn");
    const delBtn = node.querySelector(".delete-btn");
    checkBtn.addEventListener("click", async () => {
      checkBtn.disabled = true;
      checkBtn.textContent = "Checking...";
      try {
        await api(`/api/proxies/${proxy.id}/check`, { method: "POST" });
        await loadState();
      } catch (err) {
        alert(err.message);
      } finally {
        checkBtn.disabled = false;
        checkBtn.textContent = "Check";
      }
    });

    delBtn.addEventListener("click", async () => {
      if (!confirm("Delete this proxy?")) return;
      await api(`/api/proxies/${proxy.id}`, { method: "DELETE" });
      await loadState();
    });

    if (!proxy.last_status || proxy.last_status === "unknown") {
      node.classList.remove("ok", "bad");
    }

    list.appendChild(node);
  });
}

function renderTaskList(container, tasks, emptyTitle, emptyDescription) {
  container.innerHTML = "";

  if (!tasks.length) {
    container.appendChild(createEmptyState(emptyTitle, emptyDescription));
    return;
  }

  tasks.forEach((task) => {
    const card = taskCardTemplate.content.firstElementChild.cloneNode(true);
    const total = task.total_rows || task.max_rows || 0;
    const processed = task.processed_rows || 0;
    const percent = total > 0 ? Math.round((processed / total) * 100) : (task.status === "finished" || task.status === "failed" ? 100 : 0);
    const selected = task.id === state.selectedTaskId;

    card.dataset.taskId = task.id;
    card.dataset.status = task.status || "queued";
    card.classList.toggle("selected", selected);
    card.setAttribute("aria-pressed", selected ? "true" : "false");

    card.querySelector(".task-card__eyebrow").textContent = `${taskGroup(task) === "active" ? "Current task" : "Archive"} · ${formatDate(task.updated_at || task.completed_at || task.started_at || task.created_at)}`;
    card.querySelector(".task-name").textContent = task.name || task.filename || task.id;
    card.querySelector(".badge").textContent = taskStatusLabel(task.status);
    card.querySelector(".badge").setAttribute("data-state", task.status || "queued");
    card.querySelector(".task-card__meta").textContent = `${task.filename} · ID ${shortTaskId(task.id)} · ${task.use_proxy ? "with proxies" : "without proxies"} · ${task.proxy_count || 0} proxies`;
    card.querySelector(".task-card__stats").textContent = `Processed: ${processed}${total ? ` / ${total}` : ""} | OK: ${task.ok_rows || 0} | Errors: ${task.error_rows || 0} | Row: ${task.current_row || 0}`;
    card.querySelector(".task-message").textContent = task.last_message || task.last_error || "";
    card.querySelector(".progress-bar").style.width = `${Math.min(percent, 100)}%`;

    card.addEventListener("click", () => {
      selectTask(task.id).catch((err) => alert(err.message));
    });

    container.appendChild(card);
  });
}

function renderTasks() {
  const allTasks = Object.values(state.tasks).sort((a, b) => taskTimeValue(b) - taskTimeValue(a));
  const visibleTasks = allTasks.filter((task) => taskMatchesSearch(task) && taskMatchesFilter(task));
  const activeTasks = visibleTasks.filter(isActiveTask);
  const archiveTasks = visibleTasks.filter((task) => !isActiveTask(task));

  tasksTotalCount.textContent = String(allTasks.length);
  tasksActiveCount.textContent = String(allTasks.filter(isActiveTask).length);
  tasksFinishedCount.textContent = String(allTasks.filter((task) => task.status === "finished").length);
  tasksFailedCount.textContent = String(allTasks.filter((task) => task.status === "failed").length);

  runningCountLabel.textContent = String(activeTasks.length);
  archiveCountLabel.textContent = String(archiveTasks.length);

  syncTaskFilterButtons();

  if (!visibleTasks.length) {
    runningTasksList.innerHTML = "";
    archiveTasksList.innerHTML = "";
    const description = state.taskSearch.trim()
      ? "Try a different query or clear the search."
      : "Try another status filter.";
    runningTasksList.appendChild(createEmptyState("Nothing found", description));
    setStep(allTasks.length > 0 ? 3 : 2);
    return;
  }

  renderTaskList(
    runningTasksList,
    activeTasks,
    "No active tasks",
    "Tasks that are queued or running will appear here."
  );
  renderTaskList(
    archiveTasksList,
    archiveTasks,
    "Archive is empty",
    "Finished and failed tasks will appear here."
  );

  setStep(allTasks.length > 0 ? 3 : 2);
}

function formatRowStatusLabel(row) {
  if (row.status_label) return row.status_label;
  if (row.status_key === "queued") return "Queued";
  if (row.status_key === "retry") return "Retry";
  if (row.status_key === "registered") return "Registered";
  if (row.status_key === "unregistered") return "Unregistered";
  if (row.ok) return "OK";
  if (row.status_key === "unknown" || row.status === null || row.status === undefined || row.status === "") return "Unknown";
  return `HTTP ${row.status}`;
}

function rowStatusClass(row) {
  if (row.status_key === "ok" || row.status_key === "registered" || row.ok) return "is-ok";
  if (row.status_key === "queued") return "is-queued";
  if (row.status_key === "retry") return "is-retry";
  if (row.status_key === "unknown") return "is-unknown";
  if (row.status_key === "unregistered") return "is-bad";
  return "is-bad";
}

function renderStatusBreakdown(statusFilters) {
  if (!taskStatusBreakdown) return;
  taskStatusBreakdown.innerHTML = "";

  const chips = [
    { key: "all", label: "All statuses", count: state.taskRowsView.summary?.total || state.taskRowsView.task?.total_rows || state.taskRowsView.total },
    ...(statusFilters || []),
  ];

  if (!chips.length) {
    taskStatusBreakdown.appendChild(
      createEmptyState("No statuses", "Wait for the task rows to load.")
    );
    return;
  }

  chips.forEach((filter) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chip";
    button.classList.toggle("active", filter.key === state.taskRowsView.status);
    button.textContent = `${filter.label}${typeof filter.count === "number" ? `: ${filter.count}` : ""}`;
    button.addEventListener("click", () => {
      if (filter.key === state.taskRowsView.status) return;
      state.taskRowsView.status = filter.key;
      state.taskRowsView.page = 1;
      loadTaskRows(state.selectedTaskId);
    });
    taskStatusBreakdown.appendChild(button);
  });
}

function renderTaskRowsTable(rows) {
  taskRowsTableBody.innerHTML = "";

  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6;
    td.appendChild(
      createEmptyState(
        "No rows",
        state.taskRowsView.loading ? "Loading task data..." : "Try changing the status or search."
      )
    );
    tr.appendChild(td);
    taskRowsTableBody.appendChild(tr);
    return;
  }

  const fragment = document.createDocumentFragment();
  rows.forEach((row) => {
    const tr = document.createElement("tr");

    const rowCell = document.createElement("td");
    rowCell.innerHTML = `<strong>${safeText(row.row)}</strong>${
      row.source_row && row.source_row !== row.row ? `<div class="muted">source ${safeText(row.source_row)}</div>` : ""
    }`;

    const birthCell = document.createElement("td");
    birthCell.textContent = row.birthDate || row.birth_date || "—";

    const ssnCell = document.createElement("td");
    ssnCell.className = "mono";
    ssnCell.textContent = row.ssn || row.payload_ssn || "—";

    const statusCell = document.createElement("td");
    const statusBadge = document.createElement("span");
    statusBadge.className = `row-badge ${rowStatusClass(row)}`;
    statusBadge.textContent = formatRowStatusLabel(row);
    statusCell.appendChild(statusBadge);

    const sessionCell = document.createElement("td");
    sessionCell.className = "mono";
    sessionCell.textContent = row.session || row.session_cookie || "—";

    const messageCell = document.createElement("td");
    messageCell.className = "rows-table__message";
    messageCell.textContent = row.message || row.body_preview || row.last_error || "—";

    tr.appendChild(rowCell);
    tr.appendChild(birthCell);
    tr.appendChild(ssnCell);
    tr.appendChild(statusCell);
    tr.appendChild(sessionCell);
    tr.appendChild(messageCell);
    fragment.appendChild(tr);
  });

  taskRowsTableBody.appendChild(fragment);
}

function setActionLink(link, href, enabled) {
  if (!link) return;
  link.href = enabled ? href : "#";
  link.classList.toggle("is-disabled", !enabled);
  link.setAttribute("aria-disabled", enabled ? "false" : "true");
}

function renderTaskInspector() {
  const view = state.taskRowsView;
  const task = view.task || (state.selectedTaskId ? state.tasks[state.selectedTaskId] : null);

  if (!task) {
    taskDetailEmpty.classList.remove("is-hidden");
    taskDetailBody.classList.add("is-hidden");
    return;
  }

  taskDetailEmpty.classList.add("is-hidden");
  taskDetailBody.classList.remove("is-hidden");
  taskDetailBody.classList.toggle("is-loading", view.loading);

  selectedTaskName.textContent = task.name || task.filename || task.id;
  selectedTaskFile.textContent = `${task.filename} · ID ${task.id}`;
  selectedTaskStatus.textContent = taskStatusLabel(task.status);
  selectedTaskStatus.setAttribute("data-state", task.status || "queued");

  selectedTaskMeta.innerHTML = "";
  const metaPills = [
    `Created ${formatDate(task.created_at)}`,
    task.started_at ? `Started ${formatDate(task.started_at)}` : null,
    task.completed_at ? `Finished ${formatDate(task.completed_at)}` : null,
    view.loading ? "Refreshing rows..." : null,
    `Pause ${task.pause_min}s–${task.pause_max}s`,
    task.use_proxy ? `Proxies ${task.proxy_count || 0}` : "No proxies",
    task.max_rows ? `Max rows ${task.max_rows}` : "No limit",
    task.telegram_chat_id ? `Telegram chat ${task.telegram_chat_id}` : null,
    task.telegram_bot_token ? `Telegram token ${maskTelegramToken(task.telegram_bot_token)}` : null,
    view.error ? `Update error: ${view.error}` : null,
  ].filter(Boolean);

  metaPills.forEach((text) => {
    const pill = document.createElement("span");
    pill.className = "meta-pill";
    pill.textContent = text;
    selectedTaskMeta.appendChild(pill);
  });

  const summary = view.summary || {};
  taskRowsTotal.textContent = String(summary.total || task.total_rows || 0);
  taskRowsProcessed.textContent = String(summary.processed || task.processed_rows || 0);
  taskRowsSuccess.textContent = String(summary.success || task.ok_rows || 0);
  taskRowsFailed.textContent = String(summary.failed || task.error_rows || 0);
  taskRowsVisible.textContent = String(summary.visible ?? view.total ?? 0);
  taskRowsPage.textContent = `${view.page || 1} / ${view.totalPages || 1}`;
  taskRowsPageLabel.textContent = `Page ${view.page || 1} / ${view.totalPages || 1}`;
  taskRowsPageSizeLabel.textContent = `${view.pageSize || 25} / page`;
  taskVisibleRowsLabel.textContent = `Visible rows: ${summary.visible ?? view.total ?? 0}`;
  renderStatusBreakdown(view.statusFilters || []);
  renderTaskRowsTable(view.rows || []);

  if (taskRowsPrevBtn) {
    taskRowsPrevBtn.disabled = view.loading || (view.page || 1) <= 1;
  }
  if (taskRowsNextBtn) {
    taskRowsNextBtn.disabled = view.loading || (view.page || 1) >= (view.totalPages || 1);
  }

  setActionLink(taskJsonLink, `/api/tasks/${task.id}/result/json`, Boolean(task.result_file_json));
  setActionLink(taskXlsxLink, `/api/tasks/${task.id}/result/xlsx`, Boolean(task.result_file_json));
}

async function loadTaskRows(taskId, { silent = false } = {}) {
  if (!taskId) {
    state.taskRowsView = {
      task: null,
      rows: [],
      loading: false,
      error: "",
      page: 1,
      pageSize: 25,
      status: "all",
      search: "",
      total: 0,
      totalPages: 1,
      statusFilters: [],
      summary: {
        total: 0,
        processed: 0,
        success: 0,
        failed: 0,
        visible: 0,
      },
    };
    renderTaskInspector();
    return;
  }

  const requestId = ++state.taskRowsRequestId;
  const view = state.taskRowsView;
  const currentTask = state.tasks[taskId] || view.task || null;
  const query = new URLSearchParams({
    page: String(view.page || 1),
    page_size: String(view.pageSize || 25),
    status: view.status || "all",
    search: view.search || "",
  });

  state.taskRowsView = {
    ...view,
    task: currentTask,
    loading: true,
    error: "",
  };
  if (!silent) {
    renderTaskInspector();
  }

  try {
    const data = await api(`/api/tasks/${taskId}/rows?${query.toString()}`);

    if (requestId !== state.taskRowsRequestId) return;

    state.taskRowsView = {
      ...state.taskRowsView,
      task: data.task || currentTask,
      rows: data.items || [],
      page: data.page || 1,
      pageSize: data.page_size || view.pageSize || 25,
      total: data.total || 0,
      totalPages: data.total_pages || 1,
      statusFilters: data.status_filters || [],
      summary: data.summary || state.taskRowsView.summary,
      loading: false,
      error: "",
    };
    renderTaskInspector();
  } catch (err) {
    if (requestId !== state.taskRowsRequestId) return;

    state.taskRowsView = {
      ...state.taskRowsView,
      loading: false,
      error: err.message,
    };
    renderTaskInspector();
  }
}

async function refreshSelectedTaskRows(silent = true) {
  const taskId = ensureSelectedTask();
  if (!taskId) {
    renderTaskInspector();
    return;
  }
  await loadTaskRows(taskId, { silent });
}

async function loadProxies() {
  const data = await api("/api/proxies");
  state.proxies = data.items || [];
}

async function loadTasks() {
  const data = await api("/api/tasks?limit=0");
  const items = data.items || [];
  const map = {};
  items.forEach((item) => {
    map[item.id] = item;
  });
  const previousSelectedTaskId = state.selectedTaskId;
  state.tasks = map;
  const latestTask = Object.values(state.tasks).sort((a, b) => taskTimeValue(b) - taskTimeValue(a))[0] || null;
  hydrateTelegramDefaultsFromTask(latestTask);

  ensureSelectedTask();
  if (previousSelectedTaskId && previousSelectedTaskId !== state.selectedTaskId) {
    resetTaskRowsView();
  }
  renderTasks();
  await refreshSelectedTaskRows(true);
}

async function loadTelegramSettings() {
  if (telegramSettingsChatIdInput && !telegramSettingsChatIdInput.value) {
    telegramSettingsChatIdInput.value = readStorage("telegramChatId", "");
  }
  if (telegramSettingsBotTokenInput && !telegramSettingsBotTokenInput.value) {
    telegramSettingsBotTokenInput.value = readStorage("telegramBotToken", "");
  }

  try {
    const data = await api("/api/settings/telegram");
    if (telegramSettingsChatIdInput && data?.telegram_chat_id) {
      telegramSettingsChatIdInput.value = data.telegram_chat_id;
      writeStorage("telegramChatId", data.telegram_chat_id);
    }
    if (telegramSettingsBotTokenInput && data?.telegram_bot_token) {
      telegramSettingsBotTokenInput.value = data.telegram_bot_token;
      writeStorage("telegramBotToken", data.telegram_bot_token);
    }
    if (data?.telegram_chat_id || data?.telegram_bot_token) {
      setTelegramStatus("Loaded saved Telegram settings.");
    }
  } catch (error) {
    setTelegramStatus(error.message ? `Could not load settings: ${error.message}` : "Could not load settings");
  }
}

async function loadState() {
  await Promise.all([loadProxies(), loadTasks(), loadTelegramSettings()]);
  renderProxyList();
}

document.getElementById("proxyAddForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const raw = document.getElementById("proxyRawInput").value.trim();
  if (!raw) return;
  const form = new FormData();
  form.append("raw", raw);
  await api("/api/proxies", { method: "POST", body: form });
  document.getElementById("proxyRawInput").value = "";
  await loadState();
});

document.getElementById("checkAllBtn").addEventListener("click", async () => {
  for (const proxy of state.proxies) {
    await api(`/api/proxies/${proxy.id}/check`, { method: "POST" });
  }
  await loadState();
});

document.getElementById("taskCreateForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const fd = new FormData(formElement);

  const payloadFile = fd.get("payloadFile");
  if (!payloadFile || payloadFile.size === 0) {
    alert("Upload a .xlsx spreadsheet");
    return;
  }

  const telegramChatId = telegramSettingsChatIdInput ? telegramSettingsChatIdInput.value.trim() : "";
  const telegramBotToken = telegramSettingsBotTokenInput ? telegramSettingsBotTokenInput.value.trim() : "";
  if (telegramChatId) {
    fd.set("telegramChatId", telegramChatId);
    writeStorage("telegramChatId", telegramChatId);
  }
  if (telegramBotToken) {
    fd.set("telegramBotToken", telegramBotToken);
    writeStorage("telegramBotToken", telegramBotToken);
  }

  try {
    const task = await api("/api/tasks", {
      method: "POST",
      body: fd,
    });

    if (task?.id) {
      state.selectedTaskId = task.id;
      writeStorage("selectedTaskId", task.id);
    }
    if (task?.telegram_chat_id) {
      writeStorage("telegramChatId", task.telegram_chat_id);
    }
    if (task?.telegram_bot_token) {
      writeStorage("telegramBotToken", task.telegram_bot_token);
    }
    hydrateTelegramDefaultsFromTask(task);
  } catch (err) {
    alert(err.message);
    return;
  }

  formElement.reset();
  setActiveTab("tasks");
  setStep(3);
  await loadState();
});

if (telegramSettingsForm) {
  telegramSettingsForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);

    const chatId = telegramSettingsChatIdInput ? telegramSettingsChatIdInput.value.trim() : "";
    const botToken = telegramSettingsBotTokenInput ? telegramSettingsBotTokenInput.value.trim() : "";
    formData.set("telegramChatId", chatId);
    formData.set("telegramBotToken", botToken);

    try {
      const response = await api("/api/settings/telegram", {
        method: "POST",
        body: formData,
      });

      if (response?.telegram_chat_id) {
        writeStorage("telegramChatId", response.telegram_chat_id);
      } else {
        writeStorage("telegramChatId", "");
      }
      if (response?.telegram_bot_token) {
        writeStorage("telegramBotToken", response.telegram_bot_token);
      } else {
        writeStorage("telegramBotToken", "");
      }
      setTelegramStatus("Telegram settings saved.");
    } catch (error) {
      setTelegramStatus(error.message || "Failed to save settings", true);
    }
  });
}

if (taskSearchInput) {
  taskSearchInput.value = state.taskSearch;
  taskSearchInput.addEventListener("input", (event) => {
    setTaskSearch(event.target.value);
  });
}

if (taskRowsSearchInput) {
  taskRowsSearchInput.value = state.taskRowsView.search;
  taskRowsSearchInput.addEventListener("input", (event) => {
    state.taskRowsView.search = event.target.value;
    state.taskRowsView.page = 1;
    if (taskRowsSearchDebounce) {
      window.clearTimeout(taskRowsSearchDebounce);
    }
    taskRowsSearchDebounce = window.setTimeout(() => {
      taskRowsSearchDebounce = null;
      const taskId = ensureSelectedTask();
      if (!taskId) return;
      loadTaskRows(taskId).catch((err) => alert(err.message));
    }, 220);
  });
}

taskFilterButtons.forEach((button) => {
  button.addEventListener("click", () => setTaskFilter(button.dataset.taskFilter));
});

if (refreshTasksBtn) {
  refreshTasksBtn.addEventListener("click", async () => {
    try {
      await loadTasks();
    } catch (err) {
      alert(err.message);
    }
  });
}

if (taskRefreshDetailBtn) {
  taskRefreshDetailBtn.addEventListener("click", async () => {
    const taskId = ensureSelectedTask();
    if (!taskId) return;
    await loadTaskRows(taskId);
  });
}

if (taskRowsPrevBtn) {
  taskRowsPrevBtn.addEventListener("click", async () => {
    const taskId = ensureSelectedTask();
    if (!taskId || state.taskRowsView.loading) return;
    state.taskRowsView.page = Math.max(1, (state.taskRowsView.page || 1) - 1);
    await loadTaskRows(taskId);
  });
}

if (taskRowsNextBtn) {
  taskRowsNextBtn.addEventListener("click", async () => {
    const taskId = ensureSelectedTask();
    if (!taskId || state.taskRowsView.loading) return;
    state.taskRowsView.page = Math.min((state.taskRowsView.totalPages || 1), (state.taskRowsView.page || 1) + 1);
    await loadTaskRows(taskId);
  });
}

if (taskCopyIdBtn) {
  taskCopyIdBtn.addEventListener("click", async () => {
    const taskId = ensureSelectedTask();
    if (!taskId) return;
    try {
      await navigator.clipboard.writeText(taskId);
      taskCopyIdBtn.textContent = "Copied";
      window.setTimeout(() => {
        taskCopyIdBtn.textContent = "Copy ID";
      }, 1200);
    } catch {
      alert(taskId);
    }
  });
}

setInterval(() => {
  loadTasks().catch(() => {});
}, 4000);

setActiveTab("proxies");
syncTaskFilterButtons();
loadState().then(() => {
  if (!state.selectedTaskId) {
    setStep(1);
  }
});
