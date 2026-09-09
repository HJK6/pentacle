from __future__ import annotations

import subprocess

import pytest

from tests.soak.harness import (
    CpuSampler,
    SamplerInfrastructureError,
    _CLK_TCK,
    _parse_macos_cpu_output,
    _parse_macos_fd_output,
    _parse_proc_stat_ticks,
)


class FakeProcess:
    def __init__(self, pid: int, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


class FakeDaemon:
    def __init__(self, proc: FakeProcess | None) -> None:
        self.proc = proc


def _completed(
    command: list[str], stdout: str, returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, "")


def test_linux_stat_and_fd_parsers_preserve_proc_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fields = ["S"] + ["0"] * 10 + ["7", "5"]
    assert _parse_proc_stat_ticks(f"123 (daemon) {' '.join(fields)}") == 12 / _CLK_TCK

    monkeypatch.setattr("tests.soak.harness.os.listdir", lambda _path: ["0", "1", "2"])
    sampler = CpuSampler(FakeDaemon(FakeProcess(123)), platform_name="linux")
    assert sampler._fd_count(123) == 3


def test_macos_machine_readable_cpu_and_fd_parsers() -> None:
    assert _parse_macos_cpu_output("123   1:02.50\n", 123) == 62.5
    assert _parse_macos_cpu_output("123   1-02:03:04.50\n", 123) == 93784.5
    assert _parse_macos_fd_output("p123\nfcwd\nftxt\nf0\nf1\n", 123) == 4
    assert _parse_macos_fd_output("p999\nf0\n", 123) is None


def test_macos_sampler_preflight_uses_exact_facilities_and_existing_api() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[0] == "/bin/ps":
            return _completed(command, "123   0:01.25\n")
        return _completed(command, "p123\nfcwd\nftxt\nf0\n")

    sampler = CpuSampler(
        FakeDaemon(FakeProcess(123)),
        platform_name="darwin",
        command_runner=runner,
    )
    sampler.preflight()

    assert calls == [
        ["/bin/ps", "-p", "123", "-o", "pid=,cputime="],
        ["/usr/sbin/lsof", "-n", "-P", "-p", "123", "-F", "f"],
    ]


def test_missing_macos_facility_is_named_infrastructure_failure() -> None:
    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[0] == "/bin/ps":
            return _completed(command, "123   0:01.25\n")
        raise FileNotFoundError(2, "No such file or directory", command[0])

    sampler = CpuSampler(
        FakeDaemon(FakeProcess(123)),
        platform_name="darwin",
        command_runner=runner,
    )
    with pytest.raises(SamplerInfrastructureError) as caught:
        sampler.preflight()
    assert caught.value.reason == "missing_facility"
    assert caught.value.code == "sampler_missing_facility"


def test_dead_pid_is_named_infrastructure_failure() -> None:
    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        return _completed(command, "", returncode=1)

    sampler = CpuSampler(
        FakeDaemon(FakeProcess(123)),
        platform_name="darwin",
        command_runner=runner,
    )
    with pytest.raises(SamplerInfrastructureError) as caught:
        sampler.preflight()
    assert caught.value.reason == "dead_pid"


def test_unsupported_platform_refuses_before_sampler_thread_starts() -> None:
    sampler = CpuSampler(FakeDaemon(FakeProcess(123)), platform_name="freebsd")
    with pytest.raises(SamplerInfrastructureError) as caught:
        sampler.start()
    assert caught.value.reason == "unsupported_platform"
    assert not sampler._thread.is_alive()


def test_pid_swap_resets_baseline_and_accounts_each_sample_to_its_root_pid() -> None:
    class SwitchingDaemon:
        def __init__(self) -> None:
            self._pids = iter([1, 1, 2, 2])

        @property
        def proc(self) -> FakeProcess:
            return FakeProcess(next(self._pids))

    class StopAfterThreeSamples:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, _interval: float) -> bool:
            self.calls += 1
            return self.calls == 4

    cpu_values = {1: iter([10.0, 11.0]), 2: iter([100.0, 101.0])}
    sampler = CpuSampler(
        SwitchingDaemon(),
        interval_s=1.0,
        platform_name="linux",
        clock=iter([0.0, 1.0, 2.0, 3.0]).__next__,
    )
    sampler._cpu_seconds = lambda pid: next(cpu_values[pid])  # type: ignore[method-assign]
    sampler._fd_count = lambda _pid: 4  # type: ignore[method-assign]
    sampler._stop = StopAfterThreeSamples()  # type: ignore[assignment]

    sampler._run()

    assert sampler.samples == [100.0, 100.0]
    assert sampler.sample_pids == [1, 2]
    assert sampler._fds == [4, 4]
