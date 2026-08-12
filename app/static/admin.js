const state = {
  profile: null,
  modelName: "mock",
  knowledgeJobs: [],
  knowledgePollTimer: null,
  knowledgeJobsLoading: false
};

const els = {
  serviceState: document.querySelector("#serviceState"),
  modelState: document.querySelector("#modelState"),
  activeAccount: document.querySelector("#activeAccount"),
  switchAccount: document.querySelector("#switchAccount"),
  refreshAdmin: document.querySelector("#refreshAdmin"),
  metricReports: document.querySelector("#metricReports"),
  metricHigh: document.querySelector("#metricHigh"),
  metricCases: document.querySelector("#metricCases"),
  metricExcel: document.querySelector("#metricExcel"),
  metricAlerts: document.querySelector("#metricAlerts"),
  cases: document.querySelector("#cases"),
  casesCount: document.querySelector("#casesCount"),
  reports: document.querySelector("#reports"),
  reportsCount: document.querySelector("#reportsCount"),
  conversationState: document.querySelector("#conversationState"),
  conversationDetail: document.querySelector("#conversationDetail"),
  knowledgeState: document.querySelector("#knowledgeState"),
  knowledgeUploadForm: document.querySelector("#knowledgeUploadForm"),
  knowledgeFile: document.querySelector("#knowledgeFile"),
  knowledgeSubmit: document.querySelector("#knowledgeSubmit"),
  knowledgeVisionAllowed: document.querySelector("#knowledgeVisionAllowed"),
  knowledgeUploadState: document.querySelector("#knowledgeUploadState"),
  knowledgeJobs: document.querySelector("#knowledgeJobs"),
  knowledgeJobsCount: document.querySelector("#knowledgeJobsCount"),
  refreshKnowledgeJobs: document.querySelector("#refreshKnowledgeJobs"),
  rebuildVector: document.querySelector("#rebuildVector"),
  backupVector: document.querySelector("#backupVector")
};

const knowledgeUI = window.MindBridgeKnowledgeUI;

function csrfToken() {
  const cookie = document.cookie.split("; ").find((item) => item.startsWith("mindbridge_csrf="));
  return cookie ? decodeURIComponent(cookie.split("=", 2)[1]) : "";
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (["POST", "PUT", "PATCH", "DELETE"].includes((options.method || "GET").toUpperCase())) {
    headers["X-CSRF-Token"] = csrfToken();
  }
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  if (response.status === 401) {
    window.location.replace("/");
    throw new Error("登录状态已失效");
  }
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  return response;
}

function setPill(el, text, tone = "ok") {
  el.textContent = text;
  el.className = `pill ${tone}`;
}

function isAdmin(profile) {
  return profile.roles?.some((role) => role.authority === "ROLE_ADMIN");
}

function displayModel(model) {
  return (model || "").includes("mindbridge-qwen2.5-7b-ft") ? "微调 Qwen2.5-7B" : model;
}

function displayTime(value) {
  return value ? new Date(value).toLocaleString() : "";
}

function roleLabel(role) {
  const value = (role || "").toUpperCase();
  if (value === "USER") return "学生";
  if (value === "ASSISTANT") return "MindBridge";
  if (value === "SYSTEM") return "系统";
  return role || "未知角色";
}

async function checkHealth() {
  try {
    const response = await fetch("/actuator/health", { credentials: "same-origin" });
    const body = await response.json();
    setPill(els.serviceState, body.status === "UP" ? "服务正常" : `服务 ${body.status}`, body.status === "UP" ? "ok" : "danger");
  } catch {
    setPill(els.serviceState, "服务 DOWN", "danger");
  }
}

async function loadProfile() {
  try {
    const response = await api("/api/profile");
    const profile = await response.json();
    if (!isAdmin(profile)) {
      window.location.replace("/student.html");
      return null;
    }
    state.profile = profile;
    els.activeAccount.textContent = profile.displayName || profile.username;
    return profile;
  } catch {
    window.location.replace("/");
    return null;
  }
}

async function loadAgentStatus() {
  const response = await api("/api/agent/status");
  const status = await response.json();
  state.modelName = status.model || "mock";
  if (status.realModelEnabled) {
    setPill(els.modelState, `${status.provider} / ${displayModel(state.modelName)}`, "ok");
  } else {
    setPill(els.modelState, "mock 演示", "warn");
  }
}

async function loadAdminDashboard() {
  const [reportsRes, casesRes, excelRes, alertsRes] = await Promise.all([
    api("/api/admin/reports"),
    api("/api/admin/cases"),
    api("/api/admin/excel-records"),
    api("/api/admin/alerts")
  ]);
  const reports = await reportsRes.json();
  const cases = await casesRes.json();
  const excel = await excelRes.json();
  const alerts = await alertsRes.json();
  els.metricReports.textContent = reports.length;
  els.metricHigh.textContent = reports.filter((item) => item.riskLevel === "HIGH").length;
  els.metricCases.textContent = cases.length;
  els.metricExcel.textContent = excel.length;
  els.metricAlerts.textContent = alerts.length;
  els.casesCount.textContent = `${cases.length} 条`;
  els.reportsCount.textContent = `${reports.length} 条`;
  renderCases(cases);
  renderReports(reports);
}

async function loadKnowledgeStatus() {
  try {
    const response = await api("/api/admin/knowledge/status");
    const status = await response.json();
    const vector = status.vectorAvailable ? `向量 ${status.vectorChunks ?? 0}` : "向量不可用";
    els.knowledgeState.textContent = `DB ${status.databaseChunks} 片段 · ${vector}`;
  } catch (error) {
    els.knowledgeState.textContent = `读取失败：${error.message}`;
  }
}

function renderCases(cases) {
  els.cases.innerHTML = "";
  if (!cases.length) {
    els.cases.innerHTML = `<div class="empty small"><strong>暂无个案</strong><p>中高风险报告会自动创建风险个案。</p></div>`;
    return;
  }
  for (const item of cases) {
    const card = document.createElement("article");
    card.className = `case-card risk-${item.riskLevel.toLowerCase()}`;

    const head = document.createElement("div");
    head.className = "case-head";
    const title = document.createElement("strong");
    title.textContent = `个案 #${item.id} · ${item.status}`;
    const time = document.createElement("span");
    time.textContent = displayTime(item.updatedAt);
    head.append(title, time);

    const meta = document.createElement("small");
    meta.textContent = `报告 #${item.reportId} · ${item.riskLevel} · 负责人 ${item.owner || "未分配"}`;
    const summary = document.createElement("p");
    summary.textContent = item.summary || "";
    const handoff = document.createElement("pre");
    handoff.textContent = item.handoffSummary || "";

    card.append(head, meta, summary, handoff);
    els.cases.append(card);
  }
}

function renderReports(reports) {
  els.reports.innerHTML = "";
  if (!reports.length) {
    els.reports.innerHTML = `<div class="empty small"><strong>暂无报告</strong><p>学生咨询或风险场景会在这里沉淀记录。</p></div>`;
    return;
  }
  for (const item of reports) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = `report risk-${item.riskLevel.toLowerCase()}`;
    card.dataset.sessionId = item.sessionId;

    const head = document.createElement("div");
    head.className = "report-head";
    const title = document.createElement("strong");
    title.textContent = `${item.displayName} · ${item.riskLevel}`;
    const time = document.createElement("span");
    time.textContent = displayTime(item.createdAt);
    head.append(title, time);

    const summary = document.createElement("p");
    summary.textContent = item.summary;
    const content = document.createElement("small");
    content.textContent = item.content;
    const action = document.createElement("span");
    action.className = "report-action";
    action.textContent = "查看会话档案";

    card.append(head, summary, content, action);
    card.addEventListener("click", () => loadConversation(item.sessionId));
    els.reports.append(card);
  }
}

async function loadConversation(sessionId) {
  if (!sessionId) {
    els.conversationState.textContent = "该报告缺少会话 ID";
    return;
  }
  window.MindBridgeAdminPanels.open(document, "archive", localStorage, { scroll: true });
  els.conversationState.textContent = "正在读取...";
  els.conversationDetail.innerHTML = `<div class="empty small"><strong>加载中</strong><p>正在读取历史消息。</p></div>`;
  for (const card of els.reports.querySelectorAll(".report")) {
    card.classList.toggle("active", card.dataset.sessionId === sessionId);
  }
  try {
    const response = await api(`/api/admin/conversations/${encodeURIComponent(sessionId)}`);
    const conversation = await response.json();
    renderConversation(conversation);
  } catch (error) {
    els.conversationState.textContent = "读取失败";
    els.conversationDetail.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty small";
    const title = document.createElement("strong");
    title.textContent = "无法查看档案";
    const detail = document.createElement("p");
    detail.textContent = error.message;
    empty.append(title, detail);
    els.conversationDetail.append(empty);
  }
}

function renderConversation(conversation) {
  const messages = conversation.messages || [];
  els.conversationState.textContent = `${conversation.title || conversation.sessionId} · ${messages.length} 条消息`;
  els.conversationDetail.innerHTML = "";
  if (!messages.length) {
    els.conversationDetail.innerHTML = `<div class="empty small"><strong>暂无消息</strong><p>这个会话还没有写入消息记录。</p></div>`;
    return;
  }
  for (const message of messages) {
    const role = (message.role || "").toLowerCase();
    const row = document.createElement("article");
    row.className = `conversation-message ${role}`;

    const meta = document.createElement("div");
    meta.className = "conversation-meta";
    const label = document.createElement("strong");
    label.textContent = roleLabel(message.role);
    const time = document.createElement("span");
    time.textContent = displayTime(message.createdAt);
    meta.append(label, time);

    const bubble = document.createElement("div");
    bubble.className = "conversation-bubble";
    bubble.textContent = message.content || "";

    row.append(meta, bubble);
    els.conversationDetail.append(row);
  }
}

async function uploadKnowledgeFile(event) {
  event.preventDefault();
  const file = els.knowledgeFile.files?.[0];
  if (!file) {
    els.knowledgeUploadState.textContent = "请先选择文件";
    return;
  }
  const data = new FormData();
  data.append("file", file);
  const allowVision = els.knowledgeVisionAllowed.checked;
  const endpoint = `/api/admin/knowledge/files?cloudVisionAllowed=${allowVision}`;
  els.knowledgeSubmit.disabled = true;
  els.knowledgeUploadState.className = "knowledge-notice running";
  els.knowledgeUploadState.textContent = `正在上传 ${file.name}；上传结束后页面不会等待解析...`;
  try {
    const response = await api(endpoint, { method: "POST", body: data });
    const result = await response.json();
    els.knowledgeUploadState.className = "knowledge-notice accepted";
    els.knowledgeUploadState.textContent = `${file.name} 已提交后台处理（任务 ${result.jobId}）。你可以继续其他操作。`;
    els.knowledgeFile.value = "";
    await loadKnowledgeJobs({ immediate: true });
  } catch (error) {
    els.knowledgeUploadState.className = "knowledge-notice failed";
    els.knowledgeUploadState.textContent = `上传失败：${error.message}`;
  } finally {
    els.knowledgeSubmit.disabled = false;
  }
}

function clearKnowledgePoll() {
  if (state.knowledgePollTimer) {
    window.clearTimeout(state.knowledgePollTimer);
    state.knowledgePollTimer = null;
  }
}

function scheduleKnowledgePoll() {
  clearKnowledgePoll();
  if (document.hidden || !knowledgeUI.shouldPoll(state.knowledgeJobs)) return;
  state.knowledgePollTimer = window.setTimeout(() => loadKnowledgeJobs(), 3000);
}

async function loadKnowledgeJobs(options = {}) {
  if (state.knowledgeJobsLoading) return;
  state.knowledgeJobsLoading = true;
  if (options.immediate) clearKnowledgePoll();
  try {
    const response = await api("/api/admin/knowledge/jobs?limit=50");
    state.knowledgeJobs = await response.json();
    renderKnowledgeJobs(state.knowledgeJobs);
    if (!knowledgeUI.shouldPoll(state.knowledgeJobs)) loadKnowledgeStatus();
  } catch (error) {
    els.knowledgeJobs.innerHTML = "";
    const message = document.createElement("div");
    message.className = "empty small";
    const title = document.createElement("strong");
    title.textContent = "任务读取失败";
    const detail = document.createElement("p");
    detail.textContent = error.message;
    message.append(title, detail);
    els.knowledgeJobs.append(message);
  } finally {
    state.knowledgeJobsLoading = false;
    scheduleKnowledgePoll();
  }
}

function renderKnowledgeJobs(jobs) {
  els.knowledgeJobs.innerHTML = "";
  const activeCount = jobs.filter((job) => ["PENDING", "RUNNING"].includes(job.status)).length;
  els.knowledgeJobsCount.textContent = activeCount ? `${activeCount} 个处理中` : `${jobs.length} 个任务`;
  if (!jobs.length) {
    els.knowledgeJobs.innerHTML = `<div class="empty small"><strong>暂无入库任务</strong><p>上传文档后，解析与索引进度会显示在这里。</p></div>`;
    return;
  }
  for (const job of jobs) {
    els.knowledgeJobs.append(createKnowledgeJobCard(job));
  }
}

function createKnowledgeJobCard(job) {
  const meta = knowledgeUI.statusMeta(job.status);
  const article = document.createElement("article");
  article.className = `knowledge-job ${meta.tone}`;

  const head = document.createElement("div");
  head.className = "knowledge-job-head";
  const identity = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = job.displayName || "未命名文档";
  const id = document.createElement("small");
  id.textContent = `${job.jobId} · ${displayTime(job.createdAt)}`;
  identity.append(title, id);
  const badge = document.createElement("span");
  badge.className = `knowledge-job-status ${meta.tone}`;
  badge.textContent = meta.label;
  head.append(identity, badge);

  const progress = document.createElement("div");
  progress.className = `knowledge-progress ${job.totalPages ? "" : "indeterminate"}`.trim();
  const bar = document.createElement("span");
  bar.style.width = `${knowledgeUI.progressPercent(job)}%`;
  progress.append(bar);

  const stage = document.createElement("div");
  stage.className = "knowledge-job-stage";
  const label = document.createElement("span");
  label.textContent = knowledgeUI.progressLabel(job);
  const attempts = document.createElement("span");
  attempts.textContent = job.attempts ? `尝试 ${job.attempts} 次` : "尚未开始";
  stage.append(label, attempts);

  article.append(head, progress, stage);
  if (job.error) {
    const error = document.createElement("p");
    error.className = "knowledge-job-error";
    error.textContent = `${job.error.code || "处理异常"}：${job.error.message || "请稍后重试"}`;
    article.append(error);
  }
  if (knowledgeUI.canRetry(job)) {
    const actions = document.createElement("div");
    actions.className = "knowledge-job-actions";
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "ghost compact";
    retry.textContent = knowledgeUI.retryNeedsVision(job) ? "授权 Vision 并重试" : "重新处理";
    retry.addEventListener("click", () => retryKnowledgeJob(job, retry));
    actions.append(retry);
    article.append(actions);
  }
  return article;
}

async function retryKnowledgeJob(job, button) {
  button.disabled = true;
  const query = knowledgeUI.retryNeedsVision(job) ? "?cloudVisionAllowed=true" : "";
  els.knowledgeUploadState.className = "knowledge-notice running";
  els.knowledgeUploadState.textContent = `正在重新提交 ${job.displayName}...`;
  try {
    const response = await api(`/api/admin/knowledge/jobs/${encodeURIComponent(job.jobId)}/retry${query}`, {
      method: "POST"
    });
    const result = await response.json();
    els.knowledgeUploadState.className = "knowledge-notice accepted";
    els.knowledgeUploadState.textContent = `${job.displayName} 已重新进入队列（任务 ${result.jobId}）。`;
    await loadKnowledgeJobs({ immediate: true });
  } catch (error) {
    els.knowledgeUploadState.className = "knowledge-notice failed";
    els.knowledgeUploadState.textContent = `重试失败：${error.message}`;
    button.disabled = false;
  }
}

async function runKnowledgeAction(kind) {
  const isBackup = kind === "backup";
  const button = isBackup ? els.backupVector : els.rebuildVector;
  const endpoint = isBackup ? "/api/admin/knowledge/backup" : "/api/admin/knowledge/rebuild-vector";
  const original = button.textContent;
  button.disabled = true;
  els.knowledgeUploadState.textContent = isBackup ? "正在备份向量索引..." : "正在重建向量索引...";
  try {
    const response = await api(endpoint, { method: "POST" });
    const result = await response.json();
    els.knowledgeUploadState.textContent = isBackup
      ? `备份完成：${result.snapshot}`
      : `重建完成：${result.indexedChunks} 个片段`;
    loadKnowledgeStatus();
  } catch (error) {
    els.knowledgeUploadState.textContent = `操作失败：${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } finally {
    window.location.assign("/");
  }
}

els.switchAccount.addEventListener("click", logout);
els.refreshAdmin.addEventListener("click", () => {
  loadAdminDashboard();
  loadKnowledgeStatus();
  loadKnowledgeJobs({ immediate: true });
});
els.knowledgeUploadForm.addEventListener("submit", uploadKnowledgeFile);
els.refreshKnowledgeJobs.addEventListener("click", () => loadKnowledgeJobs({ immediate: true }));
els.rebuildVector.addEventListener("click", () => runKnowledgeAction("rebuild"));
els.backupVector.addEventListener("click", () => runKnowledgeAction("backup"));
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearKnowledgePoll();
  else loadKnowledgeJobs({ immediate: true });
});

window.MindBridgeAdminPanels.bind(document, localStorage);
checkHealth();
loadProfile().then((profile) => {
  if (!profile) return;
  loadAgentStatus();
  loadAdminDashboard();
  loadKnowledgeStatus();
  loadKnowledgeJobs({ immediate: true });
});
