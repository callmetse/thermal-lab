#!/usr/bin/env node
/* P1-02/03 控制端回归：会话状态机、停止 ACK、租约、控制端重启对账。
 *
 * 用法：node tools/test_session_flow.js <flow_funcs_dir>
 * 目录里需要有 start.js stop.js collect.js state.js lease.js（从线上 flows 抽出并打过补丁的函数体）。
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const dir = process.argv[2] || "D:/Temp/bench/flow_funcs";
const source = (name) => fs.readFileSync(path.join(dir, `${name}.js`), "utf8");
const bodies = {
  start: source("start"),
  stop: source("stop"),
  collect: source("collect"),
  state: source("state"),
  lease: source("lease"),
};

const ROOT = "labs/abc20261102/v1";
const PROJECT = "abc20261102";

function makeGlobal() {
  const store = new Map();
  const files = new Map();
  const g = {
    get: (k) => store.get(k),
    set: (k, v) => store.set(k, v),
    store, files,
  };
  g.set("perf", { now: () => Date.now() });
  g.set("crypto", { randomUUID: () => "run-" + Math.random().toString(16).slice(2, 8) });
  g.set("fs", { writeFileSync: (p, v) => files.set(p, v) });
  g.set("audit_path", "/srv/abc20261102/data/events.jsonl");
  return g;
}

function call(fnName, g, msg) {
  const code = `(function(global,msg){${bodies[fnName]}\n})`;
  const sandbox = { global: g, msg, Date, JSON, Math, Number, Array, Object, RegExp, String, Boolean, isNaN, parseInt, parseFloat, console, Buffer, Uint8Array, setTimeout, clearTimeout };
  const fn = vm.runInNewContext(code, sandbox, { filename: fnName + ".js" });
  return fn(g, msg);
}

function cmdOf(result) {
  const list = Array.isArray(result) ? result : [result];
  const out = list.filter(Boolean).find((m) => m && m.topic && m.payload);
  return out ? JSON.parse(typeof out.payload === "string" ? out.payload : JSON.stringify(out.payload)) : null;
}
function httpOf(result) {
  const list = Array.isArray(result) ? result : [result];
  return list.filter(Boolean)[0];
}

const results = [];
function check(name, fn) {
  try { fn(); results.push([true, name]); }
  catch (e) { results.push([false, name, e.message]); }
}
function eq(a, b, what) {
  if (a !== b) throw new Error(`${what}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  // ---------- 1. 启动：代次、starting、有限时长带 deadline ----------
  const g = makeGlobal();
  const startMsg = (payload) => ({ payload });
  let out = call("start", g, startMsg({ arm: "A", source: "camera", seconds: 10, sample_fps: 12 }));
  const run = g.get("active");
  check("start → active 带 generation=1、state=starting", () => {
    eq(!!run, true, "active 存在");
    eq(run.generation, 1, "generation");
    eq(run.state, "starting", "state");
  });
  check("start → 命令带 generation/lease_ttl_s，且返回 starting", () => {
    const cmd = cmdOf(out);
    eq(cmd.op, "start", "op");
    eq(cmd.generation, 1, "命令 generation");
    eq(cmd.lease_ttl_s, 120, "lease_ttl_s");
    eq(httpOf(out).payload.status, "starting", "http status");
  });
  check("start → 会话状态已落盘", () => {
    const file = g.get("fs") && [...g.files.keys()].find((k) => k.endsWith("session.json"));
    eq(!!file, true, "session.json 写入");
    eq(JSON.parse(g.files.get(file)).state, "starting", "落盘 state");
  });

  // ---------- 2. 重复启动被拒 ----------
  out = call("start", g, startMsg({ arm: "A", source: "camera", seconds: 10, sample_fps: 12 }));
  check("start 时已有活动会话 → 409", () => eq(httpOf(out).statusCode, 409, "statusCode"));

  // ---------- 3. worker 回报 running ----------
  const runId = run.run_id;
  const evt = (kind, extra) => ({
    topic: `${ROOT}/runs/${runId}/events`,
    payload: JSON.stringify({ project: PROJECT, run_id: runId, kind, ...extra }),
  });
  call("collect", g, evt("session_state", { state: "running", generation: 1 }));
  check("session_state running → active.state=running", () => eq(g.get("active").state, "running", "state"));
  call("collect", g, evt("heartbeat", { uptime_s: 3 }));
  check("heartbeat → 记录心跳时间", () => eq(typeof g.get("heartbeat_wall"), "number", "heartbeat_wall"));

  // ---------- 4. stop：不先清 active，标记 stopping，带 generation ----------
  out = call("stop", g, { payload: {} });
  check("stop → active 保留但标 stopping（修复前会立刻清空）", () => {
    const cmd = cmdOf(out);
    eq(cmd.op, "stop", "op");
    eq(cmd.generation, 1, "generation");
    eq(g.get("active") && g.get("active").state, "stopping", "active.state");
    eq(httpOf(out).payload.status, "stopping", "http status");
  });

  // ---------- 5. worker 回报 completed → 控制端才清 active ----------
  call("collect", g, evt("session_state", { state: "completed", generation: 1 }));
  check("session_state completed → 清 active 并留 last_session", () => {
    eq(g.get("active"), null, "active 清空");
    eq(g.get("last_session").state, "completed", "last_session.state");
  });

  // ---------- 6. 不限时：lease 不再因为 remaining<=0 失效 ----------
  const g2 = makeGlobal();
  call("start", g2, startMsg({ arm: "C", source: "camera", seconds: 0, unlimited: true, sample_fps: 12 }));
  const leaseOut = call("lease", g2, {});
  check("不限时 lease → op=lease、ttl=120（旧实现返回 null）", () => {
    const cmd = cmdOf(leaseOut);
    eq(!!cmd, true, "有消息");
    eq(cmd.op, "lease", "op");
    eq(cmd.generation, 1, "generation");
    eq(cmd.ttl_s, 120, "ttl_s");
  });

  // ---------- 7. 有限时长 lease 取剩余与 ttl 的较小值 ----------
  const g3 = makeGlobal();
  call("start", g3, startMsg({ arm: "A", source: "camera", seconds: 600, sample_fps: 12 }));
  const lease3 = cmdOf(call("lease", g3, {}));
  check("有限时长 lease → ttl≤120 且 >0", () => {
    eq(lease3.op, "lease", "op");
    if (!(lease3.ttl_s > 0 && lease3.ttl_s <= 120)) throw new Error("ttl=" + lease3.ttl_s);
  });

  // ---------- 8. 控制端重启：无 active 时先发 session_query，且有节流 ----------
  const g4 = makeGlobal();
  const q1 = cmdOf(call("lease", g4, {}));
  const q2 = cmdOf(call("lease", g4, {}));
  check("控制端重启：无 active → 发 session_query，5s 内不重复", () => {
    eq(q1.op, "session_query", "第一次");
    eq(q2, null, "第二次应被节流");
  });

  // ---------- 9. 对账：worker 说 running → 控制端接管 ----------
  call("collect", g4, {
    topic: `${ROOT}/runs/restored-1/events`,
    payload: JSON.stringify({
      project: PROJECT, run_id: "restored-1", kind: "session_state", state: "running", generation: 7,
      session: { run_id: "restored-1", arm: "A", source: "camera", seconds: 60, unlimited: false, generation: 7 },
    }),
  });
  check("对账：无 active 收到 running → 接管并保留 generation", () => {
    const a = g4.get("active");
    eq(a && a.run_id, "restored-1", "run_id");
    eq(a.generation, 7, "generation");
    eq(a.state, "running", "state");
  });

  // ---------- 10. /api/state 暴露会话字段 ----------
  call("state", g4, { payload: {} });
  check("state → 输出 session 字段（state/generation/heartbeat_age_s）", () => {
    const snapshot = call("state", g4, { payload: {} }).payload;
    eq(snapshot.session.state, "running", "session.state");
    eq(snapshot.session.generation, 7, "session.generation");
    if (!("heartbeat_age_s" in snapshot.session)) throw new Error("缺 heartbeat_age_s");
  });

  let failed = 0;
  for (const [ok, name, message] of results) {
    if (ok) console.log("PASS", name);
    else { failed += 1; console.log("FAIL", name, "—", message); }
  }
  console.log(`\n${results.length - failed}/${results.length} passed`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error("harness error:", e); process.exit(2); });
