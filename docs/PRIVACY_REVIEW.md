# Demo privacy review and deployment boundary

Review date: 17 October 2026. This is a technical implementation review, not a certification of legal or clinical privacy compliance. Real model/database checks remain tracked in [RESULTS_VALIDATION.md](RESULTS_VALIDATION.md).

## Controls checked and hardened

- The demo returns no generated SPARQL in either top-level fields or provenance, and no retry diagnostics. Database and pipeline errors use sanitized messages. Refusals do not echo query fragments, model evidence, or suggested text.
- The card and copied record contain only the original question, execution timestamp, and configured dataset details. Old query-bearing history is stripped when reopened; console developer commands no longer retrieve context, configuration, raw responses, or reports.
- Released row dictionaries contain only the explicitly returned columns. Unexpected fields cannot reach the response, history, or hosted-share snapshot through a row dictionary.
- With an active minimum-group policy, all complete executed rows are inspected before truncation. Unsupported measures and invalid counts withhold the entire result. Small subgroups also withhold accompanying totals. Withheld values are not added to demo responses, history, exports, or share drafts.
- CSV/table exports use the released table. Snapshot imports are untrusted, escaped, never executed automatically, and cannot be re-exported. Snapshot files and hosted shares exclude provenance, queries, diagnostics, and connection details.
- Demo responses and console assets use `Cache-Control: no-store` and `Referrer-Policy: no-referrer`. This also avoids reusing an outdated cached query-disclosure UI after the updated server is restarted.
- Share publication is explicit and previews the question. Export/share confirmations remind the user to remove identifying or confidential wording. Browser history carries a shared-device notice.

## Public-console mode versus the jury/developer server

The original server exposes `/nl-to-sparql` and `/agents` for judging and team development. Those endpoints intentionally return implementation details and are not an end-user privacy boundary. The default remains unchanged so existing judged API behavior and tests remain valid.

For a separate end-user console process, set:

```dotenv
DEMO_PUBLIC_MODE=true
DEMO_MIN_GROUP_SIZE=<positive threshold approved by the organizers>
```

The threshold placeholder must be replaced with an integer. No threshold was selected or enabled automatically. Public mode refuses startup if the threshold is missing, disabled, invalid, or non-positive.

In this mode the application allows only `/`, `/health`, `/demo/*`, and `/console/*`. Requests to judged, agent, documentation, and other developer routes receive a sanitized HTTP 404 before dispatch. Structured pipeline request archives are disabled to avoid retaining original questions and generated queries. Demo responses still pass through the guard and result-processing policy. Run the judged API separately in normal mode for the jury; do not expose that separate process or the database directly to console users.

Public-console mode is **not authentication**. Configure authorized access, TLS, reverse-proxy routing, protected storage, and logging/retention at deployment. Do not forward the same public origin to an unrestricted developer process or enable debug tracebacks. Audit infrastructure/model-server logs separately; suppressing the application's request archive does not control external logging.

## Remaining limits and decisions

1. A count such as 412 in browser mock mode is a synthetic fixture. Its presence is not evidence of a real patient leak or of real-data accuracy. Synthetic preview labels must remain visible.
2. Small-group suppression is disabled in the default development configuration pending organizer policy. Default development mode is not a claim that small counts are protected. Public-console mode requires an explicit threshold.
3. Whole-result suppression limits subtraction within that result. Repeated or overlapping queries can still permit inference. Query budgeting, cross-query disclosure control, and differential privacy are not implemented.
4. A grouping label, original question, operator-written dataset description, or imported file may contain sensitive text. Escaping prevents markup execution; it does not certify anonymity. Only enter approved aggregate questions and dataset descriptions. The system cannot guarantee that free text is identifier-free.
5. Anyone holding an unlisted share link can access its released question/table until expiry. Links are not authenticated archives. Keep sharing behind the deployment access policy, review the confirmation, and do not circulate confidential questions.
6. Local history and downloads remain on the user's device; loading a new privacy policy cannot retract delivered information or copies. Clear history on shared devices and apply the research project's storage policy.
7. The guard and metadata analysis are conservative software checks with known supported shapes, not a proof against every re-identification strategy. Actual model/dev-database validation and organizer review remain required before a fully verified claim.

## Screenshot and restart check

The older screenshot contains “Show how this was calculated”; that control is absent from current source. Restart the API and hard-refresh with Cmd+Shift+R. Confirm there is no query section or Copy query button, and copied provenance has no `SPARQL:` section. The narrow interpretation paragraph was a legacy grid-style collision and is now rendered as a full-width paragraph.

Recorded regression verification on 17 October 2026 at 20:26 CEST: **788 Python, 13 Node, and 68 Chromium browser tests passed**. Browser fixtures were synthetic.

The subsequent copy-icon UI replaces the visible provenance card. Copied records also include the already released table/count; preview copies explicitly mark values illustrative and not for citation. Policy gating and snapshot/share exclusions are unchanged.
