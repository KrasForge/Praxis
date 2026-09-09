"""Process-tree orchestration across pluggable executors."""

import asyncio
import hashlib
import json
import time
from uuid import uuid4
from dataclasses import asdict, dataclass, replace
from enum import Enum

from praxis.knowledge.context import ContextProvider, ContextResponse, bound_response
from praxis.knowledge.dependencies import ContextDependency, RequiredContextUnavailable
from praxis.executors.outcomes import Outcome, OutcomeStatus
from praxis.executors.protocol import CommitGuard, ControlResult, ExecutionRequest, Executor
from praxis.kernel.allocation import BudgetExceeded, BudgetManager
from praxis.kernel.budgets import RESOURCES, ResourceBudget
from praxis.kernel.authority import Authority, AuthorizationError
from praxis.kernel.capabilities import Capability, Resource
from praxis.kernel.contracts import Contract
from praxis.kernel.effects import Effect
from praxis.kernel.results import ProcessResult
from praxis.kernel.events import Event
from praxis.kernel.lifecycle import TERMINAL, State
from praxis.kernel.lineage import Lineage
from praxis.kernel.process import Process, ProcessRecords
from praxis.kernel.spec import ProcessSpec
from praxis.kernel.retry import RetryError, RetryPolicy
from praxis.kernel.usage import UsageLedger
from praxis.observability.runtime import RuntimeMetrics
from praxis.observability.tracing import process_context
from praxis.storage.protocol import ProcessStore, StoredEvent
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
        context_providers: dict[str, ContextProvider] | None = None,
    ):
        self.context_providers = dict(context_providers or {})
        self.records = records
        self.workspaces = workspaces
        self.executors = dict(executors)
        self.assignments: dict[str, str] = {}
        self.invocations: dict[str, tuple[str, str]] = {}
        self.validators = dict(validators or {})
        self.verification: dict[str, VerificationReport] = {}
        self.canonical_targets: dict[str, CanonicalDirectory] = {}
        self.deferred_commits: set[str] = set()
        self.staged_transactions: dict[str, WorkspaceTransaction] = {}
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
        self.metrics = RuntimeMetrics(
            lambda after: records.read_events(after=after) if isinstance(records, ProcessStore) else
            tuple(StoredEvent(index + 1, event) for index, event in enumerate(self.events) if index >= after),
            lambda identity: self.processes[identity].spec.executor,
        )

    def create(self, spec: ProcessSpec, parent_id: str | None = None,
               *, canonical: CanonicalDirectory | None = None, submission_key: str | None = None, submission_actor: str | None = None) -> Process:
        if parent_id is not None:
            parent = self.processes[parent_id]
            if parent.state in TERMINAL:
                raise ValueError("terminal parent cannot spawn")
        # Authority must be issued by the kernel, never inherited from a spec.
        if spec.capabilities:
            raise ValueError("capability issuance is not configured")
        process = Process(ProcessSpec.from_json(spec.to_json()), parent_id=parent_id)
        self.budgets.allocate(process.process_id, ResourceBudget.from_json(json.dumps(spec.budget)), parent_id)
        event = Event(process.process_id, "process.created",
                      {**({} if submission_key is None else {"submission_key": submission_key}),
                       **({} if submission_actor is None else {"actor": submission_actor})}, parent_id=parent_id)
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
            self.authority.require(process.process_id, Resource.EXECUTOR, "execute", self.executor_name(process))
            self.authority.require(process.process_id, Resource.WORKSPACE, "create", process.process_id)
            context = await self._resolve_context(process)
            execution_spec = replace(process.spec, context=[], inputs={**process.spec.inputs, "praxis.context": context}) if process.spec.context else process.spec
            executor = self.executors.get(self.executor_name(process))
            if executor is None:
                result = Outcome(OutcomeStatus.UNAVAILABLE, "executor_not_found")
            elif "resource_reporting" not in executor.descriptor.features and any(
                getattr(self.budgets.limits[process.process_id], resource) is not None
                for resource in RESOURCES - {"wall_milliseconds"}
            ):
                result = Outcome(OutcomeStatus.UNAVAILABLE, "budget_measurement_unavailable")
            else:
                handle = self.workspaces.create(process.process_id, retain=self.retain_workspaces or process.process_id in self.deferred_commits)
                self.handles[process.process_id] = handle
                canonical = self.canonical_targets.get(process.process_id)
                if canonical is not None:
                    self.authority.require(process.process_id, Resource.FILESYSTEM, "read", str(canonical.root))
                    transaction = WorkspaceTransaction(self.workspaces, handle, canonical)
                request = ExecutionRequest(
                    process.process_id, process.attempt_id, execution_spec, handle.workspace_id,
                    self.workspaces.path_for(handle, process.process_id),
                )
                self._move(process, State.RUNNING)
                invocation_id = str(uuid4())
                previous = next((e.event_id for e in reversed(self.events) if e.process_id == process.process_id), None)
                trace = process_context(process.process_id, lambda identity: self.processes[identity].parent_id).child("attempt", process.attempt_id).child("executor", invocation_id)
                invoked = Event(process.process_id, "executor.invoked", {
                    "trace": json.loads(trace.to_json()),
                    "invocation_id": invocation_id, "executor": self.executor_name(process),
                    "attempt_id": process.attempt_id,
                    "lineage": Lineage(process.process_id, process.attempt_id,
                                       () if previous is None else (previous,), invocation_id=invocation_id).to_json(),
                }, parent_id=process.parent_id)
                self.events.append(invoked)
                self.invocations[process.process_id] = (invocation_id, invoked.event_id)
                request = replace(request, parent_id=process.parent_id, trace_json=trace.to_json(),
                                  lineage_json=Lineage(process.process_id, process.attempt_id,
                                                      (invoked.event_id,), invocation_id=invocation_id).to_json())
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
        except RequiredContextUnavailable:
            result = Outcome(OutcomeStatus.UNAVAILABLE, "required_context_unavailable")
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
                if verified and isinstance(executor, CommitGuard) and not executor.commit_allowed(process.attempt_id):
                    verified = False
                    result = Outcome(OutcomeStatus.PARTIAL, "remote_generation_fenced")
                if verified and transaction is not None and process.process_id in self.deferred_commits:
                    self.staged_transactions[process.process_id] = transaction
                elif verified and transaction is not None:
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
        self.events.append(Event(process.process_id, "process.result", {"result": self.result(process.process_id).to_json()},
                                 parent_id=process.parent_id))
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
            decision = self.authority.authorize(process_id, Resource.EXECUTOR, "control", self.executor_name(process))
            if not decision.allowed:
                result = ControlResult(True, False, decision.reason)
                self._control_event(process, signal.value, result)
                return result
            required = State.SUSPENDED if signal == ProcessSignal.RESUME else State.RUNNING
            if process.state != required:
                result = ControlResult(True, False, "invalid_process_state")
            else:
                result = await self.executors[self.executor_name(process)].signal(
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
        decision = self.authority.authorize(process_id, Resource.EXECUTOR, "control", self.executor_name(process))
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
                result = await self.executors[self.executor_name(process)].cancel(process.attempt_id)
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
            invocation = self.invocations[process.process_id]
            payload = json.loads(result.to_json())
            validator_run_id = str(uuid4())
            payload["trace"] = json.loads(process_context(process.process_id, lambda identity: self.processes[identity].parent_id).child("attempt", process.attempt_id).child("executor", invocation[0]).child("validator", validator_run_id).to_json())
            payload["lineage"] = Lineage(process.process_id, process.attempt_id, (invocation[1],),
                                         invocation_id=invocation[0], validator_run_id=validator_run_id).to_json()
            self.events.append(Event(process.process_id, "contract.checked", payload, parent_id=process.parent_id))
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

    async def retry(self, process_id: str, policy: RetryPolicy, *, start: bool = True) -> str:
        process = self.processes[process_id]
        self.authority.require(process_id, Resource.EXECUTOR, "control", self.executor_name(process))
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
            durable_events = (tuple(entry.event for entry in self.records.read_events())
                              if isinstance(self.records, ProcessStore) else tuple(self.events))
            pending_remote = {(event.process_id, event.payload.get("attempt_id")) for event in durable_events
                              if event.process_id in family and event.type == "worker.dispatch_pending"}
            completed_remote = {(event.process_id, event.payload.get("attempt_id")) for event in durable_events
                                if event.type in {"worker.execution_completed", "worker.execution_fenced"}}
            if pending_remote - completed_remote:
                raise RetryError("unsafe_remote_replay")
            if any(event.process_id in family and (
                event.type == "workspace.committed" or
                event.type in ("effect.applying", "effect.applied") and event.payload.get("replay_safe") is not True
            ) for event in durable_events):
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
            if start:
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
            if event.type == "candidate.isolated":
                self.deferred_commits.add(event.process_id)
            elif event.type == "candidate.released":
                self.deferred_commits.discard(event.process_id)
            elif event.type in ("capability.issued", "capability.delegated"):
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
            elif event.type == "process.result":
                result = ProcessResult.from_json(event.payload["result"])
                if result.verification is not None:
                    self.verification[result.process_id] = result.verification
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

    def executor_name(self, process: Process) -> str:
        return self.assignments.get(process.process_id, process.spec.executor)

    def result(self, process_id: str) -> ProcessResult:
        process = self.processes[process_id]
        effects: dict[str, Effect] = {}
        history = (tuple(entry.event for entry in self.records.read_events(process_id))
                   if isinstance(self.records, ProcessStore) else tuple(self.events))
        for event in history:
            if event.process_id == process_id and event.type.startswith("effect.") and "effect" in event.payload:
                effect = Effect.from_json(event.payload["effect"])
                effects[effect.effect_id] = effect
        return ProcessResult(process_id, process.attempt_id, process.state, self.results[process_id],
                             verification=self.verification.get(process_id), effects=tuple(effects.values()),
                             usage=self.usage.total(process_id))

    async def _resolve_context(self, process: Process) -> dict[str, object]:
        resolved: dict[str, object] = {}
        for raw in process.spec.context:
            dependency = ContextDependency.from_dict(raw)
            provider = self.context_providers.get(dependency.provider)
            response = ContextResponse("unavailable", reason="provider_not_found")
            if provider is not None:
                try:
                    response = bound_response(dependency.request, await asyncio.wait_for(
                        provider.query(dependency.request), 15))
                except Exception:
                    response = ContextResponse("unavailable", reason="context_provider_error")
            previous = next((e.event_id for e in reversed(self.events) if e.process_id == process.process_id), None)
            payload = json.loads(json.dumps(asdict(response)))
            self.events.append(Event(process.process_id, "context.resolved", {
                "context_id": dependency.context_id, "provider": dependency.provider,
                "required": dependency.required, "response": payload,
                "lineage": Lineage(process.process_id, process.attempt_id,
                                   () if previous is None else (previous,)).to_json(),
            }, parent_id=process.parent_id))
            resolved[dependency.context_id] = payload
            if dependency.required and (response.status == "unavailable" or not response.items):
                raise RequiredContextUnavailable(dependency.context_id)
        return resolved
