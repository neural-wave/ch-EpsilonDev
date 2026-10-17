"""The optional real-data runner has deterministic tests without infrastructure."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

MODULE = Path(__file__).resolve().parents[1] / 'eval' / 'validate_demo_results.py'
spec = importlib.util.spec_from_file_location('demo_validation', MODULE)
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


@pytest.mark.parametrize('case', validation.CASES, ids=lambda c: c['name'])
def test_reference_shapes_are_supported_and_values_preserved(case):
    row = {column: '5' if role == 'patient_count' else 'synthetic category'
           for column, role in zip(case['columns'],case['roles'])}
    released = validation.verify(case,case['columns'],[row])
    assert released['rows'] == [row]


def test_runner_requires_explicit_execute_before_contacting_services():
    completed = subprocess.run([sys.executable,str(MODULE)],capture_output=True,text=True)
    assert completed.returncode == 2
    assert '--execute is required; no services were contacted' in completed.stderr


def test_oracle_detects_small_values_after_cap():
    case = validation.CASES[1]
    rows = [{'sex':str(i),'patients':'5'} for i in range(201)]
    rows[-1]['patients'] = '4'
    released = validation.verify(case,case['columns'],rows)
    assert released['truncated'] and len(released['rows']) == 200


def test_comparison_oracle_accepts_wide_and_long_but_never_fills_missing_pairs():
    case = validation.CASES[-1]
    columns = ['sex','cohort','patients']
    rows = [{'sex':'male','cohort':'diabetes','patients':'8'}, {'sex':'male','cohort':'no diabetes','patients':'20'}]
    meta = [{'role':'category'},{'role':'category'},{'role':'patient_count'}]
    expected = validation.comparison_counts(case,columns,rows,meta)
    wide = validation.comparison_counts(case,['sex','diabetes_count','no_diabetes_count'],
        [{'sex':'male','diabetes_count':'8','no_diabetes_count':'20'}],
        [{'role':'category'},{'role':'patient_count'},{'role':'numeric_measure','kind':'count'}])
    assert wide == expected
    assert validation.comparison_counts(case,columns,rows[:1],meta) != expected
    with pytest.raises(AssertionError): validation.comparison_counts(case,columns,rows + rows[:1],meta)


@pytest.mark.parametrize('backend,count,expected', [('stub','8','PENDING'),('llm','8','PASS'),('llm','9','FAIL')])
def test_real_comparison_cannot_pass_with_stub_or_wrong_numbers(tmp_path,monkeypatch,backend,count,expected):
    import asyncio
    import json
    from types import SimpleNamespace
    import httpx
    from nl2sparql.virtuoso import SelectResult
    case = validation.CASES[-1]
    source_rows = [{'sex':'male','cohort':'diabetes','patients':'8'},{'sex':'male','cohort':'no diabetes','patients':'20'}]
    class Database:
        def __init__(self,*args,**kwargs): pass
        async def select(self,query): return SelectResult(case['columns'],source_rows,1)
    monkeypatch.setattr(validation,'VirtuosoClient',Database)
    requests = []
    def respond(request):
        requests.append(request.url.path)
        if request.url.path == '/health': return httpx.Response(200,json={'backend':backend})
        if request.url.path == '/demo/config': return httpx.Response(200,json={'demo_min_group_size':None,'row_limit':200,'policy_version':1})
        if request.url.path == '/demo/ask':
            rows = [dict(source_rows[0],patients=count),source_rows[1]]
            result = validation.process_results(case['query'],case['columns'],rows,None)
            return httpx.Response(200,json=dict(is_compliant=True,executed=True,**result))
        raise AssertionError('Unexpected endpoint')
    original = httpx.AsyncClient
    monkeypatch.setattr(validation.httpx,'AsyncClient',lambda **kwargs: original(transport=httpx.MockTransport(respond),**kwargs))
    args = SimpleNamespace(timeout=2,cases=[case['name']],api_url='http://test',browser=False,report=tmp_path/'report.json')
    asyncio.run(validation.run(args))
    report = json.loads(args.report.read_text())
    check = next(c for c in report['checks'] if c['check'].endswith('_exact_counts'))
    assert check['status'] == expected
    assert ('/demo/ask' in requests) == (backend == 'llm')
    assert 'patients' not in args.report.read_text()
