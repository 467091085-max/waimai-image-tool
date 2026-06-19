# 蒜头外卖菜品图工作台

这是一个“历史图库检索 + 少量 AI 二次加工”的外卖菜品图 MVP。当前版本可以上传 Excel 菜单，展示图库风格，选择风格后生成整店正式图预览，并按平台尺寸导出图片包。

## 已实现功能

- 菜单上传：支持 `.xls` 和 `.xlsx`。
- 菜品拆分：自动统计单品、套餐、小吃/其他图片数量。
- 风格预览：展示 5 套图库风格，选择风格后展示 6 张免费单品样图。
- 出图质量：支持普通出图和精修出图两档，普通出图 10 积分/张，精修出图 20 积分/张。
- 积分计费：品牌水印 50 积分/单，额外平台 100 积分/个平台。
- 品牌水印：支持文字水印和透明 PNG Logo，支持角标和平铺。
- 正式图预览：按单品图片、套餐图片、其他图片分组显示。
- 重做额度：每单提供免费换版额度，用完后按 10 积分/张。
- 导出：支持 JPG、PNG、WebP，支持勾选、全选、单品、套餐导出，并按平台上限压缩文件大小。
- 平台尺寸：支持美团、淘宝、京东导出，不裁掉主体，按目标尺寸留边适配。

## 默认平台尺寸

当前先按常见上架规格做成配置：

| 平台 | 默认尺寸 | 当前导出上限 |
|---|---:|---:|
| 美团外卖 | 800 x 600 | 500 KB |
| 淘宝 | 800 x 800 | 500 KB |
| 京东 | 800 x 800 | 500 KB |

公开网页能查到的平台图片限制并不稳定，尤其美团商家后台规则通常需要登录后确认。当前版本先按保守值导出，避免客户下载后还要二次压缩。上线前建议再用对应商家后台的最新规则校验一次。如果平台规则变化，只需要改 `app.py` 里的 `PLATFORMS` 常量。

## 本地启动

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

默认地址：

```text
http://127.0.0.1:8790
```

如果端口被占用：

```bash
PORT=8795 python3 app.py
```

## Render 部署

Render 使用本仓库里的 `render.yaml` / `Procfile` 部署。

推荐配置：

- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`
- Python: Render 自动识别 Python 3

部署流程：

1. 推送代码到 GitHub。
2. Render 绑定该 GitHub 仓库。
3. 创建 Web Service。
4. 选择 `main` 分支。
5. 等待自动部署完成。

当前线上地址：

```text
https://waimai-image-tool-1.onrender.com
```

## 测试命令

语法检查：

```bash
python3 -m py_compile app.py
node --check static/app.js
git diff --check
```

接口和导出链路检查：

```bash
PYTHONPATH=.codex_deps python3 - <<'PY'
from app import app, EXPORT_DIR
from zipfile import ZipFile
from PIL import Image
import io

c = app.test_client()
plan = c.get("/api/plan").get_json()
style = plan["styles"][0]["id"]
out = c.post("/api/export", json={
    "style": style,
    "scope": "selected",
    "selectedRows": [1],
    "format": "jpg",
    "platforms": ["meituan", "taobao", "jd"],
    "watermark": {
        "enabled": True,
        "type": "text",
        "text": "测试品牌",
        "position": "bottom-right",
        "pattern": "corner"
    }
}).get_json()
zip_path = EXPORT_DIR / out["download"].split("/download/", 1)[1]
with ZipFile(zip_path) as zf:
    print(out)
    print(zf.namelist()[:8])
PY
```

## 数据目录

```text
data/uploads/   上传菜单
data/library/   图库与演示图库
data/exports/   导出图片包
```

这些目录已加入 `.gitignore`，不要把真实客户菜单和真实图库提交到 GitHub。

## 生产化遗留问题

当前版本还是 MVP，已经能跑通主流程，但要正式卖给客户，还需要继续补：

- 登录和真实账户系统：目前积分是前端模拟余额。
- 支付系统：需要接微信/支付宝/对公充值。
- 对象存储：真实图库不要放 Render 硬盘，建议用腾讯 COS、阿里 OSS 或 Cloudflare R2。
- 数据库：菜单、订单、积分流水、导出记录需要 PostgreSQL。
- 图库清洗后台：自动识别品牌水印、菜品名水印、可复用图、需抠图图。
- AI 接口：普通出图接混元，高清精修接 Gemini/OpenAI。
- 异步任务队列：正式批量出图应由 Worker 后台处理，前端轮询进度。
- 平台尺寸复核：上线前用美团/淘宝/京东商家后台最新规则再确认一次。
