# Roadmap

## Current Priority
Fix Render background generation so the deployed test link can produce real Hunyuan style backgrounds without blocking the server.

## Product Modules
- Customer image generation workspace
- Hunyuan background generation
- Hunyuan sample and final dish generation
- AI-generated asset repository with tags and reusable matching metadata
- Object storage / bucket integration
- Credit and payment system
- Agent commission system
- C-end invite reward system
- Anti-abuse and registration risk control
- Admin data backend
- Download and asset security
- Queue and concurrency control
- Deployment and operations readiness

## Near-Term Sequence
1. Make `/api/plan` lightweight and non-blocking.
2. Generate six category-matched backgrounds through per-style Hunyuan calls.
3. Ensure free samples and final images use the selected background.
4. Ensure final dish images use dish names and generate missing products through text-to-image.
5. Persist generated assets with category, keywords, product names, style tags, and reusable matching labels.
6. Harden productization modules after the generation flow is correct.
