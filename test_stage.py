"""Smoke test: set HTML → version increments → promote → seal.

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

from pipeline.stage import (
    init_stage_db, get_html, set_html, get_version,
    promote_to_judgment, get_judgments, seal_judgments,
    get_stage_state,
)
from pipeline.history import init_history_db
from pipeline.stage import set_broadcast

# Track broadcasts
_broadcasts = []


def _test_broadcast(event_type, data):
    _broadcasts.append({"event": event_type, "data": data})


def test():
    print(f"DB dir: {_tmpdir}")
    print()

    # 1. Init
    init_history_db()
    init_stage_db()
    set_broadcast(_test_broadcast)

    v0 = get_version()
    assert v0 == 0, f"initial version should be 0, got {v0}"
    print(f"  init  version={v0}")

    # 2. Set HTML → version++
    result = set_html("<h1>Solar Panel Analysis</h1><p>300W monocrystalline</p>")
    v1 = get_version()
    assert v1 == 1, f"version should be 1 after set_html, got {v1}"
    print(f"  set_html  version={v1}")

    # 3. Broadcast fired
    assert len(_broadcasts) == 1
    assert _broadcasts[0]["event"] == "stage_updated"
    print(f"  broadcast fired  event={_broadcasts[0]['event']}")

    # 4. Get HTML back
    html = get_html()
    assert "Solar Panel" in html
    print(f"  get_html  length={len(html)}")

    # 5. Promote to judgment (viscous)
    j_result = promote_to_judgment(
        claim_text="300W panel generates 2100 Wh/day under clear sky",
        params=[
            {"name": "panel_power", "value": 300, "unit": "W"},
            {"name": "sun_hours", "value": 7, "unit": "h"},
            {"name": "daily_output", "value": 2100, "unit": "Wh"},
        ],
    )
    v2 = get_version()
    assert v2 == 2
    j_id = j_result["judgment_id"]
    print(f"  promote_to_judgment  id={j_id[:12]}...  version={v2}")

    # 6. Full state
    state = get_stage_state()
    assert state["version"] == 2
    assert "stage_html" in state
    assert len(state["judgments"]) == 1
    print(f"  get_stage_state  judgments={len(state['judgments'])}")

    # 7. Seal judgments (viscous → solid)
    seal = seal_judgments([j_id], commit_id="c001_test")
    v3 = get_version()
    assert v3 == 3
    assert seal["sealed"] == 1
    print(f"  seal_judgments  sealed={seal['sealed']}  version={v3}")

    # 8. Verify sealed judgment is solid
    solid = get_judgments(state="solid")
    assert len(solid) == 1
    assert solid[0]["commit_id"] == "c001_test"
    print(f"  judgment is solid  commit_id={solid[0]['commit_id']}")

    # 9. Total broadcasts
    print(f"\n  total broadcasts: {len(_broadcasts)}")
    for b in _broadcasts:
        print(f"    {b['event']}  v={b['data'].get('version', '?')}")

    print(f"\nALL TESTS PASSED")
    print(f"DB: {_tmpdir}")


if __name__ == "__main__":
    test()
