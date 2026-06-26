const state = {
  plan: null,
  style: "",
  pendingStyle: "",
  menu: null,
  uploaded: false,
  uploadingFileName: "",
  running: false,
  confirmed: false,
  selectedRows: new Set(),
  account: { balance: 0, rate: "积分充值", packages: [] },
  accountLoaded: false,
  stage: 1,
  charged: false,
  chargedPoints: 0,
  refineRow: null,
  freeReworkRemaining: 0,
  stylePreview: null,
  stylePreviewError: null,
  watermark: defaultWatermark(),
  deliveryPlatforms: [],
  quality: "standard",
  exportStatus: {
    tone: "idle",
    title: "等待打包导出",
    detail: "正式图生成后，点击按钮会显示打包进度和结果。",
    download: ""
  },
  busy: null,
  previewLoadingStyle: "",
  activeJobId: "",
  lastDebitOrderId: "",
  libraryStatus: {
    label: "图库状态读取中",
    detail: "正在检查图库索引和可复用图片",
    tone: "loading"
  }
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
    color: "black",
    position: "bottom-right",
    pattern: "corner"
  };
}

const platformMeta = {
  meituan: { name: "美团", size: "800x600", ratio: "4:3", width: 800, height: 600, maxKB: 5120 },
  taobao: { name: "淘宝/饿了么", size: "800x800", ratio: "1:1", width: 800, height: 800, maxKB: 20480 },
  jd: { name: "京东", size: "800x800", ratio: "1:1", width: 800, height: 800, maxKB: 5120 }
};

const exportFormatOptions = [
  { value: "jpg", label: "JPG（默认，平台通用）" },
  { value: "jpeg", label: "JPEG（支持时保留 .jpeg）" },
  { value: "png", label: "PNG" }
];

const qualityMeta = {
  standard: { name: "普通出图", points: 100 },
  premium: { name: "精修出图", points: 200 }
};

const styleDisplayNames = ["1号背景", "2号背景", "3号背景", "4号背景", "5号背景", "6号背景"];
const fallbackExtraPlatformPoints = 100;
const fallbackWatermarkPoints = 50;
const fallbackCustomEditPoints = 150;
const stageNames = ["", "上传菜单", "选择风格/样图", "正式出图", "导出"];
let busySerial = 0;

function currentQuality() {
  return qualityMeta[state.quality] || qualityMeta.standard;
}

function imagePoints() {
  return currentQuality().points;
}

function hasStylePreviewReady() {
  if (!state.pendingStyle || state.stylePreview?.style !== state.pendingStyle || state.previewLoadingStyle) return false;
  return stylePreviewStats().success >= 6;
}

function primaryPlatform() {
  return state.deliveryPlatforms.find(id => platformMeta[id]) || "meituan";
}

function primaryPlatformMeta() {
  return platformMeta[primaryPlatform()] || platformMeta.meituan;
}

function extraPlatformPoints() {
  const raw = Number(state.plan?.pricing?.extraPlatformPoints);
  return Number.isFinite(raw) && raw > 0 ? raw : fallbackExtraPlatformPoints;
}

function customEditPoints() {
  const raw = Number(state.plan?.pricing?.customEditPoints);
  if (!Number.isFinite(raw) || raw <= 0) return fallbackCustomEditPoints;
  return raw < 100 ? raw * 10 : raw;
}

function cleanCustomerStatus(value) {
  return String(value || "")
    .replace(/模型|混元|腾讯云|Tencent|Hunyuan|Gemini|local-demo|tencent-hunyuan|generation-jobs|API|api/g, "")
    .replace(/复用/g, "沿用")
    .trim();
}

function cleanErrorText(value, fallback = "生成失败，请稍后重试") {
  const text = cleanCustomerStatus(value || fallback)
    .replace(/\s+/g, " ")
    .replace(/^[：:，,。.\s]+/, "")
    .trim();
  return text || fallback;
}

function setPreviewAspect() {
  ensureDeliveryPlatform();
  const meta = primaryPlatformMeta();
  document.documentElement.style.setProperty("--preview-aspect", `${meta.width} / ${meta.height}`);
}

function sizeLimitText(meta) {
  const kb = meta?.maxKB || 5120;
  return kb >= 1024 ? `${Math.round(kb / 1024)}MB` : `${kb}KB`;
}

function platformSpecText(meta) {
  if (!meta) return "";
  return `${meta.size}，${meta.ratio}，<=${sizeLimitText(meta)}`;
}

function platformBrief(id) {
  const meta = platformMeta[id];
  return meta ? `${meta.name} ${meta.size} <=${sizeLimitText(meta)}` : id;
}

function ensureDeliveryPlatform() {
  state.deliveryPlatforms = state.deliveryPlatforms.filter(id => platformMeta[id]);
}

function styleDisplayName(index) {
  return styleDisplayNames[index] || `${index + 1}号背景`;
}

function styleChoices(plan = state.plan) {
  const raw = Array.isArray(plan?.styles) ? plan.styles.slice(0, 6) : [];
  const choices = raw.map((style, index) => ({
    ...style,
    name: styleDisplayName(index),
    uiName: styleDisplayName(index),
    uiIndex: index
  }));
  const usedIds = new Set(choices.map(style => style.id));
  for (let index = choices.length; index < 6; index += 1) {
    const fallback = raw.length ? raw[index % raw.length] : {};
    let id = `style-${index + 1}`;
    if (usedIds.has(id)) id = `ui-style-${index + 1}`;
    usedIds.add(id);
    choices.push({
      ...fallback,
      id,
      name: styleDisplayName(index),
      uiName: styleDisplayName(index),
      uiIndex: index,
      count: fallback.count || 0,
      sample: null,
      direct: fallback.direct || 0,
      review: fallback.review || 0,
      bgReplace: fallback.bgReplace || 0,
      custom: fallback.custom || 0
    });
  }
  return choices.slice(0, 6);
}

function watermarkDemoImage() {
  const previewImage = state.stylePreview?.samples?.find(sample => sample.candidate?.url)?.candidate?.url;
  if (previewImage) return previewImage;
  const choices = styleChoices();
  const selected = choices.find(style => style.id === state.pendingStyle) || choices[0];
  if (selected?.sample?.url) return selected.sample.url;
  const resultImage = state.plan?.results?.find(row => row.candidates?.[0]?.url)?.candidates?.[0]?.url;
  return resultImage || "";
}

function imageFallbackAttr() {
  return `onerror="this.closest('[data-image-shell]')?.classList.add('image-load-failed');this.remove();"`;
}

function imageUrlFromCandidate(candidate = {}) {
  return String(candidate?.url || candidate?.imageUrl || candidate?.image_url || "").trim();
}

function validImageCandidate(candidate = {}) {
  const url = imageUrlFromCandidate(candidate);
  return url ? { ...candidate, url } : null;
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
  const text = await res.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { error: text ? text.slice(0, 180) : "服务暂时没有返回内容" };
  }
  if (!res.ok || data.error) {
    const error = new Error(data.error || "请求失败");
    error.status = res.status;
    error.data = data;
    throw error;
  }
  return data;
}

function libraryStatusFromData(data = {}) {
  const sources = data?.sources && typeof data.sources === "object" ? data.sources : {};
  const total = Number(data.total || 0);
  const reusable = Number(data.reusable || 0);
  const stores = Number(data.stores || 0);
  const realSourceCount = ["clean", "external", "watermark"].reduce((sum, key) => sum + Number(sources[key] || 0), 0);
  const likelyDemoOnly = total > 0 && realSourceCount === 0 && stores <= 1 && total <= 64;
  if (!total || likelyDemoOnly) {
    return {
      label: "仅演示图库",
      detail: total ? `当前可用 ${total} 张演示图，正式图库未接入` : "没有读取到可用真实图库，当前只能演示流程",
      tone: "warning"
    };
  }
  return {
    label: "已接入真实图库",
    detail: `${total} 张图库图，${reusable || total} 张可复用，${stores || 1} 个门店来源`,
    tone: "good"
  };
}

function libraryStatusError(error) {
  return {
    label: "COS 索引读取失败",
    detail: cleanErrorText(error?.message, "图库索引暂时不可用，请稍后重试"),
    tone: "error"
  };
}

function renderLibraryStatus() {
  const box = $("#libraryStatus");
  if (!box) return;
  const status = state.libraryStatus || {};
  box.className = `library-status ${status.tone || ""}`;
  $("#libraryStatusTitle").textContent = status.label || "图库状态读取中";
  $("#libraryStatusDetail").textContent = status.detail || "正在检查图库索引和可复用图片";
}

async function refreshLibraryStatus() {
  state.libraryStatus = {
    label: "图库状态读取中",
    detail: "正在检查图库索引和可复用图片",
    tone: "loading"
  };
  renderLibraryStatus();
  try {
    state.libraryStatus = libraryStatusFromData(await api("/api/library-status"));
  } catch (error) {
    state.libraryStatus = libraryStatusError(error);
  }
  renderLibraryStatus();
}

function isBusy(...keys) {
  if (!state.busy) return false;
  return keys.length ? keys.includes(state.busy.key) : true;
}

function renderBusy() {
  const el = $("#globalBusy");
  if (!el) return;
  if (!state.busy) {
    el.classList.remove("show");
    el.setAttribute("aria-hidden", "true");
    el.innerHTML = "";
    document.body.classList.remove("has-global-busy");
    return;
  }
  el.classList.add("show");
  el.setAttribute("aria-hidden", "false");
  document.body.classList.add("has-global-busy");
  el.innerHTML = `
    <div class="global-busy-card" role="status">
      <span class="busy-spinner"></span>
      <b>${esc(state.busy.title)}</b>
      <p>${esc(state.busy.detail || "请稍候，任务正在处理")}</p>
    </div>
  `;
}

function beginBusy(key, title, detail = "") {
  const token = ++busySerial;
  state.busy = { token, key, title, detail };
  renderBusy();
  setControls();
  return token;
}

function updateBusy(token, key, title, detail = "") {
  if (!state.busy || state.busy.token !== token) return;
  state.busy = { token, key, title, detail };
  renderBusy();
  setControls();
}

function endBusy(token) {
  if (!state.busy || state.busy.token !== token) return;
  state.busy = null;
  renderBusy();
  setControls();
}

function setButtonLoading(button, loading, loadingText = "") {
  if (!button) return;
  if (loading) {
    if (!button.dataset.defaultHtml) button.dataset.defaultHtml = button.innerHTML;
    if (loadingText) button.textContent = loadingText;
    button.classList.add("is-loading");
    button.disabled = true;
    return;
  }
  if (button.dataset.defaultHtml) {
    button.innerHTML = button.dataset.defaultHtml;
    delete button.dataset.defaultHtml;
  }
  button.classList.remove("is-loading");
  button.disabled = false;
}

function orderId(prefix) {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function applyAccount(data) {
  if (data?.account) {
    state.account = data.account;
    state.accountLoaded = true;
    renderRecharge();
    setControls();
  }
}

async function rechargeAccount(payload) {
  const data = await api("/api/recharge", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ orderId: orderId("recharge"), ...payload })
  });
  applyAccount(data);
  return data;
}

async function debitPoints(points, description, metadata = {}) {
  const id = orderId("debit");
  const data = await api("/api/debit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ orderId: id, points, description, metadata })
  });
  applyAccount(data);
  return { orderId: id, data };
}

async function refundPoints(sourceOrderId, points, description, metadata = {}) {
  const data = await api("/api/refund", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sourceOrderId, points, description, metadata })
  });
  applyAccount(data);
  return data;
}

function scrollToPanel(id) {
  const el = $(id);
  if (!el) return;
  const topbar = $(".topbar")?.getBoundingClientRect().height || 0;
  const offset = topbar + 28;
  const top = window.scrollY + el.getBoundingClientRect().top - offset;
  window.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
}

function watermarkCharge() {
  if (!state.plan || !state.watermark.enabled) return 0;
  const raw = Number(state.plan.pricing?.watermarkPoints);
  return Number.isFinite(raw) && raw > 0 ? raw : fallbackWatermarkPoints;
}

function totalCharge() {
  return baseImageCharge() + watermarkCharge() + platformCharge();
}

function platformCharge() {
  if (!state.plan) return 0;
  const extraCount = Math.max(0, state.deliveryPlatforms.length - 1);
  return extraCount * extraPlatformPoints();
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
    color: state.watermark.color === "white" ? "white" : "black",
    position: state.watermark.position,
    pattern: state.watermark.pattern
  };
}

function exportPlatforms() {
  const chosen = $("#platformSelect")?.value || "purchased";
  if (!state.deliveryPlatforms.length) {
    toast("请至少选择一个交付平台");
    setExportStatus("error", "导出失败", "请先选择至少一个交付平台。任意一个平台不加积分。");
    return [];
  }
  if (chosen === "purchased") return [...state.deliveryPlatforms];
  if (!state.deliveryPlatforms.includes(chosen)) {
    toast(`请先在交付平台和尺寸里勾选${platformMeta[chosen]?.name || "该平台"}`);
    setExportStatus("error", "导出失败", `请先勾选${platformMeta[chosen]?.name || "该平台"}，再打包导出。`);
    return [];
  }
  return [chosen];
}

function platformNames(platforms = state.deliveryPlatforms) {
  return platforms.map(id => platformMeta[id]?.name || id).join("、");
}

function setExportStatus(tone = "idle", title = "", detail = "", download = "") {
  state.exportStatus = { tone, title, detail, download };
  renderExportStatus();
}

function renderExportStatus() {
  const box = $("#exportStatus");
  if (!box) return;
  const status = state.exportStatus || {};
  const tone = status.tone || "idle";
  box.className = `export-status ${tone}`;
  const link = status.download ? `<a href="${esc(status.download)}">下载 ZIP</a>` : "";
  box.innerHTML = `
    <b>${esc(status.title || "等待打包导出")}</b>
    <span>${esc(status.detail || "正式图生成后，点击按钮会显示打包进度和结果。")}${link}</span>
  `;
}

function renderHeroLedger() {
  const balance = $("#heroBalance");
  const estimate = $("#heroEstimatePoints");
  const quality = $("#heroQualityText");
  if (balance) balance.textContent = `${state.account.balance || 0}`;
  if (quality) quality.textContent = `${currentQuality().name} · ${imagePoints()}积分/张`;
  if (!estimate) return;
  const count = state.plan?.summary?.total ?? state.menu?.count ?? 0;
  if (!count) {
    estimate.textContent = "-";
    return;
  }
  const points = state.plan ? totalCharge() : estimatedFormalPoints();
  estimate.textContent = `${points} 积分`;
}

function renderRunStatus() {
  const box = $("#runStatusPanel");
  if (!box) return;
  let tone = "idle";
  let title = "等待菜单上传";
  let detail = "上传 XLS/XLSX 菜单后，工作台会自动进入风格选择。";
  if (state.busy) {
    tone = "loading";
    const labels = {
      "upload-menu": "正在上传并解析菜单",
      "style-plan": "正在生成 6 张背景风格候选",
      "style-preview": "正在生成 6 张免费单品样图",
      "confirm-charge": "正在扣积分",
      "confirm-generate": "正在生成正式菜品图",
      "confirm-refund": "正在退回积分",
      "export-zip": "正在打包导出 ZIP",
      "export-single": "正在准备单张保存",
      "redraw-debit": "正在换版",
      "refine-debit": "正在提交自定义修改",
      recharge: "正在处理积分充值"
    };
    title = labels[state.busy.key] || state.busy.title || "任务处理中";
    detail = state.busy.detail || "请稍候，任务完成后会自动更新页面。";
  } else if (!state.uploaded) {
    tone = "idle";
  } else if (!state.plan) {
    tone = "loading";
    title = "菜单已上传，正在准备风格候选";
    detail = `${state.menu?.file || "菜单"} · ${state.menu?.count || 0} 个菜品，正在生成可选背景。`;
  } else if (!state.pendingStyle) {
    title = "请选择整店背景风格";
    detail = "上方会固定展示 6 张背景候选，选中后可生成 6 张免费单品样图。";
  } else if (!hasStylePreviewReady() && !state.confirmed) {
    tone = state.stylePreviewError?.style === state.pendingStyle ? "error" : "warning";
    title = state.stylePreviewError?.style === state.pendingStyle ? "免费样图需要重试" : "等待生成免费样图";
    detail = `${styleName(state.pendingStyle)} · 先看 6 张免费单品样图，满意后再扣积分。`;
  } else if (!state.confirmed) {
    title = "免费样图已就绪，等待确认正式出图";
    const platformText = state.deliveryPlatforms.length ? state.deliveryPlatforms.map(platformBrief).join(" / ") : "请选择交付平台";
    detail = `${currentQuality().name} ${imagePoints()}积分/张 · ${platformText} · 将扣 ${totalCharge()} 积分。`;
  } else {
    const stats = formalPlanStats(state.plan);
    tone = stats.failed ? "warning" : "idle";
    title = stats.failed ? "正式图部分完成，可重试失败项" : "正式图已生成，可预览修改或导出";
    detail = stats.failed
      ? `已完成 ${stats.completed}/${stats.total} 张，失败 ${stats.failed} 张。`
      : `${stats.completed || stats.total} 张正式图 · ${state.deliveryPlatforms.map(platformBrief).join(" / ") || "已按所选平台处理"}。`;
  }
  box.className = `run-status ${tone}`;
  box.innerHTML = `
    <span class="run-status-dot"></span>
    <div>
      <b>${esc(title)}</b>
      <p>${esc(detail)}</p>
    </div>
  `;
}

function setProgress(percent, text, stage = state.stage) {
  const normalizedStage = Math.max(1, Math.min(4, Number(stage) || 1));
  state.stage = normalizedStage;
  $("#progressBar").style.width = `${percent}%`;
  $("#progressText").textContent = text;
  $("#stageBadge").textContent = `第 ${normalizedStage} 步 · ${stageNames[normalizedStage] || "进行中"}`;
  $$(".round-step").forEach((button, index) => {
    const step = index + 1;
    button.classList.toggle("done", step < normalizedStage);
    button.classList.toggle("active", step === normalizedStage);
  });
}

function unlockPanels() {
  $("#stylesPanel").classList.toggle("locked", !state.plan);
  $("#previewPanel").classList.toggle("locked", !state.confirmed);
  $("#exportView").classList.toggle("locked", !state.confirmed);
}

const waitingGenerationStatuses = new Set(["created", "pending", "queued", "running", "limited", "waiting"]);
const failedGenerationStatuses = new Set(["failed", "failure", "error", "cancelled"]);

function estimatedFormalPoints() {
  return (state.menu?.count || 0) * imagePoints();
}

function generationStatusValue(row) {
  return String(row?.generation?.status || row?.generationStatus || row?.status || "").toLowerCase();
}

function rowProviderError(row) {
  const generation = row?.generation || {};
  return generation.provider_error || generation.providerError || generation.error || row?.provider_error || row?.providerError || row?.error || "";
}

function rowRetryable(row) {
  const generation = row?.generation || {};
  return Boolean(generation.retryable || row?.retryable);
}

function rowRefundRequired(row) {
  const generation = row?.generation || {};
  return Boolean(generation.refund_required || generation.refundRequired || row?.refund_required || row?.refundRequired);
}

function primaryCandidate(row) {
  return validImageCandidate((row?.candidates || [])[0] || row?.generation?.candidate || {});
}

function publicStatus(row) {
  const status = generationStatusValue(row);
  const providerError = rowProviderError(row);
  if (failedGenerationStatuses.has(status) || providerError || rowRefundRequired(row)) {
    return rowRetryable(row) ? "生成失败，可重试" : "生成失败";
  }
  if (waitingGenerationStatuses.has(status)) return "等待生成";
  const candidate = primaryCandidate(row);
  const raw = cleanCustomerStatus(row.publicStatus);
  if (state.confirmed && !candidate && (!raw || ["待补图", "待处理", "已生成"].includes(raw))) return "等待生成";
  if (raw && raw !== "已生成") return raw;
  if (!candidate) return state.confirmed ? "等待生成" : "待补图";
  return "已生成";
}

function isPendingGeneration(row) {
  const status = publicStatus(row);
  const generationStatus = generationStatusValue(row);
  return ["待正式生成", "生成失败", "生成失败，可重试", "等待配置", "等待生成"].includes(status)
    || waitingGenerationStatuses.has(generationStatus)
    || failedGenerationStatuses.has(generationStatus);
}

function isFailedGeneration(row) {
  const status = publicStatus(row);
  const generationStatus = generationStatusValue(row);
  return Boolean(rowProviderError(row) || rowRefundRequired(row))
    || status.includes("失败")
    || failedGenerationStatuses.has(generationStatus);
}

function isWaitingGeneration(row) {
  const status = publicStatus(row);
  const generationStatus = generationStatusValue(row);
  return !isFailedGeneration(row) && (
    ["待正式生成", "等待配置", "待补图", "待处理", "等待生成"].includes(status)
    || waitingGenerationStatuses.has(generationStatus)
  );
}

function generationStatusPillClass(row) {
  if (isFailedGeneration(row)) return "error";
  if (isWaitingGeneration(row)) return "warning";
  return "success";
}

function generationFailureMessage(row) {
  if (!isFailedGeneration(row)) return "";
  const generation = row?.generation || {};
  const reason = rowProviderError(row) || generation.reason || "生成失败，请稍后重试";
  const suffix = rowRefundRequired(row)
    ? "，需要退回本张积分"
    : rowRetryable(row)
      ? "，可重试"
      : "";
  return cleanErrorText(`${reason}${suffix}`);
}

function exportableRows(scope = "all", selectedRows = []) {
  const rows = Array.isArray(state.plan?.results) ? state.plan.results : [];
  const selected = new Set(selectedRows.map(Number).filter(Boolean));
  return rows.filter((row, index) => {
    const rowNo = index + 1;
    if (selected.size && !selected.has(rowNo)) return false;
    if (scope === "single" && row.kind !== "单品") return false;
    if (scope === "combo" && row.kind !== "套餐/组合") return false;
    return Boolean(primaryCandidate(row)) && !isPendingGeneration(row) && !isFailedGeneration(row);
  });
}

function hasExportableRows(scope = "all", selectedRows = []) {
  return exportableRows(scope, selectedRows).length > 0;
}

function exportImageFormat() {
  const value = String($("#formatSelect")?.value || "jpg").toLowerCase();
  return ["jpg", "png", "jpeg"].includes(value) ? value : "jpg";
}

function ensureExportFormatOptions() {
  const select = $("#formatSelect");
  if (!select) return;
  const current = ["jpg", "jpeg", "png"].includes(String(select.value).toLowerCase()) ? String(select.value).toLowerCase() : "jpg";
  select.innerHTML = exportFormatOptions.map(option => `<option value="${option.value}">${option.label}</option>`).join("");
  select.value = current;
}

function formalPlanStats(plan = state.plan) {
  const rows = Array.isArray(plan?.results) ? plan.results : [];
  const generation = plan?.generation || {};
  const total = Number(generation.total ?? plan?.summary?.total ?? rows.length ?? 0) || 0;
  const failedFromRows = rows.filter(isFailedGeneration).length;
  const pendingFromRows = rows.filter(row => isWaitingGeneration(row)).length;
  const failed = rows.length ? failedFromRows : (Number(generation.failed || 0) || 0);
  const pending = rows.length ? pendingFromRows : (Number(generation.pending || 0) || 0);
  const explicitCompleted = Number(generation.completed ?? generation.succeeded);
  const completed = rows.length || !Number.isFinite(explicitCompleted)
    ? Math.max(0, total - failed - pending)
    : explicitCompleted;
  return { total, completed, failed, pending };
}

function formalPlanProgressText(plan = state.plan) {
  const stats = formalPlanStats(plan);
  const base = `已完成 ${stats.completed} 张，失败 ${stats.failed} 张，总数 ${stats.total} 张`;
  if (stats.failed) return `${base}，请检查失败项`;
  if (stats.pending) return `${base}，待正式生成 ${stats.pending} 张`;
  return `${base}，可以选择导出或精修`;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

const generationJobTerminalStatuses = new Set(["completed", "succeeded", "failed", "partial", "partially_failed", "refunded", "cancelled"]);

function isGenerationJobTerminal(job) {
  return generationJobTerminalStatuses.has(String(job?.status || "").toLowerCase());
}

function generationJobStats(job) {
  const progress = job?.progress || {};
  const total = Number(progress.total ?? job?.totalItems ?? 0) || 0;
  const completed = Number(progress.completed ?? progress.succeeded ?? job?.completedItems ?? job?.succeededItems ?? 0) || 0;
  const failed = Number(progress.failed ?? job?.failedItems ?? 0) || 0;
  const pending = Number(progress.pending ?? job?.pendingItems ?? Math.max(0, total - completed - failed)) || 0;
  const rawPercent = Number(progress.percent);
  const percent = Number.isFinite(rawPercent) ? rawPercent : (total ? ((completed + failed) / total) * 100 : 0);
  return { total, completed, failed, pending, percent };
}

function generationJobProgressText(job, label = "正式生图中") {
  const stats = generationJobStats(job);
  const pendingText = stats.pending ? `，剩余 ${stats.pending} 张` : "";
  return `${label}：已完成 ${stats.completed} 张，失败 ${stats.failed} 张，总数 ${stats.total} 张${pendingText}`;
}

function generationJobFailureText(job, limit = 3) {
  const failed = (job?.items || []).filter(item => {
    const status = String(item?.status || "").toLowerCase();
    const result = item?.result || {};
    return status === "failed"
      || item?.provider_error
      || item?.providerError
      || result.provider_error
      || result.providerError
      || item?.refund_required
      || item?.refundRequired
      || result.refund_required
      || result.refundRequired;
  });
  if (!failed.length) return "";
  const names = failed
    .slice(0, limit)
    .map(item => {
      const label = item.dish || item.payload?.name || `第 ${item.index || item.itemIndex || "?"} 张`;
      const result = item?.result || {};
      const retry = item.retryable || result.retryable ? "可重试" : "";
      const refund = item.refund_required || item.refundRequired || result.refund_required || result.refundRequired ? "需退款" : "";
      const suffix = [retry, refund].filter(Boolean).join("、");
      return suffix ? `${label}（${suffix}）` : label;
    })
    .filter(Boolean);
  const more = failed.length > limit ? `等 ${failed.length} 张` : `${failed.length} 张`;
  return names.length ? `失败项：${names.join("、")}${failed.length > limit ? ` ${more}` : ""}` : `失败项 ${more}`;
}

function updateGenerationJobProgress(job, token, label = "正式生图中") {
  const stats = generationJobStats(job);
  const text = generationJobProgressText(job, label);
  const detail = [text, generationJobFailureText(job)].filter(Boolean).join("；");
  const percent = Math.min(98, Math.max(82, 82 + stats.percent * 0.16));
  updateBusy(token, "confirm-generate", "正在生成正式图", detail);
  setProgress(percent, text, 3);
}

async function createGenerationJob(payload) {
  return api("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

async function runGenerationJob(jobId, payload = {}) {
  return api(`/api/jobs/${encodeURIComponent(jobId)}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

async function retryGenerationJob(jobId, payload = {}) {
  return api(`/api/jobs/${encodeURIComponent(jobId)}/retry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run: true, ...payload })
  });
}

async function getGenerationJob(jobId) {
  return api(`/api/jobs/${encodeURIComponent(jobId)}`);
}

function shouldRunGenerationJob(job) {
  const status = String(job?.status || "").toLowerCase();
  const stats = generationJobStats(job);
  return stats.pending > 0 && ["created", "paid", "queued", "waiting"].includes(status);
}

function jobRunDeferredOnly(job) {
  const lastRun = job?.lastRun || {};
  const selected = Number(lastRun.selected || 0);
  return selected > 0
    && Number(lastRun.deferred || 0) >= selected
    && Number(lastRun.completed ?? lastRun.succeeded ?? 0) === 0
    && Number(lastRun.failed || 0) === 0;
}

async function pollGenerationJob(jobId, options = {}) {
  const token = options.token;
  let initialDeferred = jobRunDeferredOnly(options.initial?.job);
  if (options.initial?.job && token) {
    updateGenerationJobProgress(options.initial.job, token);
  }
  let response = await getGenerationJob(jobId);
  if (token) updateGenerationJobProgress(response.job, token);
  const maxPolls = options.maxPolls || 240;
  for (let attempt = 0; attempt < maxPolls; attempt += 1) {
    const job = response.job;
    if (isGenerationJobTerminal(job) || initialDeferred) return response;
    const interval = Math.max(500, Math.min(5000, Number(response.poll?.intervalMs || 1500)));
    await sleep(interval);
    response = await getGenerationJob(jobId);
    if (token) updateGenerationJobProgress(response.job, token);
    if (jobRunDeferredOnly(response.job)) {
      initialDeferred = true;
    }
  }
  throw new Error("生成任务轮询超时");
}

function generationRowFromJobItem(item, fallbackRow = {}) {
  const row = { ...fallbackRow, ...(item?.payload || {}) };
  const result = item?.result || {};
  const generation = result.generation || row.generation || {};
  const statusValues = [item?.status, result.status, generation.status].map(value => String(value || "").toLowerCase()).filter(Boolean);
  const status = statusValues[0] || "";
  const failedStatus = statusValues.some(value => ["failed", "error"].includes(value));
  const providerError = item?.provider_error || item?.providerError || result.provider_error || result.providerError || item?.error || generation.provider_error || generation.providerError || generation.error;
  const retryable = Boolean(item?.retryable ?? result.retryable ?? generation.retryable);
  const refundRequired = Boolean(item?.refund_required ?? item?.refundRequired ?? result.refund_required ?? result.refundRequired ?? generation.refund_required ?? generation.refundRequired);
  if (failedStatus || providerError || refundRequired) {
    row.publicStatus = retryable ? "生成失败，可重试" : "生成失败";
    row.backgroundAction = "生成失败";
    row.generationStatus = "failed";
    row.generation = {
      ...generation,
      jobId: item?.jobId,
      itemIndex: Number(item?.index || item?.itemIndex || 0) || undefined,
      status: "failed",
      error: providerError || result.error || "生成失败",
      provider_error: providerError || "",
      retryable,
      refund_required: refundRequired
    };
    return row;
  }
  if (["created", "pending", "queued", "running", "limited", "waiting"].includes(status)) {
    row.publicStatus = row.publicStatus || "待正式生成";
    row.backgroundAction = row.backgroundAction || "待正式生成";
    row.generationStatus = row.generationStatus || status || "pending";
    row.generation = {
      ...generation,
      jobId: item?.jobId,
      itemIndex: Number(item?.index || item?.itemIndex || 0) || undefined,
      status: status || generation.status || "pending"
    };
    return row;
  }
  row.publicStatus = row.publicStatus || "已生成";
  row.generationStatus = row.generationStatus || "succeeded";
  row.generation = {
    ...generation,
    jobId: item?.jobId,
    itemIndex: Number(item?.index || item?.itemIndex || 0) || undefined,
    status: generation.status || status || "succeeded"
  };
  return row;
}

function generationPlanFromJob(job, fallbackPlan = state.plan) {
  const snapshot = job?.planSnapshot || {};
  const fallbackRows = Array.isArray(fallbackPlan?.results) ? fallbackPlan.results : [];
  const items = Array.isArray(job?.items) ? job.items : [];
  const results = items.length
    ? items.map(item => generationRowFromJobItem(item, fallbackRows[(Number(item.index ?? item.itemIndex) || 1) - 1] || {}))
    : fallbackRows;
  const stats = generationJobStats(job);
  const pipeline = snapshot.pipeline || fallbackPlan?.pipeline || {};
  const jobStatus = String(job?.status || "").toLowerCase();
  const refundRequired = items.some(item => item?.refund_required || item?.refundRequired || item?.result?.refund_required || item?.result?.refundRequired);
  return {
    ...(fallbackPlan || {}),
    ...snapshot,
    selectedStyle: job?.style || snapshot.selectedStyle || fallbackPlan?.selectedStyle || state.pendingStyle,
    quality: snapshot.quality || fallbackPlan?.quality || { id: job?.quality || state.quality },
    pricing: snapshot.pricing || fallbackPlan?.pricing || {},
    pipeline,
    results,
    generation: {
      provider: "generation-jobs",
      action: "generation_job",
      jobId: job?.id,
      status: jobStatus || job?.status,
      total: stats.total,
      completed: stats.completed,
      succeeded: stats.completed,
      failed: stats.failed,
      pending: stats.pending,
      partial: jobStatus === "partial" || jobStatus === "partially_failed",
      refundRequired,
      configured: Boolean(pipeline?.tencent?.configured),
      items: items.map(item => item.result?.generation || item.payload?.generation || {}).filter(Boolean),
      errors: items
        .filter(item => item.status === "failed" || item.provider_error || item.providerError || item.result?.provider_error || item.result?.providerError)
        .map(item => ({ dish: item.dish || item.payload?.name, message: item.provider_error || item.providerError || item.error || item.result?.provider_error || item.result?.providerError || item.result?.error || "生成失败" }))
    }
  };
}

function setControls() {
  ensureDeliveryPlatform();
  const menuFile = state.uploadingFileName || state.menu?.file || "菜单";
  const menuCount = state.menu?.count ?? 0;
  const busy = Boolean(state.busy);
  const uploadBusy = isBusy("upload-menu");
  const planBusy = isBusy("style-plan");
  const sampleBusy = isBusy("style-preview");
  const confirmBusy = isBusy("confirm-charge", "confirm-generate");
  const exportBusy = isBusy("export-zip", "export-single");
  const startSub = $("#startJobBtn")?.querySelector(".step-copy span");
  const startEm = $("#startJobBtn")?.querySelector("em");
  const chooseEm = $("#chooseMenuBtn")?.querySelector("em");
  const formalEm = $("#formalShortcutBtn")?.querySelector("em");
  const exportEm = $("#exportShortcutBtn")?.querySelector("em");
  const readyPreview = hasStylePreviewReady();
  const hasPreviewAttempt = Boolean(state.pendingStyle && state.stylePreview?.style === state.pendingStyle);
  const hasExportable = state.confirmed && hasExportableRows();
  $("#chooseMenuBtn").disabled = state.running || busy;
  $("#chooseMenuBtn").classList.toggle("is-loading", uploadBusy);
  if (chooseEm) chooseEm.textContent = uploadBusy ? "上传中" : "点击上传";
  $("#startJobBtn").disabled = !state.uploaded || state.running || busy;
  $("#startJobBtn").classList.toggle("is-loading", planBusy);
  if (startSub) {
    startSub.textContent = planBusy ? "正在生成背景卡" : state.running ? "正在处理，请稍候" : state.plan ? "查看背景和样图" : "菜单上传后自动进入";
  }
  if (startEm) {
    startEm.textContent = planBusy ? "生成中" : state.running ? "处理中" : state.plan ? "查看设置" : state.uploaded ? "自动开始" : "等待菜单";
  }
  const sampleButton = $("#generateSamplesBtn");
  if (sampleButton) {
    sampleButton.disabled = !state.plan || !state.pendingStyle || state.running || sampleBusy || (busy && !sampleBusy);
    sampleButton.classList.toggle("is-loading", sampleBusy);
    sampleButton.textContent = sampleBusy
      ? "正在生成6张免费样图"
      : readyPreview || hasPreviewAttempt
        ? "重新生成6张免费样图"
        : "确认该背景，生成6张免费样图";
  }
  $("#formalShortcutBtn").disabled = !state.plan || (!readyPreview && !state.confirmed) || busy;
  if (formalEm) formalEm.textContent = state.confirmed ? "查看正式图" : readyPreview ? "去确认" : "样图后可用";
  $("#confirmStyleBtn").disabled = !state.plan || !state.pendingStyle || !readyPreview || state.running || busy || !state.deliveryPlatforms.length;
  $("#confirmStyleBtn").classList.toggle("is-loading", confirmBusy);
  $("#confirmStyleBtn").textContent = confirmBusy
    ? (isBusy("confirm-charge") ? "扣费中" : "生成中")
    : (state.plan && readyPreview ? `将扣 ${totalCharge()} 积分，生成正式图` : "先生成6张免费样图");
  $("#exportShortcutBtn").disabled = !state.confirmed || busy;
  if (exportEm) exportEm.textContent = exportBusy ? "打包中" : state.confirmed ? (hasExportable ? "去导出" : "暂无成图") : "生成后可用";
  $("#exportZipBtn").disabled = !state.confirmed || busy;
  $("#exportZipBtn").classList.toggle("is-loading", exportBusy);
  $("#exportZipBtn").textContent = exportBusy ? "打包中" : hasExportable ? "打包导出 ZIP" : "暂无可导出成图";
  $("#menuStatus").textContent = uploadBusy
    ? `正在上传并解析：${menuFile}`
    : state.uploaded
      ? `菜单已就绪：${menuFile} · ${menuCount} 个菜`
      : "等待选择菜单";
  $("#menuStatus").className = `menu-status ${uploadBusy ? "loading" : state.uploaded ? "good" : ""}`;
  $("#pointsBalance").textContent = `${state.account.balance || 0}`;
  renderRunStatus();
  renderLibraryStatus();
  updateChargeText();
  renderQualityControls();
  renderPlatformControls();
  renderWatermarkControls();
  renderExportStatus();
  renderHeroLedger();
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
    if (platform) parts.push(`平台附加 ${platform} 积分`);
    hint.textContent = !hasStylePreviewReady()
      ? "先生成 6 张免费样图，满意后再确认扣积分。"
      : state.deliveryPlatforms.length
        ? `${parts.join(" + ")}，点击后将扣 ${base + wm + platform} 积分并创建正式生图任务。`
        : "请先选择至少一个交付平台，首个平台不加积分。";
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

function rechargePackages() {
  const packages = Array.isArray(state.account.packages) ? state.account.packages : [];
  const fallback = [
    { cash: 49, points: 490, bonus: 10, name: "体验充值" },
    { cash: 99, points: 990, bonus: 50, name: "整店常用" },
    { cash: 299, points: 2990, bonus: 200, name: "小团队包" }
  ];
  return (packages.length ? packages : fallback).map(pkg => {
    const cash = Number(pkg.cash || 0);
    const defaults = fallback.find(item => item.cash === cash);
    return {
      ...pkg,
      cash,
      name: pkg.name || defaults?.name || "积分包",
      points: Number(pkg.points ?? defaults?.points ?? 0),
      bonus: Number(pkg.bonus ?? defaults?.bonus ?? 0)
    };
  });
}

function renderRecharge() {
  $("#rateText").textContent = "选择积分包，充值后即可生成正式图片";
  $("#rechargePackages").innerHTML = rechargePackages().map(pkg => (
    `<button class="recharge-card" data-cash="${pkg.cash}" type="button">
      <b>${esc(pkg.points + pkg.bonus)} 积分</b>
      <span>${pkg.bonus ? `含赠送 ${esc(pkg.bonus)} 积分` : "积分包"}</span>
      <em>${esc(pkg.name)} · 充值后到账</em>
    </button>`
  )).join("");
  $$(".recharge-card").forEach(button => {
    button.onclick = async () => {
      if (state.busy) return toast("请等待当前任务完成");
      const cash = Number(button.dataset.cash || 0);
      const token = beginBusy("recharge", "正在充值积分", "正在处理所选积分包，请稍候");
      setButtonLoading(button, true, "充值中");
      try {
        const data = await rechargeAccount({ cash });
        closeRecharge();
        toast(`已充值 ${data.transaction.points} 积分`);
      } catch (e) {
        toast(e.message);
      } finally {
        setButtonLoading(button, false);
        endBusy(token);
      }
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
  hint.textContent = points < 100 ? "最低 100 积分起充" : `将充值 ${Math.floor(points)} 积分`;
}

async function submitCustomRecharge() {
  const input = $("#customRechargePoints");
  const points = Math.floor(Number(input?.value || 0));
  if (!Number.isFinite(points) || points < 100) {
    toast("自定义充值最低 100 积分起充");
    input?.focus();
    return;
  }
  if (state.busy) return toast("请等待当前任务完成");
  const button = $("#customRechargeBtn");
  const token = beginBusy("recharge", "正在充值积分", `自定义充值 ${points} 积分`);
  setButtonLoading(button, true, "充值中");
  try {
    const data = await rechargeAccount({ points });
    closeRecharge();
    toast(`已充值 ${data.transaction.points} 积分`);
  } catch (e) {
    toast(e.message);
  } finally {
    setButtonLoading(button, false);
    endBusy(token);
  }
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
    { title: "上传菜单", status: state.uploaded ? "已完成" : "等待上传菜单", state: state.uploaded ? "done" : "active" },
    { title: "选择风格/样图", status: state.uploaded ? "自动生成背景中" : "待菜单", state: state.uploaded ? "active" : "" },
    { title: "正式出图", status: "待质量/平台/水印" },
    { title: "导出", status: "待正式图" }
  ]);
  $("#styleBox").innerHTML = `<div class="empty">菜单上传后会展示 6 张背景风格图</div>`;
  $("#sampleActionTitle").textContent = "先选择一个背景";
  $("#sampleActionHint").textContent = "选中后点击按钮，系统会生成 6 张免费单品样图。";
  $("#stylePreviewTitle").textContent = "先选择背景，这里会展示 6 张免费单品样图";
  setStylePreviewStatus("", "先选择菜单并生成背景。选中背景后会生成 6 张免费单品样图，不扣积分。");
  $("#stylePreviewBox").className = "style-preview-box";
  $("#stylePreviewBox").innerHTML = previewPlaceholders("等待菜单");
  $("#selectedStyleHint").textContent = "还没有选择风格";
  $("#summary").innerHTML = "";
  $("#reworkBanner").innerHTML = "";
  $("#resultBox").innerHTML = `<div class="empty">扣积分生成后展示正式图片</div>`;
  renderQualityControls();
  renderWatermarkControls();
  setControls();
}

function renderStylePlanLoading() {
  const styleBox = $("#styleBox");
  if (styleBox) {
    styleBox.innerHTML = Array.from({ length: 6 }, (_, index) => `
      <button class="style skeleton" type="button" disabled>
        <div class="style-media"></div>
        <span class="style-body">
          <b>${index + 1}号背景</b>
          <span>正在生成背景候选</span>
          <em>生成中</em>
        </span>
      </button>
    `).join("");
  }
  $("#sampleActionTitle").textContent = "正在生成 6 张背景候选";
  $("#sampleActionHint").textContent = "背景卡完成后，选择任意一张即可生成 6 张免费单品样图。";
  $("#stylePreviewTitle").textContent = "背景生成中，稍后可生成 6 张免费单品样图";
  setStylePreviewStatus("loading", "正在准备背景风格图。这里会在选择背景后展示 6 张免费单品样图。");
  $("#stylePreviewBox").className = "style-preview-box";
  $("#stylePreviewBox").innerHTML = previewPlaceholders("等待背景候选");
  $("#selectedStyleHint").textContent = "正在准备风格候选";
  renderRunStatus();
}

function renderPlan(showPreview = false) {
  const p = state.plan;
  const ready = p.results.filter(r => r.candidates?.length).length;
  const needsWork = p.summary.total - ready;
  const counts = menuCounts();
  const basePoints = baseImageCharge();
  const quality = currentQuality();
  const styles = styleChoices(p);
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
  const readyPreview = hasStylePreviewReady();
  renderWorkflow([
    { title: "上传菜单", status: `${p.menu.count} 个菜品`, state: "done" },
    { title: "选择风格/样图", status: readyPreview || state.confirmed ? "6张免费样图已就绪" : state.pendingStyle ? "待生成免费样图" : `${styles.length} 张背景可选`, state: state.confirmed || readyPreview ? "done" : "active" },
    { title: "正式出图", status: state.confirmed ? `已扣 ${state.chargedPoints || totalCharge()} 积分` : readyPreview ? `待扣 ${totalCharge()} 积分` : "待免费样图", state: state.confirmed ? "done" : readyPreview ? "active" : "" },
    { title: "导出", status: state.confirmed ? "可以导出" : "待正式图", state: state.confirmed ? "active" : "" }
  ]);
  renderStyles();
  renderStylePreview();
  renderQualityControls();
  renderPlatformControls();
  renderWatermarkControls();
  if (showPreview) renderPreview();
  else $("#resultBox").innerHTML = `<div class="empty">选择风格并确认后，系统会扣积分生成全部正式图片</div>`;
  const generation = p.generation || {};
  const formalStats = formalPlanStats(p);
  $("#summary").innerHTML = [
    `正式图 ${p.summary.total} 张`,
    `${quality.name} · ${quality.points} 积分/张`,
    state.deliveryPlatforms.length ? `交付平台 ${state.deliveryPlatforms.map(id => platformMeta[id]?.name || id).join("、")}` : "请选择交付平台",
    generation.jobId ? `任务进度 已完成 ${formalStats.completed} 张 / 失败 ${formalStats.failed} 张 / 总数 ${formalStats.total} 张` : "",
    generation.partial ? "部分图片需要重试" : "",
    generation.refundRequired ? "有图片需要退回积分" : "",
    !generation.jobId && generation.succeeded ? `本次完成 ${generation.succeeded || 0} 张` : "",
    generation.pending ? `待正式生成 ${generation.pending} 张` : "",
    generation.failed ? `生成失败 ${generation.failed} 张` : "",
    state.watermark.enabled ? `品牌水印 ${watermarkCharge()} 积分/单` : "品牌水印可选",
    `自定义修改 ${customEditPoints()} 积分/张`,
    needsWork ? `待补图 ${needsWork} 张` : "全部可生成"
  ].filter(Boolean).map(x => `<span class="pill">${esc(x)}</span>`).join("");
  renderReworkBanner();
  renderRecharge();
  setControls();
}

function renderPlatformControls() {
  ensureDeliveryPlatform();
  setPreviewAspect();
  const locked = state.confirmed || Boolean(state.busy);
  $$(".platform-check").forEach(input => {
    input.checked = state.deliveryPlatforms.includes(input.value);
    input.disabled = locked;
    input.closest(".platform-option")?.classList.toggle("required", input.checked && state.deliveryPlatforms.length === 1);
    const meta = platformMeta[input.value];
    const desc = input.closest(".platform-option")?.querySelector("em");
    if (desc && meta) {
      const selectedIndex = state.deliveryPlatforms.indexOf(input.value);
      const priceText = selectedIndex < 0
        ? (state.deliveryPlatforms.length ? `加选 +${extraPlatformPoints()}积分` : "首个平台不加积分")
        : (selectedIndex === 0 ? "当前不加积分" : `已加选 +${extraPlatformPoints()}积分`);
      desc.textContent = `${platformSpecText(meta)}，${priceText}`;
    }
  });
  const names = state.deliveryPlatforms.map(id => {
    const meta = platformMeta[id];
    return `${meta?.name || id} ${platformSpecText(meta)}`;
  });
  const charge = platformCharge();
  const previewMeta = primaryPlatformMeta();
  const nextPlatformText = state.deliveryPlatforms.length === 1
    ? `再加选任一平台 +${extraPlatformPoints()}积分`
    : state.deliveryPlatforms.length === 2
      ? `加选第三个平台再 +${extraPlatformPoints()}积分`
      : "三个平台已全选";
  $("#platformChargeHint").textContent = names.length
    ? `已选 ${state.deliveryPlatforms.length} 个平台，平台附加 +${charge}积分；水印预览按 ${previewMeta.name} ${previewMeta.ratio}；${nextPlatformText}`
    : "请选择至少 1 个平台，任意 1 个平台不加积分";
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
    input.disabled = state.confirmed || state.running || Boolean(state.busy);
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
  const meta = primaryPlatformMeta();
  const locked = state.confirmed || Boolean(state.busy);
  enabled.checked = state.watermark.enabled;
  enabled.disabled = locked;
  state.watermark.color = state.watermark.color === "white" ? "white" : "black";
  $("#watermarkType").value = state.watermark.type;
  $("#watermarkText").value = state.watermark.text;
  $("#watermarkColor").value = state.watermark.color;
  $("#watermarkPosition").value = state.watermark.position;
  $("#watermarkPattern").value = state.watermark.pattern;
  $("#watermarkOptions").classList.toggle("disabled", !state.watermark.enabled || locked);
  $("#watermarkTextWrap").style.display = state.watermark.type === "logo" ? "none" : "grid";
  $("#watermarkColorWrap").style.display = state.watermark.type === "logo" ? "none" : "grid";
  $("#watermarkLogoWrap").style.display = state.watermark.type === "logo" ? "grid" : "none";
  ["#watermarkType", "#watermarkText", "#watermarkColor", "#watermarkLogo", "#watermarkPosition", "#watermarkPattern"].forEach(selector => {
    const field = $(selector);
    if (field) field.disabled = locked;
  });
  const demo = $("#watermarkDemo");
  const text = state.watermark.text || state.menu?.store || "品牌名";
  const demoImage = watermarkDemoImage();
  const watermarkPreviewLabel = state.deliveryPlatforms.length
    ? `${meta.name} ${meta.size}，${meta.ratio}`
    : `未选平台 · 先选平台后按对应比例预览`;
  demo.className = `watermark-demo ${state.watermark.enabled ? "enabled" : ""} ${state.watermark.pattern} ${state.watermark.position}`;
  demo.innerHTML = `
    <span>${locked ? `水印已锁定 · ${watermarkPreviewLabel}` : `水印预览 · ${watermarkPreviewLabel} · 文字无底块`}</span>
    <div class="watermark-preview-canvas ${demoImage ? "" : "empty-preview"}" data-image-shell>
      ${demoImage ? `<img class="watermark-demo-image" src="${demoImage}" alt="水印示意图" ${imageFallbackAttr()}>${watermarkOverlay(text)}<span class="image-error-note">图片加载失败，暂不能预览水印</span>` : `<span class="watermark-empty">生成免费样图后可预览水印</span>`}
    </div>
  `;
  updateChargeText();
}

function watermarkOverlay(fallbackText = "品牌名") {
  if (!state.watermark.enabled) return "";
  const colorClass = state.watermark.color === "white" ? "wm-white" : "wm-black";
  const label = state.watermark.type === "logo" && state.watermark.logoData
    ? `<img src="${state.watermark.logoData}" alt="品牌 Logo">`
    : `<b>${esc(state.watermark.text || fallbackText)}</b>`;
  if (state.watermark.pattern === "tile") {
    return `<div class="wm-overlay tile ${colorClass}">${Array.from({ length: 9 }, () => `<span>${label}</span>`).join("")}</div>`;
  }
  return `<div class="wm-overlay corner ${state.watermark.position} ${colorClass}">${label}</div>`;
}

function renderReworkBanner() {
  const box = $("#reworkBanner");
  if (!box || !state.plan || !state.confirmed) {
    if (box) box.innerHTML = "";
    return;
  }
  const generation = state.plan.generation || {};
  const stats = formalPlanStats(state.plan);
  const hasFailures = stats.failed > 0;
  const partial = generation.partial || generation.status === "partial" || generation.status === "partially_failed" || (hasFailures && stats.completed > 0);
  if (hasFailures) {
    const refundText = generation.refundRequired || state.plan.results?.some(rowRefundRequired)
      ? "失败图片需要退回本张积分；"
      : "";
    const retryText = state.plan.results?.some(row => isFailedGeneration(row) && rowRetryable(row))
      ? "失败项可重试。"
      : "失败项可稍后重新生成。";
    const title = partial ? "部分生成完成" : "生成失败";
    box.className = "rework-banner partial";
    box.innerHTML = `
      <div>
        <b>${title}：已完成 ${stats.completed}/${stats.total} 张，失败 ${stats.failed} 张</b>
        <span>${esc(refundText)}${esc(retryText)}等待中的图片不会展示为成功图。</span>
      </div>
      <button class="retry-failed-btn" type="button">重试失败项</button>
    `;
    const retryButton = box.querySelector(".retry-failed-btn");
    retryButton.onclick = () => retryFailedGeneration().catch(e => toast(e.message));
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

async function retryFailedGeneration(itemIndexes = []) {
  if (!state.confirmed || !state.plan) return toast("请先生成正式图片");
  if (state.busy) return toast("请等待当前任务完成");
  const jobId = state.plan.generation?.jobId || state.activeJobId;
  if (!jobId) return toast("没有可重试的正式生图任务");
  const failedIndexes = itemIndexes.length
    ? itemIndexes
    : state.plan.results
      .filter(row => isFailedGeneration(row))
      .map(row => Number(row.generation?.itemIndex || row.row || 0))
      .filter(Boolean);
  if (!failedIndexes.length) return toast("当前没有失败项需要重试");
  const token = beginBusy("confirm-generate", "正在重试失败项", `正在重试 ${failedIndexes.length} 张失败图片`);
  try {
    const response = await retryGenerationJob(jobId, {
      itemIndexes: failedIndexes,
      limit: failedIndexes.length,
      paid: true,
      orderId: state.lastDebitOrderId
    });
    updateGenerationJobProgress(response.job, token, "失败项已重新排队");
    const completed = await pollGenerationJob(jobId, { token, initial: response, orderId: state.lastDebitOrderId });
    state.plan = generationPlanFromJob(completed.job);
    state.activeJobId = completed.job.id;
    state.confirmed = true;
    const stats = formalPlanStats(state.plan);
    setProgress(100, formalPlanProgressText(state.plan), 4);
    renderPlan(true);
    toast(`重试完成：已完成 ${stats.completed}/${stats.total} 张${stats.failed ? `，仍失败 ${stats.failed} 张` : ""}`);
  } finally {
    endBusy(token);
    setControls();
  }
}

function styleName(styleId) {
  return styleChoices().find(s => s.id === styleId)?.uiName || "已选背景";
}

function setStylePreviewStatus(kind = "", text = "") {
  const status = $("#stylePreviewStatus");
  if (!status) return;
  status.hidden = !text;
  status.className = `sample-preview-status ${kind}`;
  status.textContent = text;
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

function previewFailureStatus(status = "") {
  const value = String(status || "").toLowerCase();
  return value.includes("失败") || ["failed", "failure", "error", "cancelled"].includes(value);
}

function previewWaitingStatus(status = "") {
  const value = String(status || "").toLowerCase();
  return value.includes("等待") || waitingGenerationStatuses.has(value);
}

function normalizePreviewSample(raw = {}, index = 0, options = {}) {
  const generation = raw.generation || {};
  const status = String(raw.status || generation.status || raw.publicStatus || "").toLowerCase();
  const error = raw.error || raw.provider_error || raw.providerError || generation.error || generation.provider_error || generation.providerError || "";
  const imageUrl = raw.imageUrl || raw.image_url || raw.url || raw.candidate?.url || "";
  const completedWithoutImage = !imageUrl && ["succeeded", "success", "completed", "cached"].includes(status);
  const failed = previewFailureStatus(status) || Boolean(error) || completedWithoutImage || (options.requireImage && !imageUrl && !previewWaitingStatus(status));
  const candidate = failed || !imageUrl
    ? null
    : {
        ...(raw.candidate || {}),
        url: imageUrl,
        source: raw.source || raw.candidate?.source || ""
      };
  return {
    ...raw,
    name: raw.name || raw.dish || `样图 ${index + 1}`,
    candidate,
    generation: {
      ...generation,
      status: failed ? "failed" : (status || generation.status || (candidate ? "succeeded" : "pending")),
      error: failed ? (error || "免费样图没有返回图片") : ""
    },
    publicStatus: failed ? "样图生成失败，可重试" : (candidate ? "免费样图" : (raw.publicStatus || "等待生成"))
  };
}

function stylePreviewStats() {
  const samples = (state.stylePreview?.samples || []).slice(0, 6);
  const success = samples.filter(sample => sample?.candidate?.url && !previewFailureStatus(sample?.generation?.status)).length;
  const failed = samples.filter(sample => previewFailureStatus(sample?.generation?.status) || sample?.generation?.error).length;
  return { success, failed, total: samples.length };
}

function previewSampleCard(sample, index) {
  const name = sample?.name || `样图 ${index + 1}`;
  const image = sample?.candidate;
  const generation = sample?.generation || {};
  const status = previewFailureStatus(generation.status)
    ? (generation.error ? `生成失败：${generation.error}` : "生成失败，可重新生成")
    : previewWaitingStatus(generation.status)
      ? "等待生成"
      : "等待生成";
  if (!image) {
    return `<div class="preview-sample placeholder missing">
      <b>${esc(name)}</b>
      <div class="sample-frame"></div>
      <p>${esc(status)}</p>
    </div>`;
  }
  return `<div class="preview-sample">
    <b>${esc(name)}</b>
    <div class="sample-image-shell" data-image-shell>
      <img src="${image.url}" alt="${esc(name)}" ${imageFallbackAttr()}>
      <span class="image-error-note">样图加载失败，可重新生成</span>
    </div>
    <p>免费样图</p>
  </div>`;
}

function renderStyles() {
  const p = state.plan;
  $("#selectedStyleHint").textContent = state.pendingStyle ? `已选择：${styleName(state.pendingStyle)}` : "还没有选择风格";
  if ($("#sampleActionTitle")) {
    $("#sampleActionTitle").textContent = state.pendingStyle ? `已选择：${styleName(state.pendingStyle)}` : "先选择一个背景";
  }
  if ($("#sampleActionHint")) {
    $("#sampleActionHint").textContent = hasStylePreviewReady()
      ? "免费样图已生成，满意后可继续确认扣积分生成正式图。"
      : state.pendingStyle
        ? "点击右侧按钮，先生成 6 张免费单品样图。"
        : "选中后点击按钮，系统会生成 6 张免费单品样图。";
  }
  $("#styleBox").innerHTML = styleChoices(p).map(s => {
    const selected = s.id === state.pendingStyle;
    const loading = state.previewLoadingStyle === s.id;
    const sample = validImageCandidate(s.sample);
    return `<button class="style ${selected ? "active" : ""} ${loading ? "is-loading" : ""}" data-style="${s.id}" type="button">
      <div class="style-media ${sample ? "" : "missing"}" data-image-shell>
        ${sample ? `<img src="${sample.url}" alt="${esc(s.uiName)}" ${imageFallbackAttr()}>` : `<span>等待背景图</span>`}
        <span class="image-error-note">背景图加载失败</span>
      </div>
      <span class="style-body">
        <b>${esc(s.uiName)}</b>
        <span>整店统一背景 ${s.uiIndex + 1}/6</span>
        <em>${loading ? "样图生成中" : selected ? "已选中" : "点击选择背景"}</em>
      </span>
    </button>`;
  }).join("");
  $$(".style").forEach(button => {
    button.onclick = () => {
      if (state.busy || state.previewLoadingStyle) return toast("请等待当前样图生成完成");
      if (state.pendingStyle !== button.dataset.style) {
        state.stylePreview = null;
        state.stylePreviewError = null;
      }
      state.pendingStyle = button.dataset.style;
      renderStyles();
      renderStylePreview();
      setControls();
      setProgress(68, "已选择背景，请生成 6 张免费样图", 2);
      scrollToPanel("#styleSampleAction");
      toast("背景已选中，可生成 6 张免费样图");
    };
  });
}

function renderStylePreview() {
  const box = $("#stylePreviewBox");
  const title = $("#stylePreviewTitle");
  if (!box) return;
  if (!state.pendingStyle) {
    if (title) title.textContent = "先选择背景，这里会展示 6 张免费单品样图";
    setStylePreviewStatus("", "先选择上方任意背景。选择后会生成 6 张免费单品样图，不扣积分。");
    box.className = "style-preview-box";
    box.innerHTML = previewPlaceholders("选择背景后生成");
    return;
  }
  const previewError = state.stylePreviewError?.style === state.pendingStyle
    ? state.stylePreviewError.message
    : "";
  if (previewError) {
    if (title) title.textContent = `${styleName(state.pendingStyle)} · 免费样图生成失败`;
    setStylePreviewStatus("error", `免费样图生成失败：${previewError}。可点击“重新生成6张免费样图”重试，或选择其他背景。`);
    box.className = "style-preview-box";
    box.innerHTML = previewPlaceholders("生成失败，可重新生成");
    return;
  }
  if (!state.stylePreview || state.stylePreview.style !== state.pendingStyle) {
    const isLoading = state.previewLoadingStyle === state.pendingStyle;
    if (title) title.textContent = isLoading
      ? `${styleName(state.pendingStyle)} · 免费样图生成中`
      : `${styleName(state.pendingStyle)} · 等待生成免费样图`;
    setStylePreviewStatus(
      isLoading ? "loading" : "",
      isLoading
        ? "正在生成 6 张免费单品样图，请稍候。生成完成后会自动显示在下方。"
        : "背景已选中。点击“生成6张免费样图”后，这里会展示两行三列的 6 张单品样图。"
    );
    box.className = `style-preview-box ${isLoading ? "loading" : ""}`;
    box.innerHTML = previewPlaceholders(isLoading ? "正在生成" : "待生成");
    return;
  }
  const samples = (state.stylePreview.samples || []).slice(0, 6);
  const stats = stylePreviewStats();
  const sampleCount = stats.success;
  const failedCount = stats.failed;
  const isLoading = state.previewLoadingStyle === state.pendingStyle;
  if (title) title.textContent = `${styleName(state.pendingStyle)} · ${sampleCount}/6 张免费单品样图`;
  box.className = "style-preview-box";
  if (!samples.length) {
    setStylePreviewStatus("error", "当前菜单没有可预览的单品，套餐和组合图会在正式生成后展示。");
    box.className = "style-preview-box empty";
    box.innerHTML = "当前菜单没有可预览的单品，套餐会在正式生成后展示";
    return;
  }
  if (isLoading) {
    setStylePreviewStatus("loading", `正在逐张生成免费样图，已完成 ${sampleCount}/6 张${failedCount ? `，失败 ${failedCount} 张` : ""}。`);
  } else {
    setStylePreviewStatus(
      sampleCount === 6 ? "done" : "warning",
      sampleCount === 6
        ? "6 张免费样图已生成。确认风格后才会扣积分生成正式图片。"
        : `已生成 ${sampleCount} 张免费样图${failedCount ? `，失败 ${failedCount} 张，请重新生成。` : "，请继续等待或重新生成。"}`
    );
  }
  box.innerHTML = Array.from({ length: 6 }, (_, index) => previewSampleCard(samples[index], index)).join("");
}

async function loadStylePreview(styleId) {
  state.stylePreview = null;
  state.stylePreviewError = null;
  state.previewLoadingStyle = styleId;
  renderStyles();
  renderStylePreview();
  const token = beginBusy("style-preview", "正在生成免费样图", `${styleName(styleId)} · 6 张免费单品样图`);
  try {
    state.stylePreview = await api(`/api/style-preview?style=${encodeURIComponent(styleId)}`);
    state.stylePreview.style = state.stylePreview.style || styleId;
    const manifestSamples = Array.isArray(state.stylePreview.samples)
      ? state.stylePreview.samples
      : (state.stylePreview.imageUrl || state.stylePreview.status || state.stylePreview.error ? [state.stylePreview] : []);
    state.stylePreview.samples = Array.from({ length: 6 }, (_, index) => normalizePreviewSample(manifestSamples[index] || {
      name: `样图 ${index + 1}`,
      candidate: null,
      generation: { status: "pending", action: "Preview" },
      publicStatus: "等待生成"
    }, index));
    renderStylePreview();
    scrollToPanel("#stylePreviewBox");
    for (let index = 0; index < 6; index += 1) {
      if (state.previewLoadingStyle !== styleId) break;
      updateBusy(token, "style-preview", "正在生成免费样图", `${styleName(styleId)} · 第 ${index + 1}/6 张`);
      setProgress(68 + Math.round((index / 6) * 8), `正在生成第 ${index + 1}/6 张免费样图`, 2);
      try {
        const payload = await api(`/api/style-preview-sample?style=${encodeURIComponent(styleId)}&index=${index}`);
        if (state.previewLoadingStyle !== styleId) break;
        state.stylePreview.samples[index] = normalizePreviewSample(payload.sample || payload, index, { requireImage: true });
      } catch (sampleError) {
        const existing = state.stylePreview.samples[index] || {};
        state.stylePreview.samples[index] = {
          ...existing,
          candidate: null,
          generation: { status: "failed", error: sampleError.message || "生成失败" },
          publicStatus: "样图生成失败"
        };
      }
      renderStylePreview();
    }
    if (state.previewLoadingStyle === styleId) state.previewLoadingStyle = "";
    renderWatermarkControls();
    const ready = hasStylePreviewReady();
    setProgress(
      ready ? 78 : 74,
      ready ? "6张免费样图已返回，请确认扣积分生成正式图" : "部分免费样图生成失败，请重试",
      ready ? 3 : 2
    );
    renderPlan(false);
    scrollToPanel(ready ? "#styleConfirmBox" : "#stylePreviewBox");
  } catch (e) {
    state.stylePreviewError = { style: styleId, message: e.message || "请求失败" };
    renderStylePreview();
    scrollToPanel("#stylePreviewBox");
    throw e;
  } finally {
    if (state.previewLoadingStyle === styleId) state.previewLoadingStyle = "";
    renderStyles();
    endBusy(token);
    setControls();
  }
}

async function generateStyleSamples() {
  if (!state.plan) return toast("请先上传菜单");
  if (!state.pendingStyle) return toast("请先选择一个背景");
  if (state.busy || state.previewLoadingStyle) return toast("请等待当前任务完成");
  await loadStylePreview(state.pendingStyle);
}

function renderPreview() {
  const p = state.plan;
  if (!state.selectedRows.size) {
    p.results.forEach((_, index) => state.selectedRows.add(index + 1));
  }
  const redrawLabel = state.freeReworkRemaining > 0
    ? `换一版（免费剩余 ${state.freeReworkRemaining}）`
    : `换一版（扣 ${imagePoints()}积分）`;
  const refineLabel = `自定义修改 ${customEditPoints()}积分/张`;
  const card = (row, index) => {
    const rowNo = index + 1;
    const candidate = isPendingGeneration(row) || isFailedGeneration(row) ? null : primaryCandidate(row);
    const checked = state.selectedRows.has(rowNo) ? "checked" : "";
    const status = publicStatus(row);
    const failure = generationFailureMessage(row);
    const saveButton = candidate ? `<button class="single-save-btn" data-row="${rowNo}" type="button">单张保存</button>` : "";
    const retryButton = !candidate && isFailedGeneration(row) && (state.plan.generation?.jobId || state.activeJobId)
      ? `<button class="retry-row-btn" data-index="${row.generation?.itemIndex || rowNo}" type="button">重试</button>`
      : "";
    const editButtons = candidate
      ? `<button class="redraw-btn" data-row="${rowNo}" type="button">${redrawLabel}</button><button class="refine-btn" data-row="${rowNo}" type="button">${refineLabel}</button>`
      : retryButton;
    return `<div class="result">
      <label class="select-line"><input type="checkbox" class="row-check" data-row="${rowNo}" ${checked}> 选择</label>
      <div class="result-title">${esc(row.name)}</div>
      ${candidate ? `<div class="image-wrap" data-image-shell><img src="${candidate.url}" alt="${esc(row.name)}" ${imageFallbackAttr()}>${watermarkOverlay(state.menu?.store || "品牌名")}<span class="image-error-note">图片加载失败，可重试生成或稍后导出</span></div>` : `<div class="empty image-empty">${esc(status)}</div>`}
      <div class="result-body">
        <p>${esc(row.category || "未分类")} · ${esc(row.kind)}</p>
        <div><span class="pill ${generationStatusPillClass(row)}">${esc(status)}</span><span class="pill">正式图 ${imagePoints()} 积分/张</span></div>
        ${failure ? `<p class="result-error">${esc(failure)}</p>` : ""}
        <div class="result-actions">${saveButton}${editButtons}</div>
      </div>
    </div>`;
  };
  const groups = [
    { title: "单品", note: "常规菜品图，适合直接上架。", rows: [] },
    { title: "套餐", note: "套餐和组合图单独预览，方便核对内容。", rows: [] },
    { title: "其他", note: "饮品、小食或暂未归类的图片。", rows: [] }
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
    button.onclick = () => redrawImage(Number(button.dataset.row), button).catch(e => toast(e.message));
  });
  $$(".single-save-btn").forEach(button => {
    button.onclick = () => exportSingle(Number(button.dataset.row), button).catch(e => toast(e.message));
  });
  $$(".retry-row-btn").forEach(button => {
    button.onclick = () => retryFailedGeneration([Number(button.dataset.index)]).catch(e => toast(e.message));
  });
}

async function uploadMenu() {
  const file = $("#menuFile").files[0];
  if (!file) return;
  if (state.busy) return toast("请等待当前任务完成");
  const name = file.name.toLowerCase();
  if (!name.endsWith(".xls") && !name.endsWith(".xlsx")) {
    toast("请上传 xls 或 xlsx 菜单");
    return;
  }
  state.running = true;
  state.uploadingFileName = file.name;
  const token = beginBusy("upload-menu", "正在上传菜单", `${file.name} 正在上传并解析`);
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
    state.exportStatus = {
      tone: "idle",
      title: "等待打包导出",
      detail: "正式图生成后，点击按钮会显示打包进度和结果。",
      download: ""
    };
    state.activeJobId = "";
    state.lastDebitOrderId = "";
    state.freeReworkRemaining = 0;
    state.stylePreview = null;
    state.stylePreviewError = null;
    state.watermark = defaultWatermark();
    state.deliveryPlatforms = [];
    state.selectedRows.clear();
    renderWaiting();
    shouldAutoStart = true;
    toast("菜单已上传，正在自动生成风格方案");
  } finally {
    state.running = false;
    state.uploadingFileName = "";
    endBusy(token);
    setControls();
  }
  if (shouldAutoStart) await startJob({ auto: true });
}

async function startJob(options = {}) {
  if (!state.uploaded) return toast("请先选择菜单");
  if (state.running || state.busy) return toast("请等待当前任务完成");
  const doScroll = options.doScroll !== false;
  state.running = true;
  const token = beginBusy("style-plan", "正在生成背景风格图", "正在识别菜单并准备 6 张背景卡");
  state.confirmed = false;
  state.charged = false;
  state.chargedPoints = 0;
  state.exportStatus = {
    tone: "idle",
    title: "等待打包导出",
    detail: "正式图生成后，点击按钮会显示打包进度和结果。",
    download: ""
  };
  state.activeJobId = "";
  state.lastDebitOrderId = "";
  state.freeReworkRemaining = 0;
  state.stylePreview = null;
  state.stylePreviewError = null;
  state.selectedRows.clear();
  ensureDeliveryPlatform();
  setControls();
  setProgress(38, "正在识别菜品和品类", 2);
  renderStylePlanLoading();
  try {
    await new Promise(resolve => setTimeout(resolve, 260));
    updateBusy(token, "style-plan", "正在生成背景风格图", "正在整理 6 张背景卡，请稍候");
    setProgress(56, "正在生成 6 张背景卡", 2);
    state.plan = await api(`/api/plan?quality=${encodeURIComponent(state.quality)}`);
    state.style = state.plan.selectedStyle;
    state.pendingStyle = "";
    setProgress(66, "请选择一套图片风格", 2);
    renderPlan(false);
    if (doScroll) scrollToPanel("#stylesPanel");
    if (options.auto) toast("风格方案已生成，请选择一套风格");
  } finally {
    state.running = false;
    endBusy(token);
    setControls();
  }
}

async function confirmStyle() {
  if (!state.plan || !state.pendingStyle) return toast("请先选择风格");
  if (!hasStylePreviewReady()) return toast("请先生成 6 张免费样图");
  if (!state.deliveryPlatforms.length) return toast("请至少选择一个交付平台");
  if (state.running || state.busy) return toast("请等待当前任务完成");
  const charge = totalCharge();
  let debitOrderId = state.lastDebitOrderId || "";
  if (!state.charged && (state.account.balance || 0) < charge) {
    toast(`积分不足，本单需要 ${charge} 积分`);
    openRecharge();
    return;
  }
  state.running = true;
  const token = beginBusy(
    state.charged ? "confirm-generate" : "confirm-charge",
    state.charged ? "正在生成正式图" : "正在扣费",
    state.charged ? `${styleName(state.pendingStyle)} · 正式图生成中` : `本单将扣 ${charge} 积分，正在确认余额`
  );
  try {
    if (!state.charged) {
      const debit = await debitPoints(charge, "正式出图", {
        style: state.pendingStyle,
        quality: state.quality,
        platforms: state.deliveryPlatforms,
        watermark: state.watermark.enabled,
        imageCount: state.plan.summary?.total || state.menu?.count || 0
      });
      debitOrderId = debit.orderId;
      state.lastDebitOrderId = debitOrderId;
      state.charged = true;
      state.chargedPoints = charge;
    }
    updateBusy(token, "confirm-generate", "正在创建正式生图任务", `${styleName(state.pendingStyle)} · 已扣积分，正在准备全部图片`);
    setControls();
    setProgress(82, "已扣积分，正在创建正式生图任务", 3);
    await new Promise(resolve => setTimeout(resolve, 320));
    const created = await createGenerationJob({
      style: state.pendingStyle,
      quality: state.quality,
      watermark: watermarkPayload(),
      platforms: state.deliveryPlatforms,
      points: charge,
      orderId: debitOrderId,
      paid: true
    });
    state.activeJobId = created.job.id;
    updateGenerationJobProgress(created.job, token, "正式生图任务已创建");
    const started = await runGenerationJob(created.job.id, { paid: true, orderId: debitOrderId });
    updateGenerationJobProgress(started.job, token);
    const completed = await pollGenerationJob(created.job.id, { token, initial: started, orderId: debitOrderId });
    state.plan = generationPlanFromJob(completed.job);
    state.activeJobId = completed.job.id;
    state.quality = state.plan.quality?.id || state.quality;
    state.style = state.plan.selectedStyle;
    state.pendingStyle = state.plan.selectedStyle;
    state.confirmed = true;
    state.freeReworkRemaining = state.plan.pricing.freeReworkQuota;
    state.selectedRows.clear();
    const stats = formalPlanStats(state.plan);
    setProgress(100, formalPlanProgressText(state.plan), 4);
    renderPlan(true);
    scrollToPanel("#previewPanel");
    const suffix = `，已完成 ${stats.completed}/${stats.total} 张${stats.failed ? `，失败 ${stats.failed} 张` : ""}${stats.pending ? `，待正式生成 ${stats.pending} 张` : ""}`;
    toast(`已扣 ${charge} 积分${suffix}`);
  } catch (error) {
    if (state.charged) {
      if (debitOrderId) {
        updateBusy(token, "confirm-refund", "生成失败，正在退回积分", "正式图生成失败，本次扣费会自动退回");
        await refundPoints(debitOrderId, charge, "正式出图失败退回积分", { style: state.pendingStyle }).catch(() => {});
      }
      state.charged = false;
      state.chargedPoints = 0;
      state.freeReworkRemaining = 0;
    }
    throw error;
  } finally {
    state.running = false;
    if (token) endBusy(token);
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

async function redrawImage(rowNo, button = null) {
  if (!state.confirmed || !state.plan) return toast("请先生成正式图片");
  if (state.busy) return toast("请等待当前任务完成");
  const row = state.plan.results[rowNo - 1];
  if (!row) return;
  const price = imagePoints();
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
  const token = beginBusy("redraw-debit", "正在扣费并换版", `${row.name} · ${price} 积分`);
  setButtonLoading(button, true, "扣费中");
  try {
    await debitPoints(price, "单张换一版", { row: rowNo, dish: row.name });
    renderPlan(true);
    toast(`已扣 ${price} 积分，${row.name} 已重新生成一版`);
  } finally {
    setButtonLoading(button, false);
    endBusy(token);
  }
}

async function exportImages() {
  if (!state.confirmed) {
    setExportStatus("error", "导出失败", "请先完成正式出图，再打包导出 ZIP。");
    return toast("请先生成正式图片");
  }
  if (state.busy) return toast("请等待当前任务完成");
  const scope = $("#scopeSelect").value;
  const selectedRows = scope === "selected" ? [...state.selectedRows] : [];
  if (scope === "selected" && !selectedRows.length) {
    setExportStatus("error", "导出失败", "请先在正式图片区勾选要导出的图片。");
    return toast("请先勾选要导出的图片");
  }
  if (!hasExportableRows(scope, selectedRows)) {
    setExportStatus("error", "导出失败", "没有可导出的成图，请等待正式图完成，或只勾选已生成的图片。");
    return toast("没有可导出的成图，请等待正式图完成，或只勾选已生成的图片");
  }
  const platforms = exportPlatforms();
  if (!platforms.length) return;
  const detail = `${platformNames(platforms)} · 正在按平台真实尺寸生成 ZIP`;
  setExportStatus("loading", "正在打包导出", detail);
  const token = beginBusy("export-zip", "正在打包 ZIP", "正在按平台尺寸生成下载包，请勿重复点击");
  try {
    const imageFormat = exportImageFormat();
    const data = await api("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ style: state.style, scope, selectedRows, format: imageFormat, imageFormat: imageFormat, watermark: watermarkPayload(), platforms, quality: state.quality })
    });
    setExportStatus(
      "success",
      "打包导出成功",
      `已打包 ${data.images} 张图片，${data.platforms.length} 个平台${data.watermark ? "，已添加品牌水印。" : "。"}`,
      data.download
    );
    toast(`已打包 ${data.images} 张图片，${data.platforms.length} 个平台${data.watermark ? "，已添加品牌水印" : ""}`);
    location.href = data.download;
  } catch (error) {
    setExportStatus("error", "打包导出失败", cleanErrorText(error.message, "服务器生成 ZIP 时遇到问题，请稍后重试。"));
    throw error;
  } finally {
    endBusy(token);
  }
}

async function exportSingle(rowNo, button = null) {
  if (!state.confirmed) return toast("请先生成正式图片");
  if (state.busy) return toast("请等待当前任务完成");
  const platforms = exportPlatforms();
  if (!platforms.length) return;
  const row = state.plan?.results?.[rowNo - 1];
  if (!row || !primaryCandidate(row) || isPendingGeneration(row) || isFailedGeneration(row)) {
    return toast("这张图片还没有可导出的成图，请等待生成完成或重试失败项");
  }
  const token = beginBusy("export-single", "正在准备单张保存", `${row?.name || "单张图片"} · 正在生成下载文件`);
  setButtonLoading(button, true, "保存中");
  try {
    const imageFormat = exportImageFormat();
    const data = await api("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ style: state.style, scope: "selected", selectedRows: [rowNo], format: imageFormat, imageFormat: imageFormat, watermark: watermarkPayload(), platforms, quality: state.quality })
    });
    toast(`已准备单张图片，${data.platforms.length} 个平台${data.watermark ? "，已添加品牌水印" : ""}`);
    location.href = data.download;
  } catch (error) {
    setExportStatus("error", "单张保存失败", cleanErrorText(error.message, "单张图片导出失败，请稍后重试。"));
    throw error;
  } finally {
    setButtonLoading(button, false);
    endBusy(token);
  }
}

function openRefine(rowNo) {
  if (!state.confirmed || !state.plan) return toast("请先生成正式图片");
  const row = state.plan.results[rowNo - 1];
  if (!row) return;
  state.refineRow = rowNo;
  $("#refineTitle").textContent = `自定义修改：${row.name}`;
  $("#refinePrompt").value = "";
  $("#refinePrice").textContent = `自定义修改：${customEditPoints()} 积分/张`;
  $("#refineModal").classList.add("show");
  $("#refineModal").setAttribute("aria-hidden", "false");
  $("#refinePrompt").focus();
}

function closeRefine() {
  $("#refineModal").classList.remove("show");
  $("#refineModal").setAttribute("aria-hidden", "true");
  state.refineRow = null;
}

async function submitRefine() {
  if (!state.refineRow || !state.plan) return;
  if (state.busy) return toast("请等待当前任务完成");
  const prompt = $("#refinePrompt").value.trim();
  const price = customEditPoints();
  if (!prompt) return toast("请先填写精修要求");
  if ((state.account.balance || 0) < price) {
    toast(`积分不足，精修需要 ${price} 积分`);
    openRecharge();
    return;
  }
  const row = state.plan.results[state.refineRow - 1];
  const token = beginBusy("refine-debit", "正在扣费", `${row.name} · 自定义修改 ${price} 积分`);
  const button = $("#submitRefineBtn");
  setButtonLoading(button, true, "提交中");
  try {
    await debitPoints(price, "自定义修改", { row: state.refineRow, dish: row.name, prompt });
    setControls();
    closeRefine();
    toast(`已扣 ${price} 积分，${row.name} 已提交修改`);
  } finally {
    setButtonLoading(button, false);
    endBusy(token);
  }
}

async function refreshAccount() {
  try {
    state.account = await api("/api/account");
    state.accountLoaded = true;
  } catch {
    state.account = { balance: 0, rate: "积分充值", packages: [] };
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
$("#startJobBtn").onclick = () => {
  if (state.plan) return scrollToPanel("#stylesPanel");
  return startJob().catch(e => toast(e.message));
};
$("#generateSamplesBtn").onclick = () => generateStyleSamples().catch(e => toast(e.message));
$("#formalShortcutBtn").onclick = () => scrollToPanel(state.confirmed ? "#previewPanel" : "#styleConfirmBox");
$("#confirmStyleBtn").onclick = () => confirmStyle().catch(e => toast(e.message));
$("#selectAllBtn").onclick = () => chooseRows("all");
$("#selectSingleBtn").onclick = () => chooseRows("single");
$("#selectComboBtn").onclick = () => chooseRows("combo");
$("#exportShortcutBtn").onclick = () => {
  if (state.confirmed && !hasExportableRows()) toast("暂无可导出的成图，请等待正式图完成或重试失败项");
  scrollToPanel("#exportView");
};
$("#exportZipBtn").onclick = () => exportImages().catch(e => toast(e.message));
$("#rechargeBtn").onclick = openRecharge;
$("#closeRechargeBtn").onclick = closeRecharge;
$("#customRechargePoints").oninput = updateCustomRechargeHint;
$("#customRechargeBtn").onclick = submitCustomRecharge;
$("#closeRefineBtn").onclick = closeRefine;
$("#submitRefineBtn").onclick = () => submitRefine().catch(e => toast(e.message));
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
$("#watermarkColor").onchange = event => {
  state.watermark.color = event.target.value === "white" ? "white" : "black";
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

ensureExportFormatOptions();
refreshLibraryStatus();
refreshMenuStatus();
