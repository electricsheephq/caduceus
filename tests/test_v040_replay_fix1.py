"""Round-one replay regressions; all fixtures and input contents are synthetic."""
import json
import threading
import zipfile
from pathlib import Path

import pytest

from caduceus import ops
from caduceus.core import db, sources
from caduceus.health import apple, fhir
from tests.test_v040_replay import (
    _bundle, _bytes, _condition, _import, _replay, _rows, _separate_run_times,
)


def _apple_source(vault, tmp_path, documents=(), *, xml='<HealthData/>', standalone=False):
    path = tmp_path / ('synthetic-export.xml' if standalone else 'synthetic-export.zip')
    if standalone:
        path.write_text(xml)
    else:
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('export.xml', xml)
            for index, document in enumerate(documents):
                archive.writestr(f'clinical-records/{index}.json', json.dumps(document))
    return sources.store_source(vault, path, 'apple_health')[0]


def _withdrawal():
    return _condition('c1', onsetDateTime='2020-01-01',
                      verificationStatus={'coding': [{'code': 'entered-in-error'}]})


@pytest.mark.parametrize('earlier_empty_document', [False, True])
def test_failed_apple_retraction_blocks(vault, samples, tmp_path, monkeypatch, earlier_empty_document):
    _import(vault, tmp_path, monkeypatch, _condition('c1', onsetDateTime='2020-01-01'))
    # The first resource commits a withdrawal; the malformed resource then raises in ingest.
    documents = ([_bundle()] if earlier_empty_document else []) + [_bundle(_withdrawal(), None)]
    src = _apple_source(vault, tmp_path, documents)
    failed = apple.import_apple_health(samples, vault, src)
    assert failed.status == 'failed'
    assert not vault.execute('SELECT 1 FROM condition').fetchone()
    assert not failed.counts.get('condition_retracted')
    assert all(failed.counts[key] == 0 for key in ('samples', 'workouts', 'activity_summaries'))
    if earlier_empty_document:
        assert all(failed.counts[key] == 0 for key in ('observation', 'condition', 'medication', 'report'))
    before = _bytes(vault)
    result = _replay(vault)
    assert result['status'] == 'blocked'
    assert failed.run_id in result['ambiguous_runs']
    assert not vault.execute('SELECT 1 FROM condition').fetchone()
    assert _bytes(vault) == before


@pytest.mark.parametrize('standalone', [False, True])
def test_nonclinical_apple_never_blocks_despite_failed_and_running_runs(
        vault, samples, tmp_path, monkeypatch, standalone):
    _import(vault, tmp_path, monkeypatch, _condition())
    # A failed XML parse and a still-running retry are irrelevant without clinical members.
    src = _apple_source(vault, tmp_path, xml='<HealthData><synthetic', standalone=standalone)
    assert apple.import_apple_health(samples, vault, src).status == 'failed'
    sources.begin_run(vault, src, 'apple_health', 'synthetic-retry')
    before = _rows(samples)
    result = _replay(vault)
    assert result['status'] == 'done'
    assert result['skipped_sources'] == [src.id]
    assert result['sources_read'] == 1
    assert result['added']['condition'] == 1
    assert _rows(samples) == before


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'interrupted', 'running'])
def test_apple_clinical_zero_receipt_never_exempts_another_run(
        vault, samples, tmp_path, monkeypatch, status):
    src = _import(vault, tmp_path, monkeypatch, _condition(), samples=samples)
    other = sources.begin_run(vault, src, 'apple_health', 'synthetic-other')
    counts = {'samples': 0, 'workouts': 0, 'activity_summaries': 0,
              'observation': 0, 'condition': 0, 'medication': 0, 'report': 0}
    with db.write_txn(vault):
        vault.execute('UPDATE import_run SET status=?,counts_json=? WHERE id=?',
                      (status, json.dumps(counts), other.id))
    before = _bytes(vault)
    result = _replay(vault)
    assert result['status'] == 'blocked' and other.id in result['ambiguous_runs']
    assert _bytes(vault) == before


@pytest.mark.parametrize('kind', ['fhir-json', 'fhir-resource', 'apple-json', 'apple-zip'])
def test_reconstruction_failure_names_source(vault, samples, tmp_path, monkeypatch, fake_ctx, kind):
    src = _import(vault, tmp_path, monkeypatch, _condition(), samples=samples if kind.startswith('apple') else None)
    path = sources.source_file(vault, src)
    if kind == 'apple-json':
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('export.xml', '<HealthData/>')
            archive.writestr('clinical-records/bad.json', '{synthetic-invalid')
    else:
        path.write_text('null' if kind == 'fhir-resource' else '{synthetic-invalid')
    before = _bytes(vault)
    if kind != 'apple-zip':
        # Settings may inspect accessibility/ZIP directory, but must not parse clinical contents.
        assert ops.capture_tool(fake_ctx, {'op': 'settings'})['clinical_replay'] == {'needed': True}
    result = _replay(vault)
    assert result['status'] == 'blocked'
    assert result.get('missing_sources') == [{'source_id': src.id, 'kind': src.kind}]
    assert _bytes(vault) == before


def test_file_becomes_unreadable_after_inventory_names_source(vault, tmp_path, monkeypatch):
    src = _import(vault, tmp_path, monkeypatch, _condition())
    path = sources.source_file(vault, src)
    original_open = Path.open
    reads = 0
    def deny_second_read(self, *args, **kwargs):
        nonlocal reads
        if self == path:
            reads += 1
            if reads > 1:
                raise PermissionError('synthetic read denial')
        return original_open(self, *args, **kwargs)
    before = _bytes(vault)
    monkeypatch.setattr(Path, 'open', deny_second_read)
    result = _replay(vault)
    assert result['status'] == 'blocked'
    assert result.get('missing_sources') == [{'source_id': src.id, 'kind': 'fhir'}]
    assert _bytes(vault) == before


def test_reused_latest_import_run_id_retries(vault, tmp_path, monkeypatch):
    src = _import(vault, tmp_path, monkeypatch, _condition())
    before = _rows(vault)
    expected = []
    def replace_after_start(progress):
        if progress['sources_done']:
            return
        with db.write_txn(vault):
            old = vault.execute('SELECT max(id) FROM import_run').fetchone()[0]
            vault.execute('DELETE FROM import_run WHERE id=?', (old,))
            new = sources.begin_run(vault, src, 'fhir', 'synthetic-new-run')
            assert new.id == old
        expected.append(_bytes(vault))
    result = _replay(vault, progress=replace_after_start)
    assert result['status'] == 'retry' and result['reason'] == 'an import ran during replay'
    assert _bytes(vault) == expected[0]
    after = _rows(vault)
    assert {k: v for k, v in before.items() if k != 'import_run'} == {
        k: v for k, v in after.items() if k != 'import_run'}


def test_fhir_failed_retraction_receipt_is_unknown_and_blocks(vault, tmp_path, monkeypatch):
    _import(vault, tmp_path, monkeypatch, _condition('c1', onsetDateTime='2020-01-01'))
    path = tmp_path / 'synthetic-fhir-failure.json'
    path.write_text(json.dumps(_bundle(_withdrawal(), None)))
    src = sources.store_source(vault, path, 'fhir')[0]
    failed = fhir.import_fhir(vault, src)
    assert failed.status == 'failed' and failed.counts == {}
    assert not vault.execute('SELECT 1 FROM condition').fetchone()
    before = _bytes(vault)
    result = _replay(vault)
    assert result['status'] == 'blocked' and failed.run_id in result['ambiguous_runs']
    assert _bytes(vault) == before


def test_fhir_returned_cancelled_receipt_covers_retraction(vault, tmp_path, monkeypatch):
    _import(vault, tmp_path, monkeypatch, _condition('c1', onsetDateTime='2020-01-01'))
    path = tmp_path / 'synthetic-fhir-cancel.json'
    path.write_text(json.dumps(_bundle(_withdrawal(), _condition('later'))))
    src = sources.store_source(vault, path, 'fhir')[0]
    cancel = threading.Event()
    retract = fhir._retract
    def withdraw_then_cancel(*args, **kwargs):
        result = retract(*args, **kwargs)
        cancel.set()
        return result
    monkeypatch.setattr(fhir, '_retract', withdraw_then_cancel)
    result = fhir.import_fhir(vault, src, cancel=cancel)
    assert result.status == 'cancelled' and result.counts['condition_retracted'] == 1
    before = _bytes(vault)
    replayed = _replay(vault)
    assert replayed['status'] == 'blocked' and result.run_id in replayed['ambiguous_runs']
    assert _bytes(vault) == before


def test_fhir_cancelled_run_blocks_even_with_complete_zero_receipt(
        vault, tmp_path, monkeypatch):
    src = _import(vault, tmp_path, monkeypatch, _condition())
    cancel = threading.Event()
    read_document = fhir.read_document
    def cancel_after_read(path):
        result = read_document(path)
        cancel.set()  # ingest returns its complete zero receipt before touching a resource
        return result
    monkeypatch.setattr(fhir, 'read_document', cancel_after_read)
    monkeypatch.setattr(fhir, 'PARSER_VERSION', 'synthetic-cancelled')
    result = fhir.import_fhir(vault, src, cancel=cancel)
    assert result.status == 'cancelled'
    assert all(result.counts[key] == 0 for key in ('observation', 'condition', 'medication', 'report'))
    # A cancelled run can delete a recorded-only Condition without counting it, so zeros prove nothing.
    before = list(vault.iterdump())
    replayed = _replay(vault)
    assert replayed['status'] == 'blocked' and result.run_id in replayed['ambiguous_runs']
    assert list(vault.iterdump()) == before



def test_bot_background_result_is_returned_once(vault, tmp_path, monkeypatch, fake_ctx):
    # A replay that outlives the wait answers "running"; the next call must get its result, not start over.
    from caduceus.core import jobs
    from caduceus.health import replay as replay_module
    from tests.test_v040_replay import _condition, _import

    _import(vault, tmp_path, monkeypatch, _condition())
    resume, real = threading.Event(), replay_module.replay
    def slow(*a, **kw):
        assert resume.wait(5)
        return real(*a, **kw)
    monkeypatch.setattr(replay_module, 'replay', slow)
    monkeypatch.setattr(ops, 'IMPORT_WAIT_S', 0)
    try:
        assert ops.capture_tool(fake_ctx, {'op': 'replay'})['status'] == 'running'
    finally:
        resume.set()
        assert jobs.join('clinical-replay', 5)
    done = ops.capture_tool(fake_ctx, {'op': 'replay'})
    assert done['status'] == 'done' and done['finished_in_background'] is True
    assert done['added']['condition'] == 1
    monkeypatch.setattr(ops, 'IMPORT_WAIT_S', 5)
    assert ops.capture_tool(fake_ctx, {'op': 'replay'})['status'] == 'nothing_to_replay'
