"""显式进程观测；由原租约测试提取，不访问宿主。"""

from pathlib import Path

from trippostcollect.xhs.leases import ProcessIdentity, ProcessSnapshot


OWNER = ProcessIdentity(
    host_id="host-a",
    boot_id="boot-a",
    pid=111,
    process_started_at="2026-08-30T00:00:00+00:00",
    process_start_token="owner-start-111",
    pgid=111,
)

class FakeInspector:
    def __init__(
        self,
        *,
        host_id: str = "host-a",
        boot_id: str = "boot-a",
        identities: dict[int, ProcessIdentity] | None = None,
        presences: dict[int, bool | None] | None = None,
        groups: dict[int, list[ProcessSnapshot]] | None = None,
        profile_processes: list[ProcessSnapshot] | None = None,
        current: ProcessIdentity | None = None,
    ):
        self.host_id = host_id
        self.boot_id = boot_id
        self.identities = identities or {}
        self.presences = presences or {}
        self.groups = groups or {}
        self.profiles = profile_processes or []
        self.profile_calls: list[Path] = []
        self.current = current

    def identity(self, pid: int) -> ProcessIdentity | None:
        return self.identities.get(int(pid))

    def process_presence(self, pid: int) -> bool | None:
        if int(pid) in self.presences:
            return self.presences[int(pid)]
        return int(pid) in self.identities

    def group_members(self, pgid: int) -> list[ProcessSnapshot]:
        return list(self.groups.get(int(pgid), []))

    def profile_processes(self, profile_dir: Path) -> list[ProcessSnapshot]:
        self.profile_calls.append(Path(profile_dir).expanduser().resolve())
        return list(self.profiles)

    def current_identity(self) -> ProcessIdentity:
        if self.current is None:
            raise RuntimeError("fake current process identity was not configured")
        return self.current


class FakeMonotonicClock:
    def __init__(self, value: float = 0.0):
        self.value = float(value)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        assert 0 <= seconds <= 0.1
        self.sleeps.append(seconds)
        self.value += seconds

    def advance(self, seconds: float) -> None:
        self.value += seconds
