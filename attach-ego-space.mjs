// 在 ego lite 的 Agent Space 里打开并**常驻**助手页面。
//
// 背景：本工具的自动化全部是网页里的 setInterval 定时器，必须有一个连着
// ws://127.0.0.1:10998 的页面一直开着。ego lite 是独立应用（ego.exe 常驻），
// 它的 Agent Space 页面在脚本进程退出后依然保留 —— 页面不关，定时器就一直跑。
//
// 用法（从 .cmd 调）：
//   ego-browser nodejs -e "await import('file:///D:/hrh/GithubProgra/wog-assistant-cn/attach-ego-space.mjs')"
//
// 每次调用是新的 Node 进程，但 taskSpace(name) / tabs / Page label 持久。
// 复用同名 space 与已开的页面，不重复开、不关闭。
const SPACE = "wog-afk-persistent";
const URL = "http://127.0.0.1:8791/index.html";

const task = await taskSpace(SPACE);
console.log("SPACE_ID:", task.spaceId, "OWNERSHIP:", task.ownership);

const tabs = await task.tabs();
let page = null;
let created = false;

const existing = tabs.find((t) => (t.url || "").includes("127.0.0.1:8791"));
if (existing) {
  page = existing.label ? task.page(existing.label) : await task.adopt(existing.page);
  console.log("REUSE_EXISTING:", page.label, await page.url());
  // 若已有页面但桥没连上，重载一次
  const needReload = await page.evaluate(() => {
    const b = window.WOG_COMPANION_RUNTIME_BRIDGE;
    return !(b && typeof b.getState === "function" && b.getState().status === "connected");
  }).catch(() => true);
  if (needReload) { await page.reload().catch(() => {}); }
} else {
  const managed = (await task.pages()) || [];
  page = managed[0] || await task.newPage();
  await page.goto(URL);
  created = true;
  console.log("OPENED:", page.label, await page.url());
}

await page.waitForLoadState("domcontentloaded").catch(() => {});
await page.waitForFunction(
  () => {
    const b = window.WOG_COMPANION_RUNTIME_BRIDGE;
    return b && typeof b.getState === "function" && b.getState().status === "connected";
  },
  undefined,
  { timeout: 20000 },
).catch((e) => console.log("WAIT_CONNECTED:", String(e).slice(0, 140)));

// 让 wog-auto.js 首次 tick（2.5s）+ 日常调度起来
await page.waitForTimeout(6000);

const state = await page.evaluate(() => {
  const q = (id) => document.getElementById(id);
  const txt = (id) => { const e = q(id); return e ? (e.textContent || "").trim() : null; };
  const b = window.WOG_COMPANION_RUNTIME_BRIDGE;
  const s = b && typeof b.getState === "function" ? b.getState() : null;
  const st = q("advisorAutoLevelStatus");
  return {
    url: location.href,
    hidden: document.hidden,
    visibilityState: document.visibilityState,
    gold: txt("barGold"),
    bridgeStatus: s ? s.status : null,
    disconnects: s ? s.connectionDisconnects : null,
    evaluatorErrors: s && s.evaluatorMetrics ? s.evaluatorMetrics.errors : null,
    trainingRunning: typeof window.__wogTrainingAutoRunning === "function" ? window.__wogTrainingAutoRunning() : null,
    autoLevelStatus: st ? (st.innerText || "").trim() : null,
    autoSettings: window.__wogAuto ? window.__wogAuto.get() : null,
    dailyLog: (() => {
      const e = document.querySelector('#daily-log-output, [id*="daily"][id*="log"]');
      return e ? (e.textContent || "").trim().slice(-800) : null;
    })(),
    switchesOn: [
      "daily-auto-takeDia", "daily-auto-enchant", "daily-auto-gacha", "daily-auto-relic",
      "daily-auto-mail", "daily-auto-shop", "daily-auto-equip", "daily-auto-training",
      "baoyu-auto-jewel", "baoyu-auto-gear", "baoyu-auto-accessory", "baoyu-auto-deposit", "baoyu-auto-organize",
    ].filter((id) => { const e = q(id); return e && e.checked; }).length,
  };
});

console.log("STATE:", JSON.stringify(state, null, 2));
console.log("PERSISTENT_PAGE_LEFT_OPEN:", page.label, await page.url());
console.log("CREATED_THIS_RUN:", created);
// 故意不调用 page.close() / task.finish()：页面留在 Agent Space 里持续跑定时器。
