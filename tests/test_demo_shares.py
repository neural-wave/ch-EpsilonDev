"""Hosted demo snapshots use only authoritative released rows and expire."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from nl2sparql.api import create_app
from nl2sparql.config import Settings
from nl2sparql.demo_results import process_results
from nl2sparql.demo_shares import ShareError, ShareStore
from nl2sparql.virtuoso import SelectResult
from test_demo_results import COUNT
from test_pipeline_and_api import ScriptedQueryAgent, pipeline_with


def released(threshold=None):
    return process_results(COUNT,['patients'],[{'patients':'12'}],threshold)


def test_no_database_created_until_explicit_publish(tmp_path):
    path = tmp_path/'shares.sqlite3'
    store = ShareStore(path,60)
    token = store.stage('Synthetic question',released())
    assert token and not path.exists()
    link = store.publish(token,None)
    assert path.exists() and len(link['share_id']) == 43
    assert store.publish(token,None) == link
    result = store.get(link['share_id'],None)
    assert result['snapshot']['rows'] == [['12']]
    assert result['snapshot']['question'] == 'Synthetic question'
    assert set(result['snapshot']) == {'format_version','question','columns','rows','column_metadata','captured_at','truncated','row_limit','suppression'}
    assert 'sparql' not in json.dumps(result).lower()
    assert 'reproducibility' not in result['snapshot']
    reopened = ShareStore(path,60)
    assert reopened.get(link['share_id'],None) == result


def test_expiry_and_policy_changes_do_not_release_rows(tmp_path):
    now = [1000000000]
    store = ShareStore(tmp_path/'shares.sqlite3',60,clock=lambda:now[0])
    token = store.stage('Synthetic question',released(5))
    link = store.publish(token,5)
    with pytest.raises(ShareError) as error:
        store.get(link['share_id'],6)
    assert error.value.status == 409
    with pytest.raises(ShareError):
        store.publish(token,6)
    now[0] += 61
    with pytest.raises(ShareError) as error:
        store.get(link['share_id'],5)
    assert error.value.status == 404
    with sqlite3.connect(store.path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == 0
    now[0] += 3600
    with pytest.raises(ShareError):
        store.publish(token,5)


def test_no_withheld_or_oversized_drafts_and_bounded_capacity(tmp_path,monkeypatch):
    store = ShareStore(tmp_path/'shares.sqlite3',60)
    assert store.stage('q',{'result_status':'withheld','rows':None}) is None
    monkeypatch.setattr('nl2sparql.demo_shares.MAX_SNAPSHOT_BYTES',5)
    assert store.stage('q',released()) is None
    monkeypatch.setattr('nl2sparql.demo_shares.MAX_SNAPSHOT_BYTES',2097152)
    monkeypatch.setattr('nl2sparql.demo_shares.MAX_DRAFTS',1)
    first = store.stage('a',released())
    second = store.stage('b',released())
    with pytest.raises(ShareError): store.publish(first,None)
    store.publish(second,None)
    monkeypatch.setattr('nl2sparql.demo_shares.MAX_SHARES',1)
    third = store.stage('c',released())
    with pytest.raises(ShareError) as error: store.publish(third,None)
    assert error.value.status == 503


def test_share_api_cannot_accept_client_rows_and_reads_never_execute(tmp_path,monkeypatch):
    async def select(self,query):
        return SelectResult(['patients'],[{'patients':'12'}],1)
    monkeypatch.setattr('nl2sparql.api.VirtuosoClient.select',select)
    settings = Settings(demo_share_db_path=tmp_path/'shares.sqlite3',log_path=None,demo_show_details=False)
    client = TestClient(create_app(pipeline_with(ScriptedQueryAgent(COUNT)),settings))
    reply = client.post('/demo/ask',json={'text':'How many patients have a billed diagnosis?'}).json()
    assert reply['result_status'] == 'released' and reply['share_token']
    assert reply['sparql_query'] is None and 'sparql_query' not in reply['reproducibility']
    assert not settings.demo_share_db_path.exists()
    for body in [{'snapshot':{'rows':[['1']]}},{'share_token':reply['share_token'],'rows':[['1']]},[],{'share_token':5}]:
        assert client.post('/demo/shares',json=body).status_code == 400
    created = client.post('/demo/shares',json={'share_token':reply['share_token']})
    assert created.status_code == 200
    assert created.headers['cache-control'] == 'no-store'
    async def no_execution(*args): raise AssertionError('Shared links never rerun a query')
    monkeypatch.setattr('nl2sparql.api.VirtuosoClient.select',no_execution)
    data = client.get('/demo/shares/' + created.json()['share_id'])
    assert data.status_code == 200 and data.json()['snapshot']['rows'] == [['12']]
    assert 'reproducibility' not in data.json()['snapshot'] and 'sparql_query' not in data.json()['snapshot']
    assert data.headers['referrer-policy'] == 'no-referrer'
    assert client.get('/demo/shares/' + 'x'*43).status_code == 404
    assert client.post('/demo/shares',content='not json').status_code == 400
    assert client.post('/demo/shares',content='x'*2049).status_code == 400


def test_withheld_and_forbidden_requests_have_no_share_token(tmp_path,monkeypatch):
    async def select(self,query): return SelectResult(['patients'],[{'patients':'2'}],1)
    monkeypatch.setattr('nl2sparql.api.VirtuosoClient.select',select)
    settings = Settings(demo_min_group_size=5,demo_share_db_path=tmp_path/'shares.sqlite3')
    client = TestClient(create_app(pipeline_with(ScriptedQueryAgent(COUNT)),settings))
    answer = client.post('/demo/ask',json={'text':'How many patients have a billed diagnosis?'}).json()
    assert answer['result_status'] == 'withheld' and 'share_token' not in answer
    assert not settings.demo_share_db_path.exists()
    from nl2sparql.models import IntentFlags
    from test_pipeline_and_api import FixedIntentAgent
    pipeline = pipeline_with(ScriptedQueryAgent(COUNT))
    pipeline.intent_agent = FixedIntentAgent(IntentFlags(single_patient=True))
    async def never_execute(*args): raise AssertionError('Forbidden requests never execute')
    monkeypatch.setattr('nl2sparql.api.VirtuosoClient.select',never_execute)
    client = TestClient(create_app(pipeline,settings))
    forbidden = client.post('/demo/ask',json={'text':'How many diagnoses does patient 0042 have?'}).json()
    assert not forbidden['is_compliant'] and not forbidden['executed'] and 'share_token' not in forbidden


def test_share_failure_does_not_change_successful_execution(tmp_path,monkeypatch):
    async def select(self,query): return SelectResult(['patients'],[{'patients':'12'}],1)
    monkeypatch.setattr('nl2sparql.api.VirtuosoClient.select',select)
    def fail(*args): raise OSError('Internal details must not reach the client')
    monkeypatch.setattr('nl2sparql.api.ShareStore.stage',fail)
    client = TestClient(create_app(pipeline_with(ScriptedQueryAgent(COUNT)),Settings(demo_share_db_path=tmp_path/'s.sqlite3',demo_show_details=False)))
    data = client.post('/demo/ask',json={'text':'How many patients have a billed diagnosis?'}).json()
    assert data['result_status'] == 'released' and data['executed'] and data['rows']
    assert data['sharing_error'] == 'Sharing is temporarily unavailable.'
    assert data['sparql_query'] is None and 'sparql_query' not in data['reproducibility']


@pytest.mark.parametrize('ttl',[0,-1,True])
def test_share_ttl_must_be_positive(tmp_path,ttl):
    with pytest.raises(ValueError): ShareStore(tmp_path/'s.sqlite3',ttl)


def test_judged_endpoint_does_not_open_share_database(tmp_path,monkeypatch):
    def no_connect(*args): raise AssertionError('No SQLite in the judged endpoint')
    monkeypatch.setattr('nl2sparql.demo_shares.sqlite3.connect',no_connect)
    settings = Settings(demo_share_db_path=tmp_path/'s.sqlite3')
    client = TestClient(create_app(pipeline_with(ScriptedQueryAgent(COUNT)),settings))
    assert client.post('/nl-to-sparql',json={'text':'How many patients have a billed diagnosis?'}).json()['is_compliant']
