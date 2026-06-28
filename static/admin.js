const $ = selector => document.querySelector(selector);

const esc = value => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

const sourceLabels = {
  clean: "Clean",
  watermark: "Watermark",
  internal: "Internal",
  external: "External",
  unknown: "Unknown"
};

const assetKindLabels = {
  product_image: "菜品图",
  category_background: "品类背景",
  unknown: "未知"
};

const assetStatusLabels = {
  approved: "已通过",
  rejected: "已拒绝",
  disabled: "已停用",
  pending: "待处理"
};

const aiAssetActions = [
  { action: "approve", status: "approved", label: "通过" },
  { action: "reject", status: "rejected", label: "拒绝" },
  { action: "disable", status: "disabled", label: "停用" }
];

const jobStatusLabels = {
  queued: "排队",
  running: "执行中",
  succeeded: "成功",
  failed: "失败",
  canceled: "取消"
};

const riskDecisionLabels = {
  allow: "放行",
  deny: "拒绝",
  review: "复核"
};

const riskLevelLabels = {
  info: "信息",
  low: "低",
  medium: "中",
  high: "高"
};

const withdrawalStatusLabels = {
  pending: "待审批",
  approved: "已通过",
  rejected: "已拒绝",
  paid: "已打款",
  canceled: "已取消"
};

const withdrawalActions = [
  { status: "approved", label: "通过" },
  { status: "rejected", label: "拒绝" },
  { status: "paid", label: "打款" },
  { status: "canceled", label: "取消" }
];

function sourceClass(source) {
  return ["clean", "watermark", "internal"].includes(source) ? source : "external";
}

function assetStatusClass(status) {
  return ["approved", "rejected", "disabled", "pending"].includes(status) ? status : "pending";
}

function riskDecisionClass(decision) {
  return ["allow", "deny", "review"].includes(decision) ? decision : "review";
}

function riskLevelClass(level) {
  return ["info", "low", "medium", "high"].includes(level) ? level : "info";
}

function withdrawalStatusClass(status) {
  return ["pending", "approved", "rejected", "paid", "canceled"].includes(status) ? status : "pending";
}

function setText(selector, value) {
  const el = $(selector);
  if (el) el.textContent = value;
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2400);
}

async function api(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  const data = await response.json();
  if (!response.ok || data.error) {
    throw new Error(data.error || "请求失败");
  }
  return data;
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload || {})
  });
  const data = await response.json();
  if (!response.ok || data.error) {
    throw new Error(data.error || "请求失败");
  }
  return data;
}

async function adminList(resource, params = {}) {
  const query = new URLSearchParams(params);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return api(`/api/admin/lists/${resource}${suffix}`);
}

async function loadAdminLists() {
  const configs = [
    ["tasks", "generation-tasks"],
    ["assetAccess", "asset-access"],
    ["riskEvents", "risk-events"],
    ["settlements", "commission-settlements"],
    ["withdrawals", "withdrawals"],
    ["orders", "orders"]
  ];
  const results = await Promise.allSettled(
    configs.map(([key, resource]) => (
      adminList(resource, { limit: 8, sort: "createdAt", order: "desc" })
        .then(data => [key, data])
    ))
  );

  return results.reduce((lists, result, index) => {
    const [key, resource] = configs[index];
    if (result.status === "fulfilled") {
      const [, data] = result.value;
      lists[key] = data;
    } else {
      lists.errors.push(resource);
      lists[key] = { ok: false, resource, total: 0, items: [] };
    }
    return lists;
  }, { errors: [] });
}

function renderLibrary(data) {
  const summary = data.summary || {};
  const sources = data.sources || {};
  setText("#metricTotal", summary.total ?? 0);
  setText("#metricClean", sources.clean ?? 0);
  setText("#metricWatermark", sources.watermark ?? 0);
  setText("#metricInternal", sources.internal ?? 0);
  setText("#metricReusable", summary.reusable ?? 0);
  setText("#metricStores", `${summary.stores ?? 0} / ${summary.styles ?? 0}`);
  setText("#libraryHint", `来源 ${Object.keys(sources).length} 类，样例 ${data.samples?.length || 0} 张`);

  $("#sourceBreakdown").innerHTML = Object.entries(sources)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([source, count]) => (
      `<span class="source-chip ${sourceClass(source)}">
        ${esc(sourceLabels[source] || source)}
        <b>${esc(count)}</b>
      </span>`
    ))
    .join("");

  const samples = data.samples || [];
  $("#sampleGrid").innerHTML = samples.length ? samples.map(sample => sampleCard(sample)).join("") : (
    '<div class="empty-state">暂无图库样例</div>'
  );
}

function sampleCard(sample) {
  const source = sample.source || "unknown";
  const image = sample.url
    ? `<img src="${esc(sample.url)}" alt="${esc(sample.dishName || "样例图片")}" loading="lazy">`
    : '<span>无图片</span>';
  return (
    `<article class="sample-card">
      <figure>${image}</figure>
      <figcaption>
        <div class="sample-meta">
          <span class="pill ${sourceClass(source)}">${esc(sourceLabels[source] || source)}</span>
          <span>${sample.reusable ? "可复用" : "仅参考"}</span>
        </div>
        <b title="${esc(sample.dishName)}">${esc(sample.dishName || "未命名菜品")}</b>
        <span title="${esc(sample.store)}">${esc(sample.store || "未知门店")}</span>
      </figcaption>
    </article>`
  );
}

function renderAudit(data) {
  const audit = data.audit || {};
  setText("#auditFiles", audit.files ?? 0);
  setText("#auditParsed", audit.parsed ?? 0);
  setText("#auditFailed", audit.failed ?? 0);
  setText("#auditItems", audit.totalItems ?? 0);
  setText("#auditHint", `扫描 ${audit.scanned ?? 0} 个上传菜单文件`);

  renderCurrentMenu(data.current || {});
  renderAuditRows(audit.menus || []);
  renderAuditErrors(audit.errors || []);
}

function renderOps(data, lists = {}) {
  const summary = data.summary || {};
  const jobs = summary.jobs || {};
  const images = summary.images || {};
  const exports = summary.exports || {};
  const points = summary.points || {};
  const commissions = summary.commissions || {};
  const risk = summary.risk || {};
  const assetAccess = summary.assetAccess || {};

  setText("#opsJobs", jobs.total ?? 0);
  setText("#opsImages", images.total ?? 0);
  setText("#opsExports", exports.total ?? 0);
  setText("#opsPoints", signedNumber(points.net ?? 0));
  setText("#opsCommission", cents(commissions.pendingAmount ?? 0));
  setText("#opsRisk", risk.total ?? 0);
  setText("#opsHint", `最近任务 ${data.recentJobs?.length || 0} 条，更新时间 ${data.generatedAt || "-"}`);

  renderOpsJobs(data.recentJobs || []);
  renderOpsRisk(summary, data.risk || {}, data.assetAccess || {});
  renderProductConsole(data, summary, data.recentJobs || [], lists);
}

function renderAiAssets(data) {
  const summary = data.summary || {};
  const byKind = summary.byKind || {};
  const byCategory = summary.byCategory || {};
  const assets = data.assets || [];

  setText("#aiAssetTotal", summary.total ?? 0);
  setText("#aiAssetApproved", summary.approved ?? 0);
  setText("#aiAssetPending", summary.pending ?? 0);
  setText("#aiAssetRejected", summary.rejected ?? 0);
  setText("#aiAssetDisabled", summary.disabled ?? 0);
  setText("#aiAssetKinds", `${Object.keys(byKind).length} / ${Object.keys(byCategory).length}`);
  setText("#aiAssetHint", `展示 ${assets.length} 条，最多显示前 50 条`);

  $("#aiAssetBreakdown").innerHTML = [
    ...assetChips("类型", byKind, assetKindLabels),
    ...assetChips("品类", byCategory)
  ].join("");
  renderAiAssetRows(assets);
}

function assetChips(prefix, values, labels = {}) {
  return Object.entries(values)
    .sort(([, a], [, b]) => Number(b) - Number(a))
    .slice(0, 10)
    .map(([name, count]) => (
      `<span class="asset-chip">
        ${esc(prefix)}：${esc(labels[name] || name)}
        <b>${esc(count)}</b>
      </span>`
    ));
}

function renderAiAssetRows(assets) {
  $("#aiAssetRows").innerHTML = assets.length ? assets.map(asset => {
    const status = assetStatusClass(asset.status || "pending");
    const quality = Number(asset.qualityScore || 0);
    const assetId = asset.assetId || "";
    const qualityReasons = Array.isArray(asset.qualityReasons)
      ? asset.qualityReasons.join("；")
      : asset.qualityReasons || "";
    return (
      `<tr>
        <td>${esc(assetId || "-")}</td>
        <td>${esc(assetKindLabels[asset.kind] || asset.kind || "-")}</td>
        <td>${esc(asset.category || "-")}</td>
        <td>${esc(asset.productName || "-")}</td>
        <td>${esc(asset.styleId || "-")}</td>
        <td><span class="status-pill ${status}">${esc(assetStatusLabels[status] || status)}</span></td>
        <td>${esc(quality.toFixed(2))}<span class="row-note">${esc(qualityReasons || "-")}</span></td>
        <td>${esc(asset.createdAt || "-")}</td>
        <td>${aiAssetActionPanel(assetId)}</td>
      </tr>`
    );
  }).join("") : (
    emptyRow(9, "暂无 AI 资产")
  );
}

function aiAssetActionPanel(assetId) {
  const disabled = assetId ? "" : " disabled";
  const buttons = aiAssetActions.map(action => (
    `<button
      type="button"
      class="asset-action ${esc(action.action)}"
      data-ai-asset-action="${esc(action.action)}"
      data-ai-asset-status="${esc(action.status)}"
      data-asset-id="${esc(assetId)}"${disabled}>
      ${esc(action.label)}
    </button>`
  )).join("");

  return (
    `<div class="asset-review-actions">
      <input
        class="asset-review-note"
        data-ai-asset-note
        type="text"
        maxlength="120"
        placeholder="审核备注"
        aria-label="AI 资产审核备注">
      <div class="asset-review-buttons">${buttons}</div>
    </div>`
  );
}

async function updateAiAssetStatus(assetId, status, qualityNote) {
  return postJson(`/api/admin/actions/ai-assets/${encodeURIComponent(assetId)}/status`, {
    status,
    qualityNote
  });
}

async function handleAiAssetAction(event) {
  const button = event.target.closest("[data-ai-asset-status]");
  if (!button) return;

  const row = button.closest("tr");
  const assetId = button.dataset.assetId || "";
  const status = button.dataset.aiAssetStatus || "";
  const note = row?.querySelector("[data-ai-asset-note]")?.value.trim() || "";
  const buttons = Array.from(row?.querySelectorAll("[data-ai-asset-status]") || []);

  if (!assetId || !status) {
    toast("缺少 AI 资产或审核状态");
    return;
  }

  buttons.forEach(actionButton => {
    actionButton.disabled = true;
  });

  try {
    await updateAiAssetStatus(assetId, status, note);
    toast("AI 资产审核已更新");
    await loadAiAssets();
  } catch (error) {
    toast(error.message);
  } finally {
    buttons.forEach(actionButton => {
      actionButton.disabled = false;
    });
  }
}

function renderOpsJobs(jobs) {
  $("#opsJobRows").innerHTML = jobs.length ? jobs.map(job => (
    `<tr>
      <td>${esc(job.id || "-")}</td>
      <td>${esc(job.status || "-")}</td>
      <td>${esc(job.styleId || job.style_id || "-")}</td>
      <td>${esc(job.completedCount ?? job.completed_count ?? 0)} / ${esc(job.requestedCount ?? job.requested_count ?? 0)}</td>
      <td>${esc(job.failedCount ?? job.failed_count ?? 0)}</td>
      <td>${esc(job.createdAt || job.created_at || "-")}</td>
    </tr>`
  )).join("") : (
    '<tr><td colspan="6">暂无生成任务</td></tr>'
  );
}

function renderOpsRisk(summary, risk, assetAccess) {
  const rows = [
    ["风控事件", summary.risk?.total ?? 0],
    ["待审核/拒绝", `${summary.risk?.review ?? 0} / ${summary.risk?.denied ?? 0}`],
    ["资产访问", summary.assetAccess?.total ?? 0],
    ["访问拒绝", summary.assetAccess?.denied ?? 0],
    ["最近高风险", risk.highestLevel || "info"],
    ["最近拒绝原因", assetAccess.topDenyReason || "-"]
  ];
  $("#opsRiskList").innerHTML = rows.map(([label, value]) => (
    `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`
  )).join("");
}

function listFrom(value) {
  if (Array.isArray(value)) {
    return value.filter(item => item !== undefined && item !== null && item !== "");
  }
  return value === undefined || value === null || value === "" ? [] : [value];
}

function readinessIssues(component) {
  if (!component || typeof component !== "object") return [];
  return [
    ...listFrom(component.blockingIssues),
    ...listFrom(component.errors),
    ...listFrom(component.error)
  ];
}

function readinessWarnings(component) {
  if (!component || typeof component !== "object") return [];
  return listFrom(component.warnings);
}

function readinessReadyLabel(ready, missing = false) {
  if (missing) return "未接入";
  if (ready === true) return "Ready";
  if (ready === false) return "Not ready";
  return "未知";
}

function readinessClass(ready, missing = false, errors = [], warnings = []) {
  if (missing) return "missing";
  if (errors.length || ready === false) return "error";
  if (warnings.length || ready === undefined || ready === null) return "warning";
  return "ready";
}

function readinessCard({ title, ready, status, warnings = [], errors = [], detail = "", missing = false }) {
  const stateClass = readinessClass(ready, missing, errors, warnings);
  const warningText = warnings.length ? warnings.join("；") : "-";
  const errorText = errors.length ? errors.join("；") : "-";
  const detailHtml = detail ? `<p>${esc(detail)}</p>` : "";
  return (
    `<article class="readiness-card ${stateClass}">
      <div class="readiness-card-head">
        <h3>${esc(title)}</h3>
        <span>${esc(readinessReadyLabel(ready, missing))}</span>
      </div>
      <dl>
        <dt>ready</dt><dd>${esc(readinessReadyLabel(ready, missing))}</dd>
        <dt>status</dt><dd>${esc(status || "未知")}</dd>
        <dt>warnings</dt><dd>${esc(warningText)}</dd>
        <dt>errors</dt><dd>${esc(errorText)}</dd>
      </dl>
      ${detailHtml}
    </article>`
  );
}

function renderObjectStorageReadiness(objectStorage) {
  const missing = !objectStorage || typeof objectStorage !== "object";
  const warnings = readinessWarnings(objectStorage);
  const errors = readinessIssues(objectStorage);
  const status = missing
    ? "未接入"
    : [objectStorage.provider, objectStorage.mode || objectStorage.status].filter(Boolean).join(" / ") || "未知";
  return readinessCard({
    title: "对象存储 readiness",
    ready: objectStorage?.ready,
    status,
    warnings,
    errors,
    missing
  });
}

function secondsText(value) {
  if (value === undefined || value === null) return "-";
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "-";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  return `${Math.round(seconds / 60)}m`;
}

function renderGenerationQueueReadiness(queue) {
  const missing = !queue || typeof queue !== "object";
  const counts = queue?.countsByStatus || {};
  const queued = Number(counts.queued || 0);
  const running = Number(counts.running || 0);
  const warnings = [];
  const errors = [];

  if (!missing && Number(queue.staleCount || 0) > 0) {
    warnings.push(`stale_jobs:${queue.staleCount}`);
  }
  if (!missing && Number(queue.timedOutCount || 0) > 0) {
    warnings.push(`timed_out_jobs:${queue.timedOutCount}`);
  }
  if (!missing && queue.closed) {
    errors.push("generation_queue_closed");
  }

  const ready = missing ? undefined : !queue.closed;
  const status = missing
    ? "未接入"
    : `${queue.closed ? "已关闭" : "运行中"} · worker ${queue.workerCount ?? "-"} · queued ${queued} / running ${running}`;
  const detail = missing
    ? "/api/admin/queue-snapshot 未返回队列快照"
    : `maxPending ${queue.limits?.maxPendingJobs ?? "-"} · oldest queued ${secondsText(queue.oldestQueuedAgeSeconds)} · oldest running ${secondsText(queue.oldestRunningAgeSeconds)}`;

  return readinessCard({
    title: "生成队列状态",
    ready,
    status,
    warnings,
    errors,
    detail,
    missing
  });
}

function renderPaymentReadiness(payments, hasPaymentsField) {
  const missing = !hasPaymentsField || !payments || typeof payments !== "object";
  const warnings = readinessWarnings(payments);
  const errors = readinessIssues(payments);
  const status = missing
    ? "未接入"
    : [payments.provider, payments.mode || payments.status || payments.state].filter(Boolean).join(" / ") || "未知";
  const detail = missing ? "readiness 未返回 payments 字段" : "";
  return readinessCard({
    title: "支付 readiness",
    ready: payments?.ready,
    status,
    warnings,
    errors,
    detail,
    missing
  });
}

function renderGenerationProviderReadiness(generationProvider, hasGenerationProviderField) {
  const missing = !hasGenerationProviderField || !generationProvider || typeof generationProvider !== "object";
  const warnings = readinessWarnings(generationProvider);
  const errors = readinessIssues(generationProvider);
  const status = missing
    ? "未接入"
    : [
        generationProvider.provider,
        generationProvider.mode,
        generationProvider.tokenhubModel
      ].filter(Boolean).join(" / ") || "未知";
  const detail = missing
    ? "readiness 未返回 generationProvider 字段"
    : `TokenHub ${generationProvider.tokenhubReady ? "ready" : "not ready"} · Cloud API ${generationProvider.cloudApiReady ? "ready" : "not ready"} · tokenhubRequired ${generationProvider.tokenhubRequired ? "yes" : "no"}`;
  return readinessCard({
    title: "AI 生图 provider",
    ready: generationProvider?.ready,
    status,
    warnings,
    errors,
    detail,
    missing
  });
}

function renderOpsReadiness(readiness = {}, queueSnapshot = null) {
  const container = $("#opsReadinessCards");
  if (!container) return;

  const hasPaymentsField = Object.prototype.hasOwnProperty.call(readiness || {}, "payments");
  const hasGenerationProviderField = Object.prototype.hasOwnProperty.call(readiness || {}, "generationProvider");
  const queue = readiness?.generationQueue || queueSnapshot;
  const cards = [
    renderObjectStorageReadiness(readiness?.objectStorage),
    renderGenerationProviderReadiness(readiness?.generationProvider, hasGenerationProviderField),
    renderGenerationQueueReadiness(queue),
    renderPaymentReadiness(readiness?.payments, hasPaymentsField)
  ];

  container.innerHTML = cards.join("");

  const queueSource = readiness?.generationQueue ? "readiness" : (queueSnapshot ? "queue-snapshot" : "未接入");
  const overall = readiness?.ready === true
    ? "整体 Ready"
    : readiness?.ready === false
      ? "整体 Not ready"
      : "readiness 已读取";
  setText("#opsReadinessHint", `${overall}，队列来源 ${queueSource}`);
}

function renderProductConsole(data, summary, jobs, lists = {}) {
  const loadedLists = ["tasks", "assetAccess", "riskEvents", "settlements", "withdrawals", "orders"]
    .filter(key => lists[key]?.ok !== false && Array.isArray(lists[key]?.items)).length;
  setText("#productConsoleHint", `接入 /api/admin/dashboard + ${loadedLists}/6 个列表 API，更新时间 ${data.generatedAt || "-"}`);
  renderTaskModule(summary.jobs || {}, listItems(lists.tasks, jobs), lists.tasks);
  renderRiskEventModule(summary.risk || {}, data.risk || {}, listItems(lists.riskEvents), lists.riskEvents);
  renderAssetAuditModule(summary.assetAccess || {}, data.assetAccess || {}, listItems(lists.assetAccess), lists.assetAccess);
  renderCommissionModule(summary.commissions || {}, data.commissions || {}, listItems(lists.settlements), lists.settlements);
  renderWithdrawalModule(listItems(lists.withdrawals), lists.withdrawals);
  renderOrderModule(summary, data.commissions || {}, listItems(lists.orders), lists.orders, jobs);
}

function renderTaskModule(jobsSummary, jobs, taskList = {}) {
  const running = Number(jobsSummary.running || 0) + Number(jobsSummary.queued || 0);
  const total = taskList.total ?? jobsSummary.total ?? jobs.length;
  setText("#taskModuleBadge", `${jobs.length} / ${total} 条明细`);
  setText("#taskTotal", jobsSummary.total ?? 0);
  setText("#taskRunning", running);
  setText("#taskSuccessRate", percent(jobsSummary.successRate ?? 0));

  $("#taskRows").innerHTML = jobs.length ? jobs.slice(0, 8).map(job => {
    const requested = job.requestedCount ?? job.requested_count ?? 0;
    const completed = job.completedCount ?? job.completed_count ?? 0;
    const progress = job.progress ?? ratio(completed, requested);
    const status = job.status || "-";
    const note = [job.storeName, job.createdAt || job.created_at].filter(Boolean).join(" · ") || "-";
    return (
      `<tr>
        <td><b>${esc(job.id || "-")}</b><span class="row-note">${esc(note)}</span></td>
        <td>${esc(qualityLabel(job.quality))}</td>
        <td>${esc(jobStatusLabels[status] || status)} · ${esc(percent(progress))}</td>
        <td>${esc(signedNumber(job.pointDelta ?? 0))}</td>
        <td>${esc(job.exportCount ?? 0)} 包</td>
      </tr>`
    );
  }).join("") : emptyRow(5, "暂无生成任务记录");
}

function renderRiskEventModule(summary, risk, riskRows = [], riskList = {}) {
  const byDecision = risk.byDecision || {};
  const byLevel = risk.byLevel || {};
  const rows = riskRows.length ? riskRows : (risk.recent || []);
  const total = riskList.total ?? summary.total ?? risk.total ?? rows.length;
  const review = summary.review ?? byDecision.review ?? 0;
  const denied = summary.denied ?? summary.deny ?? byDecision.deny ?? 0;
  const badge = riskRows.length ? `${riskRows.length} / ${total} 条明细` : `最高风险：${risk.highestLevel || "info"}`;

  setText("#riskEventBadge", badge);
  setText("#riskEventTotal", summary.total ?? risk.total ?? 0);
  setText("#riskEventReview", review);
  setText("#riskEventDenied", denied);

  const decisionChips = Object.entries(byDecision).map(([decision, count]) => (
    chip(`决策：${riskDecisionLabels[decision] || decision}`, count)
  ));
  const levelChips = Object.entries(byLevel).map(([level, count]) => (
    chip(`等级：${riskLevelLabels[level] || level}`, count)
  ));
  const eventChips = groupChips("事件", risk.topEvents || []);
  $("#riskEventChips").innerHTML = [...decisionChips, ...levelChips, ...eventChips].join("") || '<span class="module-chip">暂无风险事件分布</span>';

  $("#riskEventRows").innerHTML = rows.length ? rows.slice(0, 8).map(row => {
    const decision = row.decision || "-";
    const level = row.riskLevel || "-";
    const eventNote = [row.id, row.assetId].filter(Boolean).join(" · ") || "-";
    const userNote = [row.agentId, row.ip].filter(Boolean).join(" · ") || "-";
    return (
      `<tr>
        <td><span class="status-pill ${riskDecisionClass(decision)}">${esc(riskDecisionLabels[decision] || decision)}</span></td>
        <td><span class="status-pill ${riskLevelClass(level)}">${esc(riskLevelLabels[level] || level)}</span></td>
        <td><b>${esc(row.eventType || "-")}</b><span class="row-note">${esc(eventNote)}</span></td>
        <td><b>${esc(row.userId || "-")}</b><span class="row-note">${esc(userNote)}</span></td>
        <td>${esc(row.denyReason || "-")}</td>
        <td>${esc(row.createdAt || "-")}</td>
      </tr>`
    );
  }).join("") : emptyRow(6, "暂无风险事件明细");
}

function renderAssetAuditModule(summary, assetAccess, accessRows = [], accessList = {}) {
  const total = accessList.total ?? summary.total ?? assetAccess.total ?? 0;
  const badge = accessRows.length ? `${accessRows.length} / ${total} 条明细` : (
    summary.topDenyReason ? `高频拒绝：${summary.topDenyReason}` : "访问审计"
  );
  setText("#assetAuditBadge", badge);
  setText("#assetAccessTotal", summary.total ?? assetAccess.total ?? 0);
  setText("#assetAccessAllowed", summary.allowed ?? assetAccess.allowed ?? 0);
  setText("#assetAccessDenied", summary.denied ?? assetAccess.denied ?? 0);

  const actionChips = groupChips("动作", assetAccess.byAction || []);
  const assetChips = (assetAccess.topAssets || []).map(item => (
    `<span class="module-chip">资产：${esc(item.assetId || "-")}<b>${esc(item.accessCount ?? 0)}</b></span>`
  ));
  $("#assetAuditChips").innerHTML = [...actionChips, ...assetChips].join("") || '<span class="module-chip">暂无资产访问分布</span>';

  const rows = accessRows.length ? accessRows : (assetAccess.recentDenied || []);
  $("#assetDeniedRows").innerHTML = rows.length ? rows.slice(0, 8).map(row => {
    const allowedText = row.allowed === true ? "允许" : (row.allowed === false ? "拒绝" : "-");
    const reason = row.denyReason || (row.allowed === true ? "允许访问" : "-");
    return (
    `<tr>
      <td><b>${esc(row.assetId || "-")}</b><span class="row-note">${esc(row.assetType || row.requestId || "-")}</span></td>
      <td>${esc(row.action || "-")}<span class="row-note">${esc(allowedText)}</span></td>
      <td>${esc(reason)}</td>
      <td>${esc(row.createdAt || "-")}</td>
    </tr>`
    );
  }).join("") : emptyRow(4, "暂无资产访问明细");
}

function renderCommissionModule(summary, commissions, settlementRows = [], settlementList = {}) {
  const orders = commissions.orders || summary.orders || {};
  const settlements = commissions.settlements || summary.settlements || {};
  const topAgents = commissions.topAgents || [];
  const pendingAmount = summary.pendingAmount ?? orders.pendingCommissionAmount ?? 0;
  const eligibleAmount = summary.eligibleAmount ?? orders.eligibleCommissionAmount ?? 0;
  const settledAmount = summary.settledAmount ?? orders.settledCommissionAmount ?? 0;

  setText("#commissionBadge", `${orders.total ?? summary.orderCount ?? 0} 单`);
  setText("#commissionPending", cents(pendingAmount));
  setText("#commissionEligible", cents(eligibleAmount));
  setText("#commissionSettled", cents(settledAmount));
  $("#commissionStatusChips").innerHTML = [
    chip("待处理", orders.pending ?? 0),
    chip("可结算", orders.eligible ?? 0),
    chip("已结算", orders.settled ?? 0),
    chip("结算批次", settlements.total ?? 0),
    chip("已支付批次", settlements.paid ?? 0)
  ].join("");

  if (settlementRows.length) {
    $("#commissionAgentRows").innerHTML = settlementRows.slice(0, 8).map(settlement => (
      `<tr>
        <td><b>${esc(settlement.agentId || "-")}</b><span class="row-note">${esc(settlement.settlementNo || settlement.id || "-")} · ${esc(settlement.status || "-")}</span></td>
        <td>${esc(settlement.orderCount ?? 0)}</td>
        <td>${esc(cents(settlement.totalOrderAmount ?? 0))}</td>
        <td>${esc(cents(settlement.totalCommissionAmount ?? 0))}</td>
      </tr>`
    )).join("");
    setText("#commissionBadge", `${settlementRows.length} / ${settlementList.total ?? settlementRows.length} 个结算批次`);
    return;
  }

  $("#commissionAgentRows").innerHTML = topAgents.length ? topAgents.map(agent => (
    `<tr>
      <td>${esc(agent.agentId || "-")}</td>
      <td>${esc(agent.orderCount ?? 0)}</td>
      <td>${esc(cents(agent.orderAmount ?? 0))}</td>
      <td>${esc(cents(agent.commissionAmount ?? 0))}</td>
    </tr>`
  )).join("") : emptyRow(4, "暂无代理佣金排行");
}

function renderOrderModule(summary, commissions, orderRows = [], orderList = {}, jobs = []) {
  const orders = commissions.orders || summary.commissions?.orders || {};
  const points = summary.points || {};
  const exports = summary.exports || {};
  const visibleOrderAmount = orderRows.reduce((total, order) => total + Number(order.amountCents || 0), 0);
  setText("#orderBadge", orderRows.length ? `${orderRows.length} / ${orderList.total ?? orderRows.length} 条明细` : `${exports.total ?? 0} 个导出包`);
  setText("#orderTotal", orderList.total ?? orders.total ?? 0);
  setText("#orderAmount", cents(orderRows.length ? visibleOrderAmount : orders.orderAmount ?? 0));
  setText("#orderPointsNet", signedNumber(points.net ?? 0));

  if (orderRows.length) {
    $("#orderSignalRows").innerHTML = orderRows.slice(0, 8).map(order => (
      `<tr>
        <td><b>${esc(order.orderId || order.id || "-")}</b><span class="row-note">${esc(order.provider || order.userId || "-")}</span></td>
        <td>${esc(orderStatusLabel(order.status))}</td>
        <td>${esc(signedNumber(order.points ?? 0))}</td>
        <td>${esc(cents(order.amountCents ?? 0))}</td>
      </tr>`
    )).join("");
    return;
  }

  const rows = jobs.filter(job => Number(job.pointDelta || 0) !== 0 || Number(job.exportCount || 0) > 0).slice(0, 8);
  $("#orderSignalRows").innerHTML = rows.length ? rows.map(job => (
    `<tr>
      <td>${esc(job.id || "-")}</td>
      <td>${esc(jobStatusLabels[job.status] || job.status || "-")}</td>
      <td>${esc(signedNumber(job.pointDelta ?? 0))}</td>
      <td>${esc(job.exportCount ?? 0)} 包</td>
    </tr>`
  )).join("") : emptyRow(4, "暂无订单明细记录");
}

function renderWithdrawalModule(withdrawalRows = [], withdrawalList = {}) {
  if (withdrawalList?.ok === false) {
    setText("#withdrawalBadge", "读取失败");
    setText("#withdrawalPending", "-");
    setText("#withdrawalAmount", "-");
    setText("#withdrawalPaid", "-");
    $("#withdrawalRows").innerHTML = emptyRow(6, "提现申请列表读取失败");
    return;
  }

  const rows = Array.isArray(withdrawalRows) ? withdrawalRows.slice(0, 8) : [];
  const total = withdrawalList.total ?? rows.length;
  const pending = rows.filter(row => row.status === "pending").length;
  const paid = rows.filter(row => row.status === "paid").length;
  const amount = rows.reduce((sum, row) => sum + Number(row.amountCents || 0), 0);

  setText("#withdrawalBadge", `${rows.length} / ${total} 条明细`);
  setText("#withdrawalPending", pending);
  setText("#withdrawalAmount", cents(amount));
  setText("#withdrawalPaid", paid);

  $("#withdrawalRows").innerHTML = rows.length ? rows.map(row => {
    const status = withdrawalStatusClass(row.status || "pending");
    const balance = row.balanceAvailableCents ?? row.balanceSnapshot?.availableCents ?? 0;
    const locked = row.balanceLockedWithdrawalCents ?? row.balanceSnapshot?.lockedWithdrawalCents ?? 0;
    return (
      `<tr>
        <td><b>${esc(row.agentId || "-")}</b><span class="row-note">${esc(row.id || "-")}</span></td>
        <td><span class="status-pill ${status}">${esc(withdrawalStatusLabels[status] || status)}</span><span class="row-note">${esc(row.statusReason || "-")}</span></td>
        <td>${esc(cents(row.amountCents ?? 0))}</td>
        <td>${esc(cents(balance))}<span class="row-note">锁定 ${esc(cents(locked))}</span></td>
        <td>${esc(row.createdAt || "-")}</td>
        <td>${withdrawalActionPanel(row.id || "", row.status || "")}</td>
      </tr>`
    );
  }).join("") : emptyRow(6, "暂无提现申请");
}

function withdrawalActionPanel(withdrawalId, currentStatus) {
  const disabled = withdrawalId ? "" : " disabled";
  const terminal = ["rejected", "paid", "canceled"].includes(currentStatus);
  const buttons = withdrawalActions.map(action => {
    const isDisabled = disabled || terminal || action.status === currentStatus ? " disabled" : "";
    return (
      `<button
        type="button"
        class="withdrawal-action ${esc(action.status)}"
        data-withdrawal-status="${esc(action.status)}"
        data-withdrawal-id="${esc(withdrawalId)}"${isDisabled}>
        ${esc(action.label)}
      </button>`
    );
  }).join("");
  return `<div class="withdrawal-actions">${buttons}</div>`;
}

async function updateWithdrawalStatus(withdrawalId, status) {
  return postJson(`/api/admin/actions/withdrawals/${encodeURIComponent(withdrawalId)}/status`, {
    status,
    reason: "后台运营台操作"
  });
}

async function handleWithdrawalAction(event) {
  const button = event.target.closest("[data-withdrawal-status]");
  if (!button) return;

  const row = button.closest("tr");
  const withdrawalId = button.dataset.withdrawalId || "";
  const status = button.dataset.withdrawalStatus || "";
  const buttons = Array.from(row?.querySelectorAll("[data-withdrawal-status]") || []);
  if (!withdrawalId || !status) {
    toast("缺少提现申请或目标状态");
    return;
  }

  buttons.forEach(actionButton => {
    actionButton.disabled = true;
  });

  try {
    await updateWithdrawalStatus(withdrawalId, status);
    toast("提现申请状态已更新");
    await loadOps();
  } catch (error) {
    toast(error.message);
  } finally {
    buttons.forEach(actionButton => {
      actionButton.disabled = false;
    });
  }
}

function listItems(list, fallback = []) {
  if (list?.ok === false) return fallback;
  return Array.isArray(list?.items) ? list.items : fallback;
}

function groupChips(prefix, rows) {
  return rows.map(row => chip(`${prefix}：${row.name || "-"}`, row.count ?? 0));
}

function chip(label, value) {
  return `<span class="module-chip">${esc(label)}<b>${esc(value)}</b></span>`;
}

function emptyRow(colspan, message) {
  return `<tr><td class="empty-table-cell" colspan="${colspan}">${esc(message)}</td></tr>`;
}

function qualityLabel(value) {
  if (value === "standard") return "普通";
  if (value === "premium") return "精修";
  return value || "-";
}

function orderStatusLabel(value) {
  const labels = {
    created: "已创建",
    pending: "待支付",
    paid: "已支付",
    closed: "已关闭",
    refunded: "已退款",
    canceled: "已取消",
    failed: "失败"
  };
  return labels[value] || value || "-";
}

function renderCurrentMenu(current) {
  const counts = current.kindCounts || {};
  const rows = current.available ? [
    ["文件", current.file || "-"],
    ["门店", current.store || "-"],
    ["菜品", `${current.count ?? 0}`],
    ["类型", `单品 ${counts.single ?? 0} / 套餐 ${counts.combo ?? 0} / 小食 ${counts.snack ?? 0}`],
    ["Sheet", sheetSummary(current.sheets || [])],
    ["模式", current.demo ? "演示菜单" : "上传菜单"]
  ] : [
    ["状态", "不可用"],
    ["文件", current.file || "-"],
    ["错误", current.error || "-"]
  ];
  $("#currentMenu").innerHTML = rows.map(([label, value]) => (
    `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`
  )).join("");
}

function renderAuditRows(menus) {
  $("#auditRows").innerHTML = menus.length ? menus.map(menu => {
    const counts = menu.kindCounts || {};
    return (
      `<tr>
        <td>${esc(menu.file || "-")}</td>
        <td>${esc(menu.store || "-")}</td>
        <td>${esc(menu.count ?? 0)}</td>
        <td>${esc(counts.single ?? 0)}</td>
        <td>${esc(counts.combo ?? 0)}</td>
        <td>${esc(counts.snack ?? 0)}</td>
        <td>${esc(sheetSummary(menu.sheets || []))}</td>
      </tr>`
    );
  }).join("") : (
    '<tr><td colspan="7">暂无上传菜单审计记录</td></tr>'
  );
}

function renderAuditErrors(errors) {
  $("#auditErrors").innerHTML = errors.length ? errors.map(error => (
    `<div class="error-row"><b>${esc(error.file || "未知文件")}</b>：${esc(error.error || error.message || "")}</div>`
  )).join("") : "";
}

function sheetSummary(sheets) {
  if (!sheets.length) return "-";
  return sheets.map(sheet => `${sheet.sheet || "Sheet"}@${sheet.headerRow || "?"}:${sheet.items || 0}`).join("；");
}

function signedNumber(value) {
  const number = Number(value || 0);
  return number > 0 ? `+${number}` : String(number);
}

function ratio(part, whole) {
  const total = Number(whole || 0);
  if (!total) return 0;
  return Number(part || 0) / total;
}

function percent(value) {
  const number = Number(value || 0);
  const normalized = number > 1 ? number / 100 : number;
  return `${Math.round(normalized * 100)}%`;
}

function cents(value) {
  return `¥${(Number(value || 0) / 100).toFixed(2)}`;
}

async function loadLibrary() {
  try {
    setText("#libraryHint", "读取中");
    renderLibrary(await api("/api/admin/library-sample?limit=18"));
  } catch (error) {
    setText("#libraryHint", "读取失败");
    toast(error.message);
  }
}

async function loadAudit() {
  try {
    setText("#auditHint", "读取中");
    renderAudit(await api("/api/admin/menu-audit"));
  } catch (error) {
    setText("#auditHint", "读取失败");
    toast(error.message);
  }
}

async function loadOps() {
  try {
    setText("#opsHint", "读取中");
    const [dashboard, lists] = await Promise.all([
      api("/api/admin/dashboard"),
      loadAdminLists()
    ]);
    renderOps(dashboard, lists);
    if (lists.errors?.length) {
      toast(`列表接口读取失败：${lists.errors.join("、")}`);
    }
  } catch (error) {
    setText("#opsHint", "读取失败");
    toast(error.message);
  }
}

async function loadOpsReadiness() {
  setText("#opsReadinessHint", "读取中");
  const [readinessResult, queueResult] = await Promise.allSettled([
    api("/api/ops/readiness"),
    api("/api/admin/queue-snapshot")
  ]);
  const readiness = readinessResult.status === "fulfilled" ? readinessResult.value : {};
  const queueSnapshot = queueResult.status === "fulfilled" ? queueResult.value.queue : null;

  renderOpsReadiness(readiness, queueSnapshot);

  if (readinessResult.status === "rejected") {
    setText("#opsReadinessHint", "readiness 读取失败");
    toast(readinessResult.reason.message);
  } else if (queueResult.status === "rejected") {
    toast("队列快照读取失败，已使用 readiness 队列状态");
  }
}

async function loadAiAssets() {
  try {
    setText("#aiAssetHint", "读取中");
    renderAiAssets(await api("/api/admin/ai-assets"));
  } catch (error) {
    setText("#aiAssetHint", "读取失败");
    toast(error.message);
  }
}

$("#refreshLibrary").addEventListener("click", loadLibrary);
$("#refreshAudit").addEventListener("click", loadAudit);
$("#refreshOps").addEventListener("click", loadOps);
$("#refreshReadiness").addEventListener("click", loadOpsReadiness);
$("#refreshAiAssets").addEventListener("click", loadAiAssets);
$("#aiAssetRows").addEventListener("click", handleAiAssetAction);
$("#withdrawalRows").addEventListener("click", handleWithdrawalAction);

loadOpsReadiness();
loadOps();
loadAiAssets();
loadLibrary();
loadAudit();
