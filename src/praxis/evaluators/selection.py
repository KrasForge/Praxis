"""Deterministic selection with explicit eligibility, ties and abstentions."""

from dataclasses import dataclass

from praxis.evaluators.protocol import Evaluation


@dataclass(frozen=True)
class Selection:
    status: str
    winners: tuple[str, ...]
    evaluations: tuple[Evaluation, ...]
    reason: str


@dataclass(frozen=True)
class SelectionPolicy:
    mode: str
    eligible_candidates: frozenset[str]
    eligible_evaluators: frozenset[str]
    quorum: int = 1

    def __post_init__(self) -> None:
        if self.mode not in {"score", "pairwise", "quorum", "human"}:
            raise ValueError("unknown selection mode")
        if not isinstance(self.eligible_candidates, frozenset) or not isinstance(self.eligible_evaluators, frozenset):
            raise ValueError("immutable eligibility required")
        if type(self.quorum) is not int or self.quorum < 1:
            raise ValueError("positive quorum required")

    def select(self, evaluations: tuple[Evaluation, ...], *, human_choice: str | None = None,
               actor: str | None = None) -> Selection:
        candidates = self.eligible_candidates
        if not candidates:
            return Selection("no_valid_candidate", (), evaluations, "empty_eligibility")
        if self.mode == "human":
            if human_choice is None:
                return Selection("pending", (), evaluations, "human_choice_required")
            if human_choice not in candidates or not actor:
                raise ValueError("authenticated eligible human choice required")
            return Selection("selected", (human_choice,), evaluations, "human:" + actor)
        if human_choice is not None:
            raise ValueError("human choice requires human policy")
        identities = [e.evaluator_id for e in evaluations]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate evaluator vote")
        valid = [e for e in evaluations if e.status == "ok" and e.evaluator_id in self.eligible_evaluators]
        totals: dict[str, float] = {}
        if self.mode == "score":
            # Compare only complete ballots so missing scores cannot improve a candidate.
            valid = [e for e in valid if candidates <= dict(e.scores).keys()]
            totals = {c: sum(dict(e.scores)[c] for e in valid) / len(valid) for c in candidates} if valid else {}
        elif self.mode == "quorum":
            for evaluation in valid:
                scores = {c: score for c, score in evaluation.scores if c in candidates}
                if scores.keys() != candidates:
                    continue
                best = max(scores.values())
                ballot_winners = [c for c, score in scores.items() if score == best]
                if len(ballot_winners) == 1:
                    totals[ballot_winners[0]] = totals.get(ballot_winners[0], 0) + 1
            totals = {c: votes for c, votes in totals.items() if votes >= self.quorum}
        else:
            for evaluation in valid:
                seen = set()
                ballot = dict.fromkeys(candidates, 0.0)
                for left, right, order in evaluation.comparisons:
                    if left not in candidates or right not in candidates:
                        continue
                    pair = frozenset({left, right})
                    if pair in seen:
                        raise ValueError("duplicate pairwise vote")
                    seen.add(pair)
                    ballot[left] += order
                    ballot[right] -= order
                if len(seen) == len(candidates) * (len(candidates) - 1) // 2:
                    for candidate, score in ballot.items():
                        totals[candidate] = totals.get(candidate, 0) + score
        if not totals:
            return Selection("unresolved", (), evaluations, "insufficient_evaluator_votes")
        best = max(totals.values())
        winners = tuple(sorted(c for c, score in totals.items() if score == best))
        return Selection("selected" if len(winners) == 1 else "tie", winners, evaluations,
                         "policy:" + self.mode)
