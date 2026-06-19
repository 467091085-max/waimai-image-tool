const state = {
  plan: null,
  style: "",
  pendingStyle: "",
  menu: null,
  uploaded: false,
  running: false,
  confirmed: false,
  selectedRows: new Set(),
  account: { balance: 0, rate: "1 元 = 10 积分", packages: [] },
  stage: 1
};

const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const esc = v => String(v ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2400);
}

async function api(url, opt = {}) {
  const res = await fetch(url, opt);
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || "请求失败");
  return data;
}

function scrollToPanel(id) {
  $(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function setProgress(percent, text, stage = state.stage) {
  state.stage = stage;
  $("#progressBar").style.width = `${percent}%`;
  $("#progressText").textContent = text;
  $("#stageBadge").textContent = `第 ${stage} 步`;
  $$(".round-step").forEach((button, index) => {
    const step = index + 1;
    button.classList.toggle("done", step < stage);
    button.classList.toggle("active", step === stage);
  });
}

function unlockPanels() {
  $("#stylesPanel").classList.toggle("locked", !state.plan);
  $("#previewPanel").classList.toggle("locked", !state.confirmed);
  $("#exportView").classList.toggle("locked", !state.confirmed);
}

function publicStatus(row) {
  if (row.publicStatus) return row.publicStatus;
  if (!row.candidates?.length) return "待补图";
  return "已生成";
}

function setControls() {
  const menuFile = state.menu?.file || "菜单";
  const menuCount = state.menu?.count ?? 0;
  $("#startJobBtn").disabled = !state.uploaded || state.running;
  $("#startJobBtn").querySelector("span").textContent = state.running ? "生成中" : state.plan ? "重新做图" : "识别菜品";
  $("#confirmStyleBtn").disabled = !state.plan || !state.pendingStyle || state.running;
  $("#exportShortcutBtn").disabled = !state.confirmed;
  $("#exportZipBtn").disabled = !state.confirmed;
  $("#menuStatus").textContent = state.uploaded ? `菜单已就绪：${menuFile} · ${menuCount} 个菜` : "等待选择菜单";
  $("#menuStatus").className = `menu-status ${state.uploaded ? "good" : ""}`;
  $("#pointsBalance").textContent = `${state.account.balance || 0}`;
  unlockPanels();
}

function renderWorkflow(items) {
  $("#workflowBox").innerHTML = items.map((item, index) => (
    `<div class="step ${item.state || ""}">
      <i>${index + 1}</i>
      <b>${esc(item.title)}</b>
      <p>${esc(item.status)}</p>
    </div>`
  )).join("");
}

function renderRecharge() {
  $("#rateText").textContent = state.account.rate || "1 元 = 10 积分";
  $("#rechargePackages").innerHTML = (state.account.packages || []).map(pkg => (
    `<button class="recharge-card" data-points="${pkg.points}" type="button">
      <b>${esc(pkg.name)}</b>
      <span>${pkg.points + (pkg.bonus || 0)} 积分</span>
      <em>¥${pkg.cash}${pkg.bonus ? ` · 赠 ${pkg.bonus}` : ""}</em>
    </button>`
  )).join("");
  $$(".recharge-card").forEach(button => {
    button.onclick = () => {
      const added = Number(button.dataset.points || 0);
      state.account.balance += added;
      setControls();
      closeRecharge();
      toast(`已模拟充值 ${added} 积分`);
    };
  });
}

function renderWaiting() {
  $("#items").textContent = state.menu?.count ?? "-";
  $("#category").textContent = state.uploaded ? "待识别" : "-";
  $("#imageCount").textContent = "-";
  $("#cash").textContent = "-";
  setProgress(state.uploaded ? 22 : 8, state.uploaded ? "菜单已上传，点击开始做图" : "等待选择菜单", state.uploaded ? 2 : 1);
  renderWorkflow([
    { title: "选择菜单", status: state.uploaded ? "已完成" : "等待上传", state: state.uploaded ? "done" : "active" },
    { title: "开始做图", status: state.uploaded ? "可以开始" : "待菜单", state: state.uploaded ? "active" : "" },
    { title: "选择风格", status: "待生成方案" },
    { title: "图片预览", status: "待确认风格" },
    { title: "导出图片", status: "待预览" }
  ]);
  $("#styleBox").innerHTML = `<div class="empty">点击“开始做图”后展示风格方案</div>`;
  $("#selectedStyleHint").textContent = "还没有选择风格";
  $("#summary").innerHTML = "";
  $("#resultBox").innerHTML = `<div class="empty">确认风格后展示图片预览</div>`;
  $("#paidBox").innerHTML = `<div class="empty">生成预览后显示本单积分和可选加购</div>`;
  setControls();
}

function renderPlan(showPreview = false) {
  const p = state.plan;
  const ready = p.results.filter(r => r.candidates?.length).length;
  const needsWork = p.summary.total - ready;
  state.account = p.account || state.account;
  $("#items").textContent = p.menu.count;
  $("#category").textContent = p.category.category;
  $("#imageCount").textContent = p.summary.total;
  $("#cash").textContent = `${p.quote.points} 积分`;
  renderWorkflow([
    { title: "选择菜单", status: `${p.menu.count} 个菜品`, state: "done" },
    { title: "开始做图", status: p.category.category, state: "done" },
    { title: "选择风格", status: state.confirmed ? styleName(p.selectedStyle) : "请选择一套", state: state.confirmed ? "done" : "active" },
    { title: "图片预览", status: state.confirmed ? `${p.summary.total} 张已生成` : "待确认风格", state: state.confirmed ? "done" : "" },
    { title: "导出图片", status: state.confirmed ? "可以导出" : "待预览", state: state.confirmed ? "active" : "" }
  ]);
  renderStyles();
  if (showPreview) renderPreview();
  else $("#resultBox").innerHTML = `<div class="empty">选择风格并确认后，系统会生成整店图片预览</div>`;
  $("#summary").innerHTML = [
    `图片 ${p.summary.total} 张`,
    `已生成 ${ready} 张`,
    needsWork ? `待补图 ${needsWork} 张` : "全部可预览",
    `本单 ${p.summary.points} 积分`
  ].map(x => `<span class="pill">${esc(x)}</span>`).join("");
  renderPaid();
  renderRecharge();
  setControls();
}

function styleName(styleId) {
  return state.plan?.styles.find(s => s.id === styleId)?.name || "已选风格";
}

function renderStyles() {
  const p = state.plan;
  $("#selectedStyleHint").textContent = state.pendingStyle ? `已选择：${styleName(state.pendingStyle)}` : "还没有选择风格";
  $("#styleBox").innerHTML = p.styles.map(s => {
    const selected = s.id === state.pendingStyle;
    const ready = s.direct + s.review + s.bgReplace;
    return `<button class="style ${selected ? "active" : ""}" data-style="${s.id}" type="button">
      <img src="${s.sample?.url || ""}" alt="${esc(s.name)}">
      <span class="style-body">
        <b>${esc(s.name)}</b>
        <span>适配本菜单约 ${Math.round((ready / Math.max(1, p.summary.total)) * 100)}%</span>
        <em>${selected ? "已选中" : "点击选择"}</em>
      </span>
    </button>`;
  }).join("");
  $$(".style").forEach(button => {
    button.onclick = () => {
      state.pendingStyle = button.dataset.style;
      renderStyles();
      setControls();
      toast("风格已选中，请确认生成预览");
    };
  });
}

function renderPreview() {
  const p = state.plan;
  if (!state.selectedRows.size) {
    p.results.forEach((_, index) => state.selectedRows.add(index + 1));
  }
  $("#resultBox").innerHTML = p.results.map((row, index) => {
    const rowNo = index + 1;
    const candidate = row.candidates[0];
    const checked = state.selectedRows.has(rowNo) ? "checked" : "";
    const status = publicStatus(row);
    return `<div class="result">
      <label class="select-line"><input type="checkbox" class="row-check" data-row="${rowNo}" ${checked}> 选择</label>
      ${candidate ? `<img src="${candidate.url}" alt="${esc(row.name)}">` : `<div class="empty image-empty">待补图</div>`}
      <div class="result-body">
        <b>${esc(row.name)}</b>
        <p>${esc(row.category || "未分类")} · ${esc(row.kind)}</p>
        <div><span class="pill success">${esc(status)}</span><span class="pill">${row.points} 积分</span></div>
        ${candidate ? `<a class="save-link" href="${candidate.url}" download="${esc(row.name)}.jpg">单张保存</a>` : `<p>可进入定制配菜</p>`}
      </div>
    </div>`;
  }).join("");
  $$(".row-check").forEach(input => {
    input.onchange = () => {
      const rowNo = Number(input.dataset.row);
      if (input.checked) state.selectedRows.add(rowNo);
      else state.selectedRows.delete(rowNo);
    };
  });
}

function renderPaid() {
  const p = state.plan;
  $("#paidBox").innerHTML = `<div class="paid primary-paid">
      <span>本单预计</span>
      <b>${p.quote.points} 积分</b>
      <p>${esc(p.quote.package)} · ${esc(p.quote.rate)}</p>
    </div>` +
    p.quote.addOns.map(a => `<div class="paid"><b>${esc(a.name)}</b><p>${typeof a.price === "number" ? `${a.price * 10} 积分` : esc(a.price)}</p></div>`).join("") +
    `<div class="paid"><b>邀请奖励</b><p>注册送 ${p.quote.referral.registerReward} 积分；首付返利封顶 500 积分</p></div>`;
}

async function uploadMenu() {
  const file = $("#menuFile").files[0];
  if (!file) return;
  const name = file.name.toLowerCase();
  if (!name.endsWith(".xls") && !name.endsWith(".xlsx")) {
    toast("请上传 xls 或 xlsx 菜单");
    return;
  }
  state.running = true;
  setControls();
  setProgress(15, "正在上传菜单", 1);
  const fd = new FormData();
  fd.append("file", file);
  try {
    const data = await api("/api/upload-menu", { method: "POST", body: fd });
    state.uploaded = true;
    state.menu = data.menu;
    state.plan = null;
    state.style = "";
    state.pendingStyle = "";
    state.confirmed = false;
    state.selectedRows.clear();
    renderWaiting();
    toast("菜单已上传，可以开始做图");
  } finally {
    state.running = false;
    setControls();
  }
}

async function startJob() {
  if (!state.uploaded) return toast("请先选择菜单");
  state.running = true;
  state.confirmed = false;
  state.selectedRows.clear();
  setControls();
  setProgress(38, "正在识别菜品和品类", 2);
  try {
    await new Promise(resolve => setTimeout(resolve, 260));
    setProgress(56, "正在生成风格方案", 2);
    state.plan = await api("/api/plan");
    state.style = state.plan.selectedStyle;
    state.pendingStyle = "";
    setProgress(66, "请选择一套图片风格", 3);
    renderPlan(false);
    scrollToPanel("#stylesPanel");
  } finally {
    state.running = false;
    setControls();
  }
}

async function confirmStyle() {
  if (!state.plan || !state.pendingStyle) return toast("请先选择风格");
  state.running = true;
  setControls();
  setProgress(82, "正在生成整店图片预览", 4);
  try {
    await new Promise(resolve => setTimeout(resolve, 320));
    state.plan = await api(`/api/plan?style=${encodeURIComponent(state.pendingStyle)}`);
    state.style = state.plan.selectedStyle;
    state.pendingStyle = state.plan.selectedStyle;
    state.confirmed = true;
    state.selectedRows.clear();
    setProgress(100, "图片预览已生成，可以选择导出", 5);
    renderPlan(true);
    scrollToPanel("#previewPanel");
    toast("图片预览已生成");
  } finally {
    state.running = false;
    setControls();
  }
}

function chooseRows(mode) {
  if (!state.plan) return;
  state.selectedRows.clear();
  state.plan.results.forEach((row, index) => {
    if (mode === "all" || (mode === "single" && row.kind === "单品") || (mode === "combo" && row.kind === "套餐/组合")) {
      state.selectedRows.add(index + 1);
    }
  });
  renderPreview();
}

async function exportImages() {
  if (!state.confirmed) return toast("请先生成图片预览");
  const scope = $("#scopeSelect").value;
  const selectedRows = scope === "selected" ? [...state.selectedRows] : [];
  if (scope === "selected" && !selectedRows.length) return toast("请先勾选要导出的图片");
  const data = await api("/api/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ style: state.style, scope, selectedRows, format: $("#formatSelect").value })
  });
  toast(`已打包 ${data.images} 张图片`);
  location.href = data.download;
}

async function refreshAccount() {
  try {
    state.account = await api("/api/account");
  } catch {
    state.account = { balance: 0, rate: "1 元 = 10 积分", packages: [] };
  }
  renderRecharge();
}

async function refreshMenuStatus() {
  await refreshAccount();
  try {
    const data = await api("/api/menu-status");
    state.uploaded = data.uploaded;
    state.menu = data.menu || null;
  } catch {
    state.uploaded = false;
    state.menu = null;
  }
  renderWaiting();
}

function openRecharge() {
  $("#rechargeModal").classList.add("show");
  $("#rechargeModal").setAttribute("aria-hidden", "false");
}

function closeRecharge() {
  $("#rechargeModal").classList.remove("show");
  $("#rechargeModal").setAttribute("aria-hidden", "true");
}

$("#chooseMenuBtn").onclick = () => $("#menuFile").click();
$("#menuFile").onchange = () => uploadMenu().catch(e => toast(e.message));
$("#startJobBtn").onclick = () => startJob().catch(e => toast(e.message));
$("#confirmStyleBtn").onclick = () => confirmStyle().catch(e => toast(e.message));
$("#selectAllBtn").onclick = () => chooseRows("all");
$("#selectSingleBtn").onclick = () => chooseRows("single");
$("#selectComboBtn").onclick = () => chooseRows("combo");
$("#exportShortcutBtn").onclick = () => scrollToPanel("#exportView");
$("#exportZipBtn").onclick = () => exportImages().catch(e => toast(e.message));
$("#rechargeBtn").onclick = openRecharge;
$("#closeRechargeBtn").onclick = closeRecharge;
$("#loginBtn").onclick = () => toast("登录系统接口已预留，下一步接手机号/微信登录");
$("#rechargeModal").onclick = event => {
  if (event.target.id === "rechargeModal") closeRecharge();
};

refreshMenuStatus();
