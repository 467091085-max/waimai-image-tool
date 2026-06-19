# 外卖菜品图生成系统

这是“历史图库检索 + 少量 AI 二次加工”的云端 MVP 原型。

## 功能

- 上传 Excel 菜单（支持 `.xls` 和 `.xlsx`）
- 上传菜品图库 zip
- 自动识别品类
- 菜品名标准化
- 展示 5 套风格包和覆盖率
- 出图任务拆分：直接复用 / 需换背景 / 缺图定制 / 需复核
- 套餐报价、积分、付费功能、邀请奖励展示
- 导出 zip 和匹配报告

## 本地运行

```bash
pip install -r requirements.txt
python app.py
```

打开：

```text
http://127.0.0.1:8790
```

## Render 部署

1. 把本仓库推到 GitHub。
2. 打开 Render。
3. New Web Service。
4. 选择该 GitHub 仓库。
5. Render 会读取 `render.yaml` 自动部署。

## 重要说明

当前版本为了演示，未上传数据时会自动生成 demo 菜单和 demo 图库。

正式生产版不要把大量图片放在 Render 硬盘里，应改成：

- 图片：阿里云 OSS / 腾讯云 COS / Cloudflare R2
- 数据库：PostgreSQL
- 向量检索：Qdrant / Milvus
- 出图任务：队列 Worker
