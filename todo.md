# Project Status Checklist

Status checked against `README.md`, the repository map, the base pipeline plan, and the files present in the repo on 17 October 2026. The README is the current source of truth; the earlier GGUF/llama-server checklist has been removed because the project now documents Qwen3.5-9B served by vLLM.

## Done: base implementation

- [x] End-to-end pipeline components are built and wired together.
- [x] FastAPI endpoint and fixed response contract are implemented.
- [x] Input validation and normalization are implemented.
- [x] Module A has a configurable whole-request timeout and an HTTP concurrency test command.
- [x] Intent and query agents, prompts, and OpenAI-compatible LLM client are present.
- [x] Context retrieval, schema cards, example bank, and terminology-index lookup code are present.
- [x] SPARQL parser, judge, compliance rules, and shared retry flow are implemented.
- [x] Demo, evaluation runner, Virtuoso helper, and database setup scripts are present.
- [x] Unit test suite and pull-request CI are present; run the current suite before each merge.
- [x] Baseline evaluation is recorded in README: stub 87% verdict accuracy; Qwen3.5-9B 90%, zero forbidden requests marked compliant, 2.8 s median latency.
- [x] Jury confirmed the system must be database-agnostic at request time; compliant-query execution is optional; the hackathon API returns the query for forbidden requests.

## Required project work still open

### Integration

- [ ] Decide where the API will run during judging and how GPU access will be provided.
- [ ] Run `python -m nl2sparql.load_test --url http://localhost:8080 --questions eval/questions.jsonl --requests 32 --concurrency 8 --require-llm` on the run machine; record errors, latency, throughput, and tokens. The command is ready, but the real LLM load run is pending.
- [ ] Verify clean-environment installation and startup of the final A revision. A clean checkout of the earlier revision passed on 17 October; repeat the check after the new A changes are included.
- [ ] Run the existing LLM evaluation on the final A branch and compare it with the README baseline before merging; leave evaluation code to module E.
- [ ] Finish the README for the jury after the deployment answer and teammates' full names and roles are confirmed.

### Agents and prompts

- [ ] Choose and record the final model configuration.
- [ ] Improve prompts so complex requests (especially evaluation cases #01 and #08) produce valid SPARQL and fewer allowed queries return zero rows.
- [ ] Reduce the current approximate 3,000 tokens per request.
- [ ] Measure intent-agent false alarms and prompt-injection resistance.

### Data and context

- [ ] Build the terminology index (README says the script exists, but the index is not built).
- [ ] Add English names for ICD-10-GM and CHOP codes to the index.
- [ ] Improve example retrieval (for example, evaluate embeddings against BM25).
- [ ] Write and validate more SPHN examples in the SIB `sparql-examples` format.

### Guard and judge

- [ ] Encode the gray-zone decisions after organizer answers arrive.
- [ ] Add judge style rules for code-pattern inference, `COUNT(DISTINCT ?g)`, and pattern order.
- [ ] Prevent known Virtuoso-only query errors through prompts and style rules, without database access in the request pipeline.
- [ ] Add query performance rewrites.
- [ ] Add more trap tests.

### Evaluation and demo

- [ ] Grow the labeled evaluation set to 80+ questions, including paraphrases, prompt-injection attempts, and gray zones.
- [ ] Run evaluation questions in parallel.
- [ ] Store reference results for correctness checks.
- [ ] Build a 10-run timing harness on the 100,000-patient performance database.
- [ ] Polish the demo and prepare final numbers for the report and video.

### Organizer questions

- [ ] Confirm judging deployment hardware and how the team can keep GPU access.
- [ ] Confirm whether result correctness is evaluated, or only verdicts and timings.
- [ ] Resolve whether only `SELECT` is accepted.
- [ ] Confirm query timing method, cache conditions, timeout, and dataset size.
- [ ] Resolve gray zones: fine-grained grouping, `MIN`/`MAX` that may identify a patient, and minimum group size.
- [ ] Confirm whether adversarial requests will be included and whether requests are English-only.
- [ ] Confirm conventions for interpreting requests such as “diagnosed with X”.
- [ ] Ask whether additional SPHN example queries are available.

## Submission checklist (deadline: Sunday 18 October 2026, 11:00)

- [ ] Push the code to the official `NW_26_<Company>_<Team>` repository and ensure collaborators are invited.
- [ ] Provide the jury README: overview, run instructions, dependencies, and every member's full name and role.
- [ ] Confirm `pip install -r requirements.txt` works in a clean environment and `python -m nl2sparql` starts without debugging.
- [ ] Write the report (maximum 3 pages): approach, architecture and rationale, results and figures, challenges and failed ideas, and disclosure of AI-agent/LLM coding-assistant use.
- [ ] Produce and watch the video (maximum 2 minutes) showing the goal, relevance, key results, and demo; share it by SwissTransfer.
- [ ] Credit external sources and check that no restricted terminology, derived assets, patient data, secrets, or GPU host details are included.
- [ ] Prepare a 5-minute presentation in case the team wins.

## Notes

- The base pipeline plan marks all 13 implementation steps complete. The unchecked tasks above are follow-up quality work and submission work from the README, not evidence that the base implementation is missing.
- The terminology index is generated under `assets/`, which is restricted and must not be committed. Build it only in the appropriate local environment and keep the output out of Git.
- Run `python -m pytest -q` before submission, as required by `AGENTS.md`.
