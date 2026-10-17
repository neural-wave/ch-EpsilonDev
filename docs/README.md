# Project documentation

The [project README](../README.md) is the entry point for setup, architecture, team responsibilities, verification, and delivery.

| Topic | Document |
|---|---|
| Patient graph and schema conventions | [DATA.md](DATA.md) |
| Models, prompts, and agent evaluation | [LLM_AGENTS.md](LLM_AGENTS.md) |
| Every model and configuration change, why, and its measurements | [MODEL_HISTORY.md](MODEL_HISTORY.md) |
| Compliance rules | [GUARD.md](GUARD.md) |
| Guard and judge handoff | [guard-and-judge/README.md](guard-and-judge/README.md) |
| Context and retrieval handoff | [data-and-context/README.md](data-and-context/README.md) |
| Researcher console contracts | [FRONTEND.md](FRONTEND.md) |
| Santiago's feature handoff | [features/README.md](features/README.md) |
| Privacy boundaries | [PRIVACY_REVIEW.md](PRIVACY_REVIEW.md) |
| Verification evidence and pending checks | [RESULTS_VALIDATION.md](RESULTS_VALIDATION.md) |
| Performance measurements | [PERFORMANCE.md](PERFORMANCE.md) |
| Failure analysis | [FAILURE_ANALYSIS.md](FAILURE_ANALYSIS.md) |
| Offline validator findings | [SPARQL_LLM_VALIDATOR.md](SPARQL_LLM_VALIDATOR.md) |
| Detailed operating notes and historical baselines | [operations/PROJECT_HANDBOOK.md](operations/PROJECT_HANDBOOK.md) |
| Original design | [specs/2026-10-16-nl-to-sparql-design.md](specs/2026-10-16-nl-to-sparql-design.md) |
| Original implementation plan | [plans/2026-10-16-base-pipeline.md](plans/2026-10-16-base-pipeline.md) |
| Historical status checklist, formerly root todo.md | [plans/PROJECT_STATUS_2026-10-17.md](plans/PROJECT_STATUS_2026-10-17.md) |

Historical plans and operating notes preserve their original measurements and assumptions. Use the validation record for actual completed checks, and confirm authorized service availability before running operational commands. Keep private credentials, patient files, terminology-derived assets, and raw evaluation outputs outside tracked documentation.
