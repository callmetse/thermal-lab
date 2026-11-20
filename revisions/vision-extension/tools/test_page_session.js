#!/usr/bin/env node
/* P1-03 页面侧回归：会话状态徽标必须是"状态说了算"，不是"有没有 active 说了算"。
 *
 * 用法：node tools/test_page_session.js <page.html>
 */
const fs = require("fs");
const vm = require("vm");

const pagePath = process.argv[2];
if (!pagePath) {
  console.error("usage: node test_page_session.js <page.html>");
  process.exit(2);
}
const html = fs.readFileSync(pagePath, "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);

function el(id) {
  return {
    id, value: "", disabled: false, checked: false, hidden: false, textContent: "", className: "",
    dataset: {}, style: {}, options: [], selectedOptions: [], files: [],
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    append() {}, appendChild() {}, replaceChildren() {}, remove() {},
    setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
    addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
    focus() {}, blur() {}, click() {}, scrollIntoView() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 0, height: 0 }),
    getContext: () => null, querySelector: () => null, querySelectorAll: () => [], closest: () => null,
  };
}

function boot() {
  const els = {};
  const document = {
    getElementById: (id) => els[id] || (els[id] = el(id)),
    createElement: (t) => el(t), createElementNS: (n, t) => el(t),
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {},
    body: el("body"), documentElement: el("html"),
  };
  const context = {
    document, console,
    location: { protocol: "http:", hostname: "127.0.0.1", origin: "http://127.0.0.1" },
    navigator: { userAgent: "node" }, performance: { now: () => Date.now() },
    localStorage: { getItem: () => null, setItem() {} },
    fetch: () => new Promise(() => {}),
    AbortController: class { constructor() { this.signal = {}; } abort() {} },
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    requestAnimationFrame: () => 0, cancelAnimationFrame() {},
    URL: { createObjectURL: () => "", revokeObjectURL() {} },
    WebSocket: class { constructor() { this.readyState = 0; } send() {} close() {} addEventListener() {} },
    XMLHttpRequest: class { constructor() { this.upload = {}; } open() {} setRequestHeader() {} send() {} },
    ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    Buffer, Date, JSON, Math, Number, Array, Object, RegExp, String, Boolean, isNaN, parseInt, parseFloat,
    addEventListener() {}, removeEventListener() {}, innerWidth: 1440, innerHeight: 900,
  };
  context.window = context; context.globalThis = context; context.self = context;
  const sandbox = vm.createContext(context);
  let error = null;
  try { for (const code of scripts) vm.runInContext(code, sandbox, { filename: "page.js" }); }
  catch (e) { error = e; }
  return { ctx: context, error };
}

const results = [];
const check = (name, fn) => { try { fn(); results.push([true, name]); } catch (e) { results.push([false, name, e.message]); } };
const eq = (a, b, what) => { if (a !== b) throw new Error(`${what}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); };

const { ctx, error } = boot();
if (error) console.error("init error (ignored):", error.message);
const chip = (a, s, l) => ctx.sessionChip(a, s, l);
const ACTIVE = { arm: "A", ui_channel: "A", source: "camera" };

check("无会话 → 待命", () => eq(chip(null, null, null).text, "待命", "text"));
check("starting → 启动中", () => eq(chip(ACTIVE, { state: "starting" }, null).text, "启动中 · 等待 worker 确认", "text"));
check("stopping → 停止中（不是直接空闲）", () => eq(chip(ACTIVE, { state: "stopping" }, null).text, "停止中 · 等待 worker 结束", "text"));
check("running + 心跳正常 → 通道说明", () => eq(chip(ACTIVE, { state: "running", heartbeat_age_s: 3 }, null).text, "A 通道 · 实拍", "text"));
check("running + 心跳 12s 未更新 → 失联提示", () => {
  const c = chip(ACTIVE, { state: "running", heartbeat_age_s: 12 }, null);
  if (!/失联 12s/.test(c.text)) throw new Error(c.text);
  eq(c.cls, "chip warn", "cls");
});
check("interrupted → 已中断", () => {
  const c = chip(ACTIVE, { state: "interrupted" }, null);
  if (!/已中断/.test(c.text)) throw new Error(c.text);
});
check("空闲但上次失败 → 上次会话失败", () => eq(chip(null, null, { state: "failed" }).text, "上次会话失败", "text"));
check("空闲但上次被中断 → 上次会话被中断", () => eq(chip(null, null, { state: "interrupted" }).text, "上次会话被中断", "text"));

let failed = 0;
for (const [ok, name, message] of results) {
  if (ok) console.log("PASS", name);
  else { failed += 1; console.log("FAIL", name, "—", message); }
}
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
