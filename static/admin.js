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

function sourceClass(source) {
  return ["clean", "watermark", "internal"].includes(source) ? source : "external";
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

function renderLibrary(data) {
  const summary = data.summary || {};
  const cleaning = data.cleaningSummary || summary.cleaning || {};
  const sources = data.sources || {};
  setText("#metricTotal", summary.total ?? 0);
  setText("#metricClean", sources.clean ?? 0);
  setText("#metricWatermark", sources.watermark ?? 0);
  setText("#metricInternal", sources.internal ?? 0);
  setText("#metricReusable", cleaning.reusable ?? summary.reusable ?? 0);
  setText("#metricWatermarkRisk", cleaning.watermarkRisk ?? 0);
  setText("#metricNeedsReview", cleaning.needsReview ?? 0);
  setText("#metricLowQuality", cleaning.lowQuality ?? 0);
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
  const reviewReasons = sample.reviewReasons || [];
  const state = reviewReasons.length || sample.hasBrandWatermark || sample.hasDishText
    ? "需审核"
    : sample.reusable ? "可复用" : "仅参考";
  const quality = sample.qualityScore == null ? "" : `<span>质量 ${esc(sample.qualityScore)}</span>`;
  const image = sample.url
    ? `<img src="${esc(sample.url)}" alt="${esc(sample.dishName || "样例图片")}" loading="lazy">`
    : '<span>无图片</span>';
  return (
    `<article class="sample-card">
      <figure>${image}</figure>
      <figcaption>
        <div class="sample-meta">
          <span class="pill ${sourceClass(source)}">${esc(sourceLabels[source] || source)}</span>
          <span>${esc(state)}</span>
        </div>
        <b title="${esc(sample.dishName)}">${esc(sample.dishName || "未命名菜品")}</b>
        <span title="${esc(sample.store)}">${esc(sample.store || "未知门店")}</span>
        ${quality}
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

$("#refreshLibrary").addEventListener("click", loadLibrary);
$("#refreshAudit").addEventListener("click", loadAudit);

loadLibrary();
loadAudit();
