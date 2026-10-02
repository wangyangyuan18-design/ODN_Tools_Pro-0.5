# -*- coding: utf-8 -*-
"""Automatically generate POLE EDGE from existing cable routes.

The source cable layers are never modified.  The tool:
1) loads one or more pole point layers;
2) loads one or more existing cable line layers (e.g. DC / FEEDER);
3) maps each cable route to the poles it passes within a real-world
   tolerance distance;
4) orders the passed poles along the cable geometry;
5) creates pole-to-pole POLE EDGE features in the selected output line layer.

The analysis distance is CRS-adaptive: projected CRSs are converted to metres
and geographic CRSs are analysed in a local UTM CRS.
"""

import math
from collections import defaultdict

from qgis.PyQt import QtWidgets, QtCore
from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QFont
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsMapLayerType,
    QgsPointXY,
    QgsProject,
    QgsSpatialIndex,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsWkbTypes,
)


def _pump_ui(counter, every=200):
    if counter and counter % every == 0:
        try:
            QtWidgets.QApplication.processEvents()
        except Exception:
            pass


def _point_from_feature(feature):
    geom = feature.geometry()
    if geom is None or geom.isEmpty():
        return None
    try:
        if QgsWkbTypes.geometryType(geom.wkbType()) == QgsWkbTypes.PointGeometry:
            if geom.isMultipart():
                points = geom.asMultiPoint()
                if points:
                    return QgsPointXY(points[0])
            else:
                return QgsPointXY(geom.asPoint())
        centroid = geom.centroid()
        if centroid is not None and not centroid.isEmpty():
            return QgsPointXY(centroid.asPoint())
    except Exception:
        pass
    return None


def _line_parts(geom):
    if geom is None or geom.isEmpty():
        return []
    try:
        if geom.isMultipart():
            return [list(part) for part in geom.asMultiPolyline()
                    if len(part) >= 2]
        points = list(geom.asPolyline())
        return [points] if len(points) >= 2 else []
    except Exception:
        return []


def _transform_geometry(geom, transform):
    result = QgsGeometry(geom)
    if transform is None:
        return result
    result.transform(transform)
    return result


def _utm_crs_for_layer(layer):
    source_crs = layer.crs()
    to_wgs84 = QgsCoordinateTransform(
        source_crs,
        QgsCoordinateReferenceSystem("EPSG:4326"),
        QgsProject.instance(),
    )
    extent = layer.extent()
    center = QgsPointXY(
        (extent.xMinimum() + extent.xMaximum()) / 2.0,
        (extent.yMinimum() + extent.yMaximum()) / 2.0,
    )
    center_wgs84 = to_wgs84.transform(center)
    lon = max(-180.0, min(180.0, center_wgs84.x()))
    lat = max(-80.0, min(84.0, center_wgs84.y()))
    zone = int(math.floor((lon + 180.0) / 6.0) + 1)
    zone = max(1, min(60, zone))
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return QgsCoordinateReferenceSystem("EPSG:%d" % epsg)


def _analysis_context(source_layer):
    """Return analysis CRS and tolerance conversion information."""
    crs = source_layer.crs()
    project = QgsProject.instance()

    if crs.isGeographic():
        analysis_crs = _utm_crs_for_layer(source_layer)
        source_to_analysis = QgsCoordinateTransform(
            crs, analysis_crs, project
        )
        analysis_to_source = QgsCoordinateTransform(
            analysis_crs, crs, project
        )
        return analysis_crs, source_to_analysis, analysis_to_source

    try:
        factor = QgsUnitTypes.fromUnitToUnitFactor(
            crs.mapUnits(), QgsUnitTypes.DistanceMeters
        )
        if factor > 0:
            return crs, None, None
    except Exception:
        pass

    analysis_crs = _utm_crs_for_layer(source_layer)
    source_to_analysis = QgsCoordinateTransform(
        crs, analysis_crs, project
    )
    analysis_to_source = QgsCoordinateTransform(
        analysis_crs, crs, project
    )
    return analysis_crs, source_to_analysis, analysis_to_source


def _metre_to_analysis_units(source_layer, metres):
    crs = source_layer.crs()
    if crs.isGeographic():
        return float(metres)
    try:
        factor = QgsUnitTypes.fromUnitToUnitFactor(
            crs.mapUnits(), QgsUnitTypes.DistanceMeters
        )
        if factor > 0:
            # factor = metres represented by one source CRS unit.
            return float(metres) / factor
    except Exception:
        pass
    return float(metres)


def _build_pole_records(pole_layer_ids, analysis_crs, cancel_callback):
    project = QgsProject.instance()
    records = []
    index = QgsSpatialIndex()
    index_map = {}

    scan_counter = 0
    for layer_id in pole_layer_ids:
        layer = project.mapLayer(layer_id)
        if layer is None:
            continue
        transform = None
        if layer.crs() != analysis_crs:
            transform = QgsCoordinateTransform(
                layer.crs(), analysis_crs, project
            )

        for feature in layer.getFeatures():
            scan_counter += 1
            if scan_counter % 250 == 0:
                _pump_ui(scan_counter, 250)
                if cancel_callback():
                    return records, index, index_map, True

            point = _point_from_feature(feature)
            if point is None:
                continue

            try:
                if transform is not None:
                    point = QgsPointXY(transform.transform(point))
            except Exception as exc:
                raise RuntimeError(
                    "杆路图层 %s 坐标转换失败：%s" % (layer.name(), exc)
                )

            key = (layer_id, int(feature.id()))
            record = {
                "key": key,
                "layer_id": layer_id,
                "fid": int(feature.id()),
                "layer_name": str(layer.name()),
                "point": point,
            }
            iid = len(records)
            records.append(record)

            index_feature = QgsFeature()
            index_feature.setId(iid)
            index_feature.setGeometry(QgsGeometry.fromPointXY(point))
            index.addFeature(index_feature)
            index_map[iid] = record

    if not records:
        raise RuntimeError("选定杆路图层没有可用的杆点。")
    return records, index, index_map, False


def _edge_key(key_a, key_b):
    if key_a == key_b:
        return None
    return tuple(sorted((key_a, key_b), key=lambda value: (str(value[0]), int(value[1]))))


def _part_measure(part_geom, point):
    try:
        return float(
            part_geom.lineLocatePoint(
                QgsGeometry.fromPointXY(point)
            )
        )
    except Exception:
        return None


def _collect_poles_on_part(
    part_geom,
    pole_index,
    index_map,
    tolerance_units,
    seen_keys,
    cancel_callback,
    counter,
):
    if part_geom is None or part_geom.isEmpty():
        return [], counter

    bbox = part_geom.boundingBox()
    bbox.grow(float(tolerance_units))
    candidate_ids = pole_index.intersects(bbox)

    hits = []
    local_seen = set()

    for candidate_pos, index_id in enumerate(candidate_ids, start=1):
        counter += 1
        if candidate_pos % 250 == 0:
            _pump_ui(counter, 250)
            if cancel_callback():
                return hits, counter

        record = index_map.get(index_id)
        if record is None:
            continue
        key = record["key"]
        if key in local_seen:
            continue

        pole_geom = QgsGeometry.fromPointXY(record["point"])
        try:
            distance = float(part_geom.distance(pole_geom))
        except Exception:
            continue
        if distance > tolerance_units:
            continue

        measure = _part_measure(part_geom, record["point"])
        if measure is None or measure < 0:
            continue

        local_seen.add(key)
        seen_keys.add(key)
        hits.append((measure, distance, key, record))

    # Stable ordering: cable position first, then perpendicular distance,
    # then layer/FID identity.
    hits.sort(
        key=lambda item: (
            round(item[0], 9),
            round(item[1], 9),
            str(item[2][0]),
            int(item[2][1]),
        )
    )
    return hits, counter


def _extract_endpoints(geom):
    parts = _line_parts(geom)
    if not parts:
        return []
    return [(QgsPointXY(part[0]), QgsPointXY(part[-1])) for part in parts]


def _nearest_record(point, pole_index, index_map, tolerance_units):
    candidates = pole_index.nearestNeighbor(point, 8)
    best = None
    best_distance = float("inf")
    for index_id in candidates:
        record = index_map.get(index_id)
        if record is None:
            continue
        distance = point.distance(record["point"])
        if distance <= tolerance_units and distance < best_distance:
            best = record
            best_distance = distance
    return best


def _read_existing_edges(
    output_layer,
    pole_index,
    index_map,
    output_to_analysis,
):
    """Map already-existing output edges to pole pairs to avoid duplicates."""
    existing = set()
    if output_layer is None:
        return existing

    # Existing POLE EDGE features are expected to sit exactly on pole
    # coordinates.  0.5 m is deliberately smaller than the cable snap
    # tolerance to avoid swallowing nearby, unrelated lines.
    output_snap_tolerance = 0.5
    project = QgsProject.instance()

    counter = 0
    for feature in output_layer.getFeatures():
        counter += 1
        if counter % 250 == 0:
            _pump_ui(counter, 250)

        geom = feature.geometry()
        if geom is None or geom.isEmpty():
            continue

        for start_point, end_point in _extract_endpoints(geom):
            try:
                if output_to_analysis is not None:
                    start_point = QgsPointXY(
                        output_to_analysis.transform(start_point)
                    )
                    end_point = QgsPointXY(
                        output_to_analysis.transform(end_point)
                    )
            except Exception:
                continue

            start_record = _nearest_record(
                start_point,
                pole_index,
                index_map,
                output_snap_tolerance,
            )
            end_record = _nearest_record(
                end_point,
                pole_index,
                index_map,
                output_snap_tolerance,
            )
            if start_record is None or end_record is None:
                continue

            key = _edge_key(start_record["key"], end_record["key"])
            if key is not None:
                existing.add(key)
    return existing


def _make_output_geometry(output_layer, start_point, end_point):
    if QgsWkbTypes.isMultiType(output_layer.wkbType()):
        return QgsGeometry.fromMultiPolylineXY([[start_point, end_point]])
    return QgsGeometry.fromPolylineXY([start_point, end_point])


class AutoPoleEdgeDialog(QtWidgets.QDialog):
    """Dialog for fully automatic cable-to-pole-edge generation."""

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("光缆全自动连线（生成 POLE EDGE）")
        self.setMinimumWidth(500)
        self._running = False
        self._cancel_requested = False
        self._added_feature_ids = []
        self._started_editing = False

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(9)

        title = QtWidgets.QLabel("光缆全自动连线")
        font = QFont()
        font.setPointSize(12)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)

        layout.addWidget(QtWidgets.QLabel("杆路图层（可多选）"))
        self.pole_list = QtWidgets.QListWidget()
        self.pole_list.setSelectionMode(
            QtWidgets.QAbstractItemView.NoSelection
        )
        self.pole_list.setMinimumHeight(130)
        layout.addWidget(self.pole_list)

        layout.addWidget(
            QtWidgets.QLabel(
                "已有光缆图层（可多选，支持 DC / FEEDER）"
            )
        )
        self.cable_list = QtWidgets.QListWidget()
        self.cable_list.setSelectionMode(
            QtWidgets.QAbstractItemView.NoSelection
        )
        self.cable_list.setMinimumHeight(130)
        layout.addWidget(self.cable_list)

        layout.addWidget(QtWidgets.QLabel("输出轨迹图层（POLE EDGE）"))
        self.output_combo = QtWidgets.QComboBox()
        layout.addWidget(self.output_combo)

        distance_row = QtWidgets.QHBoxLayout()
        distance_row.addWidget(QtWidgets.QLabel("光缆归杆距离（m）"))
        self.distance_spin = QtWidgets.QDoubleSpinBox()
        self.distance_spin.setRange(0.01, 1000.0)
        self.distance_spin.setDecimals(2)
        self.distance_spin.setSingleStep(0.5)
        self.distance_spin.setValue(5.0)
        self.distance_spin.setSuffix(" m")
        distance_row.addWidget(self.distance_spin)
        layout.addLayout(distance_row)

        tip = QtWidgets.QLabel(
            "算法：将所选 DC / FEEDER 按轨迹归杆，按光缆前进方向自动排列经过的杆子，"
            "相邻杆子自动生成一条 POLE EDGE。
"
            "原有光缆图层不会修改；输出图层中的已有相同杆间边不会重复生成。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#666; padding:4px 0;")
        layout.addWidget(tip)

        self.status_label = QtWidgets.QLabel("就绪")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)

        buttons = QtWidgets.QDialogButtonBox()
        self.run_btn = buttons.addButton(
            "开始全自动连线",
            QtWidgets.QDialogButtonBox.AcceptRole,
        )
        self.cancel_btn = buttons.addButton(
            "取消",
            QtWidgets.QDialogButtonBox.RejectRole,
        )
        self.run_btn.clicked.connect(self._run)
        self.cancel_btn.clicked.connect(self._cancel)
        layout.addWidget(buttons)

        self._load_layers()

    def _load_layers(self):
        self.pole_list.clear()
        self.cable_list.clear()
        self.output_combo.clear()

        project = QgsProject.instance()
        for layer in project.mapLayers().values():
            if layer.type() != QgsMapLayerType.VectorLayer:
                continue
            geometry_type = QgsWkbTypes.geometryType(layer.wkbType())

            if geometry_type == QgsWkbTypes.PointGeometry:
                item = QtWidgets.QListWidgetItem(layer.name())
                item.setData(Qt.UserRole, layer.id())
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                self.pole_list.addItem(item)

            elif geometry_type == QgsWkbTypes.LineGeometry:
                item = QtWidgets.QListWidgetItem(layer.name())
                item.setData(Qt.UserRole, layer.id())
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                self.cable_list.addItem(item)
                self.output_combo.addItem(layer.name(), layer.id())

        self._refresh_buttons()

    def _checked_ids(self, widget):
        result = []
        for i in range(widget.count()):
            item = widget.item(i)
            if item.checkState() == Qt.Checked:
                layer_id = item.data(Qt.UserRole)
                if layer_id:
                    result.append(layer_id)
        return result

    def _refresh_buttons(self):
        if self._running:
            return
        self.run_btn.setEnabled(
            bool(self._checked_ids(self.pole_list))
            and bool(self._checked_ids(self.cable_list))
            and self.output_combo.currentData() is not None
        )

    def _set_running(self, running):
        self._running = bool(running)
        for widget in (self.pole_list, self.cable_list):
            widget.setEnabled(not running)
        self.output_combo.setEnabled(not running)
        self.distance_spin.setEnabled(not running)
        self.run_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(True)

    def _cancel(self):
        if self._running:
            self._cancel_requested = True
            self.status_label.setText("正在取消，请完成当前小步骤后停止……")
            try:
                QtWidgets.QApplication.processEvents()
            except Exception:
                pass
            return
        self.reject()

    def _show_result(self, title, text, icon=QtWidgets.QMessageBox.Information):
        try:
            QtWidgets.QMessageBox.information(
                self,
                title,
                text,
            ) if icon == QtWidgets.QMessageBox.Information else QtWidgets.QMessageBox.critical(
                self,
                title,
                text,
            )
        except Exception:
            pass

    def _run(self):
        if self._running:
            return

        pole_ids = self._checked_ids(self.pole_list)
        cable_ids = self._checked_ids(self.cable_list)
        output_id = self.output_combo.currentData()
        tolerance_m = float(self.distance_spin.value())

        if not pole_ids:
            QtWidgets.QMessageBox.warning(
                self,
                "光缆全自动连线",
                "请至少选择一个杆路图层。",
            )
            return
        if not cable_ids:
            QtWidgets.QMessageBox.warning(
                self,
                "光缆全自动连线",
                "请至少选择一个已有光缆图层（DC / FEEDER）。",
            )
            return
        if not output_id:
            QtWidgets.QMessageBox.warning(
                self,
                "光缆全自动连线",
                "请选择输出 POLE EDGE 图层。",
            )
            return

        output_layer = QgsProject.instance().mapLayer(output_id)
        if output_layer is None or output_layer.type() != QgsMapLayerType.VectorLayer:
            QtWidgets.QMessageBox.critical(
                self,
                "光缆全自动连线",
                "输出轨迹图层不存在。",
            )
            return
        if QgsWkbTypes.geometryType(output_layer.wkbType()) != QgsWkbTypes.LineGeometry:
            QtWidgets.QMessageBox.critical(
                self,
                "光缆全自动连线",
                "输出轨迹图层必须是线图层（POLE EDGE）。",
            )
            return

        source_pole_layer = QgsProject.instance().mapLayer(pole_ids[0])
        if source_pole_layer is None:
            QtWidgets.QMessageBox.critical(
                self,
                "光缆全自动连线",
                "无法读取第一个杆路图层。",
            )
            return

        self._cancel_requested = False
        self._added_feature_ids = []
        self._started_editing = False
        self._set_running(True)

        try:
            result = self._process(
                pole_ids,
                cable_ids,
                output_layer,
                source_pole_layer,
                tolerance_m,
            )

            if result.get("cancelled"):
                self.status_label.setText("已取消。")
                return

            self.status_label.setText(
                "完成：新增 %d 条 POLE EDGE，跳过重复 %d 条。"
                % (result["added"], result["duplicates"])
            )
            self.progress.setValue(100)

            detail = (
                "光缆全自动连线完成\n\n"
                "杆路图层：%d\n"
                "光缆图层：%d\n"
                "处理光缆要素：%d\n"
                "包含有效杆序的光缆：%d\n"
                "涉及杆点：%d\n"
                "新生成 POLE EDGE：%d\n"
                "跳过重复边：%d\n"
                "不足 2 杆未生成：%d\n"
                "处理失败：%d"
                % (
                    result["pole_layer_count"],
                    result["cable_layer_count"],
                    result["cable_features"],
                    result["usable_cables"],
                    result["pole_hits"],
                    result["added"],
                    result["duplicates"],
                    result["no_edge_cables"],
                    result["failed_cables"],
                )
            )
            self._show_result("光缆全自动连线", detail)
        except Exception as exc:
            self._rollback_written(output_layer)
            self.status_label.setText("失败：%s" % exc)
            QtWidgets.QMessageBox.critical(
                self,
                "光缆全自动连线",
                "自动连线失败：\n%s" % exc,
            )
        finally:
            self._set_running(False)
            self._refresh_buttons()
            self._added_feature_ids = []
            self._started_editing = False

    def _process(
        self,
        pole_ids,
        cable_ids,
        output_layer,
        source_pole_layer,
        tolerance_m,
    ):
        project = QgsProject.instance()
        analysis_crs, source_to_analysis, analysis_to_source = (
            _analysis_context(source_pole_layer)
        )
        tolerance_units = _metre_to_analysis_units(
            source_pole_layer,
            tolerance_m,
        )

        self.status_label.setText("正在建立杆点索引……")
        self.progress.setValue(1)
        poles, pole_index, index_map, cancelled = _build_pole_records(
            pole_ids,
            analysis_crs,
            lambda: self._cancel_requested,
        )
        if cancelled or self._cancel_requested:
            return {"cancelled": True}

        self.status_label.setText(
            "杆点索引完成：%d 个杆点；正在读取已有 POLE EDGE……"
            % len(poles)
        )
        self.progress.setValue(5)

        output_to_analysis = None
        if output_layer.crs() != analysis_crs:
            output_to_analysis = QgsCoordinateTransform(
                output_layer.crs(), analysis_crs, project
            )

        existing_edges = _read_existing_edges(
            output_layer,
            pole_index,
            index_map,
            output_to_analysis,
        )
        if self._cancel_requested:
            return {"cancelled": True}

        cables = []
        for layer_id in cable_ids:
            layer = project.mapLayer(layer_id)
            if layer is None:
                continue
            cables.append(layer)

        started_editing = False
        self._started_editing = False
        if not output_layer.isEditable():
            if not output_layer.startEditing():
                raise RuntimeError(
                    "输出 POLE EDGE 图层无法进入编辑状态。"
                )
            started_editing = True
            self._started_editing = True

        output_transform = None
        if analysis_crs != output_layer.crs():
            output_transform = QgsCoordinateTransform(
                analysis_crs,
                output_layer.crs(),
                project,
            )

        counters = {
            "pole_layer_count": len(pole_ids),
            "cable_layer_count": len(cables),
            "cable_features": 0,
            "usable_cables": 0,
            "pole_hits": 0,
            "added": 0,
            "duplicates": 0,
            "no_edge_cables": 0,
            "failed_cables": 0,
        }
        created_edges = set(existing_edges)
        batch = []
        candidate_counter = 0

        total_cable_features = 0
        for layer in cables:
            try:
                total_cable_features += max(0, int(layer.featureCount()))
            except Exception:
                pass
        total_cable_features = max(1, total_cable_features)

        try:
            for layer_number, cable_layer in enumerate(cables, start=1):
                source_transform = None
                if cable_layer.crs() != analysis_crs:
                    source_transform = QgsCoordinateTransform(
                        cable_layer.crs(), analysis_crs, project
                    )

                for feature in cable_layer.getFeatures():
                    counters["cable_features"] += 1
                    feature_index = counters["cable_features"]
                    _pump_ui(feature_index, 25)

                    if self._cancel_requested:
                        self._rollback_written(output_layer)
                        return {"cancelled": True}

                    self.status_label.setText(
                        "处理光缆 %d/%d：%s"
                        % (
                            feature_index,
                            total_cable_features,
                            cable_layer.name(),
                        )
                    )
                    percent = 5 + int(
                        88.0 * feature_index / total_cable_features
                    )
                    self.progress.setValue(min(98, max(5, percent)))

                    geom = feature.geometry()
                    if geom is None or geom.isEmpty():
                        counters["failed_cables"] += 1
                        continue

                    try:
                        analysis_geom = _transform_geometry(
                            geom,
                            source_transform,
                        )
                    except Exception:
                        counters["failed_cables"] += 1
                        continue

                    parts = _line_parts(analysis_geom)
                    if not parts:
                        counters["failed_cables"] += 1
                        continue

                    cable_hits = []
                    cable_seen = set()

                    for part_index, part in enumerate(parts, start=1):
                        if self._cancel_requested:
                            self._rollback_written(output_layer)
                            return {"cancelled": True}

                        part_geom = QgsGeometry.fromPolylineXY(part)
                        hits, candidate_counter = _collect_poles_on_part(
                            part_geom,
                            pole_index,
                            index_map,
                            tolerance_units,
                            cable_seen,
                            lambda: self._cancel_requested,
                            candidate_counter,
                        )

                        cable_hits.extend(
                            (measure, distance, key, record, part_index)
                            for measure, distance, key, record in hits
                        )

                    if not cable_hits:
                        counters["no_edge_cables"] += 1
                        continue

                    # Sort by part first and then by position. For multipart
                    # cables, each part is treated as an independent route.
                    grouped = defaultdict(list)
                    for item in cable_hits:
                        grouped[item[4]].append(item[:4])

                    cable_added_any = False
                    for part_index in sorted(grouped):
                        ordered = grouped[part_index]
                        ordered.sort(
                            key=lambda item: (
                                round(item[0], 9),
                                round(item[1], 9),
                                str(item[2][0]),
                                int(item[2][1]),
                            )
                        )

                        # Collapse repeated hits to the same pole within a
                        # cable part. A pole can appear again later if the
                        # cable loops back; edge de-duplication handles the
                        # resulting undirected duplicate.
                        ordered_records = []
                        seen_part_poles = set()
                        for item in ordered:
                            key = item[2]
                            if key in seen_part_poles:
                                continue
                            seen_part_poles.add(key)
                            ordered_records.append(item[3])

                        counters["pole_hits"] += len(ordered_records)
                        if len(ordered_records) < 2:
                            continue

                        counters["usable_cables"] += 1
                        for a, b in zip(
                            ordered_records,
                            ordered_records[1:],
                        ):
                            edge = _edge_key(a["key"], b["key"])
                            if edge is None:
                                continue
                            if edge in created_edges:
                                counters["duplicates"] += 1
                                continue

                            start_point = QgsPointXY(a["point"])
                            end_point = QgsPointXY(b["point"])
                            if output_transform is not None:
                                start_point = QgsPointXY(
                                    output_transform.transform(start_point)
                                )
                                end_point = QgsPointXY(
                                    output_transform.transform(end_point)
                                )

                            out_feature = QgsFeature(output_layer.fields())
                            out_feature.setGeometry(
                                _make_output_geometry(
                                    output_layer,
                                    start_point,
                                    end_point,
                                )
                            )
                            batch.append(out_feature)
                            created_edges.add(edge)
                            counters["added"] += 1
                            cable_added_any = True

                            if len(batch) >= 500:
                                ok, added = output_layer.addFeatures(batch)
                                if not ok:
                                    raise RuntimeError(
                                        "写入 POLE EDGE 失败。"
                                    )
                                self._added_feature_ids.extend(
                                    int(feat.id())
                                    for feat in batch
                                    if feat.id() >= 0
                                )
                                batch.clear()
                                QtWidgets.QApplication.processEvents()
                                if self._cancel_requested:
                                    self._rollback_written(output_layer)
                                    return {"cancelled": True}

                    if not cable_added_any and len(cable_seen) >= 2:
                        # All possible edges already existed or were collapsed.
                        pass

            if batch:
                ok, _ = output_layer.addFeatures(batch)
                if not ok:
                    raise RuntimeError("写入 POLE EDGE 失败。")
                self._added_feature_ids.extend(
                    int(feat.id())
                    for feat in batch
                    if feat.id() >= 0
                )
                batch.clear()

            if started_editing:
                if not output_layer.commitChanges():
                    errors = []
                    try:
                        errors = list(output_layer.commitErrors())
                    except Exception:
                        pass
                    raise RuntimeError(
                        "POLE EDGE 保存失败：%s"
                        % ("；".join(str(e) for e in errors)
                           if errors else "未知错误")
                    )

            return counters

        except Exception:
            if batch:
                batch.clear()
            self._rollback_written(output_layer)
            raise

    def _rollback_written(self, output_layer):
        """Undo only this run's output additions."""
        if not output_layer:
            return
        if self._started_editing:
            try:
                output_layer.rollBack()
            except Exception:
                pass
            return

        ids = [fid for fid in self._added_feature_ids if fid >= 0]
        if not ids:
            return
        try:
            output_layer.deleteFeatures(ids)
        except Exception:
            pass


# Compatibility entry point if another module wants to call the tool.
def run_auto_pole_edge(
    iface,
    pole_layer_ids,
    cable_layer_ids,
    output_layer_id,
    tolerance_m=5.0,
):
    project = QgsProject.instance()
    pole_layer = project.mapLayer(pole_layer_ids[0]) if pole_layer_ids else None
    output_layer = project.mapLayer(output_layer_id)
    if pole_layer is None:
        raise RuntimeError("没有可用的杆路图层。")
    if output_layer is None:
        raise RuntimeError("没有可用的 POLE EDGE 输出图层。")
    dialog = AutoPoleEdgeDialog(iface, iface.mainWindow())
    dialog.pole_list.clear()
    dialog.cable_list.clear()
    dialog.output_combo.clear()
    dialog.distance_spin.setValue(float(tolerance_m))
    # Direct callers should normally use the dialog; the function remains as
    # a small compatibility hook for future automation.
    return dialog
