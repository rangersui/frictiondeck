"""Smoke test: write artifact → version increments → broadcast triggers.

Run: python test_stage.py
"""

import os
import sys
import tempfile

# Use temp dir so we don't pollute the repo
_tmpdir = tempfile.mkdtemp(prefix="fd4_test_")
os.environ["FRICTIONDECK_DB_DIR"] = _tmpdir

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from pipeline.stage import init_stage_db, write_artifact, get_version, get_artifacts, get_stage_state
from pipeline.stage import promote_to_judgment, get_judgments, seal_judgments
from pipeline.stage import add_relation, add_overlay, get_relations, get_overlays
from pipeline.audit import init_audit_db
from pipeline.broadcast import broadcast, subscribe
from pipeline.stage import set_broadcast

# Track broadcasts
_broadcasts = []


def _test_broadcast(event_type, data):
    _broadcasts.append({"event": event_type, "data": data})


def test():
    print(f"DB dir: {_tmpdir}")
    print()

    # 1. Init
    init_audit_db()
    init_stage_db()
    set_broadcast(_test_broadcast)

    v0 = get_version()
    assert v0 == 0, f"initial version should be 0, got {v0}"
    print(f"✓ init  version={v0}")

    # 2. Write artifact → version++
    result = write_artifact(
        content="<h1>Solar Panel Analysis</h1><p>300W monocrystalline, 21% efficiency</p>",
        content_type="html",
        metadata={"source": "datasheet_v2.pdf", "page": 3},
        source_trust="green",
    )
    v1 = get_version()
    assert v1 == 1, f"version should be 1 after first write, got {v1}"
    assert "artifact_id" in result
    a_id = result["artifact_id"]
    print(f"✓ write_artifact  id={a_id[:12]}…  version={v1}")

    # 3. Broadcast fired
    assert len(_broadcasts) == 1, f"expected 1 broadcast, got {len(_broadcasts)}"
    assert _broadcasts[0]["event"] == "artifact_added"
    assert _broadcasts[0]["data"]["version"] == 1
    print(f"✓ broadcast fired  event={_broadcasts[0]['event']}  version={_broadcasts[0]['data']['version']}")

    # 4. Write second artifact
    r2 = write_artifact(
        content="Daily power consumption: 1538 Wh including standby",
        content_type="text",
    )
    v2 = get_version()
    assert v2 == 2
    a2_id = r2["artifact_id"]
    print(f"✓ second artifact  id={a2_id[:12]}…  version={v2}")

    # 5. Promote to judgment (fluid → viscous)
    j_result = promote_to_judgment(
        artifact_id=a_id,
        claim_text="300W panel generates 2100 Wh/day under clear sky",
        params=[
            {"name": "panel_power", "value": 300, "unit": "W"},
            {"name": "sun_hours", "value": 7, "unit": "h"},
            {"name": "daily_output", "value": 2100, "unit": "Wh"},
        ],
    )
    v3 = get_version()
    assert v3 == 3
    j_id = j_result["judgment_id"]
    print(f"✓ promote_to_judgment  id={j_id[:12]}…  version={v3}")

    # 6. Add relation
    rel = add_relation(a_id, a2_id, relation_type="depends_on", label="power→consumption")
    v4 = get_version()
    assert v4 == 4
    print(f"✓ add_relation  id={rel['relation_id'][:12]}…  version={v4}")

    # 7. Add overlay (negative space)
    ov = add_overlay(
        target_id=a_id,
        target_type="artifact",
        overlay_type="negative_space",
        value={"description": "Temperature derating not covered"},
        created_by="ai",
    )
    v5 = get_version()
    assert v5 == 5
    print(f"✓ add_overlay  id={ov['overlay_id'][:12]}…  version={v5}")

    # 8. Full state
    state = get_stage_state()
    assert state["version"] == 5
    assert len(state["artifacts"]) == 2
    assert len(state["judgments"]) == 1
    assert len(state["relations"]) == 1
    assert len(state["overlays"]) == 1
    print(f"✓ get_stage_state  artifacts={len(state['artifacts'])}  judgments={len(state['judgments'])}  relations={len(state['relations'])}  overlays={len(state['overlays'])}")

    # 9. Seal judgments (viscous → solid)
    seal = seal_judgments([j_id], commit_id="c001_test")
    v6 = get_version()
    assert v6 == 6
    assert seal["sealed"] == 1
    print(f"✓ seal_judgments  sealed={seal['sealed']}  version={v6}")

    # 10. Verify sealed judgment is solid
    solid = get_judgments(state="solid")
    assert len(solid) == 1
    assert solid[0]["commit_id"] == "c001_test"
    print(f"✓ judgment is solid  commit_id={solid[0]['commit_id']}")

    # 11. Total broadcasts
    print(f"\n✓ total broadcasts: {len(_broadcasts)}")
    for b in _broadcasts:
        print(f"  {b['event']}  v={b['data'].get('version', '?')}")

    print(f"\n══ ALL TESTS PASSED ══")
    print(f"DB: {_tmpdir}")


if __name__ == "__main__":
    test()
