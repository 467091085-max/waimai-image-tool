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
  accountLoaded: false,
  stage: 1,
  charged: false,
  chargedPoints: 0,
  refineRow: null,
  freeReworkRemaining: 0,
  stylePreview: null
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

function estimatedFormalPoints() {
  return (state.menu?.count || 0) * 10;
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
  $("#startJobBtn").querySelector("span").textContent = state.running ? "生成中" : state.plan ? "重新预览" : "免费 5 张";
  $("#confirmStyleBtn").disabled = !state.plan || !state.pendingStyle || state.running;
  $("#confirmStyleBtn").textContent = state.plan ? `扣 ${state.plan.quote.points} 积分，生成正式图` : "确认风格，生成正式图";
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
  const count = state.menu?.count || 0;
  $("#items").textContent = count || "-";
  $("#category").textContent = state.uploaded ? "待识别" : "-";
  $("#imageCount").textContent = count ? `${count} 张` : "-";
  $("#cash").textContent = count ? `${estimatedFormalPoints()} 积分` : "-";
  setProgress(state.uploaded ? 22 : 8, state.uploaded ? "菜单已上传，点击生成风格预览" : "等待选择菜单", state.uploaded ? 2 : 1);
  renderWorkflow([
    { title: "选择菜单", status: state.uploaded ? "已完成" : "等待上传", state: state.uploaded ? "done" : "active" },
    { title: "风格预览", status: state.uploaded ? "免费 5 张" : "待菜单", state: state.uploaded ? "active" : "" },
    { title: "选择风格", status: "待生成方案" },
    { title: "正式生图", status: "待扣积分" },
    { title: "导出图片", status: "待预览" }
  ]);
  $("#styleBox").innerHTML = `<div class="empty">点击“风格预览”后展示 5 套样图</div>`;
  $("#stylePreviewBox").className = "style-preview-box empty";
  $("#stylePreviewBox").innerHTML = "先选择一个图库风格，这里会展示 5 张免费样图";
  $("#selectedStyleHint").textContent = "还没有选择风格";
  $("#summary").innerHTML = "";
  $("#resultBox").innerHTML = `<div class="empty">扣积分生成后展示正式图片</div>`;
  setControls();
}

function renderPlan(showPreview = false) {
  const p = state.plan;
  const ready = p.results.filter(r => r.candidates?.length).length;
  const needsWork = p.summary.total - ready;
  if (p.account && !state.accountLoaded) {
    state.account = p.account;
    state.accountLoaded = true;
  }
  $("#items").textContent = p.menu.count;
  $("#category").textContent = p.category.category;
  $("#imageCount").textContent = p.summary.total;
  $("#cash").textContent = `${p.quote.points} 积分`;
  renderWorkflow([
    { title: "选择菜单", status: `${p.menu.count} 个菜品`, state: "done" },
    { title: "风格预览", status: `免费 ${p.pricing.previewFreeImages} 张`, state: "done" },
    { title: "选择风格", status: state.confirmed ? styleName(p.selectedStyle) : "请选择一套", state: state.confirmed ? "done" : "active" },
    { title: "正式生图", status: state.confirmed ? `已扣 ${state.chargedPoints || p.quote.points} 积分` : `待扣 ${p.quote.points} 积分`, state: state.confirmed ? "done" : "" },
    { title: "导出图片", status: state.confirmed ? "可以导出" : "待预览", state: state.confirmed ? "active" : "" }
  ]);
  renderStyles();
  renderStylePreview();
  if (showPreview) renderPreview();
  else $("#resultBox").innerHTML = `<div class="empty">选择风格并确认后，系统会扣积分生成全部正式图片</div>`;
  $("#summary").innerHTML = [
    `正式图 ${p.summary.total} 张`,
    `出图 ${p.pricing.baseImagePoints} 积分/张`,
    `自定义修改 ${p.pricing.customEditPoints} 积分/次`,
    state.confirmed ? `免费重做剩余 ${state.freeReworkRemaining} 张` : `免费重做 ${p.pricing.freeReworkQuota} 张`,
    needsWork ? `待补图 ${needsWork} 张` : "全部可生成"
  ].map(x => `<span class="pill">${esc(x)}</span>`).join("");
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
      loadStylePreview(state.pendingStyle).catch(e => toast(e.message));
      toast("风格已选中，正在生成 5 张免费样图");
    };
  });
}

function renderStylePreview() {
  const box = $("#stylePreviewBox");
  if (!box) return;
  if (!state.pendingStyle) {
    box.className = "style-preview-box empty";
    box.innerHTML = "先选择一个图库风格，这里会展示 5 张免费样图";
    return;
  }
  if (!state.stylePreview || state.stylePreview.style !== state.pendingStyle) {
    box.className = "style-preview-box empty";
    box.innerHTML = "正在生成 5 张免费样图...";
    return;
  }
  box.className = "style-preview-box";
  box.innerHTML = state.stylePreview.samples.map(sample => {
    const image = sample.candidate;
    return `<div class="preview-sample">
      <b>${esc(sample.name)}</b>
      ${image ? `<img src="${image.url}" alt="${esc(sample.name)}">` : `<div class="empty image-empty">待补图</div>`}
      <p>免费样图</p>
    </div>`;
  }).join("");
}

async function loadStylePreview(styleId) {
  state.stylePreview = null;
  renderStylePreview();
  state.stylePreview = await api(`/api/style-preview?style=${encodeURIComponent(styleId)}`);
  renderStylePreview();
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
      <div class="result-title">${esc(row.name)}</div>
      ${candidate ? `<img src="${candidate.url}" alt="${esc(row.name)}">` : `<div class="empty image-empty">待补图</div>`}
      <div class="result-body">
        <p>${esc(row.category || "未分类")} · ${esc(row.kind)}</p>
        <div><span class="pill success">${esc(status)}</span><span class="pill">正式图 ${row.points} 积分</span></div>
        ${candidate ? `<div class="result-actions"><a class="save-link" href="${candidate.url}" download="${esc(row.name)}.jpg">单张保存</a><button class="redraw-btn" data-row="${rowNo}" type="button">换一版</button><button class="refine-btn" data-row="${rowNo}" type="button">自定义修改 10积分</button></div>` : `<button class="refine-btn" data-row="${rowNo}" type="button">自定义修改 10积分</button>`}
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
  $$(".refine-btn").forEach(button => {
    button.onclick = () => openRefine(Number(button.dataset.row));
  });
  $$(".redraw-btn").forEach(button => {
    button.onclick = () => redrawImage(Number(button.dataset.row));
  });
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
    state.charged = false;
    state.chargedPoints = 0;
    state.freeReworkRemaining = 0;
    state.stylePreview = null;
    state.selectedRows.clear();
    renderWaiting();
    toast("菜单已上传，可以生成风格预览");
  } finally {
    state.running = false;
    setControls();
  }
}

async function startJob() {
  if (!state.uploaded) return toast("请先选择菜单");
  state.running = true;
  state.confirmed = false;
  state.charged = false;
  state.chargedPoints = 0;
  state.freeReworkRemaining = 0;
  state.stylePreview = null;
  state.selectedRows.clear();
  setControls();
  setProgress(38, "正在识别菜品和品类", 2);
  try {
    await new Promise(resolve => setTimeout(resolve, 260));
    setProgress(56, "正在生成 5 张免费风格预览", 2);
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
  const charge = state.plan.quote.points;
  if (!state.charged) {
    if ((state.account.balance || 0) < charge) {
      toast(`积分不足，本单需要 ${charge} 积分`);
      openRecharge();
      return;
    }
    state.account.balance -= charge;
    state.charged = true;
    state.chargedPoints = charge;
  }
  state.running = true;
  setControls();
  setProgress(82, "已扣积分，正在生成全部正式图片", 4);
  try {
    await new Promise(resolve => setTimeout(resolve, 320));
    state.plan = await api(`/api/plan?style=${encodeURIComponent(state.pendingStyle)}`);
    state.style = state.plan.selectedStyle;
    state.pendingStyle = state.plan.selectedStyle;
    state.confirmed = true;
    state.freeReworkRemaining = state.plan.pricing.freeReworkQuota;
    state.selectedRows.clear();
    setProgress(100, "正式图片已生成，可以选择导出或精修", 5);
    renderPlan(true);
    scrollToPanel("#previewPanel");
    toast(`已扣 ${charge} 积分，正式图片已生成`);
  } catch (error) {
    if (state.charged) {
      state.account.balance += charge;
      state.charged = false;
      state.chargedPoints = 0;
      state.freeReworkRemaining = 0;
    }
    throw error;
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

function redrawImage(rowNo) {
  if (!state.confirmed || !state.plan) return toast("请先生成正式图片");
  const row = state.plan.results[rowNo - 1];
  if (!row) return;
  const price = state.plan.pricing.baseImagePoints;
  if (state.freeReworkRemaining > 0) {
    state.freeReworkRemaining -= 1;
    renderPlan(true);
    toast(`${row.name} 已使用 1 次免费重做额度`);
    return;
  }
  if ((state.account.balance || 0) < price) {
    toast(`免费额度已用完，换一版需要 ${price} 积分`);
    openRecharge();
    return;
  }
  state.account.balance -= price;
  renderPlan(true);
  toast(`已扣 ${price} 积分，${row.name} 已重新生成一版`);
}

async function exportImages() {
  if (!state.confirmed) return toast("请先生成正式图片");
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

function openRefine(rowNo) {
  if (!state.confirmed || !state.plan) return toast("请先生成正式图片");
  const row = state.plan.results[rowNo - 1];
  if (!row) return;
  state.refineRow = rowNo;
  $("#refineTitle").textContent = `自定义修改：${row.name}`;
  $("#refinePrompt").value = "";
  $("#refinePrice").textContent = `自定义修改：${state.plan.pricing.customEditPoints} 积分/张`;
  $("#refineModal").classList.add("show");
  $("#refineModal").setAttribute("aria-hidden", "false");
  $("#refinePrompt").focus();
}

function closeRefine() {
  $("#refineModal").classList.remove("show");
  $("#refineModal").setAttribute("aria-hidden", "true");
  state.refineRow = null;
}

function submitRefine() {
  if (!state.refineRow || !state.plan) return;
  const prompt = $("#refinePrompt").value.trim();
  const price = state.plan.pricing.customEditPoints;
  if (!prompt) return toast("请先填写精修要求");
  if ((state.account.balance || 0) < price) {
    toast(`积分不足，精修需要 ${price} 积分`);
    openRecharge();
    return;
  }
  state.account.balance -= price;
  const row = state.plan.results[state.refineRow - 1];
  setControls();
  closeRefine();
  toast(`已扣 ${price} 积分，${row.name} 已提交修改`);
}

async function refreshAccount() {
  try {
    state.account = await api("/api/account");
    state.accountLoaded = true;
  } catch {
    state.account = { balance: 0, rate: "1 元 = 10 积分", packages: [] };
    state.accountLoaded = true;
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
$("#closeRefineBtn").onclick = closeRefine;
$("#submitRefineBtn").onclick = submitRefine;
$("#loginBtn").onclick = () => toast("登录系统接口已预留，下一步接手机号/微信登录");
$("#rechargeModal").onclick = event => {
  if (event.target.id === "rechargeModal") closeRecharge();
};
$("#refineModal").onclick = event => {
  if (event.target.id === "refineModal") closeRefine();
};

refreshMenuStatus();
