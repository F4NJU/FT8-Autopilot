import logging
from collections.abc import Callable
from dataclasses import dataclass
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
    StationProfile,
    StationCooldown,
    WorkedCheck,
)
from .scoring import CandidateScorer
from .state import QsoState, QsoStateMachine

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
        if self.state.session.state is QsoState.IDLE:
            self._touch_candidate_window(event, now)
        if now - event.observed_at > timedelta(seconds=self.config.stale_decode_seconds):
            LOGGER.info("[ENGINE] candidate refused reason=stale text=%s", event.parsed.raw)
            self._emit(EngineEventKind.CANDIDATE_REFUSED, event.parsed.sender, "stale")
            return None
        identity = self._event_identity(event)
        if identity in self._seen:
            LOGGER.info("[ENGINE] candidate refused reason=duplicate text=%s", event.parsed.raw)
            self._emit(EngineEventKind.CANDIDATE_REFUSED, event.parsed.sender, "duplicate decode")
            return None
        self._seen[identity] = now

        message = event.parsed
        direct_to_local = message.is_addressed_to(self.config.local_callsign) and message.kind in {
            MessageKind.DIRECTED,
            MessageKind.REPORT,
            MessageKind.R_REPORT,
        }
        qso_active = self.state.session.state is not QsoState.IDLE
        if qso_active and message.kind not in {MessageKind.CQ, MessageKind.QRZ}:
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
            worked = self._worked_lookup(message.sender, event.frequency, event.observed_at)
            if worked.band is None:
                LOGGER.info(
                    "[ENGINE] candidate refused station=%s reason=unable to resolve band",
                    message.sender,
                )
                self._emit(EngineEventKind.CANDIDATE_REFUSED, message.sender, "unable to resolve band")
                return None
            if self._scorer is None and worked.duplicate:
                LOGGER.info(
                    "[ENGINE] candidate refused station=%s reason=worked today band=%s",
                    message.sender,
                    worked.band,
                )
                self._emit(EngineEventKind.CANDIDATE_REFUSED, message.sender, f"worked today band={worked.band}")
                return None
        metadata = None
        breakdown = None
        if self._scorer is not None:
            scoring = self._scorer.evaluate(event, kind, worked)
            if not scoring.accepted:
                LOGGER.info(
                    "[ENGINE] candidate refused station=%s reason=%s",
                    message.sender,
                    scoring.reason,
                )
                self._emit(EngineEventKind.CANDIDATE_REFUSED, message.sender, scoring.reason)
                return None
            score = scoring.breakdown.total
            metadata = scoring.metadata
            breakdown = scoring.breakdown
            force_priority = scoring.force_priority
        cooldown = self._cooldowns.get(message.sender)
        if cooldown is not None and now < cooldown.until:
            LOGGER.info(
                "[ENGINE] candidate refused station=%s cooldown=%s reason=%s until=%s",
                message.sender,
                cooldown.kind.name,
                cooldown.reason,
                cooldown.until.isoformat(),
            )
            self._emit(EngineEventKind.CANDIDATE_REFUSED, message.sender, cooldown.reason)
            return None
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
            LOGGER.info(
                "[ENGINE] candidate %s: %s score=%d state=%s",
                "direct call" if kind is CandidateKind.DIRECT_CALLER else "CQ",
                candidate.station,
                score,
                self.state.session.state.name,
            )
            self._emit(EngineEventKind.CANDIDATE_ADDED, candidate.station, "accepted", candidate)
            LOGGER.info(
                "[WINDOW] candidate %s %s",
                "DIRECT" if candidate.kind is CandidateKind.DIRECT_CALLER else "CQ",
                candidate.station,
            )
            self._update_provisional(candidate)
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
        viable = [
            candidate
            for candidate in self._candidates.values()
            if now - candidate.event.observed_at <= timedelta(seconds=self.config.stale_decode_seconds)
        ]
        if not viable:
            return None
        window = self._candidate_window
        if (
            window is not None
            and now - window.last_decode_at < timedelta(seconds=self.config.candidate_collection_seconds)
        ):
            LOGGER.debug("[WINDOW] waiting for decode window last_decode=%s", window.last_decode_at.isoformat())
            return None
        LOGGER.info("[WINDOW] closing candidates=%d", len(viable))
        direct = [candidate for candidate in viable if candidate.force_priority]
        if direct:
            pool = direct
            reason = "direct caller"
        else:
            pool = viable
            reason = "CQ candidate"
        selected = max(pool, key=lambda candidate: (candidate.score, -candidate.event.observed_at.timestamp()))
        reason = "direct caller" if selected.kind is CandidateKind.DIRECT_CALLER else "CQ candidate"
        self._candidates.clear()
        self._candidate_window = None
        self._pending_actions[selected.station] = selected.event
        LOGGER.info(
            "[ENGINE] selected: %s reason=%s state=%s",
            selected.station,
            "DIRECT_CALL" if selected.kind is CandidateKind.DIRECT_CALLER else "CQ",
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
                    action.selected_tx_df,
                    action.tx_df_reason,
                    action.tx_df_gap_width,
                )
                self.state.mark_direct_reply_sent(now)
            elif self.state.session.state is QsoState.IDLE:
                self.state.start_station(
                    event,
                    now,
                    action.selected_tx_df,
                    action.tx_df_reason,
                    action.tx_df_gap_width,
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
        LOGGER.info("[ENGINE] invalidated Decode candidates instance=%s reason=Clear", instance_id)

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
        LOGGER.info("[ENGINE] QSO complete remote=%s", remote)
        self._candidates.clear()
        self._pending_actions.clear()
        self.state.reset()
        return True

    def finalize_observed_completion(self, reason: str) -> bool:
        if self.state.session.state is not QsoState.COMPLETE:
            return False
        remote = self.state.session.remote_callsign
        LOGGER.info("[ENGINE] QSO complete remote=%s reason=%s", remote, reason)
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
        LOGGER.info("[ENGINE] QSO abandoned remote=%s reason=%s", remote, reason)
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
            call: candidate for call, candidate in self._candidates.items() if candidate.event.observed_at >= cutoff
        }

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
