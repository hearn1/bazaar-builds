# live_cron Promotion Waiver - 2026-05-18

Operator decision: promote the automated builds pipeline to `live_cron` now,
with `dry_run: false`, without waiting for the usual Gate 1 and Gate 2 timing
thresholds.

Waived gates:
- Gate 1: at least 2 counted healthy bazaardb patch windows.
- Gate 2: at least 14 calendar days of deterministic classifier output.

Rationale:
- Deterministic single-patch formulation has been reviewed and is the intended
  live behavior.
- Hosted LLM classification has been removed; no `CLAUDE_API_KEY`, Anthropic
  provider, or hosted classifier readiness is required for promotion.
- Deterministic `local_dry_run` verification completed for Karnok, Jules, and
  Stelle on 2026-05-18 using temporary state, temporary stats, and temporary
  artifacts.
- The operator is intentionally accepting the launch-timing risk instead of
  waiting for 14 calendar days or 2 counted patch windows.

This waiver does not bypass:
- deterministic classifier readiness,
- recent malformed or unhealthy bazaardb runs,
- schema validation before coach catalog mutation,
- curator review of opened coach PRs.
