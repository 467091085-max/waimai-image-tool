# DEPLOYMENT_DIAGNOSIS

Generated at: `2026-06-29T05:40:45+08:00`

Scope: diagnostic only. No application code was changed.

Current worktree:

```text
/Users/guiguixiaxia/.codex/worktrees/de51/waimai-image-tool
```

Render test site:

```text
https://waimai-image-tool-1.onrender.com
```

## A. Project Structure

Directory tree, max depth 4, excluding nested `.git` internals, `.pytest_cache` internals, and `__pycache__`:

```text
.
./.git
./.gitignore
./.pytest_cache
./.python-version
./AI-Project
./AI-Project/context
./AI-Project/context/codex-long-task-skill.md
./AI-Project/context/system.md
./AI-Project/decisions
./AI-Project/decisions/decisions.md
./AI-Project/logs
./AI-Project/logs/log-2026-06-29.md
./AI-Project/plan
./AI-Project/plan/roadmap.md
./AI-Project/plan/steps.md
./AI-Project/state
./AI-Project/state/current.md
./MODULE_STATUS.md
./PRODUCTIZATION_PLAN.md
./Procfile
./README.md
./WORKTREE_PLAN.md
./admin_actions.py
./admin_data.py
./admin_panel.py
./ai_asset_repository.py
./app.py
./asset_security.py
./auth_rules.py
./auth_service.py
./billing.py
./commission_settlement_service.py
./data
./data/app.db
./data/exports
./data/library
./data/library/.demo_ready
./data/library/.gitkeep
./data/library/_ai_asset_library
./data/library/_style_backgrounds
./data/library/_style_backgrounds/d6a529a8482e
./data/library/demo_store
./data/library/demo_store/style-1
./data/library/demo_store/style-2
./data/library/demo_store/style-3
./data/library/demo_store/style-4
./data/library/demo_store/style-5
./data/library/demo_store/style-6
./data/library/seed_518fac_龙师傅·现炒盖码饭(城西店)
./data/library/seed_518fac_龙师傅·现炒盖码饭(城西店)/style-1
./data/library/seed_518fac_龙师傅·现炒盖码饭(城西店)/style-2
./data/library/seed_518fac_龙师傅·现炒盖码饭(城西店)/style-3
./data/library/seed_518fac_龙师傅·现炒盖码饭(城西店)/style-4
./data/library/seed_518fac_龙师傅·现炒盖码饭(城西店)/style-5
./data/library/seed_518fac_龙师傅·现炒盖码饭(城西店)/style-6
./data/library/seed_603466_同福楼川湘菜
./data/library/seed_603466_同福楼川湘菜/style-1
./data/library/seed_603466_同福楼川湘菜/style-2
./data/library/seed_603466_同福楼川湘菜/style-3
./data/library/seed_603466_同福楼川湘菜/style-4
./data/library/seed_603466_同福楼川湘菜/style-5
./data/library/seed_603466_同福楼川湘菜/style-6
./data/library/seed_9cfa4b_彭城叁萬把子肉卤肉饭盖浇饭(梁家巷店)
./data/library/seed_9cfa4b_彭城叁萬把子肉卤肉饭盖浇饭(梁家巷店)/style-1
./data/library/seed_9cfa4b_彭城叁萬把子肉卤肉饭盖浇饭(梁家巷店)/style-2
./data/library/seed_9cfa4b_彭城叁萬把子肉卤肉饭盖浇饭(梁家巷店)/style-3
./data/library/seed_9cfa4b_彭城叁萬把子肉卤肉饭盖浇饭(梁家巷店)/style-4
./data/library/seed_9cfa4b_彭城叁萬把子肉卤肉饭盖浇饭(梁家巷店)/style-5
./data/library/seed_9cfa4b_彭城叁萬把子肉卤肉饭盖浇饭(梁家巷店)/style-6
./data/library/seed_a53c53_桂小柒粉馆(四惠东店)
./data/library/seed_a53c53_桂小柒粉馆(四惠东店)/style-1
./data/library/seed_a53c53_桂小柒粉馆(四惠东店)/style-3
./data/library/seed_a53c53_桂小柒粉馆(四惠东店)/style-4
./data/library/seed_a53c53_桂小柒粉馆(四惠东店)/style-5
./data/library/seed_a53c53_桂小柒粉馆(四惠东店)/style-6
./data/library/seed_a7a272_格格佳
./data/library/seed_a7a272_格格佳/style-1
./data/library/seed_a7a272_格格佳/style-2
./data/library/seed_a7a272_格格佳/style-3
./data/library/seed_a7a272_格格佳/style-4
./data/library/seed_a7a272_格格佳/style-5
./data/library/seed_a7a272_格格佳/style-6
./data/library/seed_bb51b9_李朱雀川小馆2
./data/library/seed_bb51b9_李朱雀川小馆2/style-2
./data/library/seed_bb51b9_李朱雀川小馆2/style-3
./data/library/seed_bb51b9_李朱雀川小馆2/style-4
./data/library/seed_bb51b9_李朱雀川小馆2/style-5
./data/library/seed_ca3176_蔬适圈·中式轻食健康餐(万达店)
./data/library/seed_ca3176_蔬适圈·中式轻食健康餐(万达店)/style-2
./data/library/seed_ca3176_蔬适圈·中式轻食健康餐(万达店)/style-3
./data/library/seed_ca3176_蔬适圈·中式轻食健康餐(万达店)/style-5
./data/library/seed_ca3176_蔬适圈·中式轻食健康餐(万达店)/style-6
./data/library/seed_d1dcf6_李朱雀川小馆
./data/library/seed_d1dcf6_李朱雀川小馆/style-1
./data/library/seed_d1dcf6_李朱雀川小馆/style-2
./data/library/seed_d1dcf6_李朱雀川小馆/style-3
./data/library/seed_d1dcf6_李朱雀川小馆/style-4
./data/library/seed_d1dcf6_李朱雀川小馆/style-5
./data/library/seed_d1dcf6_李朱雀川小馆/style-6
./data/library/seed_fbd580_吉庆门鲜饺铺(上坊店)
./data/library/seed_fbd580_吉庆门鲜饺铺(上坊店)/style-1
./data/library/seed_fbd580_吉庆门鲜饺铺(上坊店)/style-2
./data/library/seed_fbd580_吉庆门鲜饺铺(上坊店)/style-3
./data/library/seed_fbd580_吉庆门鲜饺铺(上坊店)/style-4
./data/library/seed_fbd580_吉庆门鲜饺铺(上坊店)/style-5
./data/library/seed_fbd580_吉庆门鲜饺铺(上坊店)/style-6
./data/library/uploaded_1782681947_777897000
./data/library/uploaded_1782681947_777897000/style-upload
./data/library/uploaded_1782682358_166450000
./data/library/uploaded_1782682358_166450000/style-upload
./data/library_index
./data/library_index/.gitkeep
./data/model_inputs
./data/model_inputs/.gitkeep
./data/object_store
./data/object_store/.gitkeep
./data/object_store/generated
./data/object_store/generated/audit-smoke
./data/storage.sqlite3
./data/uploads
./data/uploads/menu_1782584622_运营数据_JOYFUL·欢喜轻食·融合料理健康餐(总店).xlsx
./data/uploads/menu_1782592415_谨食·沙拉轻食活动及菜单方案.xls
./data/uploads/menu_1782670754_运营数据_桂小柒粉馆(四惠东店).xlsx
./data/uploads/menu_1782677175_menu.xlsx
./data/uploads/menu_1782677208_menu.xlsx
./data/uploads/menu_1782677491_menu.xlsx
./data/uploads/menu_1782677534_menu.xlsx
./data/uploads/menu_1782677976_menu.xlsx
./data/uploads/menu_1782678551_menu.xlsx
./data/uploads/menu_1782678576_menu.xlsx
./data/uploads/menu_1782678970_menu.xlsx
./data/uploads/menu_1782678999_menu.xlsx
./data/uploads/menu_1782679408_menu.xlsx
./data/uploads/menu_1782679830_menu.xlsx
./data/uploads/menu_1782680379_menu.xlsx
./data/uploads/menu_1782680864_menu.xlsx
./data/uploads/menu_1782681441_menu.xlsx
./data/uploads/menu_1782681947_menu.xlsx
./data/uploads/menu_1782682358_menu.xlsx
./download_guard.py
./generation_queue.py
./growth_rules.py
./growth_service.py
./image_pipeline.py
./job_rules.py
./library_index.py
./matching_engine.py
./menu_parser.py
./object_storage_service.py
./payment_rules.py
./payment_service.py
./platform_rules.py
./render.yaml
./requirements.txt
./risk_rules.py
./scripts
./scripts/import_seed_library.py
./scripts/scan_library.py
./sms_service.py
./static
./static/admin.css
./static/admin.js
./static/app.js
./static/styles.css
./storage_db.py
./templates
./templates/admin.html
./templates/index.html
./test_matching_engine.py
./tests
./tests/test_admin_actions.py
./tests/test_admin_data.py
./tests/test_admin_panel.py
./tests/test_ai_asset_repository.py
./tests/test_app_generation.py
./tests/test_asset_security.py
./tests/test_auth_rules.py
./tests/test_auth_service.py
./tests/test_billing.py
./tests/test_commission_settlement_api.py
./tests/test_commission_settlement_service.py
./tests/test_customer_ui_contract.py
./tests/test_download_guard.py
./tests/test_download_route.py
./tests/test_generation_queue.py
./tests/test_generation_queue_integration.py
./tests/test_growth_api_integration.py
./tests/test_growth_rules.py
./tests/test_growth_service.py
./tests/test_image_pipeline.py
./tests/test_job_rules.py
./tests/test_library_index.py
./tests/test_menu_parser.py
./tests/test_object_storage_service.py
./tests/test_payment_rules.py
./tests/test_payment_service.py
./tests/test_platform_rules.py
./tests/test_product_api_integration.py
./tests/test_risk_rules.py
./tests/test_security_regressions.py
./tests/test_sms_service.py
./tests/test_storage_db.py
./tests/test_strict_matching.py
./tests/test_withdrawal_service.py
./withdrawal_service.py
```

## B. Run Instructions

### Local development mode

From `README.md`:

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

Default local URL:

```text
http://127.0.0.1:8790
```

If the default port is occupied:

```bash
PORT=8795 python3 app.py
```

Actual app entry point from `app.py`:

```python
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8790"))
    app.run(host="0.0.0.0", port=port)
```

### Production mode

From `render.yaml` and `Procfile`:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --worker-class gthread --timeout 180
```

### Build command

From `render.yaml`:

```bash
pip install -r requirements.txt
```

### Test command used in this diagnosis

```bash
python3 -m pytest -q
```

Result:

```text
340 passed in 3.05s
```

### Local startup verification

Command:

```bash
PORT=8799 python3 app.py
```

Process output:

```text
 * Serving Flask app 'app'
 * Debug mode: off
WARNING: This is a development server. Do not use it in a production deployment. Use a production WSGI server instead.
 * Running on all addresses (0.0.0.0)
 * Running on http://127.0.0.1:8799
 * Running on http://192.168.0.57:8799
Press CTRL+C to quit
```

Local request, proxy bypassed:

```bash
curl --noproxy '*' -sS -i http://127.0.0.1:8799/api/ops/readiness
```

Response:

```text
HTTP/1.1 200 OK
Server: Werkzeug/3.1.8 Python/3.9.6
Date: Sun, 28 Jun 2026 21:40:35 GMT
Content-Type: application/json
Content-Length: 1255
Connection: close

{"generationProvider":{"appEnv":"development","blockingIssues":[],"cloudApiReady":false,"errors":[],"liveGenerationRequired":false,"missingConfig":[],"mode":"unconfigured","provider":"local-demo","ready":true,"requiredConfig":[],"tokenhubModel":"hy-image-v3.0","tokenhubReady":false,"tokenhubRequired":false,"warnings":["live_generation_provider_not_configured_local_demo_only"]},"generationQueue":{"closed":false,"countsByStatus":{"canceled":0,"completed":0,"failed":0,"queued":0,"running":0},"limits":{"maxPendingJobs":12,"staleAfterSeconds":300.0,"timeoutSeconds":1800.0,"workerCount":3},"oldestQueuedAgeSeconds":null,"oldestRunningAgeSeconds":null,"staleCount":0,"timedOutCount":0,"workerCount":3},"objectStorage":{"appEnv":"development","blockingIssues":[],"mode":"local_demo","provider":"local","ready":true,"warnings":["local_object_storage_is_for_development_only","object_signing_secret_not_configured"]},"ok":true,"payments":{"appEnv":"development","blockingIssues":[],"errors":[],"missingConfig":[],"mode":"local_demo","provider":"fake","ready":true,"requiredConfig":[],"warnings":["fake_payment_provider_is_for_development_only","fake_payment_webhook_secret_not_configured","payment_provider_not_configured_defaulting_to_fake"]},"ready":true}
```

Note: without `--noproxy '*'`, local `curl` returned `502 Bad Gateway` from the local/proxy environment. The Flask process log only recorded the proxy-bypassed successful requests.

### Production command verification

Direct executable lookup:

```bash
gunicorn --check-config app:app --bind 0.0.0.0:8791 --workers 1 --threads 8 --worker-class gthread --timeout 180
```

Result:

```text
zsh:1: command not found: gunicorn
```

Module execution:

```bash
python3 -m gunicorn --check-config app:app --bind 0.0.0.0:8791 --workers 1 --threads 8 --worker-class gthread --timeout 180
```

Result:

```text
(exit code 0, no output)
```

Interpretation: the current local shell PATH does not expose the `gunicorn` executable, but the Python package is installed and the production config validates through `python3 -m gunicorn`. Render should install `gunicorn` into its service environment through `pip install -r requirements.txt`.

## C. Render Deployment Configuration

### Repository deployment config from `render.yaml`

```yaml
services:
  - type: web
    name: waimai-image-tool
    runtime: python
    plan: free
    buildCommand: pip install -r requirements.txt
    startCommand: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --worker-class gthread --timeout 180
    healthCheckPath: /api/ops/readiness
    autoDeployTrigger: commit
    envVars:
      - key: APP_ENV
        value: staging
      - key: ENABLE_LOCAL_DEMO_BILLING
        value: "false"
      - key: ENABLE_LOCAL_DEMO_STORAGE
        value: "true"
      - key: OBJECT_SIGNING_SECRET
        generateValue: true
      - key: DOWNLOAD_SIGNING_SECRET
        generateValue: true
      - key: OBJECT_STORAGE_PROVIDER
        value: cos
      - key: OBJECT_STORAGE_PRIVATE
        value: "true"
      - key: OBJECT_STORAGE_BUCKET
        value: waimai-image-tool-inputs-1311836560
      - key: OBJECT_STORAGE_REGION
        value: ap-guangzhou
      - key: OBJECT_STORAGE_PREFIX
        value: app-objects
      - key: PAYMENT_WEBHOOK_SECRET
        generateValue: true
      - key: ADMIN_API_TOKEN
        sync: false
      - key: TENCENT_HUNYUAN_ENABLED
        value: "true"
      - key: TENCENT_HUNYUAN_MODE
        value: auto
      - key: TENCENT_HUNYUAN_SYNC_LIMIT
        value: "6"
      - key: TENCENT_TOKENHUB_API_KEY
        sync: false
      - key: TENCENT_TOKENHUB_IMAGE_MODEL
        value: hy-image-v3.0
      - key: TENCENT_TOKENHUB_POLL_TIMEOUT
        value: "120"
      - key: TENCENTCLOUD_SECRET_ID
        sync: false
      - key: TENCENTCLOUD_SECRET_KEY
        sync: false
      - key: TENCENTCLOUD_REGION
        value: ap-guangzhou
      - key: TENCENT_COS_BUCKET
        value: waimai-image-tool-inputs-1311836560
      - key: TENCENT_COS_REGION
        value: ap-guangzhou
      - key: TENCENT_COS_PREFIX
        value: waimai-model-inputs
      - key: TENCENT_COS_AI_ASSET_PREFIX
        value: ai-assets
      - key: AI_ASSET_UPLOAD_TO_COS
        value: "true"
```

### Actual online evidence

Render response headers show the online service is running behind Gunicorn:

```text
x-render-origin-server: gunicorn
```

The public service responds to HTTP requests, so this is not currently a hard build/start failure. The current failure is runtime readiness and environment/provider configuration.

### Environment variables missing on current Render runtime

Source:

```bash
curl -sS -i https://waimai-image-tool-1.onrender.com/api/ops/deployment-config
```

Raw response:

```text
HTTP/1.1 200 Connection established

HTTP/2 200
date: Sun, 28 Jun 2026 21:39:03 GMT
content-type: application/json
rndr-id: f353fd4c-ec88-4711
server: cloudflare
vary: Accept-Encoding
x-render-origin-server: gunicorn
cf-cache-status: DYNAMIC
cf-ray: a12fd78a7b411f54-LAX
alt-svc: h3=":443"; ma=86400

{"appEnv":"render","blockingIssues":["tokenhub_image_provider_required","private_remote_object_storage_provider_required","object_signing_secret_required","real_payment_provider_required","fake_payment_provider_forbidden_in_live_environment"],"missingRequiredEnv":["ADMIN_API_TOKEN","APP_ENV","OBJECT_SIGNING_SECRET","OBJECT_STORAGE_BUCKET","OBJECT_STORAGE_PRIVATE","OBJECT_STORAGE_PROVIDER","PAYMENT_PROVIDER","PAYMENT_WEBHOOK_SECRET","TENCENT_TOKENHUB_API_KEY"],"ok":true,"ready":false,"renderDetected":true,"secretsRedacted":true,"sections":[{"blockingIssues":[],"id":"runtime","items":[{"configured":false,"env":["APP_ENV"],"key":"app_env","recommended":"staging","required":true,"sensitive":false,"status":"missing"},{"configured":true,"configuredEnv":"RENDER_EXTERNAL_URL","env":["PUBLIC_BASE_URL","RENDER_EXTERNAL_URL"],"key":"public_base_url","required":false,"sensitive":false,"status":"configured","value":"https://waimai-image-tool-1.onrender.com"},{"configured":false,"env":["ADMIN_API_TOKEN"],"key":"admin_api_token","required":true,"sensitive":true,"status":"missing"}],"missingRequiredEnv":["APP_ENV","ADMIN_API_TOKEN"],"ready":false,"title":"Runtime","warnings":["admin_api_token_recommended_for_ops_endpoints"]},{"blockingIssues":["tokenhub_image_provider_required"],"id":"generationProvider","items":[{"configured":false,"description":"TokenHub HY-Image-3.0 API key used for real background and product image generation.","env":["TENCENT_TOKENHUB_API_KEY","TOKENHUB_API_KEY","HUNYUAN_TOKENHUB_API_KEY"],"key":"tencent_tokenhub_api_key","required":true,"sensitive":true,"status":"missing"},{"configured":false,"env":["TENCENT_TOKENHUB_IMAGE_MODEL"],"key":"tencent_tokenhub_image_model","recommended":"hy-image-v3.0","required":false,"sensitive":true,"status":"optional"},{"configured":true,"configuredEnv":"TENCENT_HUNYUAN_ENABLED","env":["TENCENT_HUNYUAN_ENABLED"],"key":"tencent_hunyuan_enabled","recommended":"true","required":false,"sensitive":false,"status":"configured","value":"true"}],"missingRequiredEnv":["TENCENT_TOKENHUB_API_KEY"],"ready":false,"title":"AI Generation Provider","warnings":["legacy_cloud_api_does_not_consume_tokenhub_hy_image_credits"]},{"blockingIssues":["private_remote_object_storage_provider_required","object_signing_secret_required"],"id":"objectStorage","items":[{"configured":false,"env":["OBJECT_STORAGE_PROVIDER"],"key":"object_storage_provider","recommended":"cos","required":true,"sensitive":false,"status":"missing"},{"configured":false,"env":["OBJECT_STORAGE_PRIVATE"],"key":"object_storage_private","recommended":"true","required":true,"sensitive":true,"status":"missing"},{"configured":false,"env":["OBJECT_STORAGE_BUCKET","TENCENT_COS_BUCKET"],"key":"object_storage_bucket","required":true,"sensitive":false,"status":"missing"},{"configured":true,"configuredEnv":"TENCENTCLOUD_REGION","env":["OBJECT_STORAGE_REGION","TENCENT_COS_REGION","TENCENTCLOUD_REGION"],"key":"object_storage_region","recommended":"ap-guangzhou","required":true,"sensitive":false,"status":"configured","value":"ap-guangzhou"},{"configured":false,"env":["OBJECT_STORAGE_PREFIX","TENCENT_COS_PREFIX"],"key":"object_storage_prefix","recommended":"app-objects","required":false,"sensitive":false,"status":"optional"},{"configured":false,"env":["OBJECT_SIGNING_SECRET","DOWNLOAD_SIGNING_SECRET"],"key":"object_signing_secret","required":true,"sensitive":true,"status":"missing"},{"configured":true,"configuredEnv":"TENCENTCLOUD_SECRET_ID","env":["OBJECT_STORAGE_SECRET_ID","TENCENTCLOUD_SECRET_ID","TENCENT_SECRET_ID"],"key":"object_storage_secret_id","required":true,"sensitive":true,"status":"configured","value":"configured"},{"configured":true,"configuredEnv":"TENCENTCLOUD_SECRET_KEY","env":["OBJECT_STORAGE_SECRET_KEY","TENCENTCLOUD_SECRET_KEY","TENCENT_SECRET_KEY"],"key":"object_storage_secret_key","required":true,"sensitive":true,"status":"configured","value":"configured"}],"missingRequiredEnv":["OBJECT_STORAGE_PROVIDER","OBJECT_STORAGE_PRIVATE","OBJECT_STORAGE_BUCKET","OBJECT_SIGNING_SECRET"],"ready":false,"title":"Object Storage","warnings":[]},{"blockingIssues":["real_payment_provider_required","fake_payment_provider_forbidden_in_live_environment"],"id":"payments","items":[{"allowedValues":["alipay","wechat"],"configured":false,"env":["PAYMENT_PROVIDER"],"key":"payment_provider","recommended":"wechat","required":true,"sensitive":false,"status":"missing"},{"configured":false,"env":["PAYMENT_WEBHOOK_SECRET"],"key":"payment_webhook_secret","required":true,"sensitive":true,"status":"missing"}],"missingRequiredEnv":["PAYMENT_PROVIDER","PAYMENT_WEBHOOK_SECRET"],"ready":false,"title":"Payments","warnings":[]},{"blockingIssues":[],"id":"generationQueue","items":[],"missingRequiredEnv":[],"ready":true,"title":"Generation Queue","warnings":[]}]}
```

Important mismatch:

- `render.yaml` declares `APP_ENV`, object storage env vars, generated signing secrets, TokenHub optional model values, and payment webhook secret.
- The current online service reports many of those as missing.
- This usually means the existing Render service has not applied the Blueprint env vars from `render.yaml`, or the service is configured manually and needs its environment updated in Render Dashboard.

## D. Dependency Analysis

### `requirements.txt`

```text
Flask==3.0.3
gunicorn==22.0.0
pandas==2.2.2
openpyxl==3.1.5
xlrd==2.0.1
Pillow==10.4.0
numpy==2.0.1
cos-python-sdk-v5==1.9.44
```

### `package.json`

No `package.json` exists in the current project root. This is not a Node/Vite/React deployment.

### Key dependency notes

- `Flask`: web framework.
- `gunicorn`: production WSGI server used by Render start command.
- `pandas`, `openpyxl`, `xlrd`: Excel menu parsing.
- `Pillow`, `numpy`: image processing and export.
- `cos-python-sdk-v5`: Tencent COS object storage.

### Render risk libraries

No `sharp`, `canvas`, `torch`, TensorFlow, Playwright, Chromium, or other heavy native/ML runtime dependencies are present.

Potential but manageable native-wheel dependencies:

- `Pillow`
- `numpy`
- `pandas`

These normally install on Render Python runtimes through wheels. Current online service is already running, so dependency installation is not the current blocker.

## E. Known Errors And Logs

### Local test result

Command:

```bash
python3 -m pytest -q
```

Raw output:

```text
........................................................................ [ 21%]
........................................................................ [ 42%]
........................................................................ [ 63%]
........................................................................ [ 84%]
....................................................                     [100%]
340 passed in 3.05s
```

### Python compile check

Command:

```bash
python3 -m py_compile app.py object_storage_service.py payment_service.py image_pipeline.py storage_db.py
```

Raw output:

```text
(exit code 0, no output)
```

### Render readiness

Command:

```bash
curl -sS -i https://waimai-image-tool-1.onrender.com/api/ops/readiness
```

Raw response:

```text
HTTP/1.1 200 Connection established

HTTP/2 200
date: Sun, 28 Jun 2026 21:39:03 GMT
content-type: application/json
rndr-id: d7153bff-8e69-446c
server: cloudflare
vary: Accept-Encoding
x-render-origin-server: gunicorn
cf-cache-status: DYNAMIC
cf-ray: a12fd78a9b8a2378-LAX
alt-svc: h3=":443"; ma=86400

{"generationProvider":{"appEnv":"render","blockingIssues":["tokenhub_image_provider_required"],"cloudApiReady":true,"errors":["tokenhub_image_provider_required"],"liveGenerationRequired":true,"missingConfig":["TENCENT_TOKENHUB_API_KEY"],"mode":"legacy_cloud_api","provider":"tencent-hunyuan","ready":false,"requiredConfig":[{"env":["TENCENT_TOKENHUB_API_KEY","TOKENHUB_API_KEY","HUNYUAN_TOKENHUB_API_KEY"],"key":"tencent_tokenhub_api_key"}],"tokenhubModel":"hy-image-v3.0","tokenhubReady":false,"tokenhubRequired":true,"warnings":["legacy_cloud_api_does_not_consume_tokenhub_hy_image_credits"]},"generationQueue":{"closed":false,"countsByStatus":{"canceled":0,"completed":0,"failed":0,"queued":0,"running":0},"limits":{"maxPendingJobs":12,"staleAfterSeconds":300.0,"timeoutSeconds":1800.0,"workerCount":3},"oldestQueuedAgeSeconds":null,"oldestRunningAgeSeconds":null,"staleCount":0,"timedOutCount":0,"workerCount":3},"objectStorage":{"appEnv":"render","blockingIssues":["private_remote_object_storage_provider_required","object_signing_secret_required"],"mode":"local_demo","provider":"local","ready":false,"warnings":[]},"ok":true,"payments":{"appEnv":"render","blockingIssues":["real_payment_provider_required","fake_payment_provider_forbidden_in_live_environment"],"errors":["real_payment_provider_required","fake_payment_provider_forbidden_in_live_environment"],"missingConfig":[],"mode":"local_demo","provider":"fake","ready":false,"requiredConfig":[{"allowedValues":["wechat","alipay"],"env":["PAYMENT_PROVIDER"],"key":"payment_provider"}],"warnings":[]},"ready":false}
```

### Tencent generation status on Render

Command:

```bash
curl -sS -i https://waimai-image-tool-1.onrender.com/api/tencent-status
```

Raw response:

```text
HTTP/1.1 200 Connection established

HTTP/2 200
date: Sun, 28 Jun 2026 21:39:03 GMT
content-type: application/json
cf-cache-status: DYNAMIC
rndr-id: ff3936a7-84c5-4db1
server: cloudflare
vary: Accept-Encoding
x-render-origin-server: gunicorn
cf-ray: a12fd78aac6155a3-LAX
alt-svc: h3=":443"; ma=86400

{"cloudApiReady":true,"configured":true,"cosBucket":"waimai-image-tool-inputs-1311836560","cosReady":true,"cosRegion":"ap-guangzhou","enabled":true,"missing":[],"mode":"auto","provider":"tencent-hunyuan","region":"ap-guangzhou","syncLimit":6,"tokenhubModel":"hy-image-v3.0","tokenhubReady":false}
```

Interpretation:

- Legacy Tencent Cloud API credentials are configured.
- TokenHub is not configured.
- Current purchased `HY-Image-3.0` / TokenHub credits cannot be consumed unless `TENCENT_TOKENHUB_API_KEY` is configured.

### Render style background, no generation trigger

This diagnostic intentionally used `generate=0` to avoid triggering a paid external image-generation request during a diagnosis-only run.

Command:

```bash
curl -sS -i 'https://waimai-image-tool-1.onrender.com/api/style-background?style=style-1&generate=0'
```

Raw response:

```text
HTTP/1.1 200 Connection established

HTTP/2 200
date: Sun, 28 Jun 2026 21:39:25 GMT
content-type: application/json
cf-cache-status: DYNAMIC
rndr-id: cd670b4a-d7bc-4eaf
server: cloudflare
vary: Accept-Encoding
x-render-origin-server: gunicorn
cf-ray: a12fd8076e7db8d4-LAX
alt-svc: h3=":443"; ma=86400

{"id":"style-1","name":"\u4e00\u53f7\u80cc\u666f","sample":{"aiProvider":"tencent-hunyuan","dishName":"\u80cc\u666f\u98ce\u683c\u6837\u56fe","generated":true,"generationAction":"PendingGeneration","generationProvider":"tencent-hunyuan","generationStatus":"pending","imageId":"9effde600cc69cef38","reusable":true,"score":0.0,"source":"generated-style-sample","store":"generated-style-sample","styleId":"style-1","styleName":"\u4e00\u53f7\u80cc\u666f","url":""},"styleId":"style-1"}
```

### Render plan endpoint

Command:

```bash
curl -sS -i 'https://waimai-image-tool-1.onrender.com/api/plan?quality=standard'
```

Result summary:

```text
HTTP/2 200
x-render-origin-server: gunicorn
```

Key payload facts:

```text
pricing.baseImagePoints: 10
pricing.premiumImagePoints: 20
pricing.previewFreeImages: 6
pipeline.tencent.tokenhubReady: false
pipeline.localBackgroundFallback: false
styles[*].sample.generationStatus: pending
styles[*].sample.generationAction: PendingGeneration
styles[*].sample.url: ""
```

The full plan response is large because it includes the complete demo menu, candidate matches, pricing, and six style records. It did not contain a deployment failure stack trace.

### Render deploy failure logs

No Render deploy build/start failure log was accessible from this local workspace.

Evidence:

```bash
command -v render
```

Raw result:

```text
(exit code 1, no output)
```

```bash
env | rg -i '^(RENDER|RENDER_API|RENDER_SERVICE)'
```

Raw result:

```text
(exit code 1, no output)
```

Interpretation:

- Render CLI is not installed in this shell.
- No Render API token or service env var is present in this shell.
- The public Render service itself is online and responds with HTTP 200.
- Therefore the currently observed issue is not a captured Render build failure. It is runtime readiness failure caused by missing Render environment variables and provider configuration.

## F. Conclusion

### Classification

Primary categories:

- Environment variable problem.
- Storage/provider configuration problem.
- External provider configuration problem.

Secondary category:

- Start command local PATH issue only in this shell, not confirmed as a Render issue.

Not the primary cause:

- Dependency problem: unlikely. Tests pass locally; Render service is running; no heavy native/ML dependency is present.
- Code structure problem: unlikely for the current "background image will not generate on Render" symptom.
- Render build/start command problem: unlikely. Online headers show `x-render-origin-server: gunicorn`, and public endpoints return HTTP 200.

### Root cause for Render background image failure

Render is running, but the live runtime is not product-ready:

```text
generationProvider.ready=false
blockingIssues=["tokenhub_image_provider_required"]
missingConfig=["TENCENT_TOKENHUB_API_KEY"]
tokenhubReady=false
```

The service has legacy Tencent Cloud credentials, but the purchased `HY-Image-3.0` / TokenHub credits require a TokenHub API key. Without `TENCENT_TOKENHUB_API_KEY`, the app cannot use the TokenHub image provider and will not consume the newly purchased TokenHub image credits.

### Root cause for object storage readiness failure

Render is still using local demo storage at runtime:

```text
objectStorage.mode="local_demo"
objectStorage.provider="local"
blockingIssues=["private_remote_object_storage_provider_required","object_signing_secret_required"]
```

Missing runtime env vars:

```text
OBJECT_STORAGE_PROVIDER
OBJECT_STORAGE_PRIVATE
OBJECT_STORAGE_BUCKET
OBJECT_SIGNING_SECRET
```

### Root cause for payment readiness failure

Render is still using fake/local demo payments:

```text
payments.provider="fake"
payments.mode="local_demo"
blockingIssues=["real_payment_provider_required","fake_payment_provider_forbidden_in_live_environment"]
```

Missing runtime env vars:

```text
PAYMENT_PROVIDER
PAYMENT_WEBHOOK_SECRET
```

### Most important deployment discrepancy

`render.yaml` contains the intended Render configuration, but the current Render service reports those values as missing. This indicates the current Render service has not applied the Blueprint env vars, or it was created/configured manually and needs the same env vars added in Render Dashboard.

### Files modified by this diagnostic

Only this file was created:

```text
DEPLOYMENT_DIAGNOSIS.md
```

No application code, tests, templates, static files, or configuration files were modified.
