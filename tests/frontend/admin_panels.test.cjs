const assert = require("node:assert/strict");
const test = require("node:test");

const panels = require("../../app/static/admin-panels.js");

function panel(id) {
  return {
    dataset: { module: id },
    open: false,
    listeners: {},
    addEventListener(type, listener) {
      this.listeners[type] = listener;
    },
    scrollIntoViewCalled: false,
    scrollIntoView() {
      this.scrollIntoViewCalled = true;
    }
  };
}

function fixture(saved = {}) {
  const items = [panel("cases"), panel("knowledge"), panel("reports"), panel("archive")];
  const storage = {
    value: JSON.stringify(saved),
    getItem() {
      return this.value;
    },
    setItem(key, value) {
      this.value = value;
    }
  };
  const root = {
    querySelectorAll() {
      return items;
    },
    querySelector(selector) {
      const id = selector.match(/"([^"]+)"/)?.[1];
      return items.find((item) => item.dataset.module === id) || null;
    }
  };
  return { items, root, storage };
}

test("恢复每个管理模块上次的展开状态", () => {
  const { items, root, storage } = fixture({ reports: true, cases: false });

  panels.bind(root, storage);

  assert.equal(items[0].open, false);
  assert.equal(items[2].open, true);
});

test("切换模块后持久化状态", () => {
  const { items, root, storage } = fixture();
  panels.bind(root, storage);
  items[1].open = true;

  items[1].listeners.toggle();

  assert.deepEqual(JSON.parse(storage.value), { cases: false, knowledge: true, reports: false, archive: false });
});

test("查看报告时可主动展开会话档案", () => {
  const { items, root, storage } = fixture();

  panels.open(root, "archive", storage, { scroll: true });

  assert.equal(items[3].open, true);
  assert.equal(items[3].scrollIntoViewCalled, true);
  assert.equal(JSON.parse(storage.value).archive, true);
});
