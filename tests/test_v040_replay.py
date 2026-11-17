"""Synthetic histories made with the old ingest rules, then recovered with today's parser."""
import copy
import importlib
import json
import threading
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from caduceus import ops
from caduceus.core import db, jobs, search, sources
from caduceus.core.policy import Policy
from caduceus.health import apple, fhir


def _replay(vault, **kwargs):
    return importlib.import_module('caduceus.health.replay').replay(vault, **kwargs)


def _bundle(*resources):
    return {'resourceType': 'Bundle', 'entry': [{'resource': r} for r in resources]}


def _condition(rid='condition', name='Synthetic condition', **extra):
    return {'resourceType': 'Condition', 'id': rid, 'code': {'text': name},
            'recordedDate': '2020-01-02', 'clinicalStatus': {'coding': [{'code': 'active'}]}, **extra}


def _observation(rid='one', name='Synthetic alpha', coded=False, **extra):
    concept = {'coding': [{'system': 'synthetic', 'code': rid, 'display': name}]} if coded else {'text': name}
    return {'resourceType': 'Observation', 'id': rid, 'code': concept, 'status': 'final',
            'effectiveDateTime': '2020-01-01', 'valueQuantity': {'value': 5, 'unit': 'mg'}, **extra}


def _report(rid='report', kind='DiagnosticReport', **extra):
    return {'resourceType': kind, 'id': rid, 'status': 'final', 'code': {'text': 'Synthetic report'},
            'effectiveDateTime': '2020-01-01', 'date': '2020-01-01', 'started': '2020-01-01',
            'conclusion': 'Synthetic conclusion', **extra}


@pytest.fixture(autouse=True)
def _separate_run_times(monkeypatch):
    # Real timestamps have second precision; ordinary test histories must be strictly separated.
    ticks = iter(range(10000))
    monkeypatch.setattr(sources, 'now_iso', lambda: (
        datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=next(ticks))
    ).isoformat().replace('+00:00', 'Z'))


def _old_rules(patch):
    condition = fhir._condition
    patch.setattr(fhir, '_condition', lambda v, r, s, when, precision, counts, *a, **kw:
                  condition(v, r, s, when, precision, counts, *a, **kw) if when is not None else None)
    patch.setattr(fhir, '_observation_key', lambda system, code, when, num, text, unit, component, name:
                  fhir._key(['Observation', system, code, when, [num, text], unit, code if component else None]))
    patch.setattr(fhir, 'REPORT_TYPES', frozenset())
    patch.setattr(fhir, 'PARSER_VERSION', 'synthetic-old')


def _import(vault, tmp_path, monkeypatch, document, *, old=True, samples=None):
    ordinal = vault.execute('SELECT count(*) FROM source_artifact').fetchone()[0]
    path = tmp_path / f'input-{ordinal}.json'
    kind = 'fhir'
    if samples is not None:
        kind = 'apple_health'
        path = path.with_suffix('.zip')
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('apple_health_export/export.xml', '<HealthData><Record '
                             'type="HKQuantityTypeIdentifierStepCount" sourceName="Synthetic" '
                             'startDate="2020-01-01 10:00:00 +0000" value="4" unit="count"/></HealthData>')
            archive.writestr('apple_health_export/clinical-records/records.json', json.dumps(document))
    else:
        path.write_text(json.dumps(document))
    src = sources.store_source(vault, path, kind)[0]
    with monkeypatch.context() as patch:
        if old:
            _old_rules(patch)
        result = (fhir.import_fhir(vault, src) if samples is None else
                  apple.import_apple_health(samples, vault, src))
    assert result.status == 'succeeded'
    # Simulate opening a pre-upgrade vault with the already-existing uncoded-key migration.
    with db.write_txn(vault):
        vault.execute("DELETE FROM settings WHERE key='migration:fhir_uncoded_names_v1'")
    db._migrate_vault(vault)
    with db.write_txn(vault):
        vault.execute("DELETE FROM settings WHERE key='clinical_replay'")
    return src


def _rows(conn):
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                      "AND name NOT LIKE 'search_fts%' AND name NOT LIKE 'sqlite_%'")]
    return {name: sorted((tuple(row) for row in conn.execute(f'SELECT * FROM "{name}"')), key=repr)
            for name in names}


def _bytes(vault):
    vault.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    return (db.db_dir(vault) / 'vault.db').read_bytes()


def test_old_records_recovered_and_searchable(vault, tmp_path, monkeypatch):
    src = _import(vault, tmp_path, monkeypatch, _bundle(_condition(), _observation(),
                  _observation('two', 'Synthetic beta'), _report()))
    assert vault.execute('SELECT count(*) FROM observation').fetchone()[0] == 1
    assert vault.execute('SELECT count(*) FROM condition').fetchone()[0] == 0
    result = _replay(vault)
    assert result == {'status': 'done', 'sources_read': 1,
                      'added': {'condition': 1, 'observation': 1, 'report': 1}, 'skipped_sources': []}
    for table, kind, term in [('condition', 'condition', 'condition'), ('observation', 'observation', 'beta'),
                              ('clinical_report', 'report', 'conclusion')]:
        row = vault.execute(f'SELECT * FROM {table} ORDER BY id DESC LIMIT 1').fetchone()
        assert row['source_id'] == src.id
        assert json.loads(row['raw_json'])['_caduceus']['recovered_by'] == 'clinical_replay'
        hits = search.search(vault, term, policy=Policy())['results']
        assert any(h['kind'] == kind and h['category'] == row['category'] for h in hits)


@pytest.mark.parametrize('recorded_only', [False, True])
def test_later_retraction_never_resurrected(vault, tmp_path, monkeypatch, recorded_only):
    condition = _condition(**({} if recorded_only else {'onsetDateTime': '2020-01-01'}))
    _import(vault, tmp_path, monkeypatch, _bundle(condition, _report()))
    withdrawn = copy.deepcopy(condition)
    withdrawn['verificationStatus'] = {'coding': [{'code': 'entered-in-error'}]}
    _import(vault, tmp_path, monkeypatch, _bundle(withdrawn, _report(status='cancelled')))
    assert _replay(vault)['status'] == 'done'
    assert vault.execute('SELECT count(*) FROM condition').fetchone()[0] == 0
    assert vault.execute('SELECT count(*) FROM clinical_report').fetchone()[0] == 0


def test_missing_retracting_file_blocks_without_writes(vault, tmp_path, monkeypatch, fake_ctx):
    _import(vault, tmp_path, monkeypatch, _condition())
    src = _import(vault, tmp_path, monkeypatch, _condition(verificationStatus={'coding': [{'code': 'refuted'}]}))
    sources.source_file(vault, src).unlink()
    before = _bytes(vault)
    result = _replay(vault)
    assert result['status'] == 'blocked'
    assert result['missing_sources'] == [{'source_id': src.id, 'kind': 'fhir'}]
    assert _bytes(vault) == before
    settings = ops.capture_tool(fake_ctx, {'op': 'settings'})['clinical_replay']
    assert settings['needed'] and 'missing_sources' in settings['blocked']
    assert _bytes(vault) == before


def test_cancel_during_reconstruction_then_retry(vault, tmp_path, monkeypatch):
    _import(vault, tmp_path, monkeypatch, _bundle(_condition(), _report()))
    before = _bytes(vault)
    cancel = threading.Event()
    ingest = fhir.ingest
    def interrupt(*args, **kwargs):
        counts = ingest(*args, **kwargs)
        cancel.set()
        return counts
    with monkeypatch.context() as patch:
        patch.setattr(fhir, 'ingest', interrupt)
        assert _replay(vault, cancel=cancel)['status'] == 'blocked'
    assert _bytes(vault) == before
    assert _replay(vault)['added']['condition'] == 1


def test_all_existing_rows_unchanged(vault, samples, tmp_path, monkeypatch, fake_ctx):
    # Populate non-clinical data as well as imported records, all synthetic.
    _import(vault, tmp_path, monkeypatch, _bundle(
        _condition(), _observation(coded=True), _observation(), _observation('two', 'Synthetic beta'),
        {'resourceType': 'MedicationStatement', 'id': 'med', 'status': 'active',
         'medicationCodeableConcept': {'text': 'synthetic drug'}, 'effectiveDateTime': '2020-01-01'}, _report()),
        samples=samples)
    ops.capture_tool(fake_ctx, {'op': 'journal', 'text': 'Synthetic journal', 'date': '2020-01-02'})
    ops.capture_tool(fake_ctx, {'op': 'medication', 'drug': 'synthetic user drug', 'date': '2020-01-02'})
    with db.write_txn(vault):
        vault.execute("INSERT INTO phenotype_note(trait,stance,text,category,created_at) "
                      "VALUES ('synthetic','confirms','Synthetic note','records','2020-01-01')")
        vault.execute("INSERT INTO callset(source_id,vendor,build,created_at) VALUES (1,'synthetic','37','2020-01-01')")
        vault.execute("INSERT INTO genotype_call VALUES (1,'rs0000','1',1,'AA')")
    before, samples_before = _rows(vault), _rows(samples)
    assert _replay(vault)['added'] == {'condition': 1, 'observation': 1, 'report': 1}
    after = _rows(vault)
    for table, rows in before.items():
        assert all(row in after[table] for row in rows), table
        if table not in ('condition', 'observation', 'clinical_report', 'search_doc', 'settings'):
            assert after[table] == rows, table
    assert _rows(samples) == samples_before


def test_later_condition_status_and_live_status_preserved(vault, tmp_path, monkeypatch):
    _import(vault, tmp_path, monkeypatch, _bundle(_condition(), _condition('dated', onsetDateTime='2020-01-01')))
    _import(vault, tmp_path, monkeypatch, _bundle(
        _condition(clinicalStatus={'coding': [{'code': 'resolved'}]}),
        _condition('dated', onsetDateTime='2020-01-01', clinicalStatus={'coding': [{'code': 'inactive'}]})))
    with db.write_txn(vault):
        vault.execute("UPDATE condition SET clinical_status='live-only-status'")
    before = tuple(vault.execute('SELECT * FROM condition').fetchone())
    assert _replay(vault)['added']['condition'] == 1
    assert before in _rows(vault)['condition']
    assert vault.execute('SELECT clinical_status FROM condition WHERE onset IS NULL').fetchone()[0] == 'resolved'


def test_condition_key_drift_identity_name_and_code(vault, tmp_path, monkeypatch):
    _import(vault, tmp_path, monkeypatch, _condition(), old=False)
    with db.write_txn(vault):
        vault.execute("UPDATE condition SET dedupe_key='synthetic-legacy-key', name=' SYNTHETIC   condition '")
    before = tuple(vault.execute('SELECT * FROM condition').fetchone())
    assert _replay(vault)['added']['condition'] == 0
    assert _rows(vault)['condition'] == [before]


@pytest.mark.parametrize('equal_second', [False, True])
def test_overlapping_effectful_runs_block(vault, tmp_path, monkeypatch, equal_second):
    _import(vault, tmp_path, monkeypatch, _condition())
    _import(vault, tmp_path, monkeypatch, _report())
    with db.write_txn(vault):
        vault.execute("UPDATE import_run SET started_at='2020-01-01T00:00:01Z', finished_at='2020-01-01T00:00:05Z' WHERE id=1")
        start = '2020-01-01T00:00:05Z' if equal_second else '2020-01-01T00:00:04Z'
        vault.execute("UPDATE import_run SET started_at=?, finished_at='2020-01-01T00:00:06Z' WHERE id=2", (start,))
    before = _bytes(vault)
    result = _replay(vault)
    assert result['status'] == 'blocked' and result['overlapping_runs'] == [1, 2]
    assert _bytes(vault) == before


def test_apple_clinical_only(vault, samples, tmp_path, monkeypatch):
    src = _import(vault, tmp_path, monkeypatch, _bundle(_condition(), _report()), samples=samples)
    before = _rows(samples)
    # Any attempt to open or stream sample XML during replay is an error.
    monkeypatch.setattr(apple, '_xml', lambda *a, **kw: pytest.fail('replay read samples'))
    assert _replay(vault)['added'] == {'condition': 1, 'observation': 0, 'report': 1}
    assert _rows(samples) == before
    assert vault.execute('SELECT source_id FROM condition').fetchone()[0] == src.id


def test_marker_settings_and_second_tool_call(vault, tmp_path, monkeypatch, fake_ctx):
    assert ops.capture_tool(fake_ctx, {'op': 'settings'})['clinical_replay'] == {'needed': False}
    _import(vault, tmp_path, monkeypatch, _condition())
    assert ops.capture_tool(fake_ctx, {'op': 'settings'})['clinical_replay'] == {'needed': True}
    assert ops.capture_tool(fake_ctx, {'op': 'replay'})['status'] == 'done'
    assert ops.capture_tool(fake_ctx, {'op': 'settings'})['clinical_replay'] == {'needed': False}
    before = _bytes(vault)
    assert ops.capture_tool(fake_ctx, {'op': 'replay'})['status'] == 'nothing_to_replay'
    assert _bytes(vault) == before
    marker = json.loads(vault.execute("SELECT value_json FROM settings WHERE key='clinical_replay'").fetchone()[0])
    assert marker['version'] == '1' and marker['sources'] == [1] and marker['at']


def test_import_during_replay_retries_without_publication(vault, tmp_path, monkeypatch):
    src = _import(vault, tmp_path, monkeypatch, _condition())
    ingest = fhir.ingest
    def concurrent(*args, **kwargs):
        result = ingest(*args, **kwargs)
        sources.begin_run(vault, src, 'synthetic-concurrent-import', '1')
        return result
    monkeypatch.setattr(fhir, 'ingest', concurrent)
    before = _rows(vault)
    result = _replay(vault)
    assert result['status'] == 'retry' and result['reason'] == 'an import ran during replay'
    after = _rows(vault)
    assert {k: v for k, v in after.items() if k != 'import_run'} == {k: v for k, v in before.items() if k != 'import_run'}


def test_cancelled_effectful_retry_is_ambiguous(vault, tmp_path, monkeypatch):
    _import(vault, tmp_path, monkeypatch, _condition(onsetDateTime='2020-01-01'))
    document = _condition(onsetDateTime='2020-01-01', verificationStatus={'coding': [{'code': 'refuted'}]})
    path = tmp_path / 'withdrawal.json'
    path.write_text(json.dumps(document))
    src = sources.store_source(vault, path, 'fhir')[0]
    run = sources.begin_run(vault, src, 'fhir', fhir.PARSER_VERSION)
    counts = fhir.ingest(vault, document, src.id)
    assert counts['condition_retracted'] == 1
    sources.finish_import(vault, run.id, counts=counts, status='cancelled')
    assert fhir.import_fhir(vault, src).status == 'succeeded'
    before = _bytes(vault)
    result = _replay(vault)
    assert result['status'] == 'blocked' and run.id in result['ambiguous_runs']
    assert _bytes(vault) == before


def test_missing_apple_source_blocks(vault, samples, tmp_path, monkeypatch):
    src = _import(vault, tmp_path, monkeypatch, _condition(), samples=samples)
    sources.source_file(vault, src).unlink()
    before = _bytes(vault)
    result = _replay(vault)
    assert result['status'] == 'blocked'
    assert result['missing_sources'] == [{'source_id': src.id, 'kind': 'apple_health'}]
    assert _bytes(vault) == before


def test_same_bare_id_different_report_type(vault, tmp_path, monkeypatch):
    _import(vault, tmp_path, monkeypatch, _report('123', 'ImagingStudy'), old=False)
    _import(vault, tmp_path, monkeypatch, _report('123', 'DiagnosticReport'))
    assert _replay(vault)['added']['report'] == 1
    assert {r[0] for r in vault.execute('SELECT resource_type FROM clinical_report')} == {'ImagingStudy', 'DiagnosticReport'}


@pytest.mark.parametrize('receipt,blocked', [
    ({'observation': 0, 'condition': 0, 'medication': 0, 'report': 0}, False),
    ({}, True),
    ({'observation': 0, 'condition': 0, 'medication': 0, 'report_updated': 1}, True),
])
def test_unsuccessful_receipts(vault, tmp_path, monkeypatch, receipt, blocked):
    src = _import(vault, tmp_path, monkeypatch, _condition())
    run = sources.begin_run(vault, src, 'fhir', 'synthetic-failed')
    sources.finish_import(vault, run.id, counts=receipt, status='failed')
    result = _replay(vault)
    assert result['status'] == ('blocked' if blocked else 'done')
    if blocked:
        assert run.id in result['ambiguous_runs']


def test_zero_effect_only_source_skipped(vault, tmp_path, monkeypatch):
    src = _import(vault, tmp_path, monkeypatch, _condition())
    with db.write_txn(vault):
        vault.execute("UPDATE import_run SET status='failed', counts_json=?",
                      (json.dumps({'observation': 0, 'condition': 0, 'medication': 0, 'report': 0}),))
    result = _replay(vault)
    assert result['status'] == 'done' and result['sources_read'] == 0
    assert result['skipped_sources'] == [src.id]


def test_publication_error_rolls_back_every_addition(vault, tmp_path, monkeypatch):
    _import(vault, tmp_path, monkeypatch, _bundle(_condition(), _report()))
    before = _bytes(vault)
    index_doc = search.index_doc
    def fail_live(conn, **kwargs):
        if conn is vault:
            raise RuntimeError('synthetic publication error')
        return index_doc(conn, **kwargs)
    monkeypatch.setattr(search, 'index_doc', fail_live)
    assert _replay(vault)['status'] == 'blocked'
    assert _bytes(vault) == before


def test_job_progress_duplicate_call_and_erase(vault, tmp_path, monkeypatch, fake_ctx):
    _import(vault, tmp_path, monkeypatch, _condition())
    entered, resume = threading.Event(), threading.Event()
    ingest = fhir.ingest
    def wait(*a, **kw):
        entered.set()
        assert resume.wait(5)
        return ingest(*a, **kw)
    monkeypatch.setattr(fhir, 'ingest', wait)
    monkeypatch.setattr(ops, 'IMPORT_WAIT_S', 0)
    try:
        first = ops.capture_tool(fake_ctx, {'op': 'replay'})
        assert first['status'] == 'running' and entered.wait(3)
        again = ops.capture_tool(fake_ctx, {'op': 'replay'})
        assert again['status'] == 'running' and again['progress'] == {'sources_done': 0, 'sources_total': 1}
        assert 'Wait until it finishes' in ops._erase(fake_ctx, True)
    finally:
        resume.set()
        assert jobs.join('clinical-replay', 5)


def test_replay_refuses_active_job(fake_ctx):
    resume = threading.Event()
    jobs.submit('synthetic-import-job', lambda: resume.wait(5), wait_s=0)
    try:
        result = ops.capture_tool(fake_ctx, {'op': 'replay'})
        assert result['status'] == 'blocked' and result['reason'] == 'an import job is running'
    finally:
        resume.set()
        assert jobs.join('synthetic-import-job', 5)


def test_report_and_index_exactly_match_current_ingest(vault, tmp_path, monkeypatch):
    contained = _observation('contained', 'Synthetic depression score')
    report = _report(result=[{'reference': '#contained'}, {'reference': 'Observation/linked'}],
                     contained=[contained], presentedForm=[{'contentType': 'application/pdf',
                     'title': 'Synthetic attachment', 'data': 'c3ludGhldGlj'}],
                     text={'div': '<div>Synthetic narrative</div>'})
    first = _bundle(_observation('linked', coded=True), report)
    second = _bundle({**report, 'status': 'corrected', 'conclusion': 'Synthetic revised conclusion'})
    _import(vault, tmp_path, monkeypatch, first)
    _import(vault, tmp_path, monkeypatch, second)
    expected = db.open_vault(':memory:')
    try:
        with db.write_txn(expected):
            for row in vault.execute('SELECT * FROM source_artifact'):
                expected.execute('INSERT INTO source_artifact VALUES (?,?,?,?,?,?,?,?)', tuple(row))
        fhir.ingest(expected, first, 1)
        fhir.ingest(expected, second, 2)
        reference = dict(expected.execute('SELECT * FROM clinical_report').fetchone())
        index = dict(expected.execute("SELECT title,body,category,ts FROM search_doc WHERE kind='report'").fetchone())
        assert _replay(vault)['added']['report'] == 1
        recovered = dict(vault.execute('SELECT * FROM clinical_report').fetchone())
        document = json.loads(recovered.pop('raw_json'))
        assert document['_caduceus'].pop('recovered_by') == 'clinical_replay'
        assert document == json.loads(reference.pop('raw_json'))
        assert document['_caduceus']['source_ids'] == [1, 2]
        assert document['_caduceus']['contained_categories'] == ['mental_health']
        assert 'data' not in document['presentedForm'][0]
        assert recovered == reference
        assert dict(vault.execute("SELECT title,body,category,ts FROM search_doc WHERE kind='report'").fetchone()) == index
        assert vault.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        expected.close()


def test_version_advance_only_changes_replay_setting(vault, tmp_path, monkeypatch):
    _import(vault, tmp_path, monkeypatch, _condition())
    assert _replay(vault)['status'] == 'done'
    replay = importlib.import_module('caduceus.health.replay')
    monkeypatch.setattr(replay, 'REPLAY_VERSION', '2')
    assert replay.settings(vault) == {'needed': True}
    before = _rows(vault)
    assert _replay(vault)['added'] == {'condition': 0, 'observation': 0, 'report': 0}
    after = _rows(vault)
    assert {k: v for k, v in before.items() if k != 'settings'} == {k: v for k, v in after.items() if k != 'settings'}
    assert json.loads(vault.execute("SELECT value_json FROM settings WHERE key='clinical_replay'").fetchone()[0])['version'] == '2'


def test_single_publication_transaction(vault, tmp_path, monkeypatch):
    _import(vault, tmp_path, monkeypatch, _bundle(_condition(), _report()))
    statements = []
    vault.set_trace_callback(statements.append)
    try:
        assert _replay(vault)['status'] == 'done'
    finally:
        vault.set_trace_callback(None)
    assert [sql for sql in statements if sql.startswith('BEGIN')] == ['BEGIN IMMEDIATE']
    assert statements.count('COMMIT') == 1
    assert not any(sql.startswith(('UPDATE ', 'DELETE ')) for sql in statements)
