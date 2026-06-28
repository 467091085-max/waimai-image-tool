# Steps

## Active
- [x] Finish Obsidian memory system setup.
- [x] Run targeted tests for async background generation.
- [x] Run available broader test suite.
- [x] Sync current patch to deploy repo.
- [x] Run deploy repository tests.
- [x] Push to GitHub.
- [x] Wait for Render deployment.
- [x] Verify Render `/api/plan`.
- [ ] Enable Tencent Cloud Hunyuan resources / postpaid billing.
- [ ] Verify Render `/api/style-background` returns a real image URL after Tencent resource is available.

## Completed
- [x] Reproduced Render background generation timeout.
- [x] Identified synchronous Hunyuan calls inside `/api/plan`.
- [x] Started minimal patch to split style background generation into a separate endpoint.
