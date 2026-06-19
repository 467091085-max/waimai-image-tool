const state = { plan: null, style: "", menu: null, uploaded: false, running: false };
const $ = s => document.querySelector(s);
const esc = v => String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");

function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2200);
}

async function api(url, opt = {}) {
  const res = await fetch(url, opt);
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || "请求失败");
  return data;
}

function tone(text) {
  text = String(text);
  if (text.includes("复用") || text.includes("完成") || text.includes("一致") || text.includes("就绪")) return "good";
  if (text.includes("缺") || text.includes("定制") || text.includes("失败")) return "bad";
  return "warn";
}

function switchView(viewId) {
  document.querySelectorAll(".tab,.view").forEach(el => el.classList.remove("active"));
  document.querySelector(`.tab[data-view="${viewId}"]`)?.classList.add("active");
  $(`#${viewId}`)?.classList.add("active");
}

function setControls() {
  const canStart = state.uploaded && !state.running;
  const menuFile = state.menu?.file || "菜单";
  const menuCount = state.menu?.count ?? 0;
  $("#startJobBtn").disabled = !canStart;
  $("#startJobBtn").textContent = state.running ? "正在做图..." : state.plan ? "重新做图" : "开始做图";
  $("#exportBtn").disabled = !state.plan;
  $("#menuStatus").textContent = state.uploaded
    ? `菜单已就绪：${menuFile} · ${menuCount} 个菜`
    : "等待上传菜单";
  $("#menuStatus").className = `menu-status ${state.uploaded ? "good" : ""}`;
}

function renderEmpty() {
  $("#items").textContent = state.menu?.count ?? "-";
  $("#category").textContent = state.uploaded ? "待识别" : "-";
  $("#reuse").textContent = "-";
  $("#cash").textContent = "-";
  $("#workflowBox").innerHTML = [
    ["上传 Excel 菜单", state.uploaded ? "已完成" : "待上传", state.uploaded ? `${state.menu?.count ?? 0} 个菜单项` : "支持 xls / xlsx"],
    ["开始做图", state.plan ? "已完成" : "待开始", "点击按钮后开始匹配图库"],
    ["品类识别", "待运行", "识别门店所属品类"],
    ["菜品标准化", "待运行", "合并同菜不同叫法"],
    ["展示风格包", "待运行", "展示可用风格和覆盖率"],
    ["出图排产", "待运行", "复用 / 换背景 / 定制"]
  ].map((item, index) => `<div class="step"><i>${index + 1}</i><b>${esc(item[0])}</b><p class="${tone(item[1])}">${esc(item[1])}</p><p>${esc(item[2])}</p></div>`).join("");
  $("#standardBox").innerHTML = `<div class="empty">上传菜单后点击开始做图</div>`;
  $("#styleBox").innerHTML = `<div class="empty">开始做图后展示风格包</div>`;
  $("#summary").innerHTML = "";
  $("#resultBox").innerHTML = `<div class="empty">开始做图后展示菜品匹配结果</div>`;
  $("#paidBox").innerHTML = `<div class="empty">开始做图后生成报价和积分</div>`;
  setControls();
}

function render() {
  const p = state.plan;
  $("#items").textContent = p.menu.count;
  $("#category").textContent = p.category.category;
  $("#reuse").textContent = p.summary.reuse;
  $("#cash").textContent = `¥${p.quote.cash}`;
  $("#workflowBox").innerHTML = [
    ["上传 Excel 菜单", "已完成", `${p.menu.count} 个菜单项`],
    ["开始做图", "已完成", "匹配任务已运行"],
    ["品类识别", "已完成", `${p.category.category} · ${p.category.confidence}%`],
    ["菜品标准化", "已完成", `${p.standardization.rawItems} → ${p.standardization.canonicalItems} 标准菜`],
    ["展示 5 套风格", "已完成", `${p.styles.length} 套风格包`],
    ["客户选风格", "进行中", p.selectedStyle],
    ["出图排产", "已完成", `复用 ${p.summary.reuse} / 换背景 ${p.summary.bgReplace} / 定制 ${p.summary.custom}`],
    ["导出交付", "待确认", "zip + Excel 报告"]
  ].map((item, index) => `<div class="step"><i>${index + 1}</i><b>${esc(item[0])}</b><p class="${tone(item[1])}">${esc(item[1])}</p><p>${esc(item[2])}</p></div>`).join("");
  $("#standardBox").innerHTML = [
    `<div class="card"><b>原始菜单项</b><h2>${p.standardization.rawItems}</h2></div>`,
    `<div class="card"><b>标准菜数量</b><h2>${p.standardization.canonicalItems}</h2></div>`,
    `<div class="card"><b>合并别名</b><h2>${p.standardization.aliasMerged}</h2></div>`
  ].join("") + p.standardization.samples.map(s => `<div class="card"><b>${esc(s.canonical)}</b><p>${s.count} 个叫法</p><p>${s.examples.map(esc).join(" / ")}</p></div>`).join("");
  $("#styleBox").innerHTML = p.styles.map(s => `<button class="style ${s.id === p.selectedStyle ? "active" : ""}" data-style="${s.id}"><img src="${s.sample?.url || ""}"><span class="style-body"><b>${esc(s.name)}</b><span>直接复用 ${s.direct}（${s.directRate}%）</span><span>二次加工 ${s.bgReplace + s.review}（${s.processingRate}%）</span><span>需定制 ${s.custom}（${s.customRate}%）</span><strong>${s.estimatedPoints} 积分</strong></span></button>`).join("");
  document.querySelectorAll(".style").forEach(button => {
    button.onclick = async () => {
      state.style = button.dataset.style;
      await runJob(false);
    };
  });
  $("#summary").innerHTML = [
    `总数 ${p.summary.total}`,
    `直接 ${p.summary.direct}`,
    `复核 ${p.summary.review}`,
    `缺图 ${p.summary.missing}`,
    `换背景 ${p.summary.bgReplace}`,
    `预计 ${p.summary.points} 积分`
  ].map(x => `<span class="pill">${x}</span>`).join("");
  $("#resultBox").innerHTML = p.results.map(row => {
    const candidate = row.candidates[0];
    return `<div class="result">${candidate ? `<img src="${candidate.url}">` : `<div class="empty image-empty">缺图</div>`}<div class="result-body"><b>${esc(row.name)}</b><p>${esc(row.category)} · ${esc(row.kind)}</p><div><span class="pill ${tone(row.status)}">${row.status}</span><span class="pill ${tone(row.backgroundAction)}">${row.backgroundAction}</span><span class="pill">${row.points}积分</span></div><p>${candidate ? `${esc(candidate.dishName)} | ${esc(candidate.store)} | ${candidate.score}分` : "进入定制/生成池"}</p></div></div>`;
  }).join("");
  $("#paidBox").innerHTML = `<div class="paid"><div class="price">¥${p.quote.cash}</div><b>${p.quote.package}</b><p>${p.quote.points} 积分 · ${p.quote.rate}</p></div>` + p.quote.addOns.map(a => `<div class="paid"><b>${esc(a.name)}</b><p>¥${a.price}</p></div>`).join("") + `<div class="paid"><b>邀请奖励</b><p>注册送 ${p.quote.referral.registerReward} 积分；首付 ${p.quote.referral.firstPayReward}</p></div>`;
  setControls();
}

async function uploadMenu() {
  const file = $("#menuFile").files[0];
  if (!file) return toast("请先选择菜单文件");
  const name = file.name.toLowerCase();
  if (!name.endsWith(".xls") && !name.endsWith(".xlsx")) return toast("请上传 xls 或 xlsx 菜单");
  const fd = new FormData();
  fd.append("file", file);
  toast("菜单上传中");
  const data = await api("/api/upload-menu", { method: "POST", body: fd });
  state.uploaded = true;
  state.menu = data.menu;
  state.plan = null;
  state.style = "";
  renderEmpty();
  switchView("workflow");
  toast("菜单已上传，点击开始做图");
}

async function runJob(announce = true) {
  if (!state.uploaded) return toast("请先上传菜单");
  state.running = true;
  setControls();
  if (announce) toast("开始匹配图库");
  try {
    state.plan = await api(`/api/plan?style=${encodeURIComponent(state.style)}`);
    state.style = state.plan.selectedStyle;
    state.menu = state.plan.menu;
    state.uploaded = true;
    render();
    if (announce) switchView("styles");
  } finally {
    state.running = false;
    setControls();
  }
}

async function refreshMenuStatus() {
  try {
    const data = await api("/api/menu-status");
    state.uploaded = data.uploaded;
    state.menu = data.menu || null;
  } catch {
    state.uploaded = false;
    state.menu = null;
  }
  renderEmpty();
}

document.querySelectorAll(".tab").forEach(button => {
  button.onclick = () => switchView(button.dataset.view);
});

$("#uploadMenuBtn").onclick = () => uploadMenu().catch(e => toast(e.message));
$("#startJobBtn").onclick = () => runJob(true).catch(e => toast(e.message));
$("#exportBtn").onclick = async () => {
  if (!state.plan) return toast("请先开始做图");
  const data = await api("/api/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ style: state.style, scope: $("#scopeSelect")?.value || "all" })
  });
  toast(`已导出 ${data.rows} 条`);
  location.href = data.download;
};

refreshMenuStatus();
