"""Opt-in demo verification against existing dev services; never starts infrastructure.

PYTHONPATH=src python eval/validate_demo_results.py --execute
PYTHONPATH=src python eval/validate_demo_results.py --execute --api-url http://localhost:8080 --browser

LIVE SETUP REQUIRED: complete the private .env from .env.example with the actual running
open-source model and dev Virtuoso connection settings, export them into the environment,
and start the demo API using those settings. Pass that API's URL with --api-url. This script
does not fabricate a model, load credentials automatically, or provision any services.
The remaining verification must be completed with those actual services/data; PENDING is not PASS.

Only sanitized statuses enter the report. Executed aggregate rows remain in memory and, for
browser verification, pass to the local Node helper over stdin rather than a tracked file.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import subprocess

import httpx

from nl2sparql.config import REPO_ROOT, Settings
from nl2sparql.demo_results import process_results
from nl2sparql.virtuoso import VirtuosoClient

PREFIX = '''PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#>
PREFIX icd-10-gm: <https://biomedit.ch/rdf/sphn-resource/icd-10-gm/>
PREFIX snomed: <http://snomed.info/id/>
'''
DIAGNOSIS = '''GRAPH ?g {
  ?dx a sphn:BilledDiagnosis ; sphn:hasCode ?code .
  ?code a icd-10-gm:A00 OPTION (inference "https://biomedit.ch/rdf/sphn-resource/global") .
}'''
SEX = '''GRAPH ?g {
  ?record a sphn:AdministrativeSex ; sphn:hasCode ?sex_code .
  ?sex_code a ?sex_concept .
}
VALUES (?sex_concept ?sex) {
  (snomed:248153007 "male")
  (snomed:248152002 "female")
  (snomed:32570681000036106 "indeterminate")
}'''
CASES = [
    dict(name="patient_count", columns=["patients"], roles=["patient_count"],
         question="How many patients have a billed diagnosis of cholera?",
         query=PREFIX + 'SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE { ' + DIAGNOSIS + ' }'),
    dict(name="cholera_by_sex", columns=["sex", "patients"], roles=["category", "patient_count"],
         question="How many patients have a billed diagnosis of cholera, by sex?",
         query=PREFIX + 'SELECT ?sex (COUNT(DISTINCT ?g) AS ?patients) WHERE { ' + SEX + DIAGNOSIS + ' } GROUP BY ?sex ORDER BY ?sex'),
    dict(name="two_cohorts_by_sex", columns=["sex", "cohort", "patients"], roles=["category", "category", "patient_count"],
         question="Compare patient counts with and without a billed diagnosis of cholera, grouped by sex.",
         query=PREFIX + 'SELECT ?sex ?cohort (COUNT(DISTINCT ?g) AS ?patients) WHERE { ' + SEX +
         ' BIND(IF(EXISTS { ' + DIAGNOSIS + ' }, "cholera", "no cholera") AS ?cohort) } GROUP BY ?sex ?cohort ORDER BY ?sex ?cohort'),
]

# A second comparison exercises the example requested by researchers, without changing prompts.
CASES[-1]['cohort_labels'] = ['cholera', 'no cholera']
CASES[-1]['question'] += " Label the cohorts 'cholera' and 'no cholera'."
_diabetes = DIAGNOSIS.replace('icd-10-gm:A00', 'icd-10-gm:E11')
CASES.append(dict(
    name="two_cohorts_diabetes_by_sex", columns=["sex", "cohort", "patients"],
    roles=["category", "category", "patient_count"], cohort_labels=['diabetes','no diabetes'],
    question="Compare distinct patient counts with and without a billed diagnosis of type 2 diabetes, grouped by sex. Label the cohorts 'diabetes' and 'no diabetes'.",
    query=PREFIX + 'SELECT ?sex ?cohort (COUNT(DISTINCT ?g) AS ?patients) WHERE { ' + SEX +
          ' BIND(IF(EXISTS { ' + _diabetes + ' }, "diabetes", "no diabetes") AS ?cohort) } GROUP BY ?sex ?cohort ORDER BY ?sex ?cohort',
))


def comparison_counts(case, columns, rows, metadata):
    """Canonicalize supported long/wide comparisons; never invent missing groups or zeros."""
    labels = case['cohort_labels']
    sex_values = {'male','female','indeterminate'}
    assert len(metadata) == len(columns)
    categories = [i for i,m in enumerate(metadata) if m['role'] == 'category']
    measures = [i for i,m in enumerate(metadata) if m['role'] == 'patient_count' or
                m['role'] == 'numeric_measure' and m['kind'] == 'count']
    cells = [[row.get(c) if isinstance(row,dict) else row[i] for i,c in enumerate(columns)] for row in rows]
    assert len(columns) == 3
    sex = next((i for i in categories if columns[i].lower() == 'sex'), None)
    if sex is None:
        candidates = [i for i in categories if cells and all(str(r[i]).lower() in sex_values for r in cells)]
        assert len(candidates) == 1
        sex = candidates[0]
    result = {}

    def add(key, value):
        count = Decimal(str(value))
        assert count.is_finite() and count >= 0 and count == count.to_integral_value()
        assert key[0] in sex_values and key[1] in labels and key not in result
        result[key] = int(count)

    if len(categories) == 2 and len(measures) == 1:
        cohort = next(i for i in categories if i != sex)
        for row in cells:
            add((str(row[sex]).lower(),str(row[cohort]).lower()),row[measures[0]])
    elif len(categories) == 1 and len(measures) == 2:
        normalized = {label.replace(' ','_'):label for label in labels}
        for i in measures:
            name = columns[i].lower()
            for suffix in ('_count','_patients'):
                if name.endswith(suffix): name = name[:-len(suffix)]
            assert name in normalized
            for row in cells:
                if row[i] is not None and str(row[i]) != '':
                    add((str(row[sex]).lower(),normalized[name]),row[i])
    else:
        raise AssertionError('Unsupported generated comparison shape')
    return result


def verify(case, columns, rows, threshold=5):
    """Check processing against complete source rows with an independent threshold oracle."""
    assert columns == case["columns"]
    released = process_results(case["query"], columns, rows, None)
    assert [m["role"] for m in released["column_metadata"]] == case["roles"]
    assert released["rows"] == rows[:200] and released["columns"] == columns
    assert released["truncated"] == (len(rows) > 200) and released["row_limit"] == 200
    safe = True
    for row in rows:
        try:
            count = Decimal(str(row.get("patients", "")))
            safe = safe and count.is_finite() and count >= threshold and count == count.to_integral_value()
        except (InvalidOperation, ValueError):
            safe = False
    checked = process_results(case["query"], columns, rows, threshold)
    assert checked["result_status"] == ("released" if safe else "withheld")
    if not safe:
        assert checked["rows"] is None and checked["columns"] is None
        assert "truncated" not in checked and "row_limit" not in checked
    return released


async def run(args):
    settings = Settings.from_env()
    entries, browser_cases = [], []
    cases = [case for case in CASES if not args.cases or case["name"] in args.cases]
    client = VirtuosoClient(settings.virtuoso_endpoint, timeout=args.timeout)
    async with httpx.AsyncClient(timeout=args.timeout) as http:
        policy = None
        model_ready = False
        if args.api_url:
            try:
                response = await http.get(args.api_url.rstrip('/') + '/demo/config')
                response.raise_for_status()
                candidate = response.json()
                threshold = candidate['demo_min_group_size']
                assert threshold is None or type(threshold) is int and threshold >= 1
                assert candidate['row_limit'] == 200 and candidate['policy_version'] == 1
                policy = candidate
                health = await http.get(args.api_url.rstrip('/') + '/health')
                health.raise_for_status()
                model_ready = health.json().get('backend') == 'llm'
            except Exception:
                entries.append(dict(check="api_policy", status="PENDING", reason="Demo API policy unavailable or invalid"))
        for case in cases:
            source = None
            try:
                source = await client.select(case["query"])
                verify(case, source.columns, source.rows)
                entries.append(dict(check=case["name"], status="PASS", reason="Reference rows and release policy agree"))
                if policy is not None:
                    released = process_results(case["query"], source.columns, source.rows, policy['demo_min_group_size'])
                    browser_cases.append(dict(name=case['name'], question=case['question'],
                                              response=dict(is_compliant=True, executed=True, **released)))
            except (httpx.ConnectError, httpx.ConnectTimeout):
                entries.append(dict(check=case["name"], status="PENDING", reason="Configured dev database unavailable"))
            except Exception:
                entries.append(dict(check=case["name"], status="FAIL", reason="Reference execution or processing assertion failed; no raw results recorded"))
            answer = None
            if args.api_url and model_ready:
                try:
                    response = await http.post(args.api_url.rstrip('/') + '/demo/ask', json={"text":case["question"]})
                    response.raise_for_status()
                    answer = response.json()
                    assert answer.get('is_compliant') is True and answer.get('executed') is True
                    assert answer.get('result_status') in {'released', 'withheld'}
                    entries.append(dict(check=case['name'] + '_translation', status="PASS", reason="Real-model question translated, executed, and processed"))
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    entries.append(dict(check=case['name'] + '_translation', status="PENDING", reason="Configured demo API unavailable"))
                except Exception:
                    entries.append(dict(check=case['name'] + '_translation', status="FAIL", reason="Real-model translation or execution failed; no raw results recorded"))
            else:
                entries.append(dict(check=case['name'] + '_translation', status="PENDING", reason="Requires --api-url for an authorized LLM-backed demo; stub results cannot verify real-model generation"))
            if case.get('cohort_labels'):
                check = dict(check=case['name'] + '_exact_counts',status="PENDING",reason="Requires real-model execution, reference rows, and a released comparison")
                if answer and source is not None and answer.get('result_status') == 'released':
                    try:
                        expected_meta = process_results(case['query'],source.columns,source.rows,None)['column_metadata']
                        expected = comparison_counts(case,source.columns,source.rows,expected_meta)
                        actual = comparison_counts(case,answer['columns'],answer['rows'],answer['column_metadata'])
                        assert actual == expected
                        if len({cohort for _,cohort in actual}) == 2:
                            check.update(status="PASS",reason="Real-model generated group counts exactly match the reference")
                            if args.browser:
                                fields = ('columns','rows','column_metadata','suppression','truncated','row_limit','result_status','is_compliant','executed')
                                browser_cases.append(dict(name=case['name'] + '_generated',question=case['question'],
                                                          response={key:answer[key] for key in fields if key in answer}))
                        else:
                            check['reason'] = "Data did not exercise both cohorts"
                    except Exception:
                        check.update(status="FAIL",reason="Generated comparison shape or group counts differ from the reference; no raw results recorded")
                entries.append(check)
        if args.browser and args.api_url and browser_cases:
            try:
                completed = subprocess.run(
                    ['node', str(REPO_ROOT / 'tests/browser/live-check.cjs'), args.api_url],
                    input=json.dumps(browser_cases), text=True, capture_output=True, timeout=args.timeout * 4,
                )
                if completed.returncode != 0:
                    raise RuntimeError
                entries.extend(json.loads(completed.stdout))
            except Exception:
                entries.append(dict(check="real_browser", status="FAIL", reason="Browser verification failed or browser tooling unavailable; no raw output recorded"))
        else:
            entries.append(dict(check="real_browser", status="PENDING", reason="Requires --browser, --api-url, browser tooling, and executed reference rows"))
    commit = subprocess.run(['git','rev-parse','HEAD'], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout.strip()
    report = dict(attempted_at=datetime.now(timezone.utc).isoformat(), commit=commit,
                  working_tree_note="Validate the recorded checkout; uncommitted changes may be present", checks=entries)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 1 if any(e['status'] == 'FAIL' for e in entries) else 2 if any(e['status'] == 'PENDING' for e in entries) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Explicitly authorize reference executions on configured dev Virtuoso')
    parser.add_argument('--api-url', help='Existing authorized demo API for separate NL translation checks')
    parser.add_argument('--browser', action='store_true', help='Also compare reference rows with the actual browser renderer')
    parser.add_argument('--case', dest='cases', action='append', choices=[c['name'] for c in CASES], help='Limit validation to selected reference cases (repeatable)')
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--report', type=Path, default=REPO_ROOT / 'eval/reports/demo-validation.json')
    args = parser.parse_args()
    if not args.execute:
        parser.error('--execute is required; no services were contacted')
    if args.browser and not args.api_url:
        parser.error('--browser requires --api-url')
    raise SystemExit(asyncio.run(run(args)))


if __name__ == '__main__':
    main()
