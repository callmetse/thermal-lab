#!/usr/bin/env node
/* P1-01 回归：通道与画面来源必须是两件事。
 *
 * 用法：node tools/test_channel_source.js <page.html>
 * 退出码 0 = 全部通过；失败打印 FAIL 并以 1 退出。
 */
const fs = require("fs");
const vm = require("vm");

const pagePath = process.argv[2];
if (!pagePath) {
  console.error("usage: node test_channel_source.js <page.html>");
  process.exit(2);
}
const html = fs.readFileSync(pagePath, "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (!scripts.length) {
  console.error("no <script> block found in", pagePath);
  process.exit(2);
}

function el(id) {
  return {
    id, value: "", disabled: false, checked: false, hidden: false, textContent: "",
    innerHTML: "", className: "", title: "", type: "", src: "", multiple: false,
    dataset: {}, style: {}, options: [], selectedOptions: [], files: [],
    naturalWidth: 0, naturalHeight: 0, width: 0, height: 0, clientWidth: 0, clientHeight: 0,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    append() {}, appendChild() {}, replaceChildren() {}, remove() {},
    setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
    addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
    focus() {}, blur() {}, click() {}, scrollIntoView() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 0, height: 0 }),
    getContext: () => null, querySelector: () => null, querySelectorAll: () => [],
    closest: () => null, cloneNode: () => el(id),
  };
}

function boot(pageHtml) {
  const scripts = [...pageHtml.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  const els = {};
  const document = {
    getElementById: (id) => els[id] || (els[id] = el(id)),
    createElement: (t) => el(t),
    createElementNS: (ns, t) => el(t),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {},
    body: el("body"), documentElement: el("html"), head: el("head"),
  };
  const captured = { calls: [] };
  const context = {
    document, captured, console,
    location: { protocol: "http:", hostname: "127.0.0.1", origin: "http://127.0.0.1", href: "http://127.0.0.1/lab", pathname: "/lab" },
    navigator: { userAgent: "node", clipboard: { writeText: async () => {} } },
    performance: { now: () => Date.now() },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    fetch: () => new Promise(() => {}),
    AbortController: class { constructor() { this.signal = {}; } abort() {} },
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    requestAnimationFrame: () => 0, cancelAnimationFrame() {},
    URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
    Blob: class {}, TextDecoder: class {}, TextEncoder: class {},
    WebSocket: class { constructor() { this.readyState = 0; } send() {} close() {} addEventListener() {} },
    XMLHttpRequest: class {
      constructor() { this.upload = {}; this.status = 0; this.responseText = ""; this.timeout = 0; }
      open() {} setRequestHeader() {}
      send() {
        this.status = 201;
        this.responseText = JSON.stringify({ upload_id: "a".repeat(32), duration_s: 12.5 });
        if (this.onload) this.onload();
        if (this.onloadend) this.onloadend();
      }
    },
    ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
    IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    Image: class { constructor() { this.src = ""; } addEventListener() {} },
    Audio: class { play() {} pause() {} },
    Notification: class { static requestPermission() { return Promise.resolve("denied"); } },
    addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
    innerWidth: 1440, innerHeight: 900, devicePixelRatio: 1,
  };
  context.window = context;
  context.globalThis = context;
  context.self = context;
  const sandbox = vm.createContext(context);
  let initError = null;
  try {
    for (const code of scripts) vm.runInContext(code, sandbox, { filename: "page.js" });
  } catch (error) {
    initError = error;
  }
  return { ctx: context, initError };
}

const results = [];
function check(name, fn) {
  try {
    fn();
    results.push([true, name, ""]);
  } catch (error) {
    results.push([false, name, error.message]);
  }
}
function eq(actual, expected, what) {
  if (actual !== expected) {
    throw new Error(`${what}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}
const E = (ctx, id) => ctx.document.getElementById(id);
const lastCall = (ctx) => (ctx.captured.calls.length ? ctx.captured.calls[ctx.captured.calls.length - 1] : null);

function installSpies(ctx) {
  ctx.action = async (path, data) => { ctx.captured.calls.push({ path, data }); };
  E(ctx, "region_mode").value = "full";
  ctx.readRegion = () => ({ mode: "full" });
}

async function startWith(ctx, channel, source) {
  E(ctx, "arm").value = channel;
  if (source !== undefined) E(ctx, "source").value = source;
  E(ctx, "seconds").value = "30";
  E(ctx, "sample_fps").value = "12";
  E(ctx, "confidence_min").value = "0.25";
  E(ctx, "confidence_max").value = "1";
  E(ctx, "collection_location").value = "";
  E(ctx, "collection_name").value = "";
  ctx.syncChannel();
  await ctx.start();
}

async function main() {
  const { ctx, initError } = boot(html);
  if (initError) console.error("init error (ignored):", initError.message);
  installSpies(ctx);

  check("进入 D 不改写普通通道的画面来源", () => {
    E(ctx, "arm").value = "C";
    E(ctx, "source").value = "camera";
    ctx.syncChannel();
    E(ctx, "arm").value = "D";
    ctx.syncChannel();
    eq(E(ctx, "source").value, "camera", "进入 D 后的 source");
    eq(E(ctx, "source").disabled, true, "D 时 source 应禁用");
  });

  for (const target of ["C", "B", "A"]) {
    check(`D→${target} 后 source 仍是用户选择(camera)`, () => {
      E(ctx, "arm").value = "D";
      ctx.syncChannel();
      E(ctx, "arm").value = target;
      ctx.syncChannel();
      eq(E(ctx, "source").value, "camera", `D→${target} 的 source`);
      eq(E(ctx, "source").disabled, false, `D→${target} 的 source 应恢复可编辑`);
    });
  }

  await startWith(ctx, "C", "replay");
  check("C + 显式回放 → payload source=replay", () => {
    const call = lastCall(ctx);
    eq(call.path, "start", "path");
    eq(call.data.arm, "C", "arm");
    eq(call.data.source, "replay", "source");
  });

  await startWith(ctx, "B", "camera");
  check("B + 摄像头 → payload source=camera", () => {
    const call = lastCall(ctx);
    eq(call.data.arm, "B", "arm");
    eq(call.data.source, "camera", "source");
    eq(call.data.unlimited, false, "unlimited");
  });

  const ctx2 = boot(html).ctx;
  installSpies(ctx2);
  await startWith(ctx2, "D", "camera");
  check("D 未上传视频 → 拒绝启动", () => {
    eq(ctx2.captured.calls.length, 0, "start 调用次数");
    const text = E(ctx2, "error").textContent || "";
    if (!/上传/.test(text)) throw new Error("错误提示不含上传提示: " + text);
  });

  const ctx3 = boot(html).ctx;
  installSpies(ctx3);
  E(ctx3, "video_file").files = [{ size: 1024 * 1024, type: "video/mp4" }];
  E(ctx3, "upload_button").onclick();
  if (!/上传完成/.test(E(ctx3, "upload_status").textContent || "")) {
    throw new Error("上传流程没有走通: " + E(ctx3, "upload_status").textContent);
  }
  await startWith(ctx3, "D", "camera");
  check("D 已上传 → payload arm=C/source=replay/upload_id", () => {
    const call = lastCall(ctx3);
    eq(call.data.arm, "C", "arm");
    eq(call.data.source, "replay", "source");
    eq(call.data.upload_id, "a".repeat(32), "upload_id");
    eq(call.data.seconds, 15, "seconds(ceil(12.5)+2)");
  });

  let failed = 0;
  for (const [ok, name, message] of results) {
    if (ok) console.log("PASS", name);
    else {
      failed += 1;
      console.log("FAIL", name, "—", message);
    }
  }
  console.log(`\n${results.length - failed}/${results.length} passed`);
  process.exit(failed ? 1 : 0);
}

main().catch((error) => {
  console.error("harness error:", error);
  process.exit(2);
});
