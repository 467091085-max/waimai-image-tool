# Final Sanitized Source Package

- Archive: `/Users/guiguixiaxia/Documents/Codex-Handoffs/waimai-image-tool/2026-07-30/waimai-image-tool-final-v5.zip`
- Baseline commit: `4d3214bbd251914fa314265d5ac98d12c1a302fa`
- Archive size: `1132010` bytes
- SHA-256: `9544c4eacecf465337baa164469af816f61213ff8797419152baaadee3a08111`
- Source file count: `223`
- ZIP integrity: passed

The archive excludes `.git`, every `.env` file, dependency directories,
build output, caches, bytecode, databases, runtime/browser state, `data/`,
raw deterministic reports, and nested archives.

This metadata file is maintained as the external sidecar and is intentionally
excluded from the archive to avoid a self-referential package hash.

The final credential-pattern scan matched only:

- Runtime PEM wrapper source in `payment_service.py`.
- Explicit dummy credentials and negative assertions in tests.

No real API key, token, private key, cookie, or environment file was included.
