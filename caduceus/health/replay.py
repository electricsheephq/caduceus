"""Rebuild clinical history privately, then publish only missing rows in one transaction."""
from __future__ import annotations

import json
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import PurePosixPath

from ..core import search, sources
from ..core.db import open_vault, write_txn
from . import apple, fhir

# Raise whenever a parser change must be applied to history. PARSER_VERSION alone is not enough.
REPLAY_VERSION = "1"


def inventory(vault):
    """Inventory before opening files, including missing Apple exports and all runs of each source."""
    return vault.execute(
        "SELECT s.* FROM source_artifact s WHERE EXISTS (SELECT 1 FROM import_run r "
        "WHERE r.source_id=s.id AND r.parser IN ('fhir','apple_health')) ORDER BY s.id").fetchall()


def _current(vault):
    row = vault.execute("SELECT value_json FROM settings WHERE key='clinical_replay'").fetchone()
    if row is None:
        return False
    marker = json.loads(row[0])
    return marker.get('version') == REPLAY_VERSION


def mark(vault, source_ids):
    """Caller owns the publication transaction; this is replay's only settings write."""
    at = sources.now_iso()
    vault.execute(
        "INSERT INTO settings(key,value_json,updated_at) VALUES ('clinical_replay',?,?) "
        "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
        (json.dumps({'version': REPLAY_VERSION, 'sources': source_ids, 'at': at}, sort_keys=True), at))


def _no_effect(run):
    """Only FHIR's returned counters cover its whole ingest; Apple accumulates per document.

    An exception inside ingest leaves {}, even if earlier resources committed. A cancelled run
    can delete a recorded-only Condition without counting it. Such a receipt (or a cancelled,
    running or interrupted attempt) cannot establish that nothing changed.
    """
    if run['parser'] != 'fhir' or run['status'] != 'failed':
        return False
    try:
        counts = json.loads(run['counts_json'])
    except (ValueError, TypeError):
        return False
    required = {'observation', 'condition', 'medication'}
    if not isinstance(counts, dict) or not required <= counts.keys():
        return False
    # Sparse update/retraction counters are omitted when zero by the existing importers.
    mutations = required | {'observation', 'condition', 'medication', 'report'}
    return all(type(value) is int and value == 0 for key, value in counts.items()
               if key in mutations or key.endswith(('_updated', '_retracted', '_superseded')))


def _apple_has_clinical(path):
    """Inspect the ZIP directory only, never XML, JSON or sample payloads."""
    if path.suffix.lower() != '.zip' and not zipfile.is_zipfile(path):
        return False
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            name = member.filename
            part = PurePosixPath(name)
            if part.is_absolute() or '..' in part.parts or '\\' in name or ':' in name or member.is_dir():
                continue  # same member-name exclusions as the Apple importer
            if 'clinical-records' in part.parts and part.suffix.lower() == '.json':
                return True
    return False


def _time(value):
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.utcoffset() is None:
        raise ValueError('run timestamp has no offset')
    return moment


def _history(vault, artifacts):
    missing, ambiguous, effectful, skipped = [], [], [], []
    # Inventory has already completed. Check every source even if its runs are ambiguous or harmless.
    for row in artifacts:
        src = sources._artifact(row)
        runs = vault.execute('SELECT * FROM import_run WHERE source_id=? ORDER BY id', (src.id,)).fetchall()
        is_apple = any(r['parser'] == 'apple_health' for r in runs)
        try:
            path = sources.source_file(vault, src)
            with path.open('rb') as stream:
                stream.read(1)
            if is_apple and not _apple_has_clinical(path):
                skipped.append(src.id)
                continue  # sample-only attempts never contributed to clinical history
        except (OSError, ValueError, zipfile.BadZipFile):
            missing.append({'source_id': src.id, 'kind': src.kind})
            continue
        succeeded = [r for r in runs if r['status'] == 'succeeded']
        unsafe = [r['id'] for r in runs if r['status'] != 'succeeded' and (is_apple or not _no_effect(r))]
        if len(succeeded) > 1:
            unsafe.extend(r['id'] for r in succeeded)
        if unsafe:
            ambiguous.extend(unsafe)
        if len(succeeded) == 1:
            run = succeeded[0]
            try:
                if run['parser'] not in ('fhir', 'apple_health') or _time(run['finished_at']) < _time(run['started_at']):
                    raise ValueError('invalid effectful run')
            except (ValueError, TypeError, AttributeError):
                ambiguous.append(run['id'])
            else:
                effectful.append((src, run))
        elif not succeeded and not unsafe:
            skipped.append(src.id)
    effectful.sort(key=lambda item: item[1]['id'])
    overlap = set()
    for i, (_, run) in enumerate(effectful):
        for _, prior in effectful[:i]:
            if _time(run['started_at']) <= _time(prior['finished_at']) and _time(prior['started_at']) <= _time(run['finished_at']):
                overlap.update((prior['id'], run['id']))
    blocked = {}
    if missing:
        blocked['missing_sources'] = missing
    if ambiguous:
        blocked['ambiguous_runs'] = sorted(set(ambiguous))
    if overlap:
        blocked['overlapping_runs'] = sorted(overlap)
    return effectful, skipped, blocked


def settings(vault):
    """Cheap accessibility/ZIP-directory checks only; clinical payloads are not parsed."""
    artifacts = inventory(vault)
    needed = bool(artifacts) and not _current(vault)
    result = {'needed': needed}
    if needed:
        _, _, blocked = _history(vault, artifacts)
        if blocked:
            result['blocked'] = list(blocked)
    return result


class _Cancelled(Exception):
    pass


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise _Cancelled


def _copy_provenance(vault, scratch):
    with write_txn(scratch):
        # The scratch vault's default subject has no dependants yet.
        scratch.execute('DELETE FROM subject')
        for table in ('subject', 'source_artifact'):
            rows = vault.execute(f'SELECT * FROM {table}').fetchall()
            for row in rows:
                columns = ','.join(row.keys())
                scratch.execute(f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in row)})", tuple(row))


def _documents(vault, src, parser, cancel):
    path = sources.source_file(vault, src)
    if parser == 'fhir':
        yield fhir.read_document(path)
    elif path.suffix.lower() == '.zip' or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            yield from apple.clinical_documents(archive, apple._safe_members(archive), cancel=cancel)
    # A successful standalone Apple XML has no clinical records. Never open its samples.


def _ids(row):
    return fhir._resource_ids(json.loads(row['raw_json'] or '{}'))


def _condition_identity(row):
    return row['code_system'], row['code'], fhir._normalized_name(row['name'])


def _publish(vault, scratch, cancel):
    added = {'condition': 0, 'observation': 0, 'report': 0}
    for table, kind, where in (('condition', 'condition', ''),
                              ('observation', 'observation', ' WHERE code_system IS NULL AND code IS NULL'),
                              ('clinical_report', 'report', '')):
        live = vault.execute(f'SELECT * FROM {table}').fetchall()
        keys = {(r['resource_type'], r['dedupe_key']) if kind == 'report' else r['dedupe_key'] for r in live}
        identities = set()
        if kind in ('condition', 'report'):
            for row in live:
                group = row['resource_type'] if kind == 'report' else _condition_identity(row)
                identities.update((group, rid) for rid in _ids(row))
        for row in scratch.execute(f'SELECT * FROM {table}{where} ORDER BY id'):
            _check_cancel(cancel)
            key = (row['resource_type'], row['dedupe_key']) if kind == 'report' else row['dedupe_key']
            group = row['resource_type'] if kind == 'report' else _condition_identity(row) if kind == 'condition' else None
            aliases = {(group, rid) for rid in _ids(row)} if group is not None else set()
            if key in keys or aliases & identities:
                continue
            document = json.loads(row['raw_json'])
            document.setdefault('_caduceus', {})['recovered_by'] = 'clinical_replay'
            values = {col: row[col] for col in row.keys() if col != 'id'}
            values['raw_json'] = json.dumps(document, sort_keys=True, separators=(',', ':'))
            cur = vault.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                                tuple(values.values()))
            indexed = scratch.execute('SELECT title,body,category,ts FROM search_doc WHERE kind=? AND ref=?',
                                      (kind, str(row['id']))).fetchone()
            if indexed is None:
                raise ValueError('scratch row has no search document')
            search.index_doc(vault, kind=kind, ref=str(cur.lastrowid), **dict(indexed))
            added[kind] += 1
            keys.add(key)
            identities.update(aliases)
    return added


def _run_fingerprint(vault):
    return tuple(vault.execute('SELECT count(*),max(id),max(started_at) FROM import_run').fetchone())


def replay(vault, *, cancel=None, progress=None):
    """No live import receipts or settings changes until the final atomic publication."""
    result = {'status': 'blocked', 'sources_read': 0, 'added': {'condition': 0, 'observation': 0, 'report': 0}}
    try:
        started = _run_fingerprint(vault)
        if _current(vault):
            return {**result, 'status': 'nothing_to_replay'}
        artifacts = inventory(vault)
        ordered, skipped, blocked = _history(vault, artifacts)
        result['skipped_sources'] = skipped
        if blocked:
            return {**result, **blocked}
        if progress:
            progress({'sources_done': 0, 'sources_total': len(ordered)})
        with closing(open_vault(':memory:')) as scratch:
            _copy_provenance(vault, scratch)
            for src, run in ordered:
                _check_cancel(cancel)
                try:
                    for document in _documents(vault, src, run['parser'], cancel):
                        _check_cancel(cancel)
                        fhir.ingest(scratch, document, src.id, cancel=cancel)
                except _Cancelled:
                    raise
                except Exception:
                    return {**result, 'missing_sources': [{'source_id': src.id, 'kind': src.kind}]}
                _check_cancel(cancel)
                result['sources_read'] += 1
                if progress:
                    progress({'sources_done': result['sources_read'], 'sources_total': len(ordered)})
            _check_cancel(cancel)
            with write_txn(vault):
                if _run_fingerprint(vault) != started:
                    return {**result, 'status': 'retry', 'reason': 'an import ran during replay'}
                if _current(vault):
                    return {**result, 'status': 'nothing_to_replay'}
                added = _publish(vault, scratch, cancel)
                _check_cancel(cancel)
                mark(vault, [src.id for src, _ in ordered])
                _check_cancel(cancel)
            return {**result, 'status': 'done', 'added': added}
    except _Cancelled:
        return {**result, 'reason': 'replay cancelled'}
    except Exception:
        # Neither input excerpts nor file paths may escape via parser/SQLite exception messages.
        return {**result, 'reason': 'clinical history could not be reconstructed or published'}
