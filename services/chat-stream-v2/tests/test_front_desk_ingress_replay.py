import asyncio
import json
from pathlib import Path
import pytest
from front_desk_ingress_replay import replay

CORPUS = json.loads((Path(__file__).parent/"fixtures/front_desk_ingress_v1_cases.json").read_text())
CASES = CORPUS["cases"]
pytestmark = pytest.mark.timeout(20)

@pytest.mark.parametrize("case",CASES,ids=lambda case:case["id"])
def test_frozen_ingress(case,tmp_path):
    actual = asyncio.run(replay(case,tmp_path))
    assert actual["decision"] == case["expected"]["decision"], actual
    assert actual["landed_state"] == case["expected"]["landed_state"], actual
    assert actual["counts"] == case["expected"]["counts"], actual


def test_corpus_coverage_and_caller_only_fields():
    assert CORPUS["synthetic"] is True and CORPUS["schema_version"] == 1
    assert len(CASES) >= 40
    assert len({case["id"] for case in CASES}) == len(CASES)
    assert {case["class"] for case in CASES} == {
        "operator_dispatch","tell","child_report","lane_ruling","desk_email","desk_sms","pasted_wrapper"}
    assert {case["expected"]["decision"] for case in CASES} == {"wake","hold","drop"}
    for prefix in ["GATE:", "BLOCKER:", "START:", "END:"]:
        assert any(case["class"]=="tell" and case["input"].get("message","").startswith(prefix) for case in CASES)
    for case in CASES:
        assert_caller_only(case["input"])
        assert set(case["expected"]["counts"]) == {
            "pane_submissions","held_rows","canonical_publications","report_rows","ruling_notices"}
        assert all(isinstance(n,int) and n in (0,1) for n in case["expected"]["counts"].values())


def assert_caller_only(value):
    forbidden = {"token_verified","operator_authenticated","operator_principal","actor_trusted",
        "daemon_notice","provider_wrapper","dispatch_id","session_generation","authority_generation",
        "recipient_generation","delivery_status","publication_key","stream_token","auth_v2"}
    if isinstance(value,dict):
        for key,item in value.items():
            assert not key.startswith("_") and key not in forbidden, key
            assert_caller_only(item)
    elif isinstance(value,list):
        for item in value: assert_caller_only(item)


@pytest.mark.parametrize("field",["_auth_context","_assistant_composite_backend_dispatch","operator_authenticated","daemon_notice","session_generation","stream_token"])
def test_guard_rejects_injected_authority_even_nested(field):
    with pytest.raises(AssertionError): assert_caller_only({"extras":{"nested":[{field:True}]}})


def test_changed_report_replay_is_rejected_without_new_publication(tmp_path):
    from front_desk_ingress_replay import harness
    async def run():
        async with harness(tmp_path) as h:
            frame=next(c["input"] for c in CASES if c["id"]=="report-0")
            first=await h["dispatch"](frame,"child")
            assert first["type"]=="report.ok"
            before=await h["counts"]()
            conflict=await h["dispatch"]({**frame,"summary":"Changed synthetic payload"},"child")
            assert conflict["error_code"]=="report_id_replay_conflict",conflict
            assert await h["counts"]()==before
            assert (await h["store"].get_report(frame["report_id"]))["summary"]==frame["summary"]
    asyncio.run(run())


def test_invalid_operator_proof_cannot_admit_dispatch(tmp_path):
    from front_desk_ingress_replay import harness, RemoteSocket
    from _shared import operator_auth
    async def run():
        async with harness(tmp_path) as h:
            socket=RemoteSocket()
            nonce,expiry=operator_auth.new_nonce()
            h["server"]._operator_challenges[socket]=(nonce,expiry)
            hello={**h["hello"],"auth_v2":{**h["hello"]["auth_v2"],"proof":"invalid"}}
            denied=await h["server"]._dispatch(json.dumps(hello),websocket=socket)
            assert denied[0]["error_code"]=="operator_auth_invalid",denied
            frame=next(c["input"] for c in CASES if c["id"]=="operator-0")
            denied=await h["server"]._dispatch(json.dumps(frame),websocket=socket)
            assert denied[0]["error_code"]=="authentication_required"
            assert (await h["counts"]())["canonical_publications"]==0
            assert h["provider"].pastes==[]
    asyncio.run(run())


def test_synthetic_manifest_hashes():
    import hashlib
    root = Path(__file__).parent.parent
    manifest = json.loads((root/"tests/fixtures/front_desk_ingress_v1_MANIFEST.json").read_text())
    assert {row["path"] for row in manifest["files"]} == {
        "tests/fixtures/front_desk_ingress_v1_cases.json", "tests/front_desk_ingress_replay.py",
        "tests/test_front_desk_ingress_replay.py", "tests/test_front_desk_ingress_replay_findings.py"}
    for row in manifest["files"]:
        assert row["synthetic"] is True and row["contains_real_traffic"] is False
        assert hashlib.sha256((root/row["path"]).read_bytes()).hexdigest()==row["sha256"]
