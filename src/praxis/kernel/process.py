"""Process identities and durable lifecycle metadata."""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from praxis.kernel.lifecycle import State, transition
from praxis.kernel.spec import ProcessSpec


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class LifecycleEntry:
    state: State
    timestamp: str
    attempt_id: str


@dataclass
class Process:
    spec: ProcessSpec
    process_id: str = field(default_factory=lambda: str(uuid4()))
    parent_id: str | None = None
    attempt_id: str = field(default_factory=lambda: str(uuid4()))
    history: list[LifecycleEntry] = field(default_factory=list)

    def __post_init__(self) -> None:
        UUID(self.process_id)
        UUID(self.attempt_id)
        if self.parent_id is not None:
            UUID(self.parent_id)
            if self.parent_id == self.process_id:
                raise ValueError("process cannot parent itself")
        if not self.history:
            self.history.append(LifecycleEntry(State.PENDING, now(), self.attempt_id))

    @property
    def state(self) -> State:
        return self.history[-1].state

    @property
    def created_at(self) -> str:
        return self.history[0].timestamp

    def move(self, target: State) -> None:
        transition(self.state, target)
        self.history.append(LifecycleEntry(target, now(), self.attempt_id))

    def to_json(self) -> str:
        data = asdict(self)
        data["spec"] = json.loads(self.spec.to_json())
        return json.dumps(data, sort_keys=True, allow_nan=False)

    @classmethod
    def from_json(cls, raw: str) -> "Process":
        try:
            data = json.loads(raw)
            data["spec"] = ProcessSpec.from_json(json.dumps(data["spec"]))
            entries = data["history"]
            if not entries:
                raise ValueError("missing lifecycle history")
            history: list[LifecycleEntry] = []
            for entry in entries:
                state = State(entry["state"])
                timestamp = datetime.fromisoformat(entry["timestamp"])
                if timestamp.utcoffset() is None:
                    raise ValueError("missing timezone")
                UUID(entry["attempt_id"])
                if history:
                    previous = history[-1]
                    transition(previous.state, state)
                    if timestamp < datetime.fromisoformat(previous.timestamp):
                        raise ValueError("unordered lifecycle timestamps")
                elif state != State.PENDING:
                    raise ValueError("missing initial pending state")
                history.append(LifecycleEntry(state, entry["timestamp"], entry["attempt_id"]))
            data["history"] = history
            result = cls(**data)
            if result.attempt_id != history[-1].attempt_id:
                raise ValueError("attempt mismatch")
            return result
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("corrupt process record") from exc


class ProcessRecords:
    """Atomic single-writer record files; full transactional store is separate."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def save(self, process: Process) -> None:
        UUID(process.process_id)
        raw = process.to_json()
        Process.from_json(raw)
        descriptor, name = tempfile.mkstemp(dir=self.root, prefix=".record-")
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.root / f"{process.process_id}.json")
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)

    def load(self, process_id: str) -> Process:
        UUID(process_id)
        process = Process.from_json((self.root / f"{process_id}.json").read_text())
        if process.process_id != process_id:
            raise ValueError("record identity mismatch")
        return process
