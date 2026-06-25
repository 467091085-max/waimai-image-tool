# 外卖菜品图一键生成工具交付说明

更新时间：2026-06-25

## 项目路径

本地项目目录：

```text
/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/outputs/waimai-image-tool-deploy
```

线上测试地址：

```text
https://waimai-image-tool-1.onrender.com/
```

## 本轮并行工作

本轮使用 `worktrees-v3` 拆成 7 个并行任务：

| 模块 | 状态 | 主要结果 |
|---|---|---|
| 图库资产层 | 已合并 | 扫描 `cleanpic` / `watermarkpic`，区分可复用、品牌水印、参考图、低质图、重复图 |
| 菜单解析和匹配 | 已合并 | 支持 `.xls` / `.xlsx`，真实菜单 24/24 解析成功，匹配策略改为保守严格，避免菜名错配 |
| 风格预览 | 已合并 | 固定 6 张背景卡，编号一号到六号，图库不足时不再假装成功 |
| 生成引擎 | 已合并 | 新增正式生图 job，引入腾讯混元/COS 状态、失败、重试、退款字段 |
| 导出交付 | 已合并 | 支持美团/淘宝饿了么/京东尺寸，JPG 输出，ZIP 打包，单品/套餐/选择项导出 |
| 积分后台 | 已合并 | 普通 100 积分/张、精修 200 积分/张、自定义修改 150 积分/张，充值、扣费、退款、后台流水 |
| 客户 UI | 已合并 | 新工作台 UI：上传菜单、选择背景、6 张免费样图、扣积分正式出图、分组预览、导出 |

## 本地启动

```bash
cd /Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/outputs/waimai-image-tool-deploy
python3 -m pip install -r requirements.txt
PORT=8796 PYTHONPATH=.codex_deps:. python3 app.py
```

打开：

```text
http://127.0.0.1:8796/
```

## 部署

当前使用 Render 自动部署。

关键文件：

```text
render.yaml
requirements.txt
app.py
```

Render 环境变量已验证：

```text
TENCENT_HUNYUAN_ENABLED=true
TENCENTCLOUD_SECRET_ID=已配置
TENCENTCLOUD_SECRET_KEY=已配置
TENCENT_COS_BUCKET=waimai-image-tool-inputs-1311836560
TENCENT_COS_REGION=ap-guangzhou
```

线上接口验证结果：

```text
/api/tencent-status: configured=true, cosReady=true, provider=tencent-hunyuan
/api/pipeline-config: imageEditApiReady=true, objectStorageReady=true
```

## 已验证命令

```bash
node --check static/app.js
node --check static/admin.js
PYTHONPATH=.codex_deps:. python3 -m pytest -q
PYTHONPATH=.codex_deps:. python3 -m unittest discover -s tests
PYTHONPATH=.codex_deps:. python3 scripts/smoke_export.py
```

验证结果：

```text
pytest: 87 passed
unittest: 80 passed
smoke_export: 成功生成 9 张平台图 ZIP
```

## 重要遗留问题

1. Render 线上目前只能看到仓库内置 seed 图，不能直接读取你 Mac 上的：

```text
/Users/guiguixiaxia/Documents/cleanpic
/Users/guiguixiaxia/Documents/watermarkpic
```

本地可以读取这些目录，线上服务器不可以。当前分支已提供 `scripts/sync_gallery_to_cos.py`：默认 dry-run 扫描 `cleanpic` / `watermarkpic`，生成可上传 COS 的 JSONL 索引和 summary；`cleanpic` 标为可复用，`watermarkpic` 标为 `reference_only` 且不会进入直接复用候选。上线前还需要主线程拿生产 COS 环境变量执行真实全量上传。

2. 当前腾讯混元真实生图链路已经接入并可检测配置，但本轮没有大批量消耗额度做真实全量生成压测。

3. 目前支付是模拟充值和积分流水，还没有接微信/支付宝真实支付回调。

4. 用户登录/权限还是原型状态，正式产品需要账号系统、订单归属、管理员权限。

5. 真实图库上线后，需要再跑一次“真实菜单 -> 6 张风格 -> 6 张免费样图 -> 正式生图 -> ZIP 导出”的线上完整闭环。

## 下一步

最优先做：

1. 用 `scripts/sync_gallery_to_cos.py --no-dry-run --limit 3` 先做 COS 小批量 live check。
2. 确认 summary 和 COS 对象后执行真实图库全量上传。
3. 在线上读取 COS 图库索引，而不是只读仓库内置 seed 图。
4. 用 1 个真实菜单在线上生成 6 张样图，确认混元真实出图质量和成本。
5. 再做支付和登录。
