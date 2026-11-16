from __future__ import annotations

import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from tests.soak.harness import CpuSampler


pytestmark = pytest.mark.soak


def test_bounded_real_macos_process_sampler_has_cpu_fd_and_root_pid_samples() -> None:
    if sys.platform != "darwin":
        pytest.fail("bounded sampler integration is required on hosta/macOS")

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"])
    sampler = CpuSampler(SimpleNamespace(proc=proc), interval_s=0.05)
    try:
        sampler.start()
        deadline = time.monotonic() + 3.0
        while len(sampler.samples) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        sampler.stop()
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=5)

    assert not sampler._thread.is_alive()
    assert len(sampler.samples) >= 3
    assert all(value >= 0 for value in sampler.samples)
    assert sampler.sample_pids == [proc.pid] * len(sampler.samples)
    assert sampler.fd_max() > 0
    assert proc.returncode is not None
