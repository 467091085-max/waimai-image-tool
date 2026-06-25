const state = {
  plan: null,
  style: "",
  pendingStyle: "",
  menu: null,
  uploaded: false,
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
  busy: null,
  previewLoadingStyle: ""
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

const qualityMeta = {
  standard: { name: "普通出图", points: 100 },
  premium: { name: "精修出图", points: 200 }
};

const styleDisplayNames = ["一号背景", "二号背景", "三号背景", "四号背景", "五号背景", "六号背景"];
const fallbackExtraPlatformPoints = 100;
const fallbackCustomEditPoints = 150;
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
    .replace(/模型|图库|混元|腾讯云|Tencent|Hunyuan|Gemini|API|api/g, "")
    .replace(/复用/g, "沿用")
    .trim();
}

function setPreviewAspect() {
  const meta = platformMeta[primaryPlatform()] || platformMeta.meituan;
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

function styleDisplayName(index) {
  return styleDisplayNames[index] || `${index + 1}号背景`;
}

function styleFallbackImage(index) {
  const palettes = [
    ["#f2c58d", "#fff7e7", "#9a622e"],
    ["#50545d", "#d8bd7a", "#ffffff"],
    ["#e6eaee", "#ffffff", "#5f7483"],
    ["#c84943", "#ffe3a0", "#7e241f"],
    ["#d4bd81", "#f8ffe9", "#5a8a5b"],
    ["#b7d7e8", "#fff2d4", "#336b87"]
  ];
  const [bg, soft, accent] = palettes[index % palettes.length];
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 600">
      <defs>
        <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="${soft}"/>
          <stop offset="1" stop-color="${bg}"/>
        </linearGradient>
      </defs>
      <rect width="800" height="600" fill="url(#bg)"/>
      <ellipse cx="400" cy="350" rx="255" ry="132" fill="#fff" opacity=".94"/>
      <ellipse cx="400" cy="350" rx="205" ry="96" fill="${accent}" opacity=".18"/>
      <circle cx="330" cy="325" r="58" fill="${accent}" opacity=".78"/>
      <circle cx="438" cy="346" r="74" fill="${accent}" opacity=".58"/>
      <circle cx="506" cy="314" r="44" fill="${accent}" opacity=".42"/>
      <path d="M210 438c108 42 278 51 394 5" fill="none" stroke="#ffffff" stroke-width="30" stroke-linecap="round" opacity=".54"/>
    </svg>
  `;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
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
  return resultImage || styleFallbackImage(0);
}

function imageFallbackAttr(index = 0) {
  return `onerror="this.onerror=null;this.src='${styleFallbackImage(index)}'"`;
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
  return state.plan.pricing.watermarkPoints || 0;
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
  if (row.publicStatus) return cleanCustomerStatus(row.publicStatus);
  if (!row.candidates?.length) return "待补图";
  return "已生成";
}

function isPendingGeneration(row) {
  const status = publicStatus(row);
  const generationStatus = String(row?.generation?.status || row?.generationStatus || "").toLowerCase();
  return ["待正式生成", "生成失败", "等待配置"].includes(status)
    || ["pending", "limited", "failed", "error"].includes(generationStatus);
}

function isFailedGeneration(row) {
  const status = publicStatus(row);
  const generationStatus = String(row?.generation?.status || row?.generationStatus || "").toLowerCase();
  return Boolean(row?.generation?.provider_error || row?.generation?.providerError || row?.generation?.refund_required || row?.generation?.refundRequired)
    || status.includes("失败")
    || ["failed", "error"].includes(generationStatus);
}

function isWaitingGeneration(row) {
  const status = publicStatus(row);
  const generationStatus = String(row?.generation?.status || row?.generationStatus || "").toLowerCase();
  return !isFailedGeneration(row) && (
    ["待正式生成", "等待配置", "待补图", "待处理"].includes(status)
    || ["pending", "limited", "queued", "running"].includes(generationStatus)
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
  const reason = generation.provider_error || generation.providerError || generation.error || generation.reason || "生成失败，请稍后重试";
  const suffix = generation.refund_required || generation.refundRequired
    ? "，需要退回本张积分"
    : generation.retryable
      ? "，可重试"
      : "";
  return cleanCustomerStatus(`${reason}${suffix}`);
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
  setProgress(percent, text, 4);
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
  const orderId = options.orderId || "";
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
    if (shouldRunGenerationJob(job)) {
      response = await runGenerationJob(jobId, { paid: true, orderId });
      if (token) updateGenerationJobProgress(response.job, token);
      if (jobRunDeferredOnly(response.job)) {
        initialDeferred = true;
      }
      continue;
    }
    const interval = Math.max(500, Math.min(5000, Number(response.poll?.intervalMs || 1500)));
    await sleep(interval);
    response = await getGenerationJob(jobId);
    if (token) updateGenerationJobProgress(response.job, token);
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
    row.generation = { ...generation, status: status || generation.status || "pending" };
    return row;
  }
  row.publicStatus = row.publicStatus || "已生成";
  row.generationStatus = row.generationStatus || "succeeded";
  row.generation = { ...generation, status: generation.status || status || "succeeded" };
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

async function generateFinalPlan() {
  return api("/api/generate-final", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ style: state.pendingStyle, quality: state.quality, watermark: watermarkPayload() })
  });
}

function setControls() {
  const menuFile = state.menu?.file || "菜单";
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
  const sampleEm = $("#sampleShortcutBtn")?.querySelector("em");
  const formalEm = $("#formalShortcutBtn")?.querySelector("em");
  const exportEm = $("#exportShortcutBtn")?.querySelector("em");
  const readyPreview = hasStylePreviewReady();
  const hasPreviewAttempt = Boolean(state.pendingStyle && state.stylePreview?.style === state.pendingStyle);
  $("#chooseMenuBtn").disabled = state.running || busy;
  $("#chooseMenuBtn").classList.toggle("is-loading", uploadBusy);
  if (chooseEm) chooseEm.textContent = uploadBusy ? "上传中" : "点击上传";
  $("#startJobBtn").disabled = !state.uploaded || state.running || busy;
  $("#startJobBtn").classList.toggle("is-loading", planBusy);
  if (startSub) {
    startSub.textContent = planBusy ? "正在生成背景卡" : state.running ? "正在处理，请稍候" : state.plan ? "可重新生成背景卡" : "菜单后自动开始";
  }
  if (startEm) {
    startEm.textContent = planBusy ? "生成中" : state.running ? "处理中" : state.plan ? "重新选择" : state.uploaded ? "自动开始" : "等待菜单";
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
  $("#sampleShortcutBtn").disabled = !state.plan || !state.pendingStyle || busy;
  $("#sampleShortcutBtn").classList.toggle("is-loading", sampleBusy);
  if (sampleEm) sampleEm.textContent = sampleBusy ? "生成中" : readyPreview ? "查看样图" : hasPreviewAttempt ? "重新生成" : state.pendingStyle ? "去生成" : "选择背景后可用";
  $("#formalShortcutBtn").disabled = !state.plan || (!readyPreview && !state.confirmed) || busy;
  if (formalEm) formalEm.textContent = state.confirmed ? "查看正式图" : readyPreview ? "去确认" : "样图后可用";
  $("#confirmStyleBtn").disabled = !state.plan || !state.pendingStyle || !readyPreview || state.running || busy || !state.deliveryPlatforms.length;
  $("#confirmStyleBtn").classList.toggle("is-loading", confirmBusy);
  $("#confirmStyleBtn").textContent = confirmBusy
    ? (isBusy("confirm-charge") ? "扣费中" : "生成中")
    : (state.plan && readyPreview ? `确认扣 ${totalCharge()} 积分，生成正式图` : "先生成6张免费样图");
  $("#exportShortcutBtn").disabled = !state.confirmed || busy;
  if (exportEm) exportEm.textContent = exportBusy ? "打包中" : state.confirmed ? "去导出" : "生成后可用";
  $("#exportZipBtn").disabled = !state.confirmed || busy;
  $("#exportZipBtn").classList.toggle("is-loading", exportBusy);
  $("#exportZipBtn").textContent = exportBusy ? "打包中" : "打包导出 ZIP";
  $("#menuStatus").textContent = state.uploaded ? `菜单已就绪：${menuFile} · ${menuCount} 个菜` : "等待选择菜单";
  $("#menuStatus").className = `menu-status ${state.uploaded ? "good" : ""}`;
  $("#pointsBalance").textContent = `${state.account.balance || 0}`;
  updateChargeText();
  renderQualityControls();
  renderPlatformControls();
  renderWatermarkControls();
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
        ? `${parts.join(" + ")}，确认后一次性扣除。`
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
      <b>${esc(pkg.cash)}元=${esc(pkg.points)}+${esc(pkg.bonus)}积分</b>
      <span>到账 ${esc(pkg.points + pkg.bonus)} 积分</span>
      <em>${esc(pkg.name)}</em>
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
    { title: "选择背景", status: state.uploaded ? "自动生成中" : "待菜单", state: state.uploaded ? "active" : "" },
    { title: "6张免费样图", status: "待选择背景" },
    { title: "确认正式图", status: "待扣积分" },
    { title: "导出图片", status: "待正式图" }
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
    { title: "选择背景", status: state.pendingStyle ? styleName(state.pendingStyle) : `${styles.length} 张可选`, state: state.pendingStyle || state.confirmed ? "done" : "active" },
    { title: "6张免费样图", status: readyPreview || state.confirmed ? "已生成" : state.pendingStyle ? "待点击生成" : "待选择背景", state: state.confirmed || readyPreview ? "done" : state.pendingStyle ? "active" : "" },
    { title: "确认正式图", status: state.confirmed ? `已扣 ${state.chargedPoints || totalCharge()} 积分` : readyPreview ? `待扣 ${totalCharge()} 积分` : "待样图", state: state.confirmed ? "done" : readyPreview ? "active" : "" },
    { title: "导出图片", status: state.confirmed ? "可以导出" : "待正式图", state: state.confirmed ? "active" : "" }
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
    `交付平台 ${state.deliveryPlatforms.length} 个`,
    generation.jobId ? `任务进度 已完成 ${formalStats.completed} 张 / 失败 ${formalStats.failed} 张 / 总数 ${formalStats.total} 张` : "",
    generation.partial ? "部分图片需要重试" : "",
    generation.refundRequired ? "有图片需要退回积分" : "",
    !generation.jobId && generation.succeeded ? `本次完成 ${generation.succeeded || 0} 张` : "",
    generation.pending ? `待正式生成 ${generation.pending} 张` : "",
    generation.failed ? `生成失败 ${generation.failed} 张` : "",
    state.watermark.enabled ? `品牌水印 ${p.pricing.watermarkPoints} 积分/单` : "品牌水印可选",
    `自定义修改 ${customEditPoints()} 积分/张`,
    needsWork ? `待补图 ${needsWork} 张` : "全部可生成"
  ].filter(Boolean).map(x => `<span class="pill">${esc(x)}</span>`).join("");
  renderReworkBanner();
  renderRecharge();
  setControls();
}

function renderPlatformControls() {
  setPreviewAspect();
  const locked = state.confirmed || Boolean(state.busy);
  $$(".platform-check").forEach(input => {
    input.checked = state.deliveryPlatforms.includes(input.value);
    input.disabled = locked;
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
  $("#platformChargeHint").textContent = names.length
    ? `当前平台附加积分：+${charge}积分；${names.join(" / ")}`
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
  const meta = platformMeta[primaryPlatform()] || platformMeta.meituan;
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
  demo.className = `watermark-demo ${state.watermark.enabled ? "enabled" : ""} ${state.watermark.pattern} ${state.watermark.position}`;
  demo.innerHTML = `
    <span>${locked ? "水印已锁定" : `水印预览 · ${meta.name} ${meta.size}，${meta.ratio}`}</span>
    <div class="watermark-preview-canvas">
      <img class="watermark-demo-image" src="${watermarkDemoImage()}" alt="水印示意图" ${imageFallbackAttr(0)}>
      ${watermarkOverlay(text)}
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

function normalizePreviewSample(raw = {}, index = 0, options = {}) {
  const generation = raw.generation || {};
  const status = String(raw.status || generation.status || raw.publicStatus || "").toLowerCase();
  const error = raw.error || raw.provider_error || raw.providerError || generation.error || generation.provider_error || generation.providerError || "";
  const imageUrl = raw.imageUrl || raw.image_url || raw.url || raw.candidate?.url || "";
  const completedWithoutImage = !imageUrl && ["succeeded", "success", "completed", "cached"].includes(status);
  const failed = previewFailureStatus(status) || Boolean(error) || completedWithoutImage || (options.requireImage && !imageUrl);
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
  const status = generation.status === "failed"
    ? (generation.error ? `生成失败：${generation.error}` : "生成失败，可重新生成")
    : generation.status === "pending"
      ? "生成中"
      : "待补图";
  if (!image) {
    return `<div class="preview-sample placeholder missing">
      <b>${esc(name)}</b>
      <div class="sample-frame"></div>
      <p>${esc(status)}</p>
    </div>`;
  }
  return `<div class="preview-sample">
    <b>${esc(name)}</b>
    <img src="${image.url}" alt="${esc(name)}">
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
    const sampleUrl = s.sample?.url || styleFallbackImage(s.uiIndex);
    return `<button class="style ${selected ? "active" : ""} ${loading ? "is-loading" : ""}" data-style="${s.id}" type="button">
      <img src="${sampleUrl}" alt="${esc(s.uiName)}" ${imageFallbackAttr(s.uiIndex)}>
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
      setProgress(68, "已选择背景，请生成 6 张免费样图", 3);
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
    if (title) title.textContent = `${styleName(state.pendingStyle)} · 免费样图生成中`;
    setStylePreviewStatus("loading", "正在生成 6 张免费单品样图，请稍候。生成完成后会自动显示在下方。");
    box.className = "style-preview-box loading";
    box.innerHTML = previewPlaceholders("正在生成");
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
      setProgress(68 + Math.round((index / 6) * 8), `正在生成第 ${index + 1}/6 张免费样图`, 3);
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
      ready ? 4 : 3
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
    ? `换一版（免费剩 ${state.freeReworkRemaining}）`
    : `换一版 ${imagePoints()}积分`;
  const refineLabel = `自定义修改 ${customEditPoints()}积分/张`;
  const card = (row, index) => {
    const rowNo = index + 1;
    const candidate = isPendingGeneration(row) ? null : (row.candidates || [])[0];
    const checked = state.selectedRows.has(rowNo) ? "checked" : "";
    const status = publicStatus(row);
    const failure = generationFailureMessage(row);
    const saveButton = candidate ? `<button class="single-save-btn" data-row="${rowNo}" type="button">单张保存</button>` : "";
    return `<div class="result">
      <label class="select-line"><input type="checkbox" class="row-check" data-row="${rowNo}" ${checked}> 选择</label>
      <div class="result-title">${esc(row.name)}</div>
      ${candidate ? `<div class="image-wrap"><img src="${candidate.url}" alt="${esc(row.name)}" ${imageFallbackAttr(0)}>${watermarkOverlay(state.menu?.store || "品牌名")}</div>` : `<div class="empty image-empty">${esc(status)}</div>`}
      <div class="result-body">
        <p>${esc(row.category || "未分类")} · ${esc(row.kind)}</p>
        <div><span class="pill ${generationStatusPillClass(row)}">${esc(status)}</span><span class="pill">正式图 ${imagePoints()} 积分/张</span></div>
        ${failure ? `<p class="result-error">${esc(failure)}</p>` : ""}
        <div class="result-actions">${saveButton}<button class="redraw-btn" data-row="${rowNo}" type="button">${redrawLabel}</button><button class="refine-btn" data-row="${rowNo}" type="button">${refineLabel}</button></div>
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
  state.freeReworkRemaining = 0;
  state.stylePreview = null;
  state.stylePreviewError = null;
  state.selectedRows.clear();
  setControls();
  setProgress(38, "正在识别菜品和品类", 2);
  try {
    await new Promise(resolve => setTimeout(resolve, 260));
    updateBusy(token, "style-plan", "正在生成背景风格图", "正在整理 6 张背景卡，请稍候");
    setProgress(56, "正在生成 6 张背景卡", 2);
    state.plan = await api(`/api/plan?quality=${encodeURIComponent(state.quality)}`);
    state.style = state.plan.selectedStyle;
    state.pendingStyle = "";
    setProgress(66, "请选择一套图片风格", 3);
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
  let debitOrderId = "";
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
      state.charged = true;
      state.chargedPoints = charge;
    }
    updateBusy(token, "confirm-generate", "正在创建正式生图任务", `${styleName(state.pendingStyle)} · 已扣积分，正在准备全部图片`);
    setControls();
    setProgress(82, "已扣积分，正在创建正式生图任务", 4);
    await new Promise(resolve => setTimeout(resolve, 320));
    let usedGenerateFinalFallback = false;
    try {
      const created = await createGenerationJob({
        style: state.pendingStyle,
        quality: state.quality,
        watermark: watermarkPayload(),
        platforms: state.deliveryPlatforms,
        points: charge,
        orderId: debitOrderId,
        paid: true
      });
      updateGenerationJobProgress(created.job, token, "正式生图任务已创建");
      const started = await runGenerationJob(created.job.id, { paid: true, orderId: debitOrderId });
      updateGenerationJobProgress(started.job, token);
      const completed = await pollGenerationJob(created.job.id, { token, initial: started, orderId: debitOrderId });
      state.plan = generationPlanFromJob(completed.job);
    } catch (jobError) {
      usedGenerateFinalFallback = true;
      console.warn("Generation jobs API failed, falling back to /api/generate-final", jobError);
      updateBusy(token, "confirm-generate", "正在使用兼容生成流程", "任务接口暂不可用，已自动切换到原有正式出图流程");
      setProgress(86, "任务接口暂不可用，正在使用兼容生成流程", 4);
      state.plan = await generateFinalPlan();
      state.plan.generation = { ...(state.plan.generation || {}), fallbackMode: "generate-final" };
    }
    state.quality = state.plan.quality?.id || state.quality;
    state.style = state.plan.selectedStyle;
    state.pendingStyle = state.plan.selectedStyle;
    state.confirmed = true;
    state.freeReworkRemaining = state.plan.pricing.freeReworkQuota;
    state.selectedRows.clear();
    const stats = formalPlanStats(state.plan);
    setProgress(100, formalPlanProgressText(state.plan), 5);
    renderPlan(true);
    scrollToPanel("#previewPanel");
    const suffix = `，已完成 ${stats.completed}/${stats.total} 张${stats.failed ? `，失败 ${stats.failed} 张` : ""}${stats.pending ? `，待正式生成 ${stats.pending} 张` : ""}`;
    toast(`已扣 ${charge} 积分${suffix}${usedGenerateFinalFallback ? "，已切换兼容流程" : ""}`);
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
  if (!state.confirmed) return toast("请先生成正式图片");
  if (state.busy) return toast("请等待当前任务完成");
  const scope = $("#scopeSelect").value;
  const selectedRows = scope === "selected" ? [...state.selectedRows] : [];
  if (scope === "selected" && !selectedRows.length) return toast("请先勾选要导出的图片");
  const platforms = exportPlatforms();
  if (!platforms.length) return;
  const token = beginBusy("export-zip", "正在打包 ZIP", "正在按平台尺寸生成下载包，请勿重复点击");
  try {
    const data = await api("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ style: state.style, scope, selectedRows, format: $("#formatSelect").value, watermark: watermarkPayload(), platforms, quality: state.quality })
    });
    toast(`已打包 ${data.images} 张图片，${data.platforms.length} 个平台${data.watermark ? "，已添加品牌水印" : ""}`);
    location.href = data.download;
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
  const token = beginBusy("export-single", "正在准备单张保存", `${row?.name || "单张图片"} · 正在生成下载文件`);
  setButtonLoading(button, true, "保存中");
  try {
    const data = await api("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ style: state.style, scope: "selected", selectedRows: [rowNo], format: $("#formatSelect").value, watermark: watermarkPayload(), platforms, quality: state.quality })
    });
    toast(`已准备单张图片，${data.platforms.length} 个平台${data.watermark ? "，已添加品牌水印" : ""}`);
    location.href = data.download;
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
$("#startJobBtn").onclick = () => startJob().catch(e => toast(e.message));
$("#generateSamplesBtn").onclick = () => generateStyleSamples().catch(e => toast(e.message));
$("#sampleShortcutBtn").onclick = () => {
  if (hasStylePreviewReady()) return scrollToPanel("#stylePreviewBox");
  return generateStyleSamples().catch(e => toast(e.message));
};
$("#formalShortcutBtn").onclick = () => scrollToPanel(state.confirmed ? "#previewPanel" : "#styleConfirmBox");
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

refreshMenuStatus();
