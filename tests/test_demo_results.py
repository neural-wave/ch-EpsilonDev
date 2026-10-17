"""Executed demo results: semantic metadata, whole-result privacy, and API wiring."""
import pytest
from fastapi.testclient import TestClient

from nl2sparql.config import Settings
from nl2sparql.demo_results import column_metadata, process_results
from nl2sparql.api import create_app
from nl2sparql.virtuoso import SelectResult
from test_pipeline_and_api import pipeline_with, ScriptedQueryAgent

PREFIX = 'PREFIX sphn: <https://biomedit.ch/rdf/sphn-schema/sphn#> '
COUNT = PREFIX + 'SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?s a sphn:BilledDiagnosis } }'
GROUP = PREFIX + 'SELECT ?sex (COUNT(DISTINCT ?g) AS ?patients) WHERE { GRAPH ?g { ?s a sphn:BilledDiagnosis ; sphn:hasName ?sex } } GROUP BY ?sex'


@pytest.mark.parametrize('value,expected', [(None,None), ('',None), ('DISABLED',None), ('5',5), ('1',1)])
def test_policy_config(monkeypatch, value, expected):
    monkeypatch.delenv('DEMO_MIN_GROUP_SIZE', raising=False)
    if value is not None:
        monkeypatch.setenv('DEMO_MIN_GROUP_SIZE',value)
    assert Settings.from_env().demo_min_group_size == expected


@pytest.mark.parametrize('value', ['0','-1','oops','1.5'])
def test_invalid_policy_config(monkeypatch, value):
    monkeypatch.setenv('DEMO_MIN_GROUP_SIZE',value)
    with pytest.raises(ValueError, match='DEMO_MIN_GROUP_SIZE'):
        Settings.from_env()


def test_metadata_resolves_projected_samples_and_counts():
    assert [m['role'] for m in column_metadata(GROUP, ['sex','patients'])] == ['category','patient_count']
    assert column_metadata(COUNT, ['unmatched'])[0]['role'] == 'unknown'
    for operator in ['SUM','AVG','MIN','MAX']:
        q = COUNT.replace('COUNT(DISTINCT ?g)', f'{operator}(?s)')
        assert column_metadata(q,['patients'])[0]['role'] == 'numeric_measure'
    assert column_metadata(COUNT.replace('COUNT(DISTINCT ?g)', 'COUNT(?g)'), ['patients'])[0]['role'] == 'numeric_measure'
    assert column_metadata(COUNT.replace('COUNT(DISTINCT ?g)', 'COUNT(DISTINCT ?g) + 1'), ['patients'])[0]['role'] == 'unknown'


def test_subquery_and_exists_bindings_are_not_patient_count_evidence():
    for body in ['{ SELECT ?g WHERE { GRAPH ?g { ?s a sphn:BilledDiagnosis } } }',
                 'GRAPH ?other { ?s a sphn:BilledDiagnosis } FILTER EXISTS { GRAPH ?g { ?s a sphn:BilledDiagnosis } }']:
        q = PREFIX + 'SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE { ' + body + ' }'
        assert column_metadata(q, ['patients'])[0]['role'] != 'patient_count'


@pytest.mark.parametrize('value', ['0','4','-1','3.2','NaN','Infinity','oops',None,''])
def test_withholds_small_or_unverifiable_counts(value):
    result = process_results(COUNT,['patients'],[{'patients':value}],5)
    assert result['result_status'] == 'withheld'
    assert result['rows'] is None and result['columns'] is None
    assert 'truncated' not in result


def test_threshold_and_total_subgroup_check_before_truncation():
    rows = [{'sex':str(i),'patients':'5'} for i in range(201)]
    result = process_results(GROUP,['sex','patients'],rows,5)
    assert result['result_status'] == 'released' and result['truncated'] and len(result['rows']) == 200
    assert not process_results(GROUP,['sex','patients'],rows[:200],5)['truncated']
    rows[-1]['patients'] = '4'
    assert process_results(GROUP,['sex','patients'],rows,5)['result_status'] == 'withheld'
    assert process_results(GROUP,['sex','patients'],[{'sex':'total','patients':'100'},{'sex':'subgroup','patients':'4'}],5)['rows'] is None


def test_disabled_and_unsupported_empty_results():
    rows = [{'patients':'1'}]
    assert process_results(COUNT,['patients'],rows,None)['rows'] == rows
    assert process_results(COUNT,['patients'],[],5)['rows'] == []
    assert process_results(COUNT.replace('COUNT(DISTINCT ?g)','AVG(?s)'),['patients'],[],5)['result_status'] == 'withheld'


def test_demo_wiring_executes_then_withholds(monkeypatch):
    async def select(self, query):
        return SelectResult(['patients'], [{'patients':'3'}], 1)
    monkeypatch.setattr('nl2sparql.api.VirtuosoClient.select', select)
    settings = Settings(demo_min_group_size=5, log_path=None)
    client = TestClient(create_app(pipeline_with(ScriptedQueryAgent(COUNT)),settings))
    assert client.get('/demo/config').json() == {'demo_min_group_size':5,'row_limit':200,'policy_version':1}
    result = client.post('/demo/ask',json={'text':'How many patients have a billed diagnosis?'}).json()
    assert result['is_compliant'] and result['executed']
    assert result['result_status'] == 'withheld' and result['rows'] is None


def test_alias_and_numeric_looking_category():
    query = GROUP.replace('?sex (COUNT', '(?sex AS ?code) (COUNT')
    metadata = column_metadata(query,['code','patients'])
    assert [m['role'] for m in metadata] == ['category','patient_count']
    result = process_results(query,['code','patients'],[{'code':'00.01','patients':'5'}],5)
    assert result['result_status'] == 'released'


def test_sample_expression_and_repeated_subquery_aggregate_names():
    sample = COUNT.replace('COUNT(DISTINCT ?g)', 'SAMPLE(STR(?s))')
    assert column_metadata(sample,['patients'])[0]['role'] == 'unknown'
    query = PREFIX + '''SELECT (COUNT(DISTINCT ?g) AS ?patients) (SUM(?inner) AS ?total)
    WHERE { GRAPH ?g { ?s a sphn:BilledDiagnosis }
      { SELECT (COUNT(DISTINCT ?g) AS ?inner) WHERE { GRAPH ?g { ?s a sphn:BilledDiagnosis } } }
    }'''
    assert [m['role'] for m in column_metadata(query,['patients','total'])] == ['patient_count','numeric_measure']


def test_judged_endpoint_never_executes_and_forbidden_demo_stays_unexecuted(monkeypatch):
    async def forbidden_select(*args, **kwargs):
        raise AssertionError('Database must not be reached')
    monkeypatch.setattr('nl2sparql.api.VirtuosoClient.select',forbidden_select)
    client = TestClient(create_app(pipeline_with(ScriptedQueryAgent(COUNT))))
    result = client.post('/nl-to-sparql',json={'text':'How many patients have a billed diagnosis?'}).json()
    assert result['is_compliant']
    from nl2sparql.models import IntentFlags
    from test_pipeline_and_api import FixedIntentAgent
    pipeline = pipeline_with(ScriptedQueryAgent(COUNT))
    pipeline.intent_agent = FixedIntentAgent(IntentFlags(single_patient=True))
    client = TestClient(create_app(pipeline))
    result = client.post('/demo/ask',json={'text':'How many diagnoses does patient 0042 have?'}).json()
    assert not result['is_compliant'] and not result['executed']
    assert result['result_status'] == 'not_executed' and result['sparql_query'] is None


def test_ontology_only_graph_is_not_a_verified_patient_count():
    query = PREFIX + '''SELECT (COUNT(DISTINCT ?g) AS ?patients) WHERE {
      GRAPH ?g { ?class <http://www.w3.org/2000/01/rdf-schema#subClassOf> sphn:Birth }
    }'''
    assert column_metadata(query,['patients'])[0]['role'] == 'numeric_measure'


def test_group_by_expr_without_as_unknown():
    base = PREFIX + '''SELECT {projection} (COUNT(DISTINCT ?g) AS ?patients)
    WHERE {{ GRAPH ?g {{ ?s a sphn:Birth ; sphn:hasStartDateTime ?d }} }} GROUP BY {group}'''
    query = base.format(projection='?d', group='(YEAR(?d))')
    rows = [{'d':'2000-01-01T00:00:00Z','patients':'5'}]
    assert column_metadata(query,['d','patients'])[0]['role'] == 'unknown'
    withheld = process_results(query,['d','patients'],rows,5)
    assert withheld['result_status'] == 'withheld'
    assert 'truncated' not in withheld and 'row_limit' not in withheld
    released = process_results(query,['d','patients'],rows,None)
    assert released['result_status'] == 'released' and released['rows'] == rows
    query = base.format(projection='?year', group='(YEAR(?d) AS ?year)')
    assert column_metadata(query,['year','patients'])[0]['role'] == 'category'
    assert process_results(query,['year','patients'],[{'year':'2000','patients':'5'}],5)['result_status'] == 'released'


@pytest.mark.parametrize('threshold', [True, False, 0, -1, 1.5, 'secret-invalid-setting'])
def test_demo_config_runtime_failure_is_sanitized(threshold):
    client = TestClient(create_app(pipeline_with(ScriptedQueryAgent(COUNT)),Settings(demo_min_group_size=threshold)))
    response = client.get('/demo/config')
    assert response.status_code == 503
    assert response.json() == {'error':'Demo privacy configuration unavailable.'}


@pytest.mark.parametrize('name,value', [('ROW_LIMIT','200'),('POLICY_VERSION',True)])
def test_demo_config_rejects_invalid_runtime_metadata(monkeypatch,name,value):
    monkeypatch.setattr('nl2sparql.api.' + name,value)
    client = TestClient(create_app(pipeline_with(ScriptedQueryAgent(COUNT))))
    response = client.get('/demo/config')
    assert response.status_code == 503
    assert response.json() == {'error':'Demo privacy configuration unavailable.'}


@pytest.mark.parametrize('suffix', ['NAME','VERSION','DESCRIPTION'])
def test_dataset_configuration_trims_and_maps_blanks(monkeypatch,suffix):
    key = 'DEMO_DATASET_' + suffix
    attribute = 'demo_dataset_' + suffix.lower()
    monkeypatch.delenv(key,raising=False)
    assert getattr(Settings.from_env(),attribute) is None
    for value in ['', ' \t\n ']:
        monkeypatch.setenv(key,value)
        assert getattr(Settings.from_env(),attribute) is None
    monkeypatch.setenv(key,'  synthetic release X  ')
    assert getattr(Settings.from_env(),attribute) == 'synthetic release X'


@pytest.mark.parametrize('configured', [False,True])
def test_reproducibility_preserves_question_final_query_and_pre_execution_time(monkeypatch,tmp_path,configured):
    from datetime import datetime,timezone
    events = []
    frozen = datetime(2026,10,17,17,0,0,tzinfo=timezone.utc)
    class Clock:
        @staticmethod
        def now(zone):
            assert zone is timezone.utc
            events.append('timestamp')
            return frozen
    monkeypatch.setattr('nl2sparql.api.datetime',Clock)
    received = []
    async def select(self,query):
        assert events == ['timestamp']
        events.append('execute')
        received.append(query)
        return SelectResult(['patients'],[{'patients':'12'}],1)
    monkeypatch.setattr('nl2sparql.api.VirtuosoClient.select',select)
    options = dict(demo_dataset_name='Synthetic test dataset',demo_dataset_version='test-v1',demo_dataset_description='Synthetic fixtures') if configured else {}
    settings = Settings(demo_share_db_path=tmp_path/'shares.sqlite3',log_path=None,**options)
    # The judge repairs this standard prefix before execution; provenance must keep the repaired query.
    original_query = COUNT.replace('https://biomedit.ch/rdf/sphn-schema/sphn#','urn:wrong#')
    client = TestClient(create_app(pipeline_with(ScriptedQueryAgent(original_query)),settings))
    question = '  How many patients have a billed diagnosis?\n café µ  '
    reply = client.post('/demo/ask',json={'text':question}).json()
    assert reply['result_status'] == 'released'
    record = reply['reproducibility']
    assert record['question'] == question
    assert record['executed_at'] == frozen.isoformat()
    assert record['source'] == 'database'
    assert reply['sparql_query'] is None
    assert 'sparql_query' not in record
    assert reply['diagnostics'] == {}
    assert len(received) == 1
    assert received[0] != original_query and 'urn:wrong#' not in received[0]
    expected = {'name':'Synthetic test dataset','version':'test-v1','description':'Synthetic fixtures'} if configured else {'name':None,'version':None,'description':None}
    assert record['dataset'] == expected
    assert client.get('/demo/config').json() == {'demo_min_group_size':None,'row_limit':200,'policy_version':1}


@pytest.mark.parametrize('outcome',['withheld','forbidden','error','empty'])
def test_provenance_is_null_unless_execution_releases_result(monkeypatch,tmp_path,outcome):
    from nl2sparql.models import IntentFlags
    from test_pipeline_and_api import FixedIntentAgent
    async def select(self,query):
        assert outcome != 'forbidden'
        if outcome == 'error': raise RuntimeError('Synthetic database error: ' + query)
        return SelectResult(['patients'],[] if outcome == 'empty' else [{'patients':'2'}],1)
    monkeypatch.setattr('nl2sparql.api.VirtuosoClient.select',select)
    settings = Settings(demo_min_group_size=5,demo_share_db_path=tmp_path/'s.sqlite3')
    pipeline = pipeline_with(ScriptedQueryAgent(COUNT))
    if outcome == 'forbidden': pipeline.intent_agent = FixedIntentAgent(IntentFlags(single_patient=True))
    client = TestClient(create_app(pipeline,settings))
    data = client.post('/demo/ask',json={'text':'How many patients have a billed diagnosis?'}).json()
    assert data['sparql_query'] is None
    assert COUNT not in str(data)
    if outcome == 'error':
        assert data['execution_error'] == 'The database result could not be retrieved.'
    if outcome == 'empty':
        assert data['result_status'] == 'released' and data['rows'] == []
        assert data['reproducibility']['source'] == 'database'
    else:
        assert data['reproducibility'] is None


def test_released_rows_only_contain_projected_fields():
    rows = [{'patients':'12','patient_id':'synthetic-secret-id','query':'internal-only'}]
    result = process_results(COUNT,['patients'],rows,5)
    assert result['rows'] == [{'patients':'12'}]
    assert rows[0]['patient_id'] == 'synthetic-secret-id'


@pytest.mark.parametrize('threshold',[None,0,-1,True])
def test_public_mode_requires_explicit_minimum_policy(threshold):
    with pytest.raises(ValueError,match='DEMO_PUBLIC_MODE'):
        create_app(pipeline_with(ScriptedQueryAgent(COUNT)),Settings(demo_public_mode=True,demo_min_group_size=threshold))


def test_public_console_blocks_developer_endpoints_and_request_archives(monkeypatch,tmp_path):
    from nl2sparql.orchestrator import PipelineError
    settings = Settings(demo_public_mode=True,demo_min_group_size=5,log_path=tmp_path/'private.jsonl')
    pipeline = pipeline_with(ScriptedQueryAgent(COUNT))
    pipeline.log_path = settings.log_path
    client = TestClient(create_app(pipeline,settings))
    assert pipeline.log_path is None
    for path in ['/nl-to-sparql','/agents','/agents/config','/agents/preview','/openapi.json','/docs']:
        for method in ['get','post']:
            response = getattr(client,method)(path)
            assert response.status_code == 404
            assert response.json() == {'error':'Not found.'}
    assert client.get('/demo/config').status_code == 200
    static = client.get('/console/cli/')
    assert static.headers['cache-control'] == 'no-store'
    assert static.headers['referrer-policy'] == 'no-referrer'
    async def fail(text):
        raise PipelineError('internal query and secret connection details')
    monkeypatch.setattr(pipeline,'run',fail)
    error = client.post('/demo/ask',json={'text':'Count patients'})
    assert error.status_code == 503
    assert error.json() == {'error':'The request could not be processed.'}
    assert not settings.log_path.exists()


def test_public_mode_env(monkeypatch):
    monkeypatch.setenv('DEMO_PUBLIC_MODE','true')
    assert Settings.from_env().demo_public_mode is True


def test_demo_refusal_never_echoes_query_diagnostics_or_model_suggestions(monkeypatch):
    from types import SimpleNamespace
    pipeline = pipeline_with(ScriptedQueryAgent(COUNT))
    secret = 'private-evidence-never-display'
    async def refuse(text):
        return SimpleNamespace(sparql_query=COUNT,tokens_consumed=1,is_compliant=False,
                               compliance_reason=secret,suggested_rephrase=secret,
                               timings_ms={},tokens_by_step={},diagnostics={'first_try_sparql':COUNT,'detail':secret})
    monkeypatch.setattr(pipeline,'run',refuse)
    client = TestClient(create_app(pipeline,Settings(log_path=None)))
    response = client.post('/demo/ask',json={'text':'A forbidden request'})
    assert response.status_code == 200
    assert secret not in response.text and COUNT not in response.text
    data = response.json()
    assert data['sparql_query'] is None and data['diagnostics'] == {}
    assert data['suggested_rephrase'] is None and not data['executed']
    assert data['reproducibility'] is None
    # The original judged contract remains available on the separate developer server.
    judged = client.post('/nl-to-sparql',json={'text':'A forbidden request'}).json()
    assert judged['sparql_query'] == COUNT and judged['compliance_reason'] == secret
