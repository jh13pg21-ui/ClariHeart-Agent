const els = {
  serviceState: document.querySelector("#serviceState"),
  modelState: document.querySelector("#modelState"),
  loginForm: document.querySelector("#loginForm"),
  username: document.querySelector("#username"),
  password: document.querySelector("#password"),
  loginState: document.querySelector("#loginState")
};

function isAdmin(profile) {
  return profile.roles?.some((role) => role.authority === "ROLE_ADMIN");
}

function setPill(el, text, tone = "ok") {
  if (!el) return;
  el.textContent = text;
  el.className = `pill ${tone}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, credentials: "same-origin" });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  return response;
}

function routeProfile(profile) {
  window.location.assign(isAdmin(profile) ? "/admin.html" : "/student.html");
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

async function resumeExistingLogin() {
  try {
    const response = await api("/api/profile");
    routeProfile(await response.json());
  } catch {
    setPill(els.modelState, "登录后读取", "warn");
  }
}

async function login(event) {
  event.preventDefault();
  els.loginState.textContent = "正在登录...";
  try {
    const response = await api("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: els.username.value.trim(),
        password: els.password.value
      })
    });
    els.password.value = "";
    const profile = await response.json();
    els.loginState.textContent = "登录成功，正在进入工作台";
    routeProfile(profile);
  } catch (error) {
    els.password.value = "";
    els.loginState.textContent = `登录失败：${error.message}`;
  }
}

els.loginForm.addEventListener("submit", login);
checkHealth();
resumeExistingLogin();
