"""Tests for the transient follow-up pipeline (Phase A/B) — offline, no broker,
no hardware. ALeRCE normalization, the astropy visibility engine, and end-to-end
candidate evaluation against a temp SQLite DB."""
import asyncio
import datetime as dt
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astropy import units as u  # noqa: E402

from cassa.transient.alerce import normalize_object  # noqa: E402
from cassa.transient.visibility import (  # noqa: E402
    compute_night,
    night_label,
    site_location,
    visibility,
)

# IUB Dhaka (observatory.yaml)
DHAKA = SimpleNamespace(latitude_deg=23.8138, longitude_deg=90.4246, elevation_m=8.0)
OFFSET = 6.0
# evening of 2026-06-18 at Dhaka (18:00 UTC == 00:00 local next day -> night of 06-18)
WHEN = dt.datetime(2026, 6, 18, 18, 0, 0, tzinfo=dt.timezone.utc)


# ----------------------------------------------------------------- ALeRCE
def test_alerce_normalize_full():
    item = {
        "oid": "ZTF21abcdxyz", "meanra": 150.5, "meandec": 22.0,
        "class_name": "SN", "probability": 0.93, "ndethist": "12",
        "firstmjd": 60100.1, "lastmjd": 60105.9, "lastmag": 18.2,
    }
    n = normalize_object(item)
    assert n["id"] == "ZTF21abcdxyz"
    assert n["ra_deg"] == 150.5 and n["dec_deg"] == 22.0
    assert n["class_label"] == "SN" and n["class_prob"] == 0.93
    assert n["ndethist"] == 12 and n["mag_last"] == 18.2
    assert n["lastmjd"] == 60105.9
    assert n["raw_json"] is item  # full packet preserved


def test_alerce_normalize_missing_fields_no_crash():
    n = normalize_object({"oid": "ZTF00empty"})
    assert n["id"] == "ZTF00empty"
    assert n["ra_deg"] is None and n["class_label"] is None and n["mag_last"] is None


def _alerce_settings():
    return SimpleNamespace(alerce_classifier="stamp_classifier", alerce_classes="",
                           alerce_probability=0.0, alerce_page_size=100,
                           alerce_max_pages=3, alerce_min_ndet=0)


async def _query(items, cutoff=None):
    import httpx
    from cassa.transient.alerce import AlerceClient

    def handler(request):
        assert request.url.path == "/ztf/v1/objects/"
        return httpx.Response(200, json={"total": len(items), "page": 1, "items": items})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    objs = await AlerceClient(client, _alerce_settings()).query_recent(cutoff)
    await client.aclose()
    return objs


def test_query_recent_parses_items_envelope():
    items = [
        {"oid": "ZTF1", "meanra": 10.0, "meandec": 20.0, "class_name": "SN",
         "probability": 0.9, "ndethist": "5", "firstmjd": 1, "lastmjd": 100.0},
        {"oid": "ZTF2", "meanra": 11.0, "meandec": 21.0, "class_name": "AGN",
         "probability": 0.7, "ndethist": "8", "firstmjd": 1, "lastmjd": 90.0},
    ]
    objs = asyncio.run(_query(items))
    assert [o["id"] for o in objs] == ["ZTF1", "ZTF2"]
    assert objs[0]["class_label"] == "SN" and objs[1]["class_label"] == "AGN"
    assert objs[0]["ndethist"] == 5


def test_query_recent_skips_old_without_truncating():
    # old object appears FIRST — the previous code broke here and returned nothing.
    items = [
        {"oid": "ZTF_old", "meanra": 1.0, "meandec": 2.0, "class_name": "SN",
         "probability": 0.5, "ndethist": "3", "firstmjd": 1, "lastmjd": 50.0},
        {"oid": "ZTF_new", "meanra": 3.0, "meandec": 4.0, "class_name": "SN",
         "probability": 0.8, "ndethist": "9", "firstmjd": 1, "lastmjd": 100.0},
    ]
    objs = asyncio.run(_query(items, cutoff=75.0))
    assert [o["id"] for o in objs] == ["ZTF_new"]   # old skipped, scan continued


async def _run_query(handler, settings):
    import httpx
    from cassa.transient.alerce import AlerceClient
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    objs = await AlerceClient(client, settings).query_recent()
    await client.aclose()
    return objs


def test_query_recent_per_class_labels_and_merges():
    import httpx

    def handler(request):
        cls = request.url.params.get("class")
        rows = {
            "SN": [{"oid": "ZTF_sn", "meanra": 1.0, "meandec": 2.0, "class_name": "SN",
                    "probability": 0.8, "ndethist": "3", "firstmjd": 1, "lastmjd": 100.0}],
            "AGN": [{"oid": "ZTF_agn", "meanra": 3.0, "meandec": 4.0, "class_name": "AGN",
                     "probability": 0.7, "ndethist": "5", "firstmjd": 1, "lastmjd": 90.0}],
        }.get(cls, [])
        return httpx.Response(200, json={"items": rows})

    s = SimpleNamespace(alerce_classifier="stamp_classifier", alerce_classes="SN,AGN",
                        alerce_probability=0.4, alerce_page_size=100, alerce_max_pages=2,
                        alerce_min_ndet=0)
    objs = asyncio.run(_run_query(handler, s))
    assert {o["id"]: o["class_label"] for o in objs} == {"ZTF_sn": "SN", "ZTF_agn": "AGN"}


def test_query_recent_falls_back_when_classified_empty():
    import httpx

    def handler(request):
        if "classifier" in request.url.params:          # classified query → empty
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"items": [    # plain recent query → data
            {"oid": "ZTF_plain", "meanra": 1.0, "meandec": 2.0,
             "firstmjd": 1, "lastmjd": 100.0}]})

    s = SimpleNamespace(alerce_classifier="stamp_classifier", alerce_classes="SN,AGN",
                        alerce_probability=0.4, alerce_page_size=100, alerce_max_pages=2,
                        alerce_min_ndet=0)
    objs = asyncio.run(_run_query(handler, s))
    assert [o["id"] for o in objs] == ["ZTF_plain"]    # never empty when objects exist


# ------------------------------------------------------------- visibility
def test_night_label_before_and_after_local_noon():
    # 02:00 UTC = 08:00 local (morning) -> still the previous evening's night (06-17)
    assert night_label(dt.datetime(2026, 6, 18, 2, tzinfo=dt.timezone.utc), OFFSET) == "20260617"
    # 14:00 UTC = 20:00 local (evening) -> that night (06-18)
    assert night_label(dt.datetime(2026, 6, 18, 14, tzinfo=dt.timezone.utc), OFFSET) == "20260618"


def test_compute_night_reaches_astronomical_dark():
    loc = site_location(DHAKA)
    night = compute_night(loc, WHEN, OFFSET)
    assert night.ut_date == "20260618"
    assert night.start_utc < night.end_utc
    assert night.twilight_used == -18.0  # Dhaka gets fully dark in June
    assert len(night.times) > 1


def _transit_target(night, loc):
    """RA/Dec of a target that transits at mid dark-window (guaranteed up)."""
    mid = night.times[len(night.times) // 2]
    lst = mid.sidereal_time("apparent", longitude=loc.lon)
    return lst.to(u.deg).value, loc.lat.to(u.deg).value


def test_far_south_target_not_observable():
    loc = site_location(DHAKA)
    night = compute_night(loc, WHEN, OFFSET)
    # dec -75 from lat +23.8: peak altitude is below the horizon all night
    v = visibility(150.0, -75.0, night, loc, alt_min_deg=30.0)
    assert v.observable is False
    assert v.window_start_utc is None


def test_transiting_target_observable_high():
    loc = site_location(DHAKA)
    night = compute_night(loc, WHEN, OFFSET)
    ra, dec = _transit_target(night, loc)
    v = visibility(ra, dec, night, loc, alt_min_deg=30.0)
    assert v.observable is True
    assert v.max_alt_deg > 80.0           # transits near the zenith at Dhaka
    assert v.min_airmass < 1.1
    assert v.window_start_utc < v.window_end_utc
    assert 0.0 <= v.moon_illum_frac <= 1.0


# --------------------------------------------------- candidate evaluation
def _settings():
    return SimpleNamespace(
        utc_offset_hours=OFFSET, alt_min_deg=30.0, mag_limit=18.5,
        score_w_prob=1.0, score_w_alt=1.0, score_w_airmass=0.5,
        score_w_moon=0.5, score_w_faint=0.5,
        default_exptime_s=120.0, default_count=3, default_filter_slot=None,
        auto_execute=False,
    )


async def _run_eval(tmp_path):
    from cassa.core.db import DB
    from cassa.core import transient_db  # noqa: F401 — register tables
    from cassa.transient.candidates import CandidateService

    db = DB(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await db.init()
    obs = SimpleNamespace(location=DHAKA)
    svc = CandidateService(_settings(), obs, db.sessionmaker)

    night = svc.night(WHEN)
    loc = svc._loc
    ra, dec = _transit_target(night, loc)
    objs = [
        {"id": "ZTF_up", "source": "alerce", "ra_deg": ra, "dec_deg": dec,
         "class_label": "SN", "class_prob": 0.9, "mag_last": 17.0,
         "ndethist": 5, "firstmjd": 1.0, "lastmjd": 2.0, "raw_json": {}},
        {"id": "ZTF_south", "source": "alerce", "ra_deg": 150.0, "dec_deg": -75.0,
         "class_label": "SN", "class_prob": 0.8, "mag_last": 17.0,
         "ndethist": 5, "firstmjd": 1.0, "lastmjd": 2.0, "raw_json": {}},
        {"id": "ZTF_faint", "source": "alerce", "ra_deg": ra, "dec_deg": dec,
         "class_label": "SN", "class_prob": 0.8, "mag_last": 21.0,  # past mag_limit
         "ndethist": 5, "firstmjd": 1.0, "lastmjd": 2.0, "raw_json": {}},
    ]
    # patch night() so evaluate uses the same fixed-date night as the test
    svc.night = lambda when=None: night
    n_obs = await svc.evaluate_alerts(objs)
    grouped = await svc.list_candidates(ut_date=night.ut_date, group_by="class")
    await db.dispose()
    return n_obs, grouped


def test_candidate_evaluation_shows_all_and_tags_observable(tmp_path):
    n_obs, grouped = asyncio.run(_run_eval(tmp_path))
    # everything is shown; observability is a tag, not a filter
    assert grouped["count"] == 3
    assert n_obs == 2 and grouped["observable"] == 2   # the two transit-coord objects
    sn = grouped["groups"]["SN"]
    assert len(sn) == 3
    assert sn[0]["observable"] is True and sn[0]["max_alt_deg"] > 80.0  # observable sorts first
    south = next(c for c in sn if c["id"].startswith("ZTF_south"))
    assert south["observable"] is False
    assert south["max_alt_deg"] is not None            # peak alt reported even when not up
    assert south["window_start_utc"] is None


async def _run_approve(tmp_path, action):
    from cassa.core.db import DB
    from cassa.core import transient_db  # noqa: F401
    from cassa.transient.candidates import CandidateService

    db = DB(f"sqlite+aiosqlite:///{tmp_path}/a.db")
    await db.init()
    svc = CandidateService(_settings(), SimpleNamespace(location=DHAKA), db.sessionmaker)
    night = svc.night(WHEN)
    svc.night = lambda when=None: night
    ra, dec = _transit_target(night, svc._loc)
    await svc.evaluate_alerts([
        {"id": "ZTF_up", "source": "alerce", "ra_deg": ra, "dec_deg": dec,
         "class_label": "SN", "class_prob": 0.9, "mag_last": 17.0,
         "ndethist": 5, "firstmjd": 1.0, "lastmjd": 2.0, "raw_json": {}},
    ])
    cid = f"ZTF_up_{night.ut_date}"
    if action == "reject":
        await svc.reject(cid, actor="tester")
    else:
        await svc.approve(cid, action=action, actor="tester")
    detail = await svc.get(cid)
    await db.dispose()
    return detail


def test_approve_queue_transition_and_audit(tmp_path):
    detail = asyncio.run(_run_approve(tmp_path, "queue"))
    assert detail["state"] == "approved_queue"
    assert detail["decided_by"] == "tester"
    actions = [a["action"] for a in detail["audit"]]
    assert "approve_queue" in actions


def test_approve_execute_transition(tmp_path):
    detail = asyncio.run(_run_approve(tmp_path, "execute"))
    assert detail["state"] == "approved_execute"


def test_reject_transition_and_audit(tmp_path):
    detail = asyncio.run(_run_approve(tmp_path, "reject"))
    assert detail["state"] == "rejected"
    assert [a["action"] for a in detail["audit"]] == ["reject"]


# --------------------------------------------------- request builder / queue
async def _run_build(tmp_path, action):
    from cassa.core.db import DB
    from cassa.core import transient_db  # noqa: F401
    from cassa.transient.candidates import CandidateService
    from cassa.transient.requests import RequestBuilder

    db = DB(f"sqlite+aiosqlite:///{tmp_path}/b.db")
    await db.init()
    settings = _settings()
    rb = RequestBuilder(settings, db.sessionmaker)
    svc = CandidateService(settings, SimpleNamespace(location=DHAKA), db.sessionmaker,
                           request_builder=rb)
    night = svc.night(WHEN)
    svc.night = lambda when=None: night
    ra, dec = _transit_target(night, svc._loc)
    await svc.evaluate_alerts([
        {"id": "ZTF_up", "source": "alerce", "ra_deg": ra, "dec_deg": dec,
         "class_label": "SN", "class_prob": 0.9, "mag_last": 17.0,
         "ndethist": 5, "firstmjd": 1.0, "lastmjd": 2.0, "raw_json": {}},
    ])
    cid = f"ZTF_up_{night.ut_date}"
    cand = await svc.approve(cid, action=action, actor="tester")
    queue = await rb.list_queue()
    await db.dispose()
    return cand, queue


def test_approve_builds_queue_block_with_steps(tmp_path):
    cand, queue = asyncio.run(_run_build(tmp_path, "queue"))
    assert cand["request_id"]                       # candidate linked to its request
    assert len(queue) == 1
    b = queue[0]
    assert b["state"] == "queued"
    # slew + center + autofocus + 3 expose (default_count)
    assert b["total_steps"] == 6
    assert b["request"]["mode"] == "attended"
    assert b["request"]["object_name"] == "ZTF_up"
    assert b["class_label"] == "SN"


def test_approve_execute_sets_auto_mode(tmp_path):
    _cand, queue = asyncio.run(_run_build(tmp_path, "execute"))
    assert queue[0]["request"]["mode"] == "auto"


# ------------------------------------------------------ execution sequencer
class _FakeMount:
    def __init__(self, fail=False):
        self.fail = fail
        self.slewed = None

    async def slew_to_radec(self, ra_hours, dec_deg, track=True):
        if self.fail:
            raise RuntimeError("slew failed")
        self.slewed = (ra_hours, dec_deg)

    def status(self):
        return SimpleNamespace(slewing=False)

    async def abort(self):
        pass


class _FakeCamera:
    def status(self):
        return SimpleNamespace(exposure_remaining=0.0)


class _FakeDM:
    def __init__(self, fail_slew=False):
        self.connected = True
        self.mount = _FakeMount(fail_slew)
        self.camera = _FakeCamera()
        self.captures = 0

    async def capture(self, seconds, image_type, object_name, filter_slot=None, role="camera", binning=None):
        self.captures += 1
        return {"fits": b"FAKEFITS", "meta": {"object_name": object_name, "exptime": seconds}}


class _FakeArchive:
    def __init__(self):
        self.ingested = []

    async def ingest(self, fits, meta):
        iid = f"img{len(self.ingested)}"
        self.ingested.append((iid, meta))
        return {"id": iid}


async def _run_executor(tmp_path, fail_slew=False):
    from cassa.core.db import DB
    from cassa.core import transient_db  # noqa: F401
    from cassa.core.transient_db import ExecutionBlock, ExecutionStep
    from cassa.transient.executor import ExecutionSequencer
    from cassa.transient.requests import RequestBuilder
    from sqlalchemy import select as _select

    db = DB(f"sqlite+aiosqlite:///{tmp_path}/e.db")
    await db.init()
    settings = _settings()
    rb = RequestBuilder(settings, db.sessionmaker)
    cand = {"id": "C1", "alert_id": "ZTF_x", "ra_deg": 150.0, "dec_deg": 24.0,
            "score": 5, "window_start_utc": None, "window_end_utc": None}
    built = await rb.build(cand, action="queue")
    block_id = built["block_id"]

    dm, archive = _FakeDM(fail_slew), _FakeArchive()
    state = SimpleNamespace(settings=settings, db=db, dm=dm, archive=archive)
    ex = ExecutionSequencer(SimpleNamespace(state=state))
    await ex._run_block(block_id)

    async with db.sessionmaker() as s:
        block = await s.get(ExecutionBlock, block_id)
        steps = (await s.execute(
            _select(ExecutionStep).where(ExecutionStep.block_id == block_id)
            .order_by(ExecutionStep.seq)
        )).scalars().all()
        result = (block.state, block.n_done, block.n_failed,
                  [(st.kind, st.state, st.image_id) for st in steps])
    await db.dispose()
    return result, dm, archive


def test_sequencer_runs_block_end_to_end(tmp_path):
    (state, n_done, n_failed, steps), dm, archive = asyncio.run(_run_executor(tmp_path))
    assert state == "done"
    assert n_done == 3 and n_failed == 0          # 3 default exposures
    assert len(archive.ingested) == 3 and dm.captures == 3
    kinds = {k: (st, img) for k, st, img in steps}
    assert kinds["slew"][0] == "done"
    assert kinds["center"][0] == "skipped" and kinds["autofocus"][0] == "skipped"
    # every expose step is done and linked to an archived image
    exposes = [(st, img) for k, st, img in steps if k == "expose"]
    assert all(st == "done" and img for st, img in exposes)


def test_sequencer_aborts_on_slew_failure(tmp_path):
    (state, n_done, n_failed, steps), dm, archive = asyncio.run(_run_executor(tmp_path, fail_slew=True))
    assert state == "aborted"
    assert n_done == 0 and dm.captures == 0       # never got to expose
    assert archive.ingested == []
    slew = next(st for k, st, _ in steps if k == "slew")
    assert slew == "failed"


# ----------------------------------------------------- auto-execute gating
async def _next_runnable(tmp_path, *, auto_execute, override=False, window="now"):
    from cassa.core.db import DB
    from cassa.core import transient_db  # noqa: F401
    from cassa.transient.executor import ExecutionSequencer
    from cassa.transient.requests import RequestBuilder

    db = DB(f"sqlite+aiosqlite:///{tmp_path}/g.db")
    await db.init()
    settings = _settings()
    settings.auto_execute = auto_execute
    rb = RequestBuilder(settings, db.sessionmaker)
    now = dt.datetime.now(dt.timezone.utc)
    if window == "now":
        ws, we = now - dt.timedelta(hours=1), now + dt.timedelta(hours=1)
    else:  # "past"
        ws, we = now - dt.timedelta(hours=2), now - dt.timedelta(hours=1)
    cand = {"id": "C1", "alert_id": "ZTF_x", "ra_deg": 150.0, "dec_deg": 24.0, "score": 5,
            "window_start_utc": ws.isoformat(), "window_end_utc": we.isoformat()}
    built = await rb.build(cand, action="execute")          # mode=auto
    state = SimpleNamespace(settings=settings, db=db, dm=_FakeDM(), archive=_FakeArchive())
    ex = ExecutionSequencer(SimpleNamespace(state=state))
    ex.manual_override = override
    picked = await ex._next_runnable()
    await db.dispose()
    return picked, built["block_id"]


def test_auto_dispatch_requires_flag(tmp_path):
    picked, bid = asyncio.run(_next_runnable(tmp_path, auto_execute=True))
    assert picked == bid                                    # flag on, in window → dispatch
    picked_off, _ = asyncio.run(_next_runnable(tmp_path, auto_execute=False))
    assert picked_off is None                               # flag off → never auto-runs


def test_auto_dispatch_blocked_by_override(tmp_path):
    picked, _ = asyncio.run(_next_runnable(tmp_path, auto_execute=True, override=True))
    assert picked is None                                   # manual override wins


def test_auto_dispatch_respects_window(tmp_path):
    picked, _ = asyncio.run(_next_runnable(tmp_path, auto_execute=True, window="past"))
    assert picked is None                                   # window passed → not dispatched


# ------------------------------------------------------ approval (Slack/email)
def test_sign_verify_token_roundtrip():
    from cassa.transient.approvals import sign_token, verify_token
    tok = sign_token("ZTF_x_20260618", "execute", "s3cret")
    assert verify_token(tok, "s3cret") == ("ZTF_x_20260618", "execute")


def test_verify_token_rejects_tampering_and_wrong_secret():
    import pytest
    from cassa.transient.approvals import sign_token, verify_token
    tok = sign_token("C1", "queue", "s3cret")
    with pytest.raises(ValueError):
        verify_token(tok, "wrong-secret")
    tampered = tok[:5] + ("A" if tok[5] != "A" else "B") + tok[6:]  # flip a payload char
    with pytest.raises(ValueError):
        verify_token(tampered, "s3cret")
    with pytest.raises(ValueError):
        verify_token(sign_token("C1", "queue", "s3cret", ttl=-10), "s3cret")  # expired


def test_slack_blocks_have_three_decision_buttons():
    from cassa.transient.approvals import ApprovalService
    svc = ApprovalService(_settings())
    cand = {"id": "ZTF_x_20260618", "alert_id": "ZTF_x", "class_label": "SN",
            "class_prob": 0.9, "ra_deg": 150.0, "dec_deg": 24.0, "mag": 17.0,
            "max_alt_deg": 80.0, "min_airmass": 1.0, "moon_sep_deg": 60.0,
            "score": 3.0, "window_start_utc": "2026-06-18T15:00:00.000",
            "window_end_utc": "2026-06-18T20:00:00.000"}
    blocks = svc.slack_blocks(cand)
    actions = [b for b in blocks if b["type"] == "actions"][0]
    ids = [e["action_id"] for e in actions["elements"]]
    assert ids == ["approve_queue", "approve_execute", "reject"]
    assert all(e["value"] == cand["id"] for e in actions["elements"])


async def _plan_setup(tmp_path):
    from cassa.core.db import DB
    from cassa.core import transient_db  # noqa: F401
    from cassa.transient.plans import PlanService

    db = DB(f"sqlite+aiosqlite:///{tmp_path}/p.db")
    await db.init()
    return db, PlanService(_settings(), db.sessionmaker)


async def _run_plan_flow(tmp_path):
    from cassa.core.transient_db import ExecutionStep
    from sqlalchemy import select as _select

    db, ps = await _plan_setup(tmp_path)
    saved = await ps.save_plan({
        "name": "M42 LRGB", "object_name": "M42", "ra_deg": 83.82, "dec_deg": -5.39,
        "recipe": [
            {"filter_slot": 1, "filter_name": "L", "exptime_s": 120, "count": 3, "binning": 1, "dither_px": 0},
            {"filter_slot": 2, "filter_name": "R", "exptime_s": 60, "count": 2, "binning": 2, "dither_px": 5},
        ],
        "repeat": 2, "autofocus": True, "center": True,
    })
    plans = await ps.list_plans()
    run = await ps.run_plan(saved["id"])
    async with db.sessionmaker() as s:
        steps = (await s.execute(
            _select(ExecutionStep).where(ExecutionStep.block_id == run["block_id"]).order_by(ExecutionStep.seq)
        )).scalars().all()
        kinds = [st.kind for st in steps]
        binnings = [st.params_json.get("binning") for st in steps if st.kind == "expose"]
    await db.dispose()
    return saved, plans, run, kinds, binnings


def test_plan_save_and_expand(tmp_path):
    saved, plans, run, kinds, binnings = asyncio.run(_run_plan_flow(tmp_path))
    assert saved["id"] and saved["name"] == "M42 LRGB" and len(plans) == 1
    assert run["resumed"] is False and kinds[0] == "slew"
    assert kinds.count("expose") == 10           # 2 repeats × (3 + 2)
    assert kinds.count("dither") == 3            # R dithers (between exposures, not after the last)
    assert "center" in kinds and "autofocus" in kinds
    assert binnings.count(1) == 6 and binnings.count(2) == 4   # L bin1 ×6, R bin2 ×4


async def _run_resume(tmp_path):
    from cassa.core.transient_db import BlockState, ExecutionBlock, ExecutionStep, StepState
    from sqlalchemy import select as _select

    db, ps = await _plan_setup(tmp_path)
    saved = await ps.save_plan({
        "name": "x", "object_name": "M42", "ra_deg": 10.0, "dec_deg": 20.0,
        "recipe": [{"filter_slot": 1, "filter_name": "L", "exptime_s": 5, "count": 3, "binning": 1, "dither_px": 0}],
        "repeat": 1,
    })
    bid = (await ps.run_plan(saved["id"]))["block_id"]
    async with db.sessionmaker() as s:
        block = await s.get(ExecutionBlock, bid)
        block.state = BlockState.ABORTED.value
        steps = (await s.execute(
            _select(ExecutionStep).where(ExecutionStep.block_id == bid).order_by(ExecutionStep.seq)
        )).scalars().all()
        steps[0].state = StepState.DONE.value     # slew
        steps[1].state = StepState.DONE.value     # expose 1
        steps[2].state = StepState.FAILED.value   # expose 2
        await s.commit()
    resume = await ps.run_plan(saved["id"], resume=True)
    async with db.sessionmaker() as s:
        block = await s.get(ExecutionBlock, bid)
        states = [st.state for st in (await s.execute(
            _select(ExecutionStep).where(ExecutionStep.block_id == bid).order_by(ExecutionStep.seq)
        )).scalars().all()]
        bstate = block.state
    await db.dispose()
    return resume, bid, bstate, states


def test_plan_resume_rearms_incomplete_block(tmp_path):
    resume, bid, bstate, states = asyncio.run(_run_resume(tmp_path))
    assert resume["resumed"] is True and resume["block_id"] == bid
    assert bstate == "queued"                      # re-armed for the sequencer
    assert states[0] == "done" and states[1] == "done"   # completed steps kept
    assert states[2] == "pending"                  # failed step reset for retry


def test_notify_is_noop_without_channels():
    # no Slack tokens, no SMTP configured → notify must not raise
    from cassa.transient.approvals import ApprovalService
    s = _settings()
    s.slack_bot_token = s.slack_app_token = s.slack_channel = ""
    s.smtp_host = s.smtp_to = ""
    svc = ApprovalService(s)
    cand = {"id": "C1", "alert_id": "ZTF_x", "class_label": "SN", "class_prob": 0.9,
            "ra_deg": 1.0, "dec_deg": 2.0, "mag": None, "max_alt_deg": 80.0,
            "min_airmass": 1.0, "moon_sep_deg": 60.0, "score": 1.0,
            "window_start_utc": None, "window_end_utc": None}
    asyncio.run(svc.notify(cand))  # should simply do nothing
