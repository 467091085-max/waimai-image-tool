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
  stylePreview: null,
  watermark: defaultWatermark(),
  deliveryPlatforms: [],
  quality: "standard"
};

const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const esc = v => String(v ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

function defaultWatermark() {
  return {
    enabled: false,
    type: "text",
    text: "",
    logoData: "",
    position: "bottom-right",
    pattern: "corner"
  };
}

const platformMeta = {
  meituan: { name: "美团外卖", size: "800×600", width: 800, height: 600, maxKB: 5120 },
  taobao: { name: "淘宝外卖/饿了么", size: "800×800", width: 800, height: 800, maxKB: 20480 },
  jd: { name: "京东外卖/京东秒送", size: "800×800", width: 800, height: 800, maxKB: 5120 }
};

const qualityMeta = {
  standard: { name: "普通出图", points: 10 },
  premium: { name: "精修出图", points: 20 }
};

function currentQuality() {
  return qualityMeta[state.quality] || qualityMeta.standard;
}

function imagePoints() {
  return currentQuality().points;
}

function primaryPlatform() {
  return [...state.deliveryPlatforms].reverse().find(id => platformMeta[id]) || "meituan";
}

function setPreviewAspect() {
  const meta = platformMeta[primaryPlatform()] || platformMeta.meituan;
  document.documentElement.style.setProperty("--preview-aspect", `${meta.width} / ${meta.height}`);
}

function sizeLimitText(meta) {
  const kb = meta?.maxKB || 5120;
  return kb >= 1024 ? `${Math.round(kb / 1024)}MB` : `${kb}KB`;
}

function baseImageCharge() {
  const total = state.plan?.summary?.total ?? state.menu?.count ?? 0;
  return total * imagePoints();
}

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
  const el = $(id);
  if (!el) return;
  const topbar = $(".topbar")?.getBoundingClientRect().height || 0;
  const offset = topbar + 14;
  const top = window.scrollY + el.getBoundingClientRect().top - offset;
  window.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
}

function watermarkCharge() {
  if (!state.plan || !state.watermark.enabled) return 0;
  return state.plan.pricing.watermarkPoints || 0;
}

function totalCharge() {
  return baseImageCharge() + watermarkCharge() + platformCharge();
}

function platformCharge() {
  if (!state.plan) return 0;
  const extraCount = Math.max(0, state.deliveryPlatforms.length - 1);
  return extraCount * (state.plan.pricing.extraPlatformPoints || 0);
}

function menuCounts() {
  const counts = state.plan?.menu?.kindCounts || state.menu?.kindCounts || {};
  const total = counts.total ?? state.plan?.summary?.total ?? state.menu?.count ?? 0;
  const single = counts.single ?? state.plan?.results?.filter(row => row.kind === "单品").length ?? 0;
  const combo = counts.combo ?? state.plan?.results?.filter(row => row.kind === "套餐/组合").length ?? 0;
  const snack = counts.snack ?? Math.max(0, total - single - combo);
  return { total, single, combo, snack };
}

function watermarkPayload() {
  return {
    enabled: Boolean(state.watermark.enabled),
    type: state.watermark.type,
    text: state.watermark.text || state.menu?.store || "品牌水印",
    logoData: state.watermark.logoData || "",
    position: state.watermark.position,
    pattern: state.watermark.pattern
  };
}

function exportPlatforms() {
  const chosen = $("#platformSelect")?.value || "purchased";
  if (!state.deliveryPlatforms.length) {
    toast("请至少选择一个交付平台");
    return [];
  }
  if (chosen === "purchased") return [...state.deliveryPlatforms];
  if (!state.deliveryPlatforms.includes(chosen)) {
    toast(`请先在交付平台和尺寸里勾选${platformMeta[chosen]?.name || "该平台"}`);
    return [];
  }
  return [chosen];
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
  return (state.menu?.count || 0) * imagePoints();
}

function publicStatus(row) {
  if (row.publicStatus) return row.publicStatus;
  if (!row.candidates?.length) return "待补图";
  return "已生成";
}

function setControls() {
  const menuFile = state.menu?.file || "菜单";
  const menuCount = state.menu?.count ?? 0;
  const startSub = $("#startJobBtn")?.querySelector(".step-copy span");
  const startEm = $("#startJobBtn")?.querySelector("em");
  const exportEm = $("#exportShortcutBtn")?.querySelector("em");
  $("#startJobBtn").disabled = !state.uploaded || state.running;
  if (startSub) {
    startSub.textContent = state.running ? "正在处理，请稍候" : state.plan ? "可重新生成免费样图" : "上传菜单后自动展示";
  }
  if (startEm) {
    startEm.textContent = state.running ? "处理中" : state.plan ? "重新预览" : state.uploaded ? "自动开始" : "等待菜单";
  }
  $("#confirmStyleBtn").disabled = !state.plan || !state.pendingStyle || state.running || !state.deliveryPlatforms.length;
  $("#confirmStyleBtn").textContent = state.plan ? `扣 ${totalCharge()} 积分，生成正式图` : "确认风格，生成正式图";
  $("#exportShortcutBtn").disabled = !state.confirmed;
  if (exportEm) exportEm.textContent = state.confirmed ? "去导出" : "生成后可用";
  $("#exportZipBtn").disabled = !state.confirmed;
  $("#menuStatus").textContent = state.uploaded ? `菜单已就绪：${menuFile} · ${menuCount} 个菜` : "等待选择菜单";
  $("#menuStatus").className = `menu-status ${state.uploaded ? "good" : ""}`;
  $("#pointsBalance").textContent = `${state.account.balance || 0}`;
  updateChargeText();
  renderQualityControls();
  renderPlatformControls();
  unlockPanels();
}

function updateChargeText() {
  if (!state.plan) return;
  const base = baseImageCharge();
  const wm = watermarkCharge();
  const platform = platformCharge();
  $("#cash").textContent = `${base + wm + platform} 积分`;
  const hint = $("#confirmChargeHint");
  if (hint) {
    const parts = [`正式出图 ${base} 积分`];
    if (wm) parts.push(`品牌水印 ${wm} 积分`);
    if (platform) parts.push(`增加平台 ${platform} 积分`);
    hint.textContent = state.deliveryPlatforms.length
      ? `${parts.join(" + ")}，确认后一次性扣除。`
      : "请先选择至少一个交付平台，首个平台免费。";
  }
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
    `<button class="recharge-card" data-points="${pkg.points + (pkg.bonus || 0)}" type="button">
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
  updateCustomRechargeHint();
}

function updateCustomRechargeHint() {
  const input = $("#customRechargePoints");
  const hint = $("#customRechargeCash");
  if (!input || !hint) return;
  const points = Number(input.value || 0);
  if (!points) {
    hint.textContent = "最低 100 积分起充";
    return;
  }
  const cash = (points / 10).toFixed(points % 10 === 0 ? 0 : 1);
  hint.textContent = points < 100 ? "最低 100 积分起充" : `约 ¥${cash}`;
}

function submitCustomRecharge() {
  const input = $("#customRechargePoints");
  const points = Math.floor(Number(input?.value || 0));
  if (!Number.isFinite(points) || points < 100) {
    toast("自定义充值最低 100 积分起充");
    input?.focus();
    return;
  }
  state.account.balance += points;
  setControls();
  closeRecharge();
  toast(`已模拟充值 ${points} 积分`);
}

function renderWaiting() {
  const count = state.menu?.count || 0;
  const counts = menuCounts();
  $("#items").textContent = count || "-";
  $("#singleCount").textContent = state.uploaded ? `${counts.single || 0} 张` : "-";
  $("#comboCount").textContent = state.uploaded ? `${counts.combo || 0} 张` : "-";
  $("#snackCount").textContent = state.uploaded ? `${counts.snack || 0} 张` : "-";
  $("#imageCount").textContent = count ? `${count} 张` : "-";
  $("#cash").textContent = count ? `${estimatedFormalPoints()} 积分` : "-";
  setProgress(state.uploaded ? 22 : 8, state.uploaded ? "菜单已上传，正在自动生成风格方案" : "等待选择菜单", state.uploaded ? 2 : 1);
  renderWorkflow([
    { title: "选择菜单", status: state.uploaded ? "已完成" : "等待上传", state: state.uploaded ? "done" : "active" },
    { title: "风格预览", status: state.uploaded ? "自动生成中" : "待菜单", state: state.uploaded ? "active" : "" },
    { title: "选择风格", status: "待生成方案" },
    { title: "正式生图", status: "待扣积分" },
    { title: "导出图片", status: "待预览" }
  ]);
  $("#styleBox").innerHTML = `<div class="empty">菜单上传后会自动展示可选风格</div>`;
  $("#stylePreviewBox").className = "style-preview-box";
  $("#stylePreviewBox").innerHTML = previewPlaceholders("等待菜单");
  $("#selectedStyleHint").textContent = "还没有选择风格";
  $("#summary").innerHTML = "";
  $("#reworkBanner").innerHTML = "";
  $("#resultBox").innerHTML = `<div class="empty">扣积分生成后展示正式图片</div>`;
  renderQualityControls();
  setControls();
}

function renderPlan(showPreview = false) {
  const p = state.plan;
  const ready = p.results.filter(r => r.candidates?.length).length;
  const needsWork = p.summary.total - ready;
  const counts = menuCounts();
  const basePoints = baseImageCharge();
  const quality = currentQuality();
  if (p.account && !state.accountLoaded) {
    state.account = p.account;
    state.accountLoaded = true;
  }
  $("#items").textContent = p.menu.count;
  $("#singleCount").textContent = `${counts.single} 张`;
  $("#comboCount").textContent = `${counts.combo} 张`;
  $("#snackCount").textContent = `${counts.snack} 张`;
  $("#imageCount").textContent = `${p.summary.total} 张`;
  $("#cash").textContent = `${totalCharge()} 积分`;
  renderWorkflow([
    { title: "选择菜单", status: `${p.menu.count} 个菜品`, state: "done" },
    { title: "风格预览", status: `已展示 ${p.styles.length} 套风格`, state: "done" },
    { title: "选择风格", status: state.confirmed ? styleName(p.selectedStyle) : "请选择一套", state: state.confirmed ? "done" : "active" },
    { title: "正式生图", status: state.confirmed ? `已扣 ${state.chargedPoints || basePoints} 积分` : `待扣 ${basePoints} 积分`, state: state.confirmed ? "done" : "" },
    { title: "导出图片", status: state.confirmed ? "可以导出" : "待预览", state: state.confirmed ? "active" : "" }
  ]);
  renderStyles();
  renderStylePreview();
  renderQualityControls();
  renderPlatformControls();
  renderWatermarkControls();
  if (showPreview) renderPreview();
  else $("#resultBox").innerHTML = `<div class="empty">选择风格并确认后，系统会扣积分生成全部正式图片</div>`;
  $("#summary").innerHTML = [
    `正式图 ${p.summary.total} 张`,
    `${quality.name} · ${quality.points} 积分/张`,
    `交付平台 ${state.deliveryPlatforms.length} 个`,
    p.generation?.configured ? `腾讯云已接入 · 本次 ${p.generation.succeeded || 0} 张` : "",
    state.watermark.enabled ? `品牌水印 ${p.pricing.watermarkPoints} 积分/单` : "品牌水印可选",
    `自定义修改 ${p.pricing.customEditPoints} 积分/次`,
    needsWork ? `待补图 ${needsWork} 张` : "全部可生成"
  ].filter(Boolean).map(x => `<span class="pill">${esc(x)}</span>`).join("");
  renderReworkBanner();
  renderRecharge();
  setControls();
}

function renderPlatformControls() {
  setPreviewAspect();
  const locked = state.confirmed;
  $$(".platform-check").forEach(input => {
    input.checked = state.deliveryPlatforms.includes(input.value);
    input.disabled = locked;
    const meta = platformMeta[input.value];
    const desc = input.closest(".platform-option")?.querySelector("em");
    if (desc && meta) {
      const selectedIndex = state.deliveryPlatforms.indexOf(input.value);
      const priceText = selectedIndex < 0
        ? (state.deliveryPlatforms.length ? "加选 +100积分" : "选中免费")
        : (selectedIndex === 0 ? "当前免费" : "已加选 +100积分");
      desc.textContent = `${meta.size} · ≤${sizeLimitText(meta)} · ${priceText}`;
    }
  });
  const names = state.deliveryPlatforms.map(id => {
    const meta = platformMeta[id];
    return `${meta?.name || id} ${meta?.size || ""} · ≤${sizeLimitText(meta)}`;
  });
  const charge = platformCharge();
  $("#platformChargeHint").textContent = names.length
    ? (charge ? `${names.join(" / ")} · +${charge}积分` : `${names.join(" / ")} · 免费`)
    : "请选择至少 1 个平台";
  const select = $("#platformSelect");
  if (select) {
    if (select.value !== "purchased" && !state.deliveryPlatforms.includes(select.value)) {
      select.value = "purchased";
    }
    Array.from(select.options).forEach(option => {
      option.disabled = false;
    });
  }
}

function renderQualityControls() {
  const quality = currentQuality();
  $$(".quality-radio").forEach(input => {
    input.checked = input.value === state.quality;
    input.disabled = state.confirmed || state.running;
  });
  const hint = $("#qualityChargeHint");
  if (hint) {
    hint.textContent = `${quality.name} · ${quality.points}积分/张`;
  }
}

function renderWatermarkControls() {
  const enabled = $("#watermarkEnabled");
  if (!enabled) return;
  setPreviewAspect();
  const meta = platformMeta[primaryPlatform()] || platformMeta.meituan;
  const locked = state.confirmed;
  enabled.checked = state.watermark.enabled;
  enabled.disabled = locked;
  $("#watermarkType").value = state.watermark.type;
  $("#watermarkText").value = state.watermark.text;
  $("#watermarkPosition").value = state.watermark.position;
  $("#watermarkPattern").value = state.watermark.pattern;
  $("#watermarkOptions").classList.toggle("disabled", !state.watermark.enabled || locked);
  $("#watermarkTextWrap").style.display = state.watermark.type === "logo" ? "none" : "grid";
  $("#watermarkLogoWrap").style.display = state.watermark.type === "logo" ? "grid" : "none";
  ["#watermarkType", "#watermarkText", "#watermarkLogo", "#watermarkPosition", "#watermarkPattern"].forEach(selector => {
    const field = $(selector);
    if (field) field.disabled = locked;
  });
  const demo = $("#watermarkDemo");
  const text = state.watermark.text || state.menu?.store || "品牌名";
  demo.className = `watermark-demo ${state.watermark.enabled ? "enabled" : ""} ${state.watermark.pattern} ${state.watermark.position}`;
  demo.innerHTML = `
    <span>${locked ? "水印已锁定" : `水印预览 · ${meta.name} ${meta.size}`}</span>
    <div class="watermark-preview-canvas">
      ${watermarkOverlay(text)}
    </div>
  `;
  updateChargeText();
}

function watermarkOverlay(fallbackText = "品牌名") {
  if (!state.watermark.enabled) return "";
  const label = state.watermark.type === "logo" && state.watermark.logoData
    ? `<img src="${state.watermark.logoData}" alt="品牌 Logo">`
    : `<b>${esc(state.watermark.text || fallbackText)}</b>`;
  if (state.watermark.pattern === "tile") {
    return `<div class="wm-overlay tile">${Array.from({ length: 9 }, () => `<span>${label}</span>`).join("")}</div>`;
  }
  return `<div class="wm-overlay corner ${state.watermark.position}">${label}</div>`;
}

function renderReworkBanner() {
  const box = $("#reworkBanner");
  if (!box || !state.plan || !state.confirmed) {
    if (box) box.innerHTML = "";
    return;
  }
  const total = state.plan.pricing.freeReworkQuota;
  const left = Math.max(0, state.freeReworkRemaining);
  const used = Math.max(0, total - left);
  const price = imagePoints();
  box.className = `rework-banner ${left ? "has-free" : "paid-only"}`;
  box.innerHTML = `
    <b>${left ? `免费换版剩余 ${left}/${total} 张` : "免费换版已用完"}</b>
    <span>${left ? `已使用 ${used} 张，用完后每次 ${price} 积分/张` : `继续换一版将扣 ${price} 积分/张`}</span>
  `;
}

function styleName(styleId) {
  return state.plan?.styles.find(s => s.id === styleId)?.name || "已选风格";
}

function previewPlaceholders(text = "免费样图") {
  return Array.from({ length: 6 }, (_, index) => `
    <div class="preview-sample placeholder">
      <b>样图 ${index + 1}</b>
      <div class="sample-frame"></div>
      <p>${esc(text)}</p>
    </div>
  `).join("");
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
      toast("风格已选中，正在生成 6 张免费单品样图");
    };
  });
}

function renderStylePreview() {
  const box = $("#stylePreviewBox");
  if (!box) return;
  if (!state.pendingStyle) {
    box.className = "style-preview-box";
    box.innerHTML = previewPlaceholders("选择风格后生成");
    return;
  }
  if (!state.stylePreview || state.stylePreview.style !== state.pendingStyle) {
    box.className = "style-preview-box";
    box.innerHTML = previewPlaceholders("正在生成");
    return;
  }
  box.className = "style-preview-box";
  if (!state.stylePreview.samples.length) {
    box.className = "style-preview-box empty";
    box.innerHTML = "当前菜单没有可预览的单品，套餐会在正式生成后展示";
    return;
  }
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
  setProgress(72, "6 张免费单品样图已生成，请确认风格", 3);
  setControls();
  scrollToPanel("#stylePreviewBox");
}

function renderPreview() {
  const p = state.plan;
  if (!state.selectedRows.size) {
    p.results.forEach((_, index) => state.selectedRows.add(index + 1));
  }
  const redrawLabel = state.freeReworkRemaining > 0
    ? `换一版（免费剩 ${state.freeReworkRemaining}）`
    : `换一版 ${imagePoints()}积分`;
  const card = (row, index) => {
    const rowNo = index + 1;
    const candidate = row.candidates[0];
    const checked = state.selectedRows.has(rowNo) ? "checked" : "";
    const status = publicStatus(row);
    return `<div class="result">
      <label class="select-line"><input type="checkbox" class="row-check" data-row="${rowNo}" ${checked}> 选择</label>
      <div class="result-title">${esc(row.name)}</div>
      ${candidate ? `<div class="image-wrap"><img src="${candidate.url}" alt="${esc(row.name)}">${watermarkOverlay(state.menu?.store || "品牌名")}</div>` : `<div class="empty image-empty">待补图</div>`}
      <div class="result-body">
        <p>${esc(row.category || "未分类")} · ${esc(row.kind)}</p>
        <div><span class="pill success">${esc(status)}</span><span class="pill">正式图 ${row.points} 积分</span></div>
        ${candidate ? `<div class="result-actions"><button class="single-save-btn" data-row="${rowNo}" type="button">单张保存</button><button class="redraw-btn" data-row="${rowNo}" type="button">${redrawLabel}</button><button class="refine-btn" data-row="${rowNo}" type="button">自定义修改 10积分</button></div>` : `<button class="refine-btn" data-row="${rowNo}" type="button">自定义修改 10积分</button>`}
      </div>
    </div>`;
  };
  const groups = [
    { title: "单品图片", note: "常规菜品图，适合直接上架。", rows: [] },
    { title: "套餐图片", note: "按套餐名和拆分菜品组合成图，和单品分开审核、分开导出。", rows: [] },
    { title: "其他图片", note: "饮品、小食或暂未归类的图片。", rows: [] }
  ];
  p.results.forEach((row, index) => {
    if (row.kind === "单品") groups[0].rows.push({ row, index });
    else if (row.kind === "套餐/组合") groups[1].rows.push({ row, index });
    else groups[2].rows.push({ row, index });
  });
  $("#resultBox").innerHTML = groups.filter(group => group.rows.length).map(group => `
    <section class="result-section">
      <div class="result-section-head">
        <div>
          <h4>${esc(group.title)}</h4>
          <p>${esc(group.note)}</p>
        </div>
        <span>${group.rows.length} 张</span>
      </div>
      <div class="result-grid">
        ${group.rows.map(({ row, index }) => card(row, index)).join("")}
      </div>
    </section>
  `).join("");
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
  $$(".single-save-btn").forEach(button => {
    button.onclick = () => exportSingle(Number(button.dataset.row)).catch(e => toast(e.message));
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
  let shouldAutoStart = false;
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
    state.watermark = defaultWatermark();
    state.deliveryPlatforms = [];
    state.selectedRows.clear();
    renderWaiting();
    shouldAutoStart = true;
    toast("菜单已上传，正在自动生成风格方案");
  } finally {
    state.running = false;
    setControls();
  }
  if (shouldAutoStart) await startJob({ auto: true });
}

async function startJob(options = {}) {
  if (!state.uploaded) return toast("请先选择菜单");
  if (state.running) return;
  const doScroll = options.doScroll !== false;
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
    setProgress(56, "正在生成 6 张免费单品风格预览", 2);
    state.plan = await api(`/api/plan?quality=${encodeURIComponent(state.quality)}`);
    state.style = state.plan.selectedStyle;
    state.pendingStyle = "";
    setProgress(66, "请选择一套图片风格", 3);
    renderPlan(false);
    if (doScroll) scrollToPanel("#stylesPanel");
    if (options.auto) toast("风格方案已生成，请选择一套风格");
  } finally {
    state.running = false;
    setControls();
  }
}

async function confirmStyle() {
  if (!state.plan || !state.pendingStyle) return toast("请先选择风格");
  if (!state.deliveryPlatforms.length) return toast("请至少选择一个交付平台");
  const charge = totalCharge();
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
    state.plan = await api("/api/generate-final", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ style: state.pendingStyle, quality: state.quality })
    });
    state.quality = state.plan.quality?.id || state.quality;
    state.style = state.plan.selectedStyle;
    state.pendingStyle = state.plan.selectedStyle;
    state.confirmed = true;
    state.freeReworkRemaining = state.plan.pricing.freeReworkQuota;
    state.selectedRows.clear();
    setProgress(100, "正式图片已生成，可以选择导出或精修", 5);
    renderPlan(true);
    scrollToPanel("#previewPanel");
    const gen = state.plan.generation;
    const suffix = gen?.configured ? `，腾讯云生成 ${gen.succeeded || 0} 张` : "";
    toast(`已扣 ${charge} 积分，正式图片已生成${suffix}`);
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
  const price = row.points || imagePoints();
  if (state.freeReworkRemaining > 0) {
    state.freeReworkRemaining -= 1;
    renderPlan(true);
    toast(`${row.name} 已免费换版，剩余 ${state.freeReworkRemaining}/${state.plan.pricing.freeReworkQuota} 张`);
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
  const platforms = exportPlatforms();
  if (!platforms.length) return;
  const data = await api("/api/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ style: state.style, scope, selectedRows, format: $("#formatSelect").value, watermark: watermarkPayload(), platforms, quality: state.quality })
  });
  toast(`已打包 ${data.images} 张图片，${data.platforms.length} 个平台${data.watermark ? "，已添加品牌水印" : ""}`);
  location.href = data.download;
}

async function exportSingle(rowNo) {
  if (!state.confirmed) return toast("请先生成正式图片");
  const platforms = exportPlatforms();
  if (!platforms.length) return;
  const data = await api("/api/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ style: state.style, scope: "selected", selectedRows: [rowNo], format: $("#formatSelect").value, watermark: watermarkPayload(), platforms, quality: state.quality })
  });
  toast(`已准备单张图片，${data.platforms.length} 个平台${data.watermark ? "，已添加品牌水印" : ""}`);
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
  if (state.uploaded) {
    startJob({ auto: true, doScroll: false }).catch(e => toast(e.message));
  }
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
$("#customRechargePoints").oninput = updateCustomRechargeHint;
$("#customRechargeBtn").onclick = submitCustomRecharge;
$("#closeRefineBtn").onclick = closeRefine;
$("#submitRefineBtn").onclick = submitRefine;
$("#loginBtn").onclick = () => toast("登录系统接口已预留，下一步接手机号/微信登录");
$$(".platform-check").forEach(input => {
  input.onchange = event => {
    const value = event.target.value;
    if (event.target.checked && !state.deliveryPlatforms.includes(value)) {
      state.deliveryPlatforms.push(value);
    }
    if (!event.target.checked) {
      if (state.deliveryPlatforms.length <= 1) {
        event.target.checked = true;
        toast("请至少保留一个交付平台");
        renderPlatformControls();
        return;
      }
      state.deliveryPlatforms = state.deliveryPlatforms.filter(id => id !== value);
    }
    renderPlatformControls();
    if (state.plan) renderPlan(state.confirmed);
    else setControls();
  };
});
$$(".quality-radio").forEach(input => {
  input.onchange = event => {
    if (state.confirmed) return;
    state.quality = event.target.value;
    renderQualityControls();
    if (state.plan) renderPlan(false);
    else renderWaiting();
    toast(`${currentQuality().name}：${imagePoints()} 积分/张`);
  };
});
$("#watermarkEnabled").onchange = event => {
  state.watermark.enabled = event.target.checked;
  renderWatermarkControls();
  renderPlan(state.confirmed);
};
$("#watermarkType").onchange = event => {
  state.watermark.type = event.target.value;
  renderWatermarkControls();
  if (state.confirmed) renderPreview();
};
$("#watermarkText").oninput = event => {
  state.watermark.text = event.target.value;
  renderWatermarkControls();
  if (state.confirmed) renderPreview();
};
$("#watermarkPosition").onchange = event => {
  state.watermark.position = event.target.value;
  renderWatermarkControls();
  if (state.confirmed) renderPreview();
};
$("#watermarkPattern").onchange = event => {
  state.watermark.pattern = event.target.value;
  renderWatermarkControls();
  if (state.confirmed) renderPreview();
};
$("#watermarkLogo").onchange = event => {
  const file = event.target.files?.[0];
  if (!file) return;
  if (file.type !== "image/png" && !file.name.toLowerCase().endsWith(".png")) {
    event.target.value = "";
    return toast("Logo 请上传透明 PNG 文件");
  }
  const reader = new FileReader();
  reader.onload = () => {
    state.watermark.logoData = String(reader.result || "");
    state.watermark.type = "logo";
    renderWatermarkControls();
    if (state.confirmed) renderPreview();
  };
  reader.readAsDataURL(file);
};
$("#rechargeModal").onclick = event => {
  if (event.target.id === "rechargeModal") closeRecharge();
};
$("#refineModal").onclick = event => {
  if (event.target.id === "refineModal") closeRefine();
};

refreshMenuStatus();
