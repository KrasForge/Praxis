"""Process-tree orchestration across pluggable executors."""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from enum import Enum

from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.executors.protocol import ControlResult, ExecutionRequest, Executor
from praxis.kernel.allocation import BudgetExceeded, BudgetManager
from praxis.kernel.budgets import RESOURCES, ResourceBudget
from praxis.kernel.authority import Authority, AuthorizationError
from praxis.kernel.capabilities import Capability, Resource
from praxis.kernel.contracts import Contract
from praxis.kernel.events import Event
from praxis.kernel.lifecycle import TERMINAL, State
from praxis.kernel.process import Process, ProcessRecords
from praxis.kernel.spec import ProcessSpec
from praxis.kernel.retry import RetryError, RetryPolicy
from praxis.kernel.usage import UsageLedger
from praxis.storage.protocol import ProcessStore
from praxis.storage.journal import EventJournal
from praxis.validators.policy import VerificationReport, evaluate
from praxis.validators.protocol import CheckResult, CheckStatus, ValidationInput, Validator
from praxis.workspaces.transaction import CanonicalDirectory, WorkspaceTransaction
from praxis.workspaces.local import LocalWorkspaces
from praxis.workspaces.protocol import WorkspaceHandle


class ProcessSignal(str, Enum):
    SUSPEND = "suspend"
    RESUME = "resume"
    TERMINATE = "terminate"
    INTERRUPT = "interrupt"


class CancellationPolicy(str, Enum):
    SELF = "self"
    TREE = "tree"


@dataclass(frozen=True)
class ChildOutcome:
    process_id: str
    state: State
    outcome: Outcome


class Kernel:
    def __init__(
        self, records: ProcessRecords | ProcessStore, workspaces: LocalWorkspaces,
        executors: dict[str, Executor], *, retain_workspaces: bool = False,
        authority: Authority | None = None, validators: dict[str, Validator] | None = None,
    ):
        self.records = records
        self.workspaces = workspaces
        self.executors = dict(executors)
        self.validators = dict(validators or {})
        self.verification: dict[str, VerificationReport] = {}
        self.canonical_targets: dict[str, CanonicalDirectory] = {}
        self.retain_workspaces = retain_workspaces
        self.processes: dict[str, Process] = {}
        self.tasks: dict[str, asyncio.Task[Outcome]] = {}
        self.results: dict[str, Outcome] = {}
        self.handles: dict[str, WorkspaceHandle] = {}
        self.authority = authority or Authority()
        if isinstance(records, ProcessStore):
            self.authority.events = EventJournal(records)
        self.events = self.authority.events
        self.usage = UsageLedger(self.events)
        self.budgets = BudgetManager(self.usage)
        self.started: dict[str, asyncio.Event] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def create(self, spec: ProcessSpec, parent_id: str | None = None,
               *, canonical: CanonicalDirectory | None = None) -> Process:
        if parent_id is not None:
            parent = self.processes[parent_id]
            if parent.state in TERMINAL:
                raise ValueError("terminal parent cannot spawn")
        # Authority must be issued by the kernel, never inherited from a spec.
        if spec.capabilities:
            raise ValueError("capability issuance is not configured")
        process = Process(ProcessSpec.from_json(spec.to_json()), parent_id=parent_id)
        self.budgets.allocate(process.process_id, ResourceBudget.from_json(json.dumps(spec.budget)), parent_id)
        event = Event(process.process_id, "process.created", parent_id=parent_id)
        self._persist(process, event)
        self.authority.configure_process(process.process_id, parent_id)
        self.usage.register(process.process_id, parent_id)
        self.processes[process.process_id] = process
        if canonical is not None:
            self.canonical_targets[process.process_id] = canonical
        self.started[process.process_id] = asyncio.Event()
        self.locks[process.process_id] = asyncio.Lock()
        return process

    def start(self, process_id: str) -> None:
        process = self.processes[process_id]
        if process.state != State.PENDING or process_id in self.tasks:
            raise ValueError("process already started")
        self.tasks[process_id] = asyncio.create_task(self._run(process))

    def spawn(self, parent_id: str, spec: ProcessSpec) -> str:
        child = self.create(spec, parent_id)
        self.start(child.process_id)
        return child.process_id

    def _move(self, process: Process, state: State) -> None:
        process.move(state)
        self._persist(process, Event(process.process_id, "process.state", {"state": state.value},
                                     parent_id=process.parent_id))

    async def _run(self, process: Process) -> Outcome:
        started_at = time.monotonic()
        handle: WorkspaceHandle | None = None
        transaction: WorkspaceTransaction | None = None
        verified = False
        try:
            self.authority.require(process.process_id, Resource.EXECUTOR, "execute", process.spec.executor)
            self.authority.require(process.process_id, Resource.WORKSPACE, "create", process.process_id)
            executor = self.executors.get(process.spec.executor)
            if executor is None:
                result = Outcome(OutcomeStatus.UNAVAILABLE, "executor_not_found")
            elif "resource_reporting" not in executor.descriptor.features and any(
                getattr(self.budgets.limits[process.process_id], resource) is not None
                for resource in RESOURCES - {"wall_milliseconds"}
            ):
                result = Outcome(OutcomeStatus.UNAVAILABLE, "budget_measurement_unavailable")
            else:
                handle = self.workspaces.create(process.process_id, retain=self.retain_workspaces)
                self.handles[process.process_id] = handle
                canonical = self.canonical_targets.get(process.process_id)
                if canonical is not None:
                    self.authority.require(process.process_id, Resource.FILESYSTEM, "read", str(canonical.root))
                    transaction = WorkspaceTransaction(self.workspaces, handle, canonical)
                request = ExecutionRequest(
                    process.process_id, process.attempt_id, process.spec, handle.workspace_id,
                    self.workspaces.path_for(handle, process.process_id),
                )
                self._move(process, State.RUNNING)
                control = await executor.start(request)
                self.started[process.process_id].set()
                if not control.applied:
                    result = Outcome(OutcomeStatus.UNAVAILABLE, control.reason)
                else:
                    remaining = self.budgets.remaining(process.process_id, "wall_milliseconds")
                    try:
                        if remaining == 0:
                            raise TimeoutError()
                        result = await asyncio.wait_for(executor.collect_result(process.attempt_id),
                                                        None if remaining is None else remaining / 1000)
                    except TimeoutError:
                        await executor.cancel(process.attempt_id)
                        result = Outcome(OutcomeStatus.BUDGET_EXHAUSTED, "wall_budget_exhausted")
        except AuthorizationError:
            result = Outcome(OutcomeStatus.FAILED, "capability_denied")
        except Exception:
            result = Outcome(OutcomeStatus.FAILED, "executor_error")
        self.started[process.process_id].set()
        measured = {"wall_milliseconds": int((time.monotonic() - started_at) * 1000)}
        try:
            self.budgets.check(process.process_id, measured)
        except BudgetExceeded:
            result = Outcome(OutcomeStatus.BUDGET_EXHAUSTED, "wall_budget_exhausted")
        self.usage.record(process.process_id, process.attempt_id, f"{process.attempt_id}:wall", measured)
        try:
            if result.status == OutcomeStatus.COMPLETED and handle is not None:
                report = await self._verify(process, handle)
                verified = report.approved
                if verified and transaction is not None:
                    self.authority.require(process.process_id, Resource.WORKSPACE, "commit", process.process_id)
                    self.authority.require(process.process_id, Resource.FILESYSTEM, "write", str(transaction.canonical.root))
                    self.events.append(transaction.commit(report))
            if not verified and transaction is not None:
                self.events.append(transaction.rollback())
        except Exception:
            verified = False
            result = Outcome(OutcomeStatus.FAILED, "verification_or_commit_error")
            if transaction is not None and not transaction.committed:
                self.events.append(transaction.rollback())
        self.results[process.process_id] = result
        self.events.append(Event(process.process_id, "process.outcome", json.loads(result.to_json()),
                                 parent_id=process.parent_id))
        async with self.locks[process.process_id]:
            self._move(process, result.process_state(verified=verified))
        if handle is not None:
            if self.authority.authorize(process.process_id, Resource.WORKSPACE,
                                        "destroy", process.process_id).allowed:
                self.workspaces.cleanup(handle)
        self.budgets.release(process.process_id)
        return result

    async def join(self, parent_id: str, child_ids: list[str]) -> tuple[ChildOutcome, ...]:
        if len(set(child_ids)) != len(child_ids):
            raise ValueError("duplicate child identity")
        for child_id in child_ids:
            child = self.processes[child_id]
            if child.parent_id != parent_id:
                raise ValueError("join requires direct child")
            if child_id not in self.tasks:
                raise ValueError("child has not started")
        results = await asyncio.gather(*(asyncio.shield(self.tasks[c]) for c in child_ids))
        return tuple(ChildOutcome(c, self.processes[c].state, result)
                     for c, result in zip(child_ids, results))

    async def signal(self, process_id: str, signal: ProcessSignal) -> ControlResult:
        if not isinstance(signal, ProcessSignal):
            raise ValueError("typed process signal required")
        process = self.processes[process_id]
        if process_id in self.tasks:
            await self.started[process_id].wait()
        async with self.locks[process_id]:
            decision = self.authority.authorize(process_id, Resource.EXECUTOR, "control", process.spec.executor)
            if not decision.allowed:
                result = ControlResult(True, False, decision.reason)
                self._control_event(process, signal.value, result)
                return result
            required = State.SUSPENDED if signal == ProcessSignal.RESUME else State.RUNNING
            if process.state != required:
                result = ControlResult(True, False, "invalid_process_state")
            else:
                result = await self.executors[process.spec.executor].signal(
                    process.attempt_id, signal.value
                )
                if result.applied and signal in (ProcessSignal.SUSPEND, ProcessSignal.RESUME):
                    self._move(process, State.SUSPENDED if signal == ProcessSignal.SUSPEND
                               else State.RUNNING)
            self._control_event(process, signal.value, result)
            return result

    def _control_event(self, process: Process, operation: str, result: ControlResult) -> None:
        self.events.append(Event(process.process_id, "process.control", {
            "operation": operation, "supported": result.supported,
            "applied": result.applied, "reason": result.reason,
        }, parent_id=process.parent_id))

    async def cancel(
        self, process_id: str, policy: CancellationPolicy = CancellationPolicy.SELF,
    ) -> dict[str, ControlResult]:
        if not isinstance(policy, CancellationPolicy):
            raise ValueError("typed cancellation policy required")
        process = self.processes[process_id]
        decision = self.authority.authorize(process_id, Resource.EXECUTOR, "control", process.spec.executor)
        if not decision.allowed:
            result = ControlResult(True, False, decision.reason)
            self._control_event(process, "cancel", result)
            return {process_id: result}
        results: dict[str, ControlResult] = {}
        if policy == CancellationPolicy.TREE:
            children = [p.process_id for p in self.processes.values() if p.parent_id == process_id]
            for child in children:
                results.update(await self.cancel(child, policy))
        if process_id in self.tasks:
            await self.started[process_id].wait()
        async with self.locks[process_id]:
            if process.state in TERMINAL:
                result = ControlResult(True, False, "already_terminal")
            elif process.state == State.PENDING:
                self.results[process_id] = Outcome(OutcomeStatus.CANCELLED, "cancelled")
                self._move(process, State.CANCELLED)
                self.budgets.release(process_id)
                result = ControlResult(True, True, "cancelled")
            else:
                result = await self.executors[process.spec.executor].cancel(process.attempt_id)
            self._control_event(process, "cancel", result)
            results[process_id] = result
        if result.applied and process_id in self.tasks:
            await asyncio.shield(self.tasks[process_id])
        return results

    async def _verify(self, process: Process, handle: WorkspaceHandle) -> VerificationReport:
        self.authority.require(process.process_id, Resource.WORKSPACE, "snapshot", process.process_id)
        snapshot = self.workspaces.snapshot(handle)
        files: list[tuple[str, bytes]] = []
        for path, digest in snapshot.files:
            if path.endswith("/"):
                continue
            blob = (self.workspaces.root / "blobs" / digest).read_bytes()
            if hashlib.sha256(blob).hexdigest() != digest:
                raise ValueError("corrupt verification snapshot")
            files.append((path, blob.split(b"\n", 1)[1]))
        source = ValidationInput(process.process_id, process.attempt_id, snapshot.snapshot_id, tuple(files))
        contract = Contract.from_json(json.dumps(process.spec.contract))
        results = []
        for check in contract.invariants + contract.validators:
            validator = self.validators.get(check.validator)
            if validator is None:
                result = CheckResult(check.check_id, CheckStatus.UNAVAILABLE, "validator_unavailable")
            else:
                try:
                    result = await validator.validate(source, check)
                    if result.check_id != check.check_id:
                        raise ValueError("validator identity mismatch")
                except Exception:
                    result = CheckResult(check.check_id, CheckStatus.ERROR, "validator_error")
            results.append(result)
            self.events.append(Event(process.process_id, "contract.checked", json.loads(result.to_json()),
                                     parent_id=process.parent_id))
        report = evaluate(contract, source, tuple(results))
        self.verification[process.process_id] = report
        self.events.append(Event(process.process_id, "contract.evaluated", {
            "approved": report.approved, "snapshot_id": report.snapshot_id,
            "missing_outputs": list(report.missing_outputs),
            "required_failures": list(report.required_failures),
            "advisory_failures": list(report.advisory_failures),
        }, parent_id=process.parent_id))
        return report

    def _persist(self, process: Process, event: Event) -> None:
        if isinstance(self.records, ProcessStore):
            self.records.save(process, (event,))
        else:
            self.records.save(process)
        self.events.append(event)

    async def retry(self, process_id: str, policy: RetryPolicy) -> str:
        process = self.processes[process_id]
        self.authority.require(process_id, Resource.EXECUTOR, "control", process.spec.executor)
        async with self.locks[process_id]:
            if process.state != State.FAILED:
                raise RetryError("process_not_failed")
            outcome = self.results.get(process_id)
            if outcome is None or outcome.status == OutcomeStatus.COMPLETED or not (
                outcome.retryable or outcome.reason in policy.retryable_reasons
            ):
                raise RetryError("outcome_not_retryable")
            attempts = len({entry.attempt_id for entry in process.history})
            if attempts >= policy.max_attempts:
                raise RetryError("retry_exhausted")
            family = {process_id}
            while True:
                expanded = family | {p.process_id for p in self.processes.values() if p.parent_id in family}
                if expanded == family:
                    break
                family = expanded
            if any(event.process_id in family and (
                event.type == "workspace.committed" or
                event.type in ("effect.applying", "effect.applied") and event.payload.get("replay_safe") is not True
            ) for event in self.events):
                raise RetryError("unsafe_effect_replay")
            if policy.backoff_seconds:
                await asyncio.sleep(policy.backoff_seconds * (2 ** (attempts - 1)))
            self.budgets.reactivate(process_id)
            previous = process.attempt_id
            process.new_attempt()
            self._persist(process, Event(process_id, "process.retry", {
                "previous_attempt_id": previous, "attempt_id": process.attempt_id,
                "attempt_number": attempts + 1, "backoff_seconds": policy.backoff_seconds,
            }, parent_id=process.parent_id))
            self.tasks.pop(process_id, None)
            self.started[process_id] = asyncio.Event()
            self.start(process_id)
            return process.attempt_id

    def recover_records(self) -> None:
        if not isinstance(self.records, ProcessStore):
            raise ValueError("recovery requires a process store")
        if self.tasks:
            raise ValueError("cannot recover over running work")
        self.processes = {identity: self.records.load(identity) for identity in self.records.list_processes()}
        self.authority.parents = {p.process_id: p.parent_id for p in self.processes.values()}
        self.usage.parents = dict(self.authority.parents)
        self.budgets.parents = dict(self.authority.parents)
        self.budgets.limits = {p.process_id: ResourceBudget.from_json(json.dumps(p.spec.budget)) for p in self.processes.values()}
        self.budgets.active = {p.process_id for p in self.processes.values() if p.state not in TERMINAL}
        for process in self.processes.values():
            self.started[process.process_id] = asyncio.Event()
            self.locks[process.process_id] = asyncio.Lock()
        for event in self.events:
            if event.type in ("capability.issued", "capability.delegated"):
                raw = event.payload.get("capability")
                if isinstance(raw, str):
                    capability = Capability.from_json(raw)
                    if capability.recipient != event.process_id:
                        raise ValueError("corrupt capability provenance")
                    self.authority.grants[capability.capability_id] = capability
            elif event.type == "capability.revoked":
                self.authority.revoked.add(event.payload["capability_id"])
            elif event.type == "usage.recorded":
                self.usage.record(event.process_id, event.payload["attempt_id"], event.payload["usage_id"],
                                  event.payload["values"], emit=False)
            elif event.type == "process.outcome":
                self.results[event.process_id] = Outcome.from_json(json.dumps(event.payload))

    def report_usage(self, process_id: str, attempt_id: str, usage_id: str, values: dict[str, int]) -> None:
        process = self.processes[process_id]
        if process.attempt_id != attempt_id or process.state not in (State.RUNNING, State.SUSPENDED):
            raise ValueError("usage report from inactive attempt")
        previous = self.usage.entries.get(usage_id)
        if previous is None:
            self.budgets.check(process_id, values)
        self.usage.record(process_id, attempt_id, usage_id, values)
