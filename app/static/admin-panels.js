(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.MindBridgeAdminPanels = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  const STORAGE_KEY = "mindbridge.adminPanelState";

  function read(storage) {
    try {
      const parsed = JSON.parse(storage?.getItem(STORAGE_KEY) || "{}");
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch {
      return {};
    }
  }

  function collect(root) {
    return Array.from(root.querySelectorAll("details[data-module]"));
  }

  function write(root, storage) {
    const state = {};
    for (const panel of collect(root)) {
      state[panel.dataset.module] = panel.open;
    }
    storage?.setItem(STORAGE_KEY, JSON.stringify(state));
  }

  function bind(root, storage) {
    const saved = read(storage);
    for (const panel of collect(root)) {
      panel.open = saved[panel.dataset.module] === true;
      panel.addEventListener("toggle", () => write(root, storage));
    }
  }

  function open(root, moduleId, storage, options = {}) {
    const panel = collect(root).find((item) => item.dataset.module === moduleId);
    if (!panel) return false;
    panel.open = true;
    write(root, storage);
    if (options.scroll) {
      panel.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    return true;
  }

  return { bind, open };
});
