# Prototypes from the 17 October failure analysis

These files are the measured prototypes behind the fixes ranked in [docs/FAILURE_ANALYSIS.md](../../docs/FAILURE_ANALYSIS.md) §4. They are not wired into the pipeline and have no tests except the guard rule; port each check into `src/nl2sparql/` with a regression test, keep the hint texts, and measure false positives on the reference queries before enabling it. Run them with the project's `.venv`.

| File | Fix # | What it is |
|---|---|---|
| `syntax_diagnose.py` | 1 | `diagnose(text)` turns an unparseable query into hints that name the real error; `normalize(text)` repairs the three unambiguous mistakes |
| `grouping_hint.py` | 11 | builds the "group by the code's class instead" hint for an R2/R5 violation |
| `orchestrator_policy.py` | 12 | `decide()` with R1 never retried, plus the test |
| `guard_rule_patch.py`, `test_guard_graph_variable.py` | 13 | the identifier-pinning rule and its 20 tests (use the repo's `guard` fixture instead of `patched_guard`) |
| `intent_postcheck.py` | 21 | `confirm_identifier_flag()` for counting requests without a reveal cue |
| `checks_wrong_rows.py` | 3, 7, 8, 10 | unbound variable, duplicate aggregate columns, inference on a variable class, record link typed as a code node |
| `checks_same_rows.py` | 3, 5, 6, 7, 19 | unbound aggregate input, re-bound variable, date + duration, record counts, value-set checks |
| `checks_empty.py` | 9, 19 | class-scoped property check, typed value-set variable, value-set member on the wrong class |
