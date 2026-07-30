# Final Sanitized Source Package

- Archive: `/Users/guiguixiaxia/Documents/Codex-Handoffs/waimai-image-tool/2026-07-30/waimai-image-tool-final-v6.zip`
- Implementation baseline: `f680c24b0ed826a6ea999d614cf7deb172d7710e`
- Archive size: `1134096` bytes
- SHA-256: `f5409d62d735f70f57d27a422897cd72c3ca09981bd901402b7a08b01d076be3`
- Source file count: `224`
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
