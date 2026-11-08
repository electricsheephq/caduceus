"""Local genotype lookup. Tools apply the user's privacy policy before displaying calls."""
from __future__ import annotations

import re

from ..core.contracts import Call, CallState


class NoCallset(LookupError):
    """No DNA callset exists for this lookup."""


# A callset is complete once a dtc import of its source succeeded with at least one row. Rows from a
# running, cancelled or failed import stay stored (the next run resumes them) but are never looked up.
_COMPLETE = ("EXISTS (SELECT 1 FROM import_run r WHERE r.source_id = c.source_id AND r.parser = 'dtc' "
             "AND r.status = 'succeeded') AND c.marker_count > 0")


def callsets(conn) -> list[dict]:
    return [dict(row) for row in conn.execute(
        f"SELECT c.*, ({_COMPLETE}) AS complete FROM callset c ORDER BY c.created_at DESC, c.id DESC")]


def get_calls(conn, markers, *, callset_id=None) -> dict[str, Call]:
    if callset_id is None:
        selected = conn.execute(
            f"SELECT c.id FROM callset c WHERE {_COMPLETE} ORDER BY c.created_at DESC, c.id DESC LIMIT 1"
        ).fetchone()
        if selected is None and conn.execute("SELECT 1 FROM callset LIMIT 1").fetchone() is not None:
            raise NoCallset("The DNA import has not finished; check it with caduceus_capture op=status.")
    else:
        selected = conn.execute("SELECT id FROM callset WHERE id=?", (callset_id,)).fetchone()
    if selected is None:
        raise NoCallset("Import a DNA callset before looking up markers.")
    callset_id = selected[0]
    if isinstance(markers, str):
        markers = [markers]
    result = {}
    for name in markers:
        if not isinstance(name, str):
            continue
        marker = name.strip().lower()
        resolved = None
        row = conn.execute("SELECT * FROM genotype_call WHERE callset_id=? AND marker=?",
                           (callset_id, marker)).fetchone()
        if row is None:
            aliases = {r[0].strip().lower() for r in conn.execute(
                "SELECT marker FROM marker_alias WHERE alias=? COLLATE NOCASE", (marker,)
            )}
            if len(aliases) > 1 or (not aliases and not re.fullmatch(r"(?:rs|i)[0-9]+", marker)):
                result[name] = Call(name, CallState.UNRESOLVED, callset_id=callset_id)
                continue
            if aliases:
                resolved = next(iter(aliases))
                row = conn.execute("SELECT * FROM genotype_call WHERE callset_id=? AND marker=?",
                                   (callset_id, resolved)).fetchone()
        if row is None:
            result[name] = Call(name, CallState.NOT_ON_CHIP, callset_id=callset_id, resolved_marker=resolved)
        else:
            state = CallState.NO_CALL if row["genotype"] in ("--", "") else CallState.CALLED
            result[name] = Call(name, state, row["genotype"], row["chrom"], row["pos"], callset_id, resolved)
    return result
