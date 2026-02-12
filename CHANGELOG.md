# Changelog

## v0.2.8 — 2026-02-12

- UX: removed dashboard login/logout flow and related token-auth UI to avoid confusion
- DX: enabled auto-reload by default for local development
- CLI: added `--no-debug` to run without auto-reload
- Docs: removed token-login instructions and updated security guidance

## v0.2.7 — 2026-02-12

- Security: changed default bind host to `127.0.0.1` (localhost only)
- Security: added optional token auth (`--auth-token` / `OPENCLAW_DASHBOARD_TOKEN`) for UI, API, and OTLP endpoints
- Security: added built-in token login UI for browser access when auth is enabled
- UX/Security: added dashboard Logout button + `/auth/logout` endpoint to clear auth cookie and browser session state
- Security: added startup warnings when binding to non-local hosts without auth
- Reliability: added SSE guardrails (max stream duration and max concurrent stream clients)
- Docs: added security and auth guidance in README

## v0.2.6 — 2026-02-10

**Major Features & Polish Release**

- **🧠 Automation Advisor**: Self-writing skills with pattern detection engine
- **💰 Cost Optimizer**: Real-time cost tracking with local model fallback recommendations  
- **🕰️ Time Travel**: Historical component data scrubbing with visual timeline
- **📚 Skill Templates Library**: Complete automation templates for rapid development
- **🔧 Enhanced Error Handling**: Production-ready error recovery and graceful degradation
- **✅ Startup Validation**: New user onboarding with configuration checks
- **🚀 Performance**: Server-side caching, client prefetch, optimized modal loading
- **📖 Documentation**: Comprehensive skill template library and BUILD_STATUS tracking

**Quality Improvements**

- Enhanced error handling with specific exception types and recovery mechanisms
- Automatic backup of corrupted metrics files before attempting fixes
- Better disk space detection and helpful error messages
- Startup configuration validation for new open-source users
- Modal data cross-contamination fixes with proper cleanup
- Repository sync and version bump maintenance

## v0.2.5 — 2026-02-08

- Fix: Flask threaded mode (Sessions/Memory tabs loading)
- Fix: Log file detection (openclaw-* prefix support)
- Fix: Flow SVG duplicate filter ID conflict
- Fix: Flow Live Activity Feed event patterns
- Fix: Task card → transcript modal wiring
- Improvement: Better log format parsing for new OpenClaw versions

## v0.2.4

- Initial tracked release
