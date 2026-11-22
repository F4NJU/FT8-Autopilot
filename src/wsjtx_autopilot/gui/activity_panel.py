"""Activity, candidate, rare-worked and session-map table rendering."""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QLabel,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wsjtx_autopilot.engine.models import EngineEvent, EngineEventKind
from wsjtx_autopilot.engine.parser import parse_ft8_message

from .session_map import SessionQsoMapDialog
from .viewmodels import ActivityRow, CandidateRow, SessionQsoMapPoint, StatusView

LOGGER = logging.getLogger(__name__)


class ActivityPanelMixin:
    def _activity_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        label = QLabel("DECODE ACTIVITY")
        label.setObjectName("panelTitle")
        self.activity = QTableWidget(0, 4)
        self.activity.setHorizontalHeaderLabels(["UTC", "SNR", "DF", "MESSAGE"])
        self.activity.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.activity.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.activity.verticalHeader().hide()
        activity_header = self.activity.horizontalHeader()
        activity_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        activity_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        activity_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        activity_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(label)
        layout.addWidget(self.activity)
        return panel

    def _rare_worked_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        label = QLabel("STATIONS RARES SUIVIES")
        label.setObjectName("panelTitle")
        self.rare_worked_summary = QLabel("Aucune station rare reçue par MQTT cette session.")
        self.rare_worked_summary.setObjectName("infoText")
        self.rare_worked = QTableWidget(0, 4)
        self.rare_worked.setHorizontalHeaderLabels(["UTC", "CALL", "BANDE", "ÉTAT"])
        self.rare_worked.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.rare_worked.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rare_worked.verticalHeader().hide()
        rare_header = self.rare_worked.horizontalHeader()
        rare_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        rare_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        rare_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        rare_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._rare_worked_rows: dict[tuple[str, str], int] = {}
        # Candidate events remain handled in the background for status messages,
        # so replacing the visual panel cannot affect radio-control behavior.
        self.pending_direct_label = QLabel("Pending Direct Calls: 0")
        self._pending_direct_stations: set[str] = set()
        self.candidates = QTableWidget(0, 5)
        self.candidates.setHorizontalHeaderLabels(["CALL", "SNR", "DXCC", "CONT", "SCORE"])
        self.candidates.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.candidates.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.candidates.verticalHeader().hide()
        candidate_header = self.candidates.horizontalHeader()
        for column in range(5):
            candidate_header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.candidates.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        ignore = QAction(f"Ignore for {self._settings.ignore_minutes} minutes", self.candidates)
        ignore.triggered.connect(self._ignore_selected)
        blacklist = QAction("Add to blacklist", self.candidates)
        blacklist.triggered.connect(self._blacklist_selected)
        self.candidates.addAction(ignore)
        self.candidates.addAction(blacklist)
        layout.addWidget(label)
        layout.addWidget(self.rare_worked_summary)
        layout.addWidget(self.rare_worked)
        return panel

    def _show_session_diagnostics(self, snapshot: dict[str, object]) -> None:
        selected = int(snapshot.get("candidates_selected", 0))
        sent = int(snapshot.get("replies_sent", 0))
        completed = int(snapshot.get("qso_completed", 0))
        refused = int(snapshot.get("candidates_refused", 0))
        busy = int(snapshot.get("stations_busy", 0))
        local_rejections = int(snapshot.get("local_rejections", 0))
        failures = int(snapshot.get("control_failures", 0))
        auto_watchdog = int(snapshot.get("autopilot_watchdogs", 0))
        wsjtx_watchdog = int(snapshot.get("wsjtx_watchdogs", 0))
        success_rate = snapshot.get("reply_success_percent")
        success_text = "—" if success_rate is None else f"{int(success_rate)} %"
        self.session_diagnostics.setText(
            f"SÉLECTIONS : {selected} · REPLIES ENVOYÉS : {sent} · QSO TERMINÉS : {completed} · RÉUSSITE : {success_text}\n"
            f"ÉCARTÉES : {refused} (DÉJÀ OCCUPÉES : {busy}) · REFUS WSJT-X : {local_rejections} · ÉCHECS : {failures}"
            f" · WATCHDOGS : AutoPilot {auto_watchdog} / WSJT-X {wsjtx_watchdog}"
        )
        recent = snapshot.get("recent", ())
        if isinstance(recent, (tuple, list)):
            self.session_diagnostic_log.setPlainText("\n".join(str(item) for item in recent[:3]))

    def _add_rare_station_worked(self, callsign: str, band: str) -> None:
        key = (callsign.upper(), band.lower())
        row = self._rare_worked_rows.get(key)
        if row is None:
            row = self.rare_worked.rowCount()
            self.rare_worked.insertRow(row)
            self._rare_worked_rows[key] = row
        for column, value in enumerate((datetime.now().strftime("%H:%M"), callsign.upper(), band, "TRAVAILLÉ")):
            self.rare_worked.setItem(row, column, QTableWidgetItem(value))
        self._update_rare_summary()

    def _add_rare_mqtt_spot(self, spot) -> None:
        callsign = str(getattr(spot, "callsign", "")).strip().upper()
        band = str(getattr(spot, "band", "")).strip().lower()
        observed_at = getattr(spot, "observed_at", None)
        if not callsign or not band:
            return
        key = (callsign, band)
        row = self._rare_worked_rows.get(key)
        if row is None:
            row = self.rare_worked.rowCount()
            self.rare_worked.insertRow(row)
            self._rare_worked_rows[key] = row
        timestamp = observed_at.strftime("%H:%M") if isinstance(observed_at, datetime) else datetime.now().strftime("%H:%M")
        state = self.rare_worked.item(row, 3)
        status = state.text() if state is not None and state.text() == "TRAVAILLÉ" else "VU MQTT"
        for column, value in enumerate((timestamp, callsign, band, status)):
            self.rare_worked.setItem(row, column, QTableWidgetItem(value))
        self._update_rare_summary()

    def _add_unconfirmed_dxcc_mqtt_spot(self, spot) -> None:
        """Show one accepted DXCC discovery without exposing the raw MQTT firehose."""
        callsign = str(getattr(spot, "callsign", "")).strip().upper()
        band = str(getattr(spot, "band", "")).strip().lower()
        observed_at = getattr(spot, "observed_at", None)
        if not callsign or not band:
            return
        key = (callsign, band)
        row = self._rare_worked_rows.get(key)
        if row is None:
            row = self.rare_worked.rowCount()
            self.rare_worked.insertRow(row)
            self._rare_worked_rows[key] = row
        timestamp = observed_at.strftime("%H:%M") if isinstance(observed_at, datetime) else datetime.now().strftime("%H:%M")
        state = self.rare_worked.item(row, 3)
        status = state.text() if state is not None and state.text() == "TRAVAILLÉ" else "DXCC MQTT"
        for column, value in enumerate((timestamp, callsign, band, status)):
            self.rare_worked.setItem(row, column, QTableWidgetItem(value))
        self._update_rare_summary()

    def _update_rare_summary(self) -> None:
        seen = self.rare_worked.rowCount()
        rare_seen = sum(
            1
            for row in range(seen)
            if (item := self.rare_worked.item(row, 3)) is not None and item.text() == "VU MQTT"
        )
        dxcc_seen = sum(
            1
            for row in range(seen)
            if (item := self.rare_worked.item(row, 3)) is not None and item.text() == "DXCC MQTT"
        )
        worked = sum(
            1
            for row in range(seen)
            if (item := self.rare_worked.item(row, 3)) is not None and item.text() == "TRAVAILLÉ"
        )
        self.rare_worked_summary.setText(
            f"MQTT : {rare_seen} rare(s) suivie(s) · {dxcc_seen} DXCC à confirmer · QSO : {worked}"
        )

    def _add_activity(self, row: ActivityRow) -> None:
        target = 0
        self.activity.insertRow(target)
        values = [
            row.observed_at.strftime("%H:%M:%S"),
            f"{row.snr:+d}",
            str(row.delta_frequency),
            row.message,
        ]
        for column, value in enumerate(values):
            self.activity.setItem(target, column, QTableWidgetItem(value))
        while self.activity.rowCount() > 250:
            self.activity.removeRow(self.activity.rowCount() - 1)
        parsed = parse_ft8_message(row.message)
        if parsed is not None:
            self.hunt_panel.note_local_decode(parsed.sender, row.observed_at)

    def _handle_engine_event(self, event: EngineEvent) -> None:
        if event.kind is EngineEventKind.PENDING_DIRECT_ADDED:
            self._pending_direct_stations.add(event.station)
            self._update_pending_direct_label()
            self.statusBar().showMessage(f"Pending Direct Call: {event.station}", 5000)
        elif event.kind is EngineEventKind.PENDING_DIRECT_REMOVED:
            self._pending_direct_stations.discard(event.station)
            self._update_pending_direct_label()
        row = CandidateRow.from_engine_event(event)
        if event.kind is EngineEventKind.CANDIDATE_REFUSED:
            self.statusBar().showMessage(f"Refused {event.station or '-'}: {event.reason}", 5000)
            return
        if row is None:
            return
        if event.kind is EngineEventKind.CANDIDATE_ADDED:
            kind = "DIRECT CALL / priority override" if row.kind == "DIRECT_CALLER" else row.kind
            self.statusBar().showMessage(
                f"Candidate {row.station} ({kind}) - waiting for decode window..."
            )
        target = self._candidate_rows.get(row.station)
        if target is None:
            target = self.candidates.rowCount()
            self.candidates.insertRow(target)
            self._candidate_rows[row.station] = target
        values = [
            row.station,
            f"{row.snr:+d}",
            row.dxcc,
            row.continent,
            str(row.score),
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if event.kind is EngineEventKind.CANDIDATE_SELECTED:
                item.setBackground(QColor("#173f37"))
            self.candidates.setItem(target, column, item)
        if event.kind is EngineEventKind.CANDIDATE_SELECTED:
            self.statusBar().showMessage(f"Selected {row.station}: {row.score_detail}", 8000)

    def _update_pending_direct_label(self) -> None:
        count = len(self._pending_direct_stations)
        next_station = next(iter(sorted(self._pending_direct_stations)), "-")
        suffix = f" | Next: {next_station}" if count else ""
        self.pending_direct_label.setText(f"Pending Direct Calls: {count}{suffix}")

    def _show_connection(self, connected: bool) -> None:
        self.connection.setText("WSJT-X ONLINE" if connected else "OFFLINE")
        self.connection.setProperty("online", connected)
        self.connection.style().unpolish(self.connection)
        self.connection.style().polish(self.connection)

    def _show_armed(self, armed: bool) -> None:
        if self._armed != armed:
            LOGGER.warning("[GUI] ARM state divergence backend_armed=%s ui_armed=%s; resynchronizing", armed, self._armed)
        self._armed = armed
        self.arm_button.setText("DISARM NOW" if armed else "ARM CONTROL")
        self.arm_button.setProperty("armed", armed)
        self.arm_button.style().unpolish(self.arm_button)
        self.arm_button.style().polish(self.arm_button)
        self._update_swr_reset_button()
        LOGGER.info("[GUI] ARM state update armed=%s", armed)
        LOGGER.info("[GUI] control state backend_armed=%s ui_armed=%s", armed, self._armed)

    def _show_swr_safety_notification(self, message: str) -> None:
        QMessageBox.critical(
            self,
            "Protection SWR — AutoPilot désarmé",
            "AutoPilot a arrêté l’émission et s’est désarmé.\n\n"
            f"{message}\n\n"
            "Vérifiez l’antenne et l’accord avant de réarmer.",
        )

    def _show_status(self, status: StatusView) -> None:
        self.frequency.setText(status.frequency)
        self.mode.setText(status.mode)
        self.tx_state.setText(status.tx_state)
        self._local_grid = status.local_grid
        self._refresh_session_qso_map()

    def _add_session_qso_map_point(self, point: object) -> None:
        if not isinstance(point, SessionQsoMapPoint):
            return
        self._session_qso_points.append(point)
        self.session_map_button.setText(f"CARTE QSO ({len(self._session_qso_points)})")
        self._refresh_session_qso_map()

    def _show_session_qso_map(self) -> None:
        if self._session_map_dialog is None:
            self._session_map_dialog = SessionQsoMapDialog(self)
        self._refresh_session_qso_map()
        self._session_map_dialog.show()
        self._session_map_dialog.raise_()
        self._session_map_dialog.activateWindow()

    def _refresh_session_qso_map(self) -> None:
        if self._session_map_dialog is not None:
            self._session_map_dialog.set_snapshot(self._local_grid, tuple(self._session_qso_points))

