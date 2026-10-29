"""Bounded process-isolated execution for the required deterministic B table."""
import asyncio
from concurrent.futures import ProcessPoolExecutor
import gc
import hashlib
import json
import logging
import multiprocessing
import os
from pathlib import Path
import platform
import shutil
import signal
import sys
import subprocess
import time
import traceback

import pytest


def _case(case,root):
    from repin_state_matrix import run
    started=time.monotonic();key=hashlib.sha256(case.id.encode()).hexdigest()
    directory=Path(root)/key;directory.mkdir()
    logging.disable(logging.CRITICAL)
    def expired(_signal,_frame):raise TimeoutError("matrix case exceeded 45 seconds")
    previous=signal.signal(signal.SIGALRM,expired);signal.setitimer(signal.ITIMER_REAL,45)
    error=None
    try:
        with pytest.MonkeyPatch.context() as isolated:asyncio.run(run(case,directory,isolated))
    except BaseException:
        error=traceback.format_exc()
    finally:
        signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,previous)
        shutil.rmtree(directory)
        gc.collect()
    evidence=Path(os.environ["PENTACLE_GATE_EVIDENCE_DIR"])
    if error:
        failures=evidence/"matrix-failures";failures.mkdir(exist_ok=True)
        (failures/(key+".txt")).write_text(case.id+"\n"+error)
    receipt=evidence/"state-machine-cases"/(key+".json")
    if not receipt.exists():error=error or "case did not write required state snapshots"
    return {"case_id":case.id,"outcome":"failed" if error else "passed","seconds":time.monotonic()-started,
        "pid":os.getpid(),"receipt_sha256":hashlib.sha256(receipt.read_bytes()).hexdigest() if receipt.exists() else None}


def _batch(arguments):
    case,root=arguments
    return _case(case,root)


def inputs():
    root=Path(__file__).resolve().parents[3]
    selected=[*Path(__file__).parent.glob("repin_*matrix*.py"),Path(__file__).parent/"test_account_repin.py",
        Path(__file__).parent/"test_account_repin_state_machine.py"]
    for pattern in ("services/chat-stream-v2/account*.py","services/chat-stream-v2/provider_account*.py",
            "services/chat-stream-v2/provider_cli*.py","services/_shared/provider_cli_release.py",
            "scripts/gate_env.py","services/chat-stream-v2/tools/run_gate.py","services/chat-stream-v2/pytest.ini"):
        selected.extend(root.glob(pattern))
    head=subprocess.check_output(["git","rev-parse","HEAD"],cwd=root,text=True).strip()
    tree=subprocess.check_output(["git","rev-parse","HEAD^{tree}"],cwd=root,text=True).strip()
    return {"candidate_sha":head,"candidate_tree":tree,"files":{str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(set(selected))},
        "python":sys.version,"executable":sys.executable,"platform":platform.platform(),
        "gate_evidence_dir":os.environ["PENTACLE_GATE_EVIDENCE_DIR"]}


def execute(cases,tmp_path,*,workers):
    evidence=Path(os.environ["PENTACLE_GATE_EVIDENCE_DIR"]);started=time.monotonic()
    ids=[case.id for case in cases]
    assert len(ids)==len(set(ids)),"Each transition product needs a unique stable case ID"
    receipts=[];children=set();before=inputs()
    with ProcessPoolExecutor(max_workers=workers,mp_context=multiprocessing.get_context("spawn")) as pool:
        for receipt in pool.map(_batch,((case,str(tmp_path)) for case in cases),chunksize=4):
            receipts.append(receipt);children.add(receipt["pid"])
    for pid in children:
        with pytest.raises(ProcessLookupError):os.kill(pid,0)
    assert not any(tmp_path.iterdir()),"Every owned case fixture must be removed after its receipt"
    assert inputs()==before,"Source, harness and interpreter inputs must stay frozen through the run"
    return receipts,{"seconds":time.monotonic()-started,"workers":workers,"worker_pids":sorted(children),
        "all_workers_exited":True,"all_case_fixtures_removed":True,"inputs":inputs()}
