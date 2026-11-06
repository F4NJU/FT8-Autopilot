from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPaintEvent, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wsjtx_autopilot.engine.location import StationLocation, grid_to_location

from .viewmodels import SessionQsoMapPoint


def project_equirectangular(latitude: float, longitude: float, width: float, height: float) -> QPointF:
    """Return a point in an equirectangular world map viewport."""

    return QPointF((longitude + 180.0) / 360.0 * width, (90.0 - latitude) / 180.0 * height)


def fit_world_viewport(bounds: QRectF) -> QRectF:
    """Centre a 2:1 world map inside arbitrary widget bounds.

    GeoJSON coordinates are longitude/latitude, so drawing them into a square
    or portrait widget would visibly stretch the continents.  Keeping this
    viewport at the natural 360° × 180° proportion avoids that distortion.
    """

    map_width = min(bounds.width(), bounds.height() * 2.0)
    map_height = map_width / 2.0
    return QRectF(
        bounds.center().x() - map_width / 2.0,
        bounds.center().y() - map_height / 2.0,
        map_width,
        map_height,
    )


_WORLD_MAP_PATH = Path(__file__).with_name("assets") / "naturalearth-110m-countries.geojson"


@lru_cache(maxsize=1)
def _world_geometries() -> tuple[tuple[tuple[tuple[tuple[float, float], ...], ...], ...], ...]:
    """Load the bundled, public-domain Natural Earth 1:110m land contours.

    The map deliberately remains local/offline.  Coordinates are GeoJSON
    longitude/latitude pairs and are projected only while painting.
    """

    data = json.loads(_WORLD_MAP_PATH.read_text(encoding="utf-8"))
    geometries = []
    for feature in data.get("features", []):
        geometry = feature.get("geometry") or {}
        kind = geometry.get("type")
        coordinates = geometry.get("coordinates") or []
        polygons = [coordinates] if kind == "Polygon" else coordinates if kind == "MultiPolygon" else []
        for polygon in polygons:
            rings = []
            for ring in polygon:
                points = tuple((float(longitude), float(latitude)) for longitude, latitude, *_ in ring)
                if len(points) >= 3:
                    rings.append(points)
            if rings:
                geometries.append(tuple(rings))
    return tuple(geometries)


def _projected_land_path(viewport) -> QPainterPath:
    """Create a correct country-outline path for the widget viewport."""

    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    for polygon in _world_geometries():
        for ring in polygon:
            path.addPolygon(
                QPolygonF(
                    [
                        QPointF(
                            viewport.left()
                            + project_equirectangular(latitude, longitude, viewport.width(), viewport.height()).x(),
                            viewport.top()
                            + project_equirectangular(latitude, longitude, viewport.width(), viewport.height()).y(),
                        )
                        for longitude, latitude in ring
                    ]
                )
            )
    return path


class SessionMapWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._local_grid = ""
        self._points: tuple[SessionQsoMapPoint, ...] = ()
        self.setMinimumSize(520, 270)

    def set_snapshot(self, local_grid: str, points: tuple[SessionQsoMapPoint, ...]) -> None:
        self._local_grid = local_grid.strip().upper()
        self._points = points
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        available = QRectF(self.rect().adjusted(10, 12, -10, -24))
        painter.fillRect(available, QColor("#09131e"))
        viewport = fit_world_viewport(available)
        painter.setPen(QPen(QColor("#263b4b"), 1))
        for longitude in range(-180, 181, 30):
            point = project_equirectangular(0, longitude, viewport.width(), viewport.height())
            painter.drawLine(viewport.left() + point.x(), viewport.top(), viewport.left() + point.x(), viewport.bottom())
        for latitude in range(-60, 91, 30):
            point = project_equirectangular(latitude, 0, viewport.width(), viewport.height())
            painter.drawLine(viewport.left(), viewport.top() + point.y(), viewport.right(), viewport.top() + point.y())

        painter.setPen(QPen(QColor("#426b78"), 0.8))
        painter.setBrush(QColor("#1c4654"))
        painter.drawPath(_projected_land_path(viewport))

        locations: dict[tuple[float, float], list[SessionQsoMapPoint]] = defaultdict(list)
        for point in self._points:
            location = grid_to_location(point.grid)
            if location is not None:
                locations[(location.latitude, location.longitude)].append(point)
        for (latitude, longitude), grouped in locations.items():
            projected = project_equirectangular(latitude, longitude, viewport.width(), viewport.height())
            center = QPointF(viewport.left() + projected.x(), viewport.top() + projected.y())
            painter.setPen(QPen(QColor("#ffd166"), 1.5))
            painter.setBrush(QColor("#e88b2c"))
            painter.drawEllipse(center, 4.5, 4.5)
            if len(grouped) > 1:
                painter.setPen(QColor("#fff4d6"))
                painter.drawText(center + QPointF(6, -5), str(len(grouped)))

        local = grid_to_location(self._local_grid)
        if local is not None:
            projected = project_equirectangular(local.latitude, local.longitude, viewport.width(), viewport.height())
            center = QPointF(viewport.left() + projected.x(), viewport.top() + projected.y())
            painter.setPen(QPen(QColor("#b8ecff"), 2))
            painter.setBrush(QColor("#198bd1"))
            painter.drawEllipse(center, 6.5, 6.5)
            painter.drawText(center + QPointF(9, -8), "VOUS")

        painter.setPen(QColor("#71889a"))
        painter.drawText(
            available.left(),
            self.height() - 5,
            "Natural Earth 1:110m · jaune : QSO journalisé · bleu : votre station",
        )


class SessionQsoMapDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Carte QSO — session en cours")
        self.resize(1050, 690)
        self.map_widget = SessionMapWidget()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("UTC", "CALL", "LOCATOR", "BANDE"))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        details = QHBoxLayout()
        details.addWidget(QLabel("QSO JOURNALISÉS"))
        details.addWidget(self.summary, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(self.map_widget, 3)
        layout.addLayout(details)
        layout.addWidget(self.table, 2)

    def set_snapshot(self, local_grid: str, points: tuple[SessionQsoMapPoint, ...]) -> None:
        locatable = tuple(point for point in points if grid_to_location(point.grid) is not None)
        missing = len(points) - len(locatable)
        local = grid_to_location(local_grid)
        station_text = local.grid if local is not None else "locator en attente de WSJT-X"
        suffix = f" · {missing} sans locator" if missing else ""
        self.summary.setText(f"Station : {station_text}\nSession : {len(points)} QSO journalisé(s) · {len(locatable)} placé(s){suffix}")
        self.map_widget.set_snapshot(local_grid, points)
        self.table.setRowCount(0)
        for point in reversed(points):
            row = self.table.rowCount()
            self.table.insertRow(row)
            utc = point.logged_at.astimezone(timezone.utc).strftime("%H:%M") if point.logged_at.tzinfo else point.logged_at.strftime("%H:%M")
            for column, value in enumerate((utc, point.callsign, point.grid or "—", point.band)):
                self.table.setItem(row, column, QTableWidgetItem(value))
