import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from wsjtx_autopilot.config import AppConfig

from .cq import CqEligibility
from .models import (
    ActionKind,
    ActionOutcome,
    Candidate,
    CandidateKind,
    CooldownKind,
    DecodeEvent,
    EngineEvent,
    EngineEventKind,
    IntendedAction,
    MessageKind,
    ScoreBreakdown,
    ScoreComponent,
    StationProfile,
    StationCooldown,
    WorkedCheck,
)
from .pending_direct import PendingDirectCall, PendingDirectCallQueue
from .scoring import CandidateScorer
from .state import QsoState, QsoStateMachine
from wsjtx_autopilot.worked.bands import BandResolver

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class CandidateWindow:
    period_key: tuple[object, ...]
    last_decode_at: datetime
    provisional: Candidate | None = None


class DecisionEngine:
    def __init__(
        self,
        config: AppConfig,
        worked_lookup: Callable[[str, int | None, datetime], WorkedCheck] | None = None,
        scorer: CandidateScorer | None = None,
        event_sink: Callable[[EngineEvent], None] | None = None,
        cq_eligibility: CqEligibility | None = None,
    ) -> None:
        self.config = config
        self.state = QsoStateMachine(
            config.local_callsign,
            config.qso_timeout_seconds,
            config.max_retries,
            config.max_no_progress_periods,
        )
        self._candidates: dict[str, Candidate] = {}
        self._seen: dict[str, datetime] = {}
        self._cooldowns: dict[str, StationCooldown] = {}
        self._pending_actions: dict[str, DecodeEvent] = {}
        self.pending_direct_calls = PendingDirectCallQueue(config.pending_direct_ttl_seconds)
        self._pending_candidate_stations: set[str] = set()
        self._promoting_pending: PendingDirectCall | None = None
        self._pending_rejection_reason = "USER_RULE"
        self._candidate_window: CandidateWindow | None = None
        self._worked_lookup = worked_lookup
        self._scorer = scorer
        self._event_sink = event_sink
        self._cq_eligibility = cq_eligibility or CqEligibility(
            StationProfile(config.local_callsign),
            respond_to_cq_dx=config.respond_to_cq_dx,
        )

    def observe(self, event: DecodeEvent, now: datetime | None = None) -> Candidate | None:
        now = now or event.observed_at
        self._prune(now)
        promoting_pending = self._promoting_pending is not None
        if self.state.session.state is QsoState.IDLE and not promoting_pending:
            self._touch_candidate_window(event, now)
        message = event.parsed
        direct_to_local = message.is_addressed_to(self.config.local_callsign) and message.kind in {
            MessageKind.DIRECTED,
            MessageKind.REPORT,
            MessageKind.R_REPORT,
        }
        if direct_to_local:
            direct_df = event.original.delta_frequency if event.original is not None else -1
            LOGGER.info("[DIRECT] received station=%s df=%s", message.sender, direct_df if direct_df >= 0 else "-")
        freshness_seconds = (
            self.config.pending_direct_ttl_seconds
            if promoting_pending
            else self.config.stale_decode_seconds
        )
        if now - event.observed_at > timedelta(seconds=freshness_seconds):
            LOGGER.info("[ENGINE] candidate refused reason=stale text=%s", event.parsed.raw)
            if direct_to_local:
                self._log_direct_rejected(message.sender, "STALE")
            self._emit(EngineEventKind.CANDIDATE_REFUSED, event.parsed.sender, "stale")
            return None
        identity = self._event_identity(event)
        if identity in self._seen and not promoting_pending:
            LOGGER.info("[ENGINE] candidate refused reason=duplicate text=%s", event.parsed.raw)
            self._emit(EngineEventKind.CANDIDATE_REFUSED, event.parsed.sender, "duplicate decode")
            return None
        self._seen[identity] = now

        qso_active = self.state.session.state is not QsoState.IDLE
        if qso_active and message.kind not in {MessageKind.CQ, MessageKind.QRZ}:
            if direct_to_local and message.sender != self.state.session.remote_callsign:
                self.queue_pending_direct(event, now, "current QSO active")
                return None
            LOGGER.info(
                "[ENGINE] candidate refused sender=%s reason=active QSO state=%s remote=%s",
                event.parsed.sender,
                self.state.session.state.name,
                self.state.session.remote_callsign or "-",
            )
            self.state.observe(event)
            self._emit(EngineEventKind.CANDIDATE_REFUSED, event.parsed.sender, "active QSO")
            return None
        if qso_active:
            LOGGER.info(
                "[ENGINE] queued candidate during active QSO station=%s remote=%s",
                message.sender,
                self.state.session.remote_callsign,
            )
        if direct_to_local:
            kind = CandidateKind.DIRECT_CALLER
            score = self.config.direct_caller_priority + event.snr
            force_priority = True
            LOGGER.info("[ENGINE] direct caller detected: %s", message.sender)
        elif message.kind in {MessageKind.CQ, MessageKind.QRZ}:
            kind = CandidateKind.CQ
            score = event.snr
            force_priority = False
        else:
            LOGGER.info("[ENGINE] candidate refused reason=exchange not actionable text=%s", message.raw)
            self._emit(EngineEventKind.CANDIDATE_REFUSED, message.sender, "exchange not actionable")
            return None

        if kind is CandidateKind.CQ:
            cq_result = self._cq_eligibility.evaluate(message)
            if not cq_result.accepted:
                LOGGER.info(
                    "[ENGINE] candidate refused station=%s reason=%s",
                    message.sender,
                    cq_result.reason,
                )
                self._emit(EngineEventKind.CANDIDATE_REFUSED, message.sender, cq_result.reason)
                return None

        scorer_checks_dupes = self._scorer is not None and not self._scorer.preferences.allow_dupes
        worked: WorkedCheck | None = None
        if (not self.config.allow_dupes or scorer_checks_dupes) and self._worked_lookup is not None:
            eligibility_at = now if promoting_pending else event.observed_at
            worked = self._worked_lookup(message.sender, event.frequency, eligibility_at)
            if worked.band is None:
                LOGGER.info(
                    "[ENGINE] candidate refused station=%s reason=unable to resolve band",
                    message.sender,
                )
                self._emit(EngineEventKind.CANDIDATE_REFUSED, message.sender, "unable to resolve band")
                if direct_to_local:
                    self._log_direct_rejected(message.sender, "UNKNOWN_BAND")
                return None
            if self._scorer is None and worked.duplicate:
                LOGGER.info(
                    "[ENGINE] candidate refused station=%s reason=worked today band=%s",
                    message.sender,
                    worked.band,
                )
                self._emit(EngineEventKind.CANDIDATE_REFUSED, message.sender, f"worked today band={worked.band}")
                if direct_to_local:
                    self._log_direct_rejected(message.sender, "WORKED_TODAY")
                return None
        metadata = None
        breakdown = None
        if self._scorer is not None:
            scoring = self._scorer.evaluate(event, kind, worked, now if promoting_pending else None)
            if not scoring.accepted:
                LOGGER.info(
                    "[ENGINE] candidate refused station=%s reason=%s",
                    message.sender,
                    scoring.reason,
                )
                self._emit(EngineEventKind.CANDIDATE_REFUSED, message.sender, scoring.reason)
                if direct_to_local:
                    self._log_direct_rejected(message.sender, self._direct_blocker_name(scoring.reason))
                return None
            score = scoring.breakdown.total
            metadata = scoring.metadata
            breakdown = scoring.breakdown
            force_priority = scoring.force_priority
            LOGGER.debug(
                "[SCORE] station=%s total=%d components=%s force_priority=%s",
                message.sender,
                scoring.breakdown.total,
                ",".join(f"{item.name}:{item.value}" for item in scoring.breakdown.components) or "none",
                scoring.force_priority,
            )
        cooldown = self._cooldowns.get(message.sender)
        if cooldown is not None and now < cooldown.until:
            if direct_to_local and cooldown.kind.is_soft_for_direct_call():
                LOGGER.info("[DIRECT] hard_blockers=[]")
                LOGGER.info("[DIRECT] soft_blockers=[%s]", cooldown.kind.name)
                LOGGER.info(
                    "[ENGINE] direct call overrides soft cooldown station=%s previous_reason=%s",
                    message.sender,
                    cooldown.kind.name,
                )
                LOGGER.info(
                    "[DIRECT] override soft blocker=%s station=%s previous_reason=%s",
                    cooldown.kind.name,
                    message.sender,
                    cooldown.reason,
                )
                del self._cooldowns[message.sender]
            else:
                LOGGER.info(
                    "[ENGINE] candidate refused station=%s cooldown=%s reason=%s until=%s",
                    message.sender,
                    cooldown.kind.name,
                    cooldown.reason,
                    cooldown.until.isoformat(),
                )
                if direct_to_local:
                    self._log_direct_rejected(message.sender, f"COOLDOWN_{cooldown.kind.name}")
                self._emit(EngineEventKind.CANDIDATE_REFUSED, message.sender, cooldown.reason)
                return None
        elif direct_to_local:
            LOGGER.info("[DIRECT] hard_blockers=[]")
            LOGGER.info("[DIRECT] soft_blockers=[]")
        candidate = Candidate(
            station=message.sender,
            kind=kind,
            event=event,
            score=score,
            force_priority=force_priority,
            **({"metadata": metadata} if metadata is not None else {}),
            **({"score_breakdown": breakdown} if breakdown is not None else {}),
        )
        current = self._candidates.get(candidate.station)
        if current is None or candidate.score > current.score:
            self._candidates[candidate.station] = candidate
            if promoting_pending:
                age = self._promoting_pending.age_seconds(now) if self._promoting_pending is not None else 0.0
                recency = max(
                    0,
                    round(20 * (1 - age / self.config.pending_direct_ttl_seconds)),
                )
                components = candidate.score_breakdown.components + (ScoreComponent("RECENCY", recency),)
                candidate = replace(
                    candidate,
                    score=candidate.score + recency,
                    score_breakdown=ScoreBreakdown(candidate.score + recency, components),
                )
                self._candidates[candidate.station] = candidate
                self._pending_candidate_stations.add(candidate.station)
                LOGGER.info(
                    "[DIRECT] pending candidate %s score=%d age=%.1f repeats=%d",
                    candidate.station,
                    candidate.score,
                    age,
                    self._promoting_pending.repeat_count if self._promoting_pending is not None else 1,
                )
            LOGGER.info(
                "[ENGINE] candidate %s: %s score=%d state=%s",
                "direct call" if kind is CandidateKind.DIRECT_CALLER else "CQ",
                candidate.station,
                candidate.score,
                self.state.session.state.name,
            )
            LOGGER.debug(
                "[CANDIDATE] station=%s kind=%s snr=%d frequency=%s mode=%s df=%s score=%d worked_today=%s",
                candidate.station,
                candidate.kind.name,
                event.snr,
                event.frequency or "-",
                event.mode,
                event.original.delta_frequency if event.original is not None else "-",
                candidate.score,
                worked.duplicate if worked is not None else "unknown",
            )
            self._emit(EngineEventKind.CANDIDATE_ADDED, candidate.station, "accepted", candidate)
            LOGGER.info(
                "[WINDOW] candidate %s %s",
                "DIRECT" if candidate.kind is CandidateKind.DIRECT_CALLER else "CQ",
                candidate.station,
            )
            if not promoting_pending:
                self._update_provisional(candidate)
            if direct_to_local:
                LOGGER.info("[DIRECT] candidate accepted station=%s", candidate.station)
            return candidate
        else:
            LOGGER.info(
                "[ENGINE] candidate refused station=%s reason=lower duplicate score=%d",
                candidate.station,
                score,
            )
            self._emit(EngineEventKind.CANDIDATE_REFUSED, candidate.station, "lower duplicate score", candidate)
        return None

    def note_decode_arrival(self, period_key: tuple[object, ...], now: datetime) -> None:
        """Extend the RX batch even when the Decode is later rejected."""
        if self.state.session.state is QsoState.IDLE:
            self._touch_candidate_window_key(period_key, now)

    def decide(self, now: datetime) -> IntendedAction | None:
        if not self.config.autocall_enabled or self.state.session.state is not QsoState.IDLE:
            LOGGER.debug(
                "[ENGINE] no decision autocall=%s state=%s",
                self.config.autocall_enabled,
                self.state.session.state.name,
            )
            return None
        self._promote_pending_direct_calls(now)
        viable = [
            candidate
            for candidate in self._candidates.values()
            if (
                now - candidate.event.observed_at
                <= timedelta(
                    seconds=(
                        self.config.pending_direct_ttl_seconds
                        if candidate.station in self._pending_candidate_stations
                        else self.config.stale_decode_seconds
                    )
                )
            )
        ]
        if not viable:
            return None
        window = self._candidate_window
        has_pending = any(candidate.station in self._pending_candidate_stations for candidate in viable)
        if (
            not has_pending
            and window is not None
            and now - window.last_decode_at < timedelta(seconds=self.config.candidate_collection_seconds)
        ):
            LOGGER.debug("[WINDOW] waiting for decode window last_decode=%s", window.last_decode_at.isoformat())
            return None
        direct_count = sum(candidate.kind is CandidateKind.DIRECT_CALLER for candidate in viable)
        LOGGER.info(
            "[WINDOW] closing candidates=%d direct_candidates=%d cq_candidates=%d",
            len(viable),
            direct_count,
            len(viable) - direct_count,
        )
        direct = [candidate for candidate in viable if candidate.kind is CandidateKind.DIRECT_CALLER]
        forced_direct = [candidate for candidate in direct if candidate.force_priority]
        if forced_direct:
            pool = forced_direct
            reason = "direct caller"
        elif direct:
            pool = viable
            reason = "direct caller competes by score"
        else:
            pool = viable
            reason = "CQ candidate"
        selected = max(pool, key=lambda candidate: (candidate.score, candidate.event.observed_at.timestamp()))
        selected_was_pending = selected.station in self._pending_candidate_stations
        reason = (
            "PENDING_DIRECT_CALL"
            if selected_was_pending
            else "direct caller"
            if selected.kind is CandidateKind.DIRECT_CALLER
            else "CQ candidate"
        )
        self._candidates.clear()
        self._pending_candidate_stations.clear()
        self._candidate_window = None
        if selected_was_pending:
            self.pending_direct_calls.remove(selected.station)
            self._emit(EngineEventKind.PENDING_DIRECT_REMOVED, selected.station, "selected", selected)
        self._pending_actions[selected.station] = selected.event
        LOGGER.info(
            "[ENGINE] selected: %s reason=%s state=%s",
            selected.station,
            reason,
            self.state.session.state.name,
        )
        self._emit(EngineEventKind.CANDIDATE_SELECTED, selected.station, reason, selected)
        action_kind = (
            ActionKind.DIRECT_REPLY
            if selected.kind is CandidateKind.DIRECT_CALLER
            else ActionKind.CQ_REPLY
        )
        return IntendedAction(
            action_kind,
            selected.station,
            reason,
            selected.event.original,
            selected.event.observed_at,
        )

    def record_action_outcome(self, action: IntendedAction, outcome: ActionOutcome, now: datetime) -> None:
        """Apply execution evidence separately from the proposed decision."""
        event = self._pending_actions.pop(action.station, None)
        if event is None:
            LOGGER.warning("[ENGINE] ignored action outcome without pending proposal station=%s", action.station)
            return
        LOGGER.info("[ENGINE] action outcome=%s station=%s", outcome.name, action.station)
        if outcome is ActionOutcome.SENT:
            self._set_station_cooldown(
                action.station,
                now,
                self.config.dry_run_cooldown_seconds,
                CooldownKind.STATION_RETRY,
                "initiation cooldown",
            )
            if action.kind is ActionKind.DIRECT_REPLY:
                self.state.start_from_observed_exchange(
                    event,
                )
                self.state.mark_direct_reply_sent(now)
            elif self.state.session.state is QsoState.IDLE:
                self.state.start_station(
                    event,
                    now,
                )
            LOGGER.info(
                "[ENGINE] action sent station=%s state=%s reason=reply sent",
                action.station,
                self.state.session.state.name,
            )
            if action.kind is ActionKind.CQ_REPLY:
                LOGGER.info("[ENGINE] QSO active remote=%s", action.station)
            return
        if outcome is ActionOutcome.REJECTED_LOCAL:
            LOGGER.info(
                "[ENGINE] action rejected locally station=%s state=%s cooldown=none",
                action.station,
                self.state.session.state.name,
            )
            return
        cooldown_until = now + timedelta(seconds=self.config.dry_run_cooldown_seconds)
        self._set_station_cooldown(
            action.station,
            now,
            self.config.dry_run_cooldown_seconds,
            CooldownKind.STATION_RETRY,
            "dry-run cooldown",
        )
        LOGGER.info(
            "[ENGINE] action outcome=%s station=%s state=%s cooldown_until=%s",
            outcome.name,
            action.station,
            self.state.session.state.name,
            cooldown_until.isoformat(),
        )

    def invalidate_instance_decodes(self, instance_id: str) -> None:
        """Discard candidates and pending actions invalidated by WSJT-X Clear."""
        self._candidates = {
            station: candidate
            for station, candidate in self._candidates.items()
            if candidate.event.original is None or candidate.event.original.instance_id != instance_id
        }
        self._pending_actions = {
            station: event
            for station, event in self._pending_actions.items()
            if event.original is None or event.original.instance_id != instance_id
        }
        for entry in self.pending_direct_calls.invalidate_instance(instance_id):
            LOGGER.info("[DIRECT] pending rejected station=%s reason=CLEAR", entry.station)
            self._emit(EngineEventKind.PENDING_DIRECT_REMOVED, entry.station, "CLEAR")
        LOGGER.info("[ENGINE] invalidated Decode candidates instance=%s reason=Clear", instance_id)

    def queue_pending_direct(self, event: DecodeEvent, now: datetime, reason: str) -> None:
        """Retain an addressed Direct Call until the active QSO/finalization can release it."""
        station = event.parsed.sender
        band = BandResolver().resolve(event.frequency) if event.frequency is not None else None
        self.pending_direct_calls.offer(event, now, band or "unknown")
        session = self.state.session
        LOGGER.info(
            "[DIRECT] pending station=%s reason=%s current_remote=%s",
            station,
            reason,
            session.remote_callsign or "-",
        )
        if session.state is not QsoState.IDLE:
            LOGGER.info(
                "[DIRECT] pending direct caller %s current_attempt=%s progressed=%s",
                station,
                session.remote_callsign or "-",
                "yes" if session.state is QsoState.QSO_ACTIVE else "no",
            )
        pending_candidate = Candidate(
            station,
            CandidateKind.DIRECT_CALLER,
            event,
            self.config.direct_caller_priority + event.snr,
            force_priority=True,
        )
        self._emit(
            EngineEventKind.PENDING_DIRECT_ADDED,
            station,
            "Waiting for current QSO to finish",
            pending_candidate,
        )

    def cancel_pending_action(self, action: IntendedAction, reason: str) -> bool:
        if self._pending_actions.pop(action.station, None) is None:
            return False
        LOGGER.info("[ENGINE] pending action cancelled station=%s reason=%s", action.station, reason)
        return True

    def complete_qso(self, station: str, instance_id: str | None, reason: str) -> bool:
        if not self.state.matches_remote(station, instance_id):
            return False
        remote = self.state.session.remote_callsign
        self.state.mark_complete(reason)
        LOGGER.info("[QSO id=%s] complete remote=%s reason=%s", self.state.session.qso_id, remote, reason)
        self._candidates.clear()
        self._pending_actions.clear()
        self.state.reset()
        return True

    def finalize_observed_completion(self, reason: str) -> bool:
        if self.state.session.state is not QsoState.COMPLETE:
            return False
        remote = self.state.session.remote_callsign
        LOGGER.info("[QSO id=%s] complete remote=%s reason=%s", self.state.session.qso_id, remote, reason)
        self._candidates.clear()
        self._pending_actions.clear()
        self.state.reset()
        return True

    def abort_qso(
        self,
        reason: str,
        now: datetime | None = None,
        cooldown_seconds: float | None = None,
        cooldown_kind: CooldownKind = CooldownKind.STALLED_QSO,
        preserve_candidates: bool = False,
    ) -> bool:
        if self.state.session.state is QsoState.IDLE:
            return False
        remote = self.state.session.remote_callsign
        LOGGER.info("[QSO id=%s] abandoned remote=%s reason=%s", self.state.session.qso_id, remote, reason)
        if remote is not None and now is not None and cooldown_seconds is not None:
            reason_text = (
                "remote busy with another QSO"
                if cooldown_kind is CooldownKind.REMOTE_BUSY_OTHER_QSO
                else "remote returned to CQ"
                if cooldown_kind is CooldownKind.REMOTE_RETURNED_TO_CQ
                else "temporary stalled-QSO cooldown"
            )
            cooldown = self._set_station_cooldown(
                remote,
                now,
                cooldown_seconds,
                cooldown_kind,
                reason_text,
            )
            LOGGER.info("[ENGINE] station cooldown %s until=%s", remote, cooldown.until.isoformat())
        if not preserve_candidates:
            self._candidates.clear()
        self._pending_actions.clear()
        self.state.abort(reason)
        if preserve_candidates:
            LOGGER.info("[ENGINE] immediate candidate rescan candidates=%d", len(self._candidates))
        return True

    def remote_engaged_other(self, event: DecodeEvent, now: datetime) -> tuple[str, str] | None:
        session = self.state.session
        message = event.parsed
        if session.state is QsoState.IDLE or session.remote_callsign is None:
            return None
        if message.sender != session.remote_callsign or message.addressee is None:
            return None
        if (
            session.instance_id is not None
            and event.original is not None
            and event.original.instance_id != session.instance_id
        ):
            return None
        if message.is_addressed_to(session.local_callsign):
            return None
        remote = session.remote_callsign
        other = message.addressee
        LOGGER.warning("[ENGINE] remote engaged another station remote=%s other=%s", remote, other)
        LOGGER.warning("[ENGINE] QSO abandoned remote=%s reason=working %s", remote, other)
        return remote, other

    def remote_cq_during_attempt(self, event: DecodeEvent, now: datetime) -> str | None:
        session = self.state.session
        message = event.parsed
        if session.state not in {QsoState.CALLING_STATION, QsoState.DIRECT_REPLY_SENT}:
            return None
        if session.remote_callsign is None or message.sender != session.remote_callsign:
            return None
        if (
            session.instance_id is not None
            and event.original is not None
            and event.original.instance_id != session.instance_id
        ):
            return None
        if message.kind not in {MessageKind.CQ, MessageKind.QRZ}:
            return None
        identity = self._event_identity(event)
        if identity in self._seen:
            return "ignored"
        self._seen[identity] = now
        period_key = (
            event.original.instance_id,
            event.original.decode_time,
            event.mode,
        ) if event.original is not None else (event.mode, event.observed_at)
        if session.last_remote_cq_period == period_key:
            return "ignored"
        session.last_remote_cq_period = period_key
        session.remote_cq_count += 1
        maximum = self.config.max_remote_cq_during_attempt
        if session.remote_cq_count < maximum:
            LOGGER.info(
                "[ENGINE] remote CQ during attempt remote=%s count=%d/%d",
                session.remote_callsign,
                session.remote_cq_count,
                maximum,
            )
            return "tolerated"
        remote = session.remote_callsign
        LOGGER.warning(
            "[ENGINE] remote returned to CQ remote=%s count=%d/%d",
            remote,
            session.remote_cq_count,
            maximum,
        )
        return "aborted"

    def cooldown_for(self, station: str, now: datetime) -> StationCooldown | None:
        cooldown = self._cooldowns.get(station.upper())
        return cooldown if cooldown is not None and cooldown.until > now else None

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.config.stale_decode_seconds)
        self._seen = {identity: seen_at for identity, seen_at in self._seen.items() if seen_at >= cutoff}
        self._cooldowns = {
            station: cooldown
            for station, cooldown in self._cooldowns.items()
            if cooldown.until > now
        }
        self._candidates = {
            call: candidate
            for call, candidate in self._candidates.items()
            if call in self._pending_candidate_stations or candidate.event.observed_at >= cutoff
        }
        for entry in self.pending_direct_calls.prune(now):
            self._emit(EngineEventKind.PENDING_DIRECT_REMOVED, entry.station, "TTL_EXPIRED")

    def _promote_pending_direct_calls(self, now: datetime) -> None:
        for entry in self.pending_direct_calls.entries(now):
            self._promoting_pending = entry
            self._pending_rejection_reason = "USER_RULE"
            try:
                candidate = self.observe(entry.event, now)
            finally:
                self._promoting_pending = None
            if candidate is not None:
                continue
            self.pending_direct_calls.remove(entry.station)
            LOGGER.info(
                "[DIRECT] pending rejected station=%s reason=%s",
                entry.station,
                self._pending_rejection_reason,
            )
            self._emit(
                EngineEventKind.PENDING_DIRECT_REMOVED,
                entry.station,
                self._pending_rejection_reason,
            )

    def invalidate_pending_context(self, instance_id: str, band: str, mode: str) -> None:
        for entry in self.pending_direct_calls.invalidate_context(instance_id, band, mode):
            LOGGER.info("[DIRECT] pending rejected station=%s reason=CONTEXT_CHANGED", entry.station)
            self._emit(EngineEventKind.PENDING_DIRECT_REMOVED, entry.station, "CONTEXT_CHANGED")

    def pending_direct_snapshot(self, now: datetime) -> list[dict[str, object]]:
        return self.pending_direct_calls.snapshot(now)

    def clear_candidates(self) -> None:
        self._candidates.clear()
        self._pending_candidate_stations.clear()
        self._candidate_window = None

    def _touch_candidate_window(self, event: DecodeEvent, now: datetime) -> None:
        self._touch_candidate_window_key(self._candidate_period_key(event), now)

    def _touch_candidate_window_key(self, period_key: tuple[object, ...], now: datetime) -> None:
        if self._candidate_window is None:
            self._candidate_window = CandidateWindow(period_key, now)
            return
        if self._candidate_window.period_key != period_key:
            if self._candidates:
                LOGGER.info(
                    "[WINDOW] replacing unclosed period old_candidates=%d",
                    len(self._candidates),
                )
                self._candidates.clear()
            self._candidate_window = CandidateWindow(period_key, now)
            return
        self._candidate_window.last_decode_at = now

    def _update_provisional(self, candidate: Candidate) -> None:
        window = self._candidate_window
        if window is None:
            return
        previous = window.provisional
        forced = [item for item in self._candidates.values() if item.force_priority]
        pool = forced or list(self._candidates.values())
        provisional = max(pool, key=lambda item: (item.score, -item.event.observed_at.timestamp()))
        window.provisional = provisional
        if (
            previous is not None
            and previous.station != provisional.station
            and previous.kind is CandidateKind.CQ
            and provisional.kind is CandidateKind.DIRECT_CALLER
        ):
            LOGGER.info(
                "[ENGINE] candidate preempted old=%s type=CQ new=%s type=DIRECT_CALL",
                previous.station,
                provisional.station,
            )

    @staticmethod
    def _log_direct_rejected(station: str, blocker: str) -> None:
        LOGGER.info("[DIRECT] hard_blockers=[%s]", blocker)
        LOGGER.info("[DIRECT] soft_blockers=[]")
        LOGGER.info("[DIRECT] rejected station=%s blocker=%s", station, blocker)

    @staticmethod
    def _direct_blocker_name(reason: str) -> str:
        lowered = reason.lower()
        if "worked today" in lowered:
            return "WORKED_TODAY"
        if "blacklisted" in lowered:
            return "BLACKLIST"
        if "snr below" in lowered:
            return "MINIMUM_SNR"
        if "ignored until" in lowered:
            return "USER_TEMPORARY_IGNORE"
        if "direct calls ignored" in lowered:
            return "USER_POLICY"
        return "USER_RULE"

    @staticmethod
    def _candidate_period_key(event: DecodeEvent) -> tuple[object, ...]:
        if event.original is not None:
            return (event.original.instance_id, event.original.decode_time, event.mode)
        return (event.mode, event.frequency, event.period)

    def _emit(
        self,
        kind: EngineEventKind,
        station: str,
        reason: str,
        candidate: Candidate | None = None,
    ) -> None:
        if self._promoting_pending is not None and kind is EngineEventKind.CANDIDATE_REFUSED:
            self._pending_rejection_reason = self._direct_blocker_name(reason)
        if self._event_sink is not None:
            self._event_sink(EngineEvent(kind, station, reason, candidate))

    def _set_station_cooldown(
        self,
        station: str,
        now: datetime,
        seconds: float,
        kind: CooldownKind,
        reason: str,
    ) -> StationCooldown:
        cooldown = StationCooldown(now + timedelta(seconds=seconds), kind, reason)
        self._cooldowns[station.upper()] = cooldown
        return cooldown

    @staticmethod
    def _event_identity(event: DecodeEvent) -> str:
        return event.unique_id or f"{event.mode}|{event.parsed.raw}|{event.observed_at.isoformat()}"
