from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CustomerUiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        self.script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.styles = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
        self.admin_template = (ROOT / "templates" / "admin.html").read_text(encoding="utf-8")
        self.admin_script = (ROOT / "static" / "admin.js").read_text(encoding="utf-8")

    def test_top_workflow_has_four_large_round_step_buttons(self) -> None:
        self.assertEqual(self.template.count('class="round-step'), 4)
        for required in ["上传菜单", "选择风格/样图", "正式出图", "导出图片"]:
            self.assertIn(required, self.template)
        self.assertIn('id="formalShortcutBtn"', self.template)

    def test_customer_pricing_copy_stays_at_product_decision(self) -> None:
        customer_copy = "\n".join([self.template, self.script])
        self.assertIn('standard: { name: "普通出图", points: 10 }', self.script)
        self.assertIn('premium: { name: "精修出图", points: 20 }', self.script)
        self.assertIn("普通出图 · 10积分/张", customer_copy)
        self.assertIn("精修出图", customer_copy)
        self.assertIn("20积分/张", customer_copy)
        self.assertNotIn("100积分/张", customer_copy)
        self.assertNotIn("200积分/张", customer_copy)

    def test_customer_copy_does_not_expose_gallery_shortcut_language(self) -> None:
        customer_copy = "\n".join([self.template, self.script])
        forbidden = ["已接入真实图库", "当前可用", "正式图库未接入", "真实图库", "免费样图预览"]
        for text in forbidden:
            self.assertNotIn(text, customer_copy)

    def test_customer_image_cards_render_full_cover_images(self) -> None:
        image_wrap_rule = re.search(r"\.image-wrap\s*>\s*img\s*\{(?P<body>[^}]*)\}", self.styles)
        preview_rule = re.search(r"\.preview-sample\s+img\s*\{(?P<body>[^}]*)\}", self.styles)

        self.assertIsNotNone(image_wrap_rule)
        self.assertIsNotNone(preview_rule)
        self.assertIn("height: 100%", image_wrap_rule.group("body"))
        self.assertIn("object-fit: cover", image_wrap_rule.group("body"))
        self.assertIn("object-fit: cover", preview_rule.group("body"))
        self.assertNotRegex(self.styles, r"\.image-wrap[^{]*\{[^}]*filter\s*:\s*blur")
        self.assertNotIn("blurred", self.script)

    def test_customer_ui_never_replaces_failed_generation_with_color_block_art(self) -> None:
        self.assertNotIn("styleFallbackImage", self.script)
        self.assertNotIn("data:image/svg+xml", self.script)
        self.assertIn("this.hidden=true", self.script)

    def test_style_selection_requires_explicit_sample_generation(self) -> None:
        style_handler_start = self.script.index('$$(".style").forEach')
        style_handler_end = self.script.index("function renderStylePreview", style_handler_start)
        style_handler = self.script[style_handler_start:style_handler_end]
        sample_button_start = self.script.index('$("#generateSamplesBtn").onclick')
        sample_button_end = self.script.index('$("#formalShortcutBtn").onclick', sample_button_start)
        sample_button_handler = self.script[sample_button_start:sample_button_end]
        load_preview_start = self.script.index("async function loadStylePreview")
        load_preview_end = self.script.index("async function uploadMenu", load_preview_start)
        load_preview_body = self.script[load_preview_start:load_preview_end]

        self.assertNotIn("loadStylePreview", style_handler)
        self.assertNotIn("/api/style-preview", style_handler)
        self.assertNotIn("generate=1", style_handler)
        self.assertIn('$("#generateSamplesBtn").onclick', self.script)
        self.assertIn("loadStylePreview(state.pendingStyle)", sample_button_handler)
        self.assertNotIn("generate=1", load_preview_body)
        self.assertEqual(self.script.count("/api/style-preview?"), 1)
        self.assertEqual(self.script.count("/api/style-background?"), 1)
        self.assertEqual(self.script.count("/api/background-catalog?"), 1)
        self.assertEqual(self.script.count("/api/style-preview-sample?"), 1)
        self.assertIn("await loadStylePreviewSample(styleId, index)", load_preview_body)
        self.assertIn("PREVIEW_SAMPLE_MAX_ATTEMPTS", self.script)
        self.assertIn("TRANSIENT_HTTP_STATUSES.has(res.status)", self.script)
        self.assertIn('typeof data.retryable === "boolean"', self.script)
        self.assertIn("networkFailure || error?.retryable === true", self.script)
        self.assertIn("state.previewRequestedStyle !== styleId", self.script)
        self.assertIn("state.stylePreview.samples[index] =", self.script)

    def test_selected_background_identity_is_sent_through_every_generation_stage(self) -> None:
        for required in [
            "function backgroundIdentityForStyle",
            "function selectedBackgroundIdentity",
            "function backgroundQuery",
            "function selectedBackgroundPayload",
            "backgroundAssetId",
            "backgroundSha256",
            "selectedBackground?.assetId",
            "selectedBackground?.sha256",
        ]:
            self.assertIn(required, self.script)

        self.assertIn("/api/style-preview?", self.script)
        self.assertIn("/api/style-preview-sample?", self.script)
        self.assertIn('api("/api/generation-jobs"', self.script)
        self.assertGreaterEqual(self.script.count("...selectedBackgroundPayload("), 2)
        self.assertIn("selectedBackground?.assetId", self.script)
        self.assertIn("selectedBackground?.sha256", self.script)

    def test_menu_upload_identity_is_retained_for_generation_and_export(self) -> None:
        self.assertIn('menuUploadId: ""', self.script)
        self.assertIn(
            "state.menuUploadId = data.menuUploadId || \"\"",
            self.script,
        )
        self.assertGreaterEqual(
            self.script.count("menuUploadId: state.menuUploadId"),
            3,
        )
        self.assertNotIn('refundPoints(debitOrderId', self.script)

    def test_formal_generation_uses_async_jobs_with_timeout_status(self) -> None:
        confirm_start = self.script.index("async function confirmStyle")
        confirm_end = self.script.index("function chooseRows", confirm_start)
        confirm_body = self.script[confirm_start:confirm_end]

        self.assertIn('api("/api/generation-jobs"', self.script)
        self.assertIn("fetchGenerationJob", self.script)
        self.assertIn("cancelGenerationJob", self.script)
        self.assertIn("requestGenerationCancel", self.script)
        self.assertIn("sleepForGenerationPoll", self.script)
        self.assertIn('id="cancelGenerationBtn"', self.script)
        self.assertIn("/cancel", self.script)
        self.assertIn("waitForGenerationJob", self.script)
        self.assertIn("generationJobFailureReason", self.script)
        self.assertIn("updateGenerationJobProgress", self.script)
        self.assertIn("timedOut", self.script)
        self.assertIn("stale", self.script)
        self.assertIn("elapsedSeconds", self.script)
        self.assertIn("正式图仍在排队", self.script)
        self.assertIn("正式图仍在生成", self.script)
        self.assertIn("正式图生成超时", self.script)
        self.assertIn("正式图任务已取消", self.script)
        self.assertIn("createGenerationJob", confirm_body)
        self.assertIn("state.generationJob", confirm_body)
        self.assertIn("waitForGenerationJob", confirm_body)
        self.assertNotIn('api("/api/generate-final"', self.script)

    def test_customer_image_refinement_uses_bounded_real_async_jobs(self) -> None:
        payload_start = self.script.index("function imageRefinementRequestPayload")
        payload_end = self.script.index("async function createImageRefinement", payload_start)
        payload_body = self.script[payload_start:payload_end]
        poll_start = self.script.index("async function waitForImageRefinement")
        poll_end = self.script.index("function refinementSourceForRow", poll_start)
        poll_body = self.script[poll_start:poll_end]

        for field in [
            "parentGenerationJobId",
            "sourceAssetId",
            "sourceRevisionJobId",
            "mode",
            "prompt",
            "idempotencyKey",
        ]:
            self.assertIn(field, payload_body)
        for forbidden in ["points", "quality", "userId", "objectKey"]:
            self.assertNotIn(forbidden, payload_body)

        self.assertIn('api("/api/image-refinements"', self.script)
        self.assertIn("fetchImageRefinement", poll_body)
        self.assertIn("REFINEMENT_MAX_POLL_ATTEMPTS", poll_body)
        self.assertIn("REFINEMENT_POLL_DELAY_MS", poll_body)
        self.assertIn(
            "attempt < REFINEMENT_MAX_POLL_ATTEMPTS",
            poll_body,
        )
        self.assertIn('job?.status === "completed"', poll_body)
        self.assertIn("refinementTerminalStatuses.has", poll_body)
        self.assertIn("等待超时", poll_body)
        self.assertNotIn("while (true)", poll_body)

    def test_rework_and_refine_replace_candidate_only_after_server_completion(self) -> None:
        redraw_start = self.script.index("async function redrawImage")
        redraw_end = self.script.index("async function exportImages", redraw_start)
        redraw_body = self.script[redraw_start:redraw_end]
        submit_start = self.script.index("async function submitRefine")
        submit_end = self.script.index("async function refreshAccount", submit_start)
        submit_body = self.script[submit_start:submit_end]
        apply_start = self.script.index("function applyCompletedImageRefinement")
        apply_end = self.script.index("async function redrawImage", apply_start)
        apply_body = self.script[apply_start:apply_end]

        for body, mode in [(redraw_body, '"rework"'), (submit_body, '"refine"')]:
            self.assertIn("refinementSourceForRow", body)
            self.assertIn("createImageRefinement", body)
            self.assertIn("waitForImageRefinement", body)
            self.assertIn("applyCompletedImageRefinement", body)
            self.assertIn("source.parentGenerationJobId", body)
            self.assertIn("source.sourceAssetId", body)
            self.assertIn(mode, body)
            self.assertNotIn("debitPoints", body)
            self.assertNotIn("/api/debit", body)

        self.assertNotIn("state.freeReworkRemaining -=", redraw_body)
        self.assertIn('job?.status !== "completed"', apply_body)
        self.assertIn("candidate.url = image.url", apply_body)
        self.assertIn("candidate.deliveryAssetId = sourceAssetId", apply_body)
        self.assertIn("candidate.revisionJobId = job.jobId", apply_body)
        self.assertIn("candidate.revisionAssetId = image.assetId", apply_body)
        self.assertIn("candidate.revisionSha256 = image.sha256", apply_body)
        self.assertIn("freeReworkQuotaVerified", apply_body)
        self.assertIn("state.freeReworkRemaining = Math.max", apply_body)
        self.assertIn("applyAccount(job)", apply_body)
        self.assertIn("renderPlan(true)", apply_body)
        self.assertGreater(
            submit_body.index("closeRefine()"),
            submit_body.index("applyCompletedImageRefinement"),
        )

    def test_legacy_generation_results_fail_closed_before_refinement(self) -> None:
        source_start = self.script.index("function refinementSourceForRow")
        source_end = self.script.index("function applyCompletedImageRefinement", source_start)
        source_body = self.script[source_start:source_end]

        self.assertIn("state.completedGenerationJobId", source_body)
        self.assertIn("source?.deliveryAssetId", source_body)
        self.assertIn("source.revisionAssetId || source.deliveryAssetId", source_body)
        self.assertIn("source.revisionJobId ||", source_body)
        self.assertIn("请重新正式出图后再修改", source_body)

    def test_refinement_lineage_and_export_use_latest_completed_revision(self) -> None:
        source_start = self.script.index("function refinementSourceForRow")
        source_end = self.script.index("function applyCompletedImageRefinement", source_start)
        source_body = self.script[source_start:source_end]
        export_start = self.script.index("function revisionJobIdsForExport")
        export_end = self.script.index("function openRefine", export_start)
        export_body = self.script[export_start:export_end]

        self.assertIn("revisionAssetId || source.deliveryAssetId", source_body)
        self.assertIn("sourceRevisionJobId", source_body)
        self.assertIn("revisionJobId", source_body)
        self.assertIn("revisionJobIdsForExport", export_body)
        self.assertIn("revisionJobIds:", export_body)
        self.assertIn("revisionJobId", export_body)

    def test_customer_auth_ui_uses_phone_otp_session_apis(self) -> None:
        customer_copy = "\n".join([self.template, self.script])

        for required in [
            'id="authWidget"',
            'id="authPanel"',
            'id="authPhoneInput"',
            'id="authCodeInput"',
            'id="requestOtpBtn"',
            'id="verifyOtpBtn"',
            'id="authStatus"',
            'id="authPhone"',
            'id="logoutBtn"',
        ]:
            self.assertIn(required, self.template)

        for required in [
            "/api/auth/request-otp",
            "/api/auth/verify-otp",
            "/api/auth/session",
            "/api/auth/logout",
            "requestAuthOtp",
            "verifyAuthOtp",
            "loadAuthSession",
            "logoutAuth",
            "localStorage",
            "Authorization",
            "Bearer",
        ]:
            self.assertIn(required, self.script)

        self.assertNotIn("登录系统接口已预留", customer_copy)
        self.assertNotIn("下一步接手机号/微信登录", customer_copy)

    def test_customer_business_api_defaults_to_session_bearer_token(self) -> None:
        api_start = self.script.index("function isPublicAuthApi")
        api_end = self.script.index("function readStoredAuthToken", api_start)
        api_helpers = self.script[api_start:api_end]

        for required in [
            "function shouldAttachDefaultAuth",
            "function withDefaultAuthOptions",
            "new URL(url, window.location.href)",
            "target.origin === window.location.origin",
            'target.pathname.startsWith("/api/")',
            "!isPublicAuthApi(url)",
            '"/api/auth/request-otp"',
            '"/api/auth/verify-otp"',
            "state.auth.token",
            "new Headers(opt.headers || {})",
            'headers.has("Authorization")',
            'headers.set("Authorization", `Bearer ${token}`)',
            "return { ...opt, headers }",
            "fetch(url, withDefaultAuthOptions(url, opt))",
        ]:
            self.assertIn(required, api_helpers)

        request_otp_start = self.script.index("async function requestAuthOtp")
        request_otp_end = self.script.index("async function verifyAuthOtp", request_otp_start)
        request_otp_body = self.script[request_otp_start:request_otp_end]
        verify_otp_start = self.script.index("async function verifyAuthOtp")
        verify_otp_end = self.script.index("async function logoutAuth", verify_otp_start)
        verify_otp_body = self.script[verify_otp_start:verify_otp_end]

        self.assertIn('authJsonOptions({ phone })', request_otp_body)
        self.assertIn('authJsonOptions({ challengeId: state.auth.challengeId, code })', verify_otp_body)
        self.assertNotIn("authHeaders", request_otp_body)
        self.assertNotIn("authHeaders", verify_otp_body)

    def test_private_media_urls_are_materialized_recursively_at_api_boundary(self) -> None:
        helper_start = self.script.index("function privateMediaTarget")
        helper_end = self.script.index("async function api", helper_start)
        helpers = self.script[helper_start:helper_end]
        api_start = self.script.index("async function api")
        api_end = self.script.index("async function downloadProtectedFile", api_start)
        api_body = self.script[api_start:api_end]

        for required in [
            'const privateMediaObjectUrlCache = new Map()',
            'typeof value !== "string"',
            "new URL(value, window.location.href)",
            "target.origin === window.location.origin",
            'target.pathname.startsWith("/api/private-media/")',
            "async function materializePrivateMediaUrls",
            "Array.isArray(value)",
            "Promise.all(value.map",
            "Object.entries(value)",
            "Object.fromEntries(entries)",
            "return value",
        ]:
            self.assertIn(required, self.script if required.startswith("const ") else helpers)

        self.assertIn("return await materializePrivateMediaUrls(data)", api_body)
        self.assertNotIn('pathname.startsWith("/download/")', helpers)

    def test_private_media_fetch_supports_bearer_or_same_origin_staging_auth(self) -> None:
        fetch_start = self.script.index("function privateMediaObjectUrl")
        fetch_end = self.script.index("async function materializePrivateMediaUrls", fetch_start)
        fetch_body = self.script[fetch_start:fetch_end]

        for required in [
            "privateMediaObjectUrlCache.get(signedUrl)",
            "privateMediaObjectUrlCache.set(signedUrl, pending)",
            "privateMediaObjectUrlCache.set(signedUrl, objectUrl)",
            "privateMediaObjectUrlCache.delete(signedUrl)",
            "state.auth.token",
            'if (token) headers.set("Authorization", `Bearer ${token}`)',
            'credentials: "same-origin"',
            "response.ok",
            'response.headers.get("Content-Type")',
            'startsWith("image/")',
            "response.blob()",
            "URL.createObjectURL(blob)",
            'error.code = "private_media_url_invalid"',
            'wrapped.code = "private_media_network_error"',
            "私有图片加载失败",
            "私有图片响应格式错误",
        ]:
            self.assertIn(required, fetch_body)

        self.assertNotIn("return signedUrl", fetch_body)
        self.assertNotIn("if (!token)", fetch_body)
        self.assertIn("clearPrivateMediaObjectUrlCache()", self.script)
        self.assertIn("URL.revokeObjectURL(objectUrl)", self.script)

    def test_background_media_failures_are_not_reported_as_hunyuan_failures(self) -> None:
        failure_start = self.script.index("function styleGenerationFailureText")
        failure_end = self.script.index("function allStyleBackgroundsBlocked", failure_start)
        failure_body = self.script[failure_start:failure_end]
        loader_start = self.script.index("async function loadStyleBackground")
        loader_end = self.script.index("async function loadStyleBackgrounds", loader_start)
        loader_body = self.script[loader_start:loader_end]

        self.assertIn('errorCode.startsWith("private_media_")', failure_body)
        self.assertIn('return "背景图片加载失败"', failure_body)
        self.assertIn('mediaLoadError ? "MediaLoadError" : "ProviderError"', loader_body)
        self.assertIn("generationErrorCode: errorCode", loader_body)

    def test_admin_has_productized_dashboard_containers(self) -> None:
        for required in [
            'id="taskModule"',
            'id="riskEventModule"',
            'id="assetAuditModule"',
            'id="commissionModule"',
            'id="withdrawalModule"',
            'id="orderModule"',
            'data-ui-contract="task-container"',
            'data-ui-contract="risk-events-container"',
            'data-ui-contract="asset-audit-container"',
            'data-ui-contract="commission-container"',
            'data-ui-contract="withdrawal-container"',
            'data-ui-contract="order-container"',
        ]:
            self.assertIn(required, self.admin_template)

        for required in [
            "renderTaskModule",
            "renderRiskEventModule",
            "renderAssetAuditModule",
            "renderCommissionModule",
            "renderWithdrawalModule",
            "renderOrderModule",
            'api("/api/admin/dashboard")',
        ]:
            self.assertIn(required, self.admin_script)

    def test_admin_frontend_uses_product_list_apis(self) -> None:
        list_loader_start = self.admin_script.index("async function loadAdminLists")
        list_loader_end = self.admin_script.index("function renderOps", list_loader_start)
        list_loader = self.admin_script[list_loader_start:list_loader_end]

        self.assertIn("/api/admin/lists/", self.admin_script)
        self.assertIn("Promise.allSettled", list_loader)
        self.assertIn("limit: 8", list_loader)
        self.assertIn('sort: "createdAt"', list_loader)
        for resource in [
            "generation-tasks",
            "asset-access",
            "risk-events",
            "commission-settlements",
            "withdrawals",
            "orders",
        ]:
            self.assertIn(resource, list_loader)

    def test_admin_ops_readiness_panel_calls_readiness_api(self) -> None:
        for required in [
            'id="opsReadinessPanel"',
            'id="opsReadinessCards"',
            'id="opsReadinessHint"',
            'data-ui-contract="ops-readiness-status"',
            "运维状态",
        ]:
            self.assertIn(required, self.admin_template)

        for required in [
            "loadOpsReadiness",
            "renderOpsReadiness",
            'api("/api/ops/readiness")',
            'api("/api/admin/queue-snapshot")',
            "objectStorage",
            "generationProvider",
            "generationQueue",
            "payments",
            "AI 生图 provider",
            "未接入",
        ]:
            self.assertIn(required, self.admin_script)

    def test_admin_risk_events_ui_uses_list_api_fields(self) -> None:
        for required in [
            'id="riskEventRows"',
            'id="riskEventBadge"',
            'id="riskEventTotal"',
            'id="riskEventReview"',
            'id="riskEventDenied"',
            'id="riskEventChips"',
            "<th>决策</th>",
            "<th>风险等级</th>",
            "<th>事件</th>",
            "<th>用户</th>",
            "<th>拒绝原因</th>",
            "<th>时间</th>",
        ]:
            self.assertIn(required, self.admin_template)

        risk_renderer_start = self.admin_script.index("function renderRiskEventModule")
        risk_renderer_end = self.admin_script.index("function renderAssetAuditModule", risk_renderer_start)
        risk_renderer = self.admin_script[risk_renderer_start:risk_renderer_end]

        for required in [
            "risk-events",
            "listItems(lists.riskEvents)",
            "risk.recent",
            "row.decision",
            "row.riskLevel",
            "row.eventType",
            "row.userId",
            "row.denyReason",
            "row.createdAt",
            'emptyRow(6, "暂无风险事件明细")',
        ]:
            self.assertIn(required, self.admin_script if required in {"risk-events", "listItems(lists.riskEvents)"} else risk_renderer)

    def test_admin_ai_asset_review_ui_posts_status_actions(self) -> None:
        for required in [
            'data-ui-contract="ai-asset-review-table"',
            "<th>审核</th>",
        ]:
            self.assertIn(required, self.admin_template)

        for required in [
            "/api/admin/actions/ai-assets/",
            "/status",
            "updateAiAssetStatus",
            "handleAiAssetAction",
            "postJson",
            "qualityNote",
            "data-ai-asset-note",
            "data-ai-asset-action",
            "data-ai-asset-status",
            "loadAiAssets();",
            'action: "approve"',
            'action: "reject"',
            'action: "disable"',
            'status: "approved"',
            'status: "rejected"',
            'status: "disabled"',
            'emptyRow(9, "暂无 AI 资产")',
        ]:
            self.assertIn(required, self.admin_script)

    def test_admin_withdrawal_ui_uses_list_api_and_posts_status_actions(self) -> None:
        for required in [
            'id="withdrawalRows"',
            'id="withdrawalBadge"',
            'id="withdrawalPending"',
            'id="withdrawalAmount"',
            'id="withdrawalPaid"',
            "<th>代理</th>",
            "<th>状态</th>",
            "<th>金额</th>",
            "<th>余额</th>",
            "<th>申请时间</th>",
            "<th>审批</th>",
        ]:
            self.assertIn(required, self.admin_template)

        withdrawal_renderer_start = self.admin_script.index("function renderWithdrawalModule")
        withdrawal_renderer_end = self.admin_script.index("function listItems", withdrawal_renderer_start)
        withdrawal_renderer = self.admin_script[withdrawal_renderer_start:withdrawal_renderer_end]

        global_required = {
            "withdrawals",
            "listItems(lists.withdrawals)",
            'status: "approved"',
            'status: "rejected"',
            'status: "paid"',
            'status: "canceled"',
        }
        for required in [
            "withdrawals",
            "listItems(lists.withdrawals)",
            "row.agentId",
            "row.status",
            "row.amountCents",
            "row.balanceAvailableCents",
            "row.createdAt",
            "/api/admin/actions/withdrawals/",
            "updateWithdrawalStatus",
            "handleWithdrawalAction",
            "data-withdrawal-status",
            "data-withdrawal-id",
            'status: "approved"',
            'status: "rejected"',
            'status: "paid"',
            'status: "canceled"',
            'emptyRow(6, "暂无提现申请")',
            'emptyRow(6, "提现申请列表读取失败")',
        ]:
            self.assertIn(required, self.admin_script if required in global_required else withdrawal_renderer)


if __name__ == "__main__":
    unittest.main()
