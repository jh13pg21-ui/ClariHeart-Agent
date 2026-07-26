const AUTH_KEY = "mindbridge.auth";

const state = {
  profile: null,
  modelName: "mock"
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
  reports: document.querySelector("#reports"),
  conversationState: document.querySelector("#conversationState"),
  conversationDetail: document.querySelector("#conversationDetail"),
  knowledgeState: document.querySelector("#knowledgeState"),
  knowledgeUploadForm: document.querySelector("#knowledgeUploadForm"),
  knowledgeFile: document.querySelector("#knowledgeFile"),
  knowledgeUploadState: document.querySelector("#knowledgeUploadState"),
  rebuildVector: document.querySelector("#rebuildVector"),
  backupVector: document.querySelector("#backupVector")
};

function readAuth() {
  try {
    return JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null");
  } catch {
    return null;
  }
}

function clearAuth() {
  sessionStorage.removeItem(AUTH_KEY);
}

function authHeader() {
  const auth = readAuth();
  if (!auth?.token) {
    window.location.replace("/");
    return "";
  }
  return `Basic ${auth.token}`;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), Authorization: authHeader() };
  const response = await fetch(path, { ...options, headers });
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
    const response = await fetch("/actuator/health");
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
    clearAuth();
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
  els.knowledgeUploadState.textContent = "正在切分入库...";
  try {
    const response = await api("/api/admin/knowledge/file", { method: "POST", body: data });
    const result = await response.json();
    els.knowledgeUploadState.textContent = `${result.source} 已入库 ${result.chunks} 个片段`;
    els.knowledgeFile.value = "";
    loadKnowledgeStatus();
  } catch (error) {
    els.knowledgeUploadState.textContent = `上传失败：${error.message}`;
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

function logout() {
  clearAuth();
  window.location.assign("/");
}

els.switchAccount.addEventListener("click", logout);
els.refreshAdmin.addEventListener("click", () => {
  loadAdminDashboard();
  loadKnowledgeStatus();
});
els.knowledgeUploadForm.addEventListener("submit", uploadKnowledgeFile);
els.rebuildVector.addEventListener("click", () => runKnowledgeAction("rebuild"));
els.backupVector.addEventListener("click", () => runKnowledgeAction("backup"));

checkHealth();
loadProfile().then((profile) => {
  if (!profile) return;
  loadAgentStatus();
  loadAdminDashboard();
  loadKnowledgeStatus();
});
