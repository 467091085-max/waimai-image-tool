# Worktree Plan

主仓库用于稳定演示和部署：

```text
/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/outputs/waimai-image-tool-deploy
branch: main
```

真实资料目录只在本机读取，不提交到 Git：

```text
/Users/guiguixiaxia/Documents/menus
/Users/guiguixiaxia/Documents/cleanpic
/Users/guiguixiaxia/Documents/watermarkpic
```

当前样本规模：

```text
menus: 24 files
cleanpic: 851 files
watermarkpic: 1491 files
```

## Worktrees

```text
worktrees/library-import       feature/library-import
worktrees/menu-parser          feature/menu-parser
worktrees/matching-engine      feature/matching-engine
worktrees/image-pipeline       feature/image-pipeline
worktrees/account-billing      feature/account-billing
worktrees/admin-panel          feature/admin-panel
worktrees/storage-db           feature/storage-db
```

## Responsibilities

`feature/library-import`

- Scan `cleanpic` and `watermarkpic`.
- Build image inventory metadata.
- Generate thumbnails.
- Mark clean/watermarked/source folder.
- Prepare reusable library records.

`feature/menu-parser`

- Parse all Excel files under `menus`.
- Improve header detection and field mapping.
- Keep downloadable standard menu template.
- Produce structured menu JSON for matching.

`feature/matching-engine`

- Normalize dish names.
- Build alias/canonical dish tables.
- Match uploaded menu items to library images.
- Return coverage rate by style and dish type.

`feature/image-pipeline`

- Apply selected platform sizes.
- Apply brand watermark.
- Package JPG exports.
- Keep final images RGB and platform-safe.

`feature/account-billing`

- Replace frontend-only points with server-side account records.
- Add point balance, point orders, point ledger.
- Prepare payment integration boundary.

`feature/admin-panel`

- Internal library admin.
- Edit dish name, category, style, reusable status.
- Review watermarked images and failed matches.

`feature/storage-db`

- Add SQLite/PostgreSQL-ready schema.
- Separate local dev DB from production DB.
- Prepare object storage interface for future COS/OSS.

## Merge Order

1. `feature/menu-parser`
2. `feature/library-import`
3. `feature/matching-engine`
4. `feature/image-pipeline`
5. `feature/admin-panel`
6. `feature/storage-db`
7. `feature/account-billing`

`main` stays deployable. Each branch should be merged only after local tests pass.

## Local Data Rule

Do not copy real customer files into the repository. Scripts should read from the local source directories above or from environment variables.
