const state = {
  plan: null,
  style: "",
  pendingStyle: "",
  menu: null,
  uploaded: false,
  running: false,
  confirmed: false,
  selectedRows: new Set()
};

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

function switchView(viewId) {
  document.querySelectorAll(".tab,.view").forEach(el => el.classList.remove("active"));
  document.querySelector(`.tab[data-view="${viewId}"]`)?.classList.add("active");
  $(`#${viewId}`)?.classList.add("active");
}

function setProgress(percent, text) {
  $("#progressBar").style.width = `${percent}%`;
  $("#progressText").textContent = text;
}

function publicStatus(row) {
  if (!row.candidates?.length) return "待补图";
  if (row.status === "直接可用") return "已生成";
  return "待精修";
}

function setControls() {
  const menuFile = state.menu?.file || "菜单";
  const menuCount = state.menu?.count ?? 0;
  $("#startJobBtn").disabled = !state.uploaded || state.running;
  $("#startJobBtn").textContent = state.running ? "生成中..." : state.plan ? "重新开始" : "开始做图";
  $("#confirmStyleBtn").disabled = !state.plan || !state.pendingStyle || state.running;
  $("#exportBtn").disabled = !state.confirmed;
  $("#exportZipBtn").disabled = !state.confirmed;
  $("#menuStatus").textContent = state.uploaded ? `菜单已就绪：${menuFile} · ${menuCount} 个菜` : "等待上传菜单";
  $("#menuStatus").className = `menu-status ${state.uploaded ? "good" : ""}`;
}

function renderWorkflow(items) {
  $("#workflowBox").innerHTML = items.map((item, index) => (
    `<div class="step ${item.state || ""}"><i>${index + 1}</i><b>${esc(item.title)}</b><p>${esc(item.status)}</p><p>${esc(item.desc)}</p></div>`
  )).join("");
}

function renderWaiting() {
  $("#items").textContent = state.menu?.count ?? "-";
  $("#category").textContent = state.uploaded ? "待识别" : "-";
  $("#imageCount").textContent = "-";
  $("#cash").textContent = "-";
  setProgress(state.uploaded ? 16 : 0, state.uploaded ? "菜单已上传，点击开始做图" : "等待上传菜单");
  renderWorkflow([
    { title: "上传菜单", status: state.uploaded ? "已完成" : "待上传", desc: state.uploaded ? `${state.menu?.count ?? 0} 个菜品` : "支持 xls / xlsx", state: state.uploaded ? "done" : "" },
    { title: "开始生成", status: "待开始", desc: "解析品类和菜品结构" },
    { title: "选择风格", status: "待选择", desc: "选择整店统一视觉方案" },
    { title: "生成预览", status: "待生成", desc: "生成全部菜品图预览" },
    { title: "勾选导出", status: "待导出", desc: "按需要导出单张或 ZIP" }
  ]);
  $("#styleBox").innerHTML = `<div class="empty">点击开始做图后展示风格方案</div>`;
  $("#summary").innerHTML = "";
  $("#resultBox").innerHTML = `<div class="empty">确认风格后展示图片预览</div>`;
  $("#paidBox").innerHTML = `<div class="empty">生成预览后展示费用和增值服务</div>`;
  setControls();
}

function renderPlan(showPreview = false) {
  const p = state.plan;
  const ready = p.results.filter(r => r.candidates?.length).length;
  const needsWork = p.summary.total - ready;
  $("#items").textContent = p.menu.count;
  $("#category").textContent = p.category.category;
  $("#imageCount").textContent = p.summary.total;
  $("#cash").textContent = `¥${p.quote.cash}`;
  renderWorkflow([
    { title: "上传菜单", status: "已完成", desc: `${p.menu.count} 个菜品`, state: "done" },
    { title: "开始生成", status: "已完成", desc: `${p.category.category}`, state: "done" },
    { title: "选择风格", status: state.confirmed ? "已确认" : "请选择", desc: state.confirmed ? styleName(p.selectedStyle) : "点击风格卡片后确认", state: state.confirmed ? "done" : "active" },
    { title: "生成预览", status: state.confirmed ? "已完成" : "待确认风格", desc: state.confirmed ? `${p.summary.total} 张图片预览` : "确认后开始生成", state: state.confirmed ? "done" : "" },
    { title: "勾选导出", status: state.confirmed ? "可导出" : "待生成", desc: "支持单张保存和 ZIP 打包", state: state.confirmed ? "active" : "" }
  ]);
  renderStyles();
  if (showPreview) renderPreview();
  else $("#resultBox").innerHTML = `<div class="empty">请选择一个风格，然后点击确认风格并生成预览</div>`;
  $("#summary").innerHTML = [
    `图片 ${p.summary.total} 张`,
    `已生成 ${ready} 张`,
    needsWork ? `待精修 ${needsWork} 张` : "全部可预览",
    `预估 ${p.summary.points} 积分`
  ].map(x => `<span class="pill">${esc(x)}</span>`).join("");
  renderPaid();
  setControls();
}

function styleName(styleId) {
  return state.plan?.styles.find(s => s.id === styleId)?.name || "已选风格";
}

function renderStyles() {
  const p = state.plan;
  $("#styleBox").innerHTML = p.styles.map(s => {
    const selected = s.id === state.pendingStyle;
    const ready = s.direct + s.review;
    return `<button class="style ${selected ? "active" : ""}" data-style="${s.id}">
      <img src="${s.sample?.url || ""}">
      <span class="style-body">
        <b>${esc(s.name)}</b>
        <span>适合本菜单约 ${Math.round((ready / Math.max(1, p.summary.total)) * 100)}%</span>
        <span>${selected ? "已选择，点击右上角确认" : "点击选择这个风格"}</span>
      </span>
    </button>`;
  }).join("");
  document.querySelectorAll(".style").forEach(button => {
    button.onclick = () => {
      state.pendingStyle = button.dataset.style;
      renderStyles();
      setControls();
      toast("已选择风格，请点击确认");
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
        <div><span class="pill">${esc(status)}</span><span class="pill">${row.points}积分</span></div>
        ${candidate ? `<a class="save-link" href="${candidate.url}" download="${esc(row.name)}.jpg">单张保存</a>` : `<p>可进入定制配菜或人工精修</p>`}
      </div>
    </div>`;
  }).join("");
  document.querySelectorAll(".row-check").forEach(input => {
    input.onchange = () => {
      const rowNo = Number(input.dataset.row);
      if (input.checked) state.selectedRows.add(rowNo);
      else state.selectedRows.delete(rowNo);
    };
  });
}

function renderPaid() {
  const p = state.plan;
  $("#paidBox").innerHTML = `<div class="paid"><div class="price">¥${p.quote.cash}</div><b>${esc(p.quote.package)}</b><p>${p.quote.points} 积分 · ${p.quote.rate}</p></div>` +
    p.quote.addOns.map(a => `<div class="paid"><b>${esc(a.name)}</b><p>¥${a.price}</p></div>`).join("") +
    `<div class="paid"><b>邀请奖励</b><p>注册送 ${p.quote.referral.registerReward} 积分；首付 ${p.quote.referral.firstPayReward}</p></div>`;
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
  setProgress(8, "正在上传菜单");
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
    toast("菜单已上传");
  } finally {
    state.running = false;
    setControls();
  }
}

async function startJob() {
  if (!state.uploaded) return toast("请先上传菜单");
  state.running = true;
  state.confirmed = false;
  state.selectedRows.clear();
  setControls();
  setProgress(28, "正在识别菜品和品类");
  toast("开始生成方案");
  try {
    await new Promise(resolve => setTimeout(resolve, 280));
    setProgress(48, "正在生成风格方案");
    state.plan = await api("/api/plan");
    state.style = state.plan.selectedStyle;
    state.pendingStyle = state.plan.selectedStyle;
    setProgress(62, "请选择风格方案");
    renderPlan(false);
    switchView("styles");
  } finally {
    state.running = false;
    setControls();
  }
}

async function confirmStyle() {
  if (!state.plan || !state.pendingStyle) return toast("请先选择风格");
  state.running = true;
  setControls();
  setProgress(72, "正在按所选风格生成预览");
  try {
    await new Promise(resolve => setTimeout(resolve, 300));
    state.plan = await api(`/api/plan?style=${encodeURIComponent(state.pendingStyle)}`);
    state.style = state.plan.selectedStyle;
    state.pendingStyle = state.plan.selectedStyle;
    state.confirmed = true;
    state.selectedRows.clear();
    setProgress(100, "图片预览已生成");
    renderPlan(true);
    switchView("preview");
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
  if (!state.confirmed) return toast("请先确认风格并生成预览");
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

async function refreshMenuStatus() {
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

document.querySelectorAll(".tab").forEach(button => {
  button.onclick = () => switchView(button.dataset.view);
});

$("#menuFile").onchange = () => uploadMenu().catch(e => toast(e.message));
$("#startJobBtn").onclick = () => startJob().catch(e => toast(e.message));
$("#confirmStyleBtn").onclick = () => confirmStyle().catch(e => toast(e.message));
$("#selectAllBtn").onclick = () => chooseRows("all");
$("#selectSingleBtn").onclick = () => chooseRows("single");
$("#selectComboBtn").onclick = () => chooseRows("combo");
$("#exportBtn").onclick = () => switchView("exportView");
$("#exportZipBtn").onclick = () => exportImages().catch(e => toast(e.message));

refreshMenuStatus();
