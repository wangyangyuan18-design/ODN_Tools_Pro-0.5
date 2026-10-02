# -*- coding: utf-8 -*-
"""Distribution Cable pole snapping and Pole statistics.

The source Distribution Cable layer is never edited. One Run creates a
temporary snapped copy, analyses pole passage/turns, writes DC No and
DC TURN to the selected pole layers, then shows the summary report.

All distance inputs are real metres. Projected CRSs use their unit conversion
factor; geographic CRSs are analysed in a local UTM CRS.
"""
import math

from qgis.PyQt import QtWidgets, QtCore
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMapLayerType,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant


TEMP_PREFIX = "DC_归杆_临时_"


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
            return QgsPointXY(geom.asPoint())
        return QgsPointXY(geom.centroid().asPoint())
    except Exception:
        return None


def _line_parts(geom):
    if geom is None or geom.isEmpty():
        return []
    try:
        if geom.isMultipart():
            return [list(part) for part in geom.asMultiPolyline() if len(part) >= 2]
        points = list(geom.asPolyline())
        return [points] if len(points) >= 2 else []
    except Exception:
        return []


def _transform_point(point, transform):
    if transform is None:
        return QgsPointXY(point)
    return QgsPointXY(transform.transform(point))


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
    crs = source_layer.crs()

    if crs.isGeographic():
        analysis_crs = _utm_crs_for_layer(source_layer)
        return (
            analysis_crs,
            QgsCoordinateTransform(crs, analysis_crs, QgsProject.instance()),
            QgsCoordinateTransform(analysis_crs, crs, QgsProject.instance()),
            1.0,
        )

    try:
        factor = QgsUnitTypes.fromUnitToUnitFactor(
            crs.mapUnits(), QgsUnitTypes.DistanceMeters
        )
        if factor > 0:
            # One source CRS unit represents factor metres.
            return crs, None, None, 1.0 / factor
    except Exception:
        pass

    analysis_crs = _utm_crs_for_layer(source_layer)
    return (
        analysis_crs,
        QgsCoordinateTransform(crs, analysis_crs, QgsProject.instance()),
        QgsCoordinateTransform(analysis_crs, crs, QgsProject.instance()),
        1.0,
    )


def _collect_poles(pole_layer_ids, analysis_crs):
    project = QgsProject.instance()
    records = []

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
            point = _point_from_feature(feature)
            if point is None:
                continue
            try:
                point = _transform_point(point, transform)
            except Exception as exc:
                raise RuntimeError(
                    "杆路图层 %s 坐标转换失败：%s" % (layer.name(), exc)
                )

            records.append({
                "key": (layer_id, int(feature.id())),
                "layer_id": layer_id,
                "fid": int(feature.id()),
                "point": point,
            })

    if not records:
        raise RuntimeError("选定杆路图层没有可用的杆点。")
    return records


def _build_point_index(records):
    index = QgsSpatialIndex()
    for idx, record in enumerate(records):
        feature = QgsFeature()
        feature.setId(idx)
        feature.setGeometry(QgsGeometry.fromPointXY(record["point"]))
        index.addFeature(feature)
    return index


def _nearest_pole(point, pole_records, pole_index, tolerance_units):
    candidate_ids = pole_index.nearestNeighbor(point, 8)
    best = None
    best_distance = float("inf")

    for idx in candidate_ids:
        if idx < 0 or idx >= len(pole_records):
            continue
        record = pole_records[idx]
        distance = point.distance(record["point"])
        if distance <= tolerance_units and distance < best_distance:
            best = record
            best_distance = distance

    return best, best_distance


def _snap_geometry(geom, pole_records, pole_index, tolerance_units):
    parts = _line_parts(geom)
    if not parts:
        return None, 0, 0, 0

    output_parts = []
    snapped_vertices = 0
    unsnapped_vertices = 0
    collapsed_parts = 0

    for points in parts:
        output = []
        part_collapsed = False

        for point in points:
            record, _ = _nearest_pole(
                point, pole_records, pole_index, tolerance_units
            )
            if record is not None:
                target = QgsPointXY(record["point"])
                snapped_vertices += 1
            else:
                target = QgsPointXY(point)
                unsnapped_vertices += 1

            if output and target.distance(output[-1]) <= 1e-9:
                part_collapsed = True
                continue
            output.append(target)
            _pump_ui(snapped_vertices + unsnapped_vertices, 2000)

        if part_collapsed:
            collapsed_parts += 1
        if len(output) >= 2:
            output_parts.append(output)

    if not output_parts:
        return None, snapped_vertices, unsnapped_vertices, collapsed_parts

    if geom.isMultipart():
        output_geom = QgsGeometry.fromMultiPolylineXY(output_parts)
    else:
        output_geom = QgsGeometry.fromPolylineXY(output_parts[0])

    return output_geom, snapped_vertices, unsnapped_vertices, collapsed_parts


def _make_temp_layer(source_layer):
    project = QgsProject.instance()
    name = TEMP_PREFIX + source_layer.name()

    for old in list(project.mapLayersByName(name)):
        project.removeMapLayer(old.id())

    geometry_type = (
        "MultiLineString"
        if QgsWkbTypes.isMultiType(source_layer.wkbType())
        else "LineString"
    )
    output = QgsVectorLayer(geometry_type, name, "memory")
    output.setCrs(source_layer.crs())

    provider = output.dataProvider()
    provider.addAttributes(list(source_layer.fields()))
    provider.addAttributes([
        QgsField("SNAP_VERTS", QVariant.Int),
        QgsField("UNSNAPPED", QVariant.Int),
        QgsField("COLLAPSED", QVariant.Int),
    ])
    output.updateFields()
    project.addMapLayer(output)
    return output


def _snap_distribution_cable(
    source_layer,
    pole_records,
    pole_index,
    source_to_analysis,
    analysis_to_source,
    tolerance_units,
):
    output = _make_temp_layer(source_layer)
    provider = output.dataProvider()

    total = 0
    snapped_features = 0
    snapped_vertices = 0
    unsnapped_vertices = 0
    collapsed_features = 0

    for source_index, source_feature in enumerate(source_layer.getFeatures(), start=1):
        _pump_ui(source_index, 100)
        source_geom = source_feature.geometry()
        if source_geom is None or source_geom.isEmpty():
            continue
        total += 1

        analysis_geom = _transform_geometry(
            source_geom, source_to_analysis
        )
        new_analysis_geom, snap_count, unsnap_count, collapsed_parts = (
            _snap_geometry(
                analysis_geom,
                pole_records,
                pole_index,
                tolerance_units,
            )
        )
        if new_analysis_geom is None:
            continue

        output_geom = _transform_geometry(
            new_analysis_geom, analysis_to_source
        )

        feature = QgsFeature(output.fields())
        attributes = list(source_feature.attributes())
        attributes.extend([
            snap_count,
            unsnap_count,
            collapsed_parts,
        ])
        feature.setAttributes(attributes)
        feature.setGeometry(output_geom)

        if not provider.addFeature(feature):
            raise RuntimeError(
                "无法写入临时Distribution Cable图层，FID=%s"
                % source_feature.id()
            )

        if snap_count:
            snapped_features += 1
        snapped_vertices += snap_count
        unsnapped_vertices += unsnap_count
        if collapsed_parts:
            collapsed_features += 1

    output.updateExtents()
    return output, {
        "total": total,
        "feature_count": output.featureCount(),
        "snapped_features": snapped_features,
        "snapped_vertices": snapped_vertices,
        "unsnapped_vertices": unsnapped_vertices,
        "collapsed_features": collapsed_features,
    }


def _safe_line_locate(line, point):
    try:
        location = line.lineLocatePoint(
            QgsGeometry.fromPointXY(point)
        )
        return location if location >= 0 else None
    except Exception:
        return None


def _point_at(line, distance):
    try:
        return QgsPointXY(line.interpolate(distance).asPoint())
    except Exception:
        return None


def _local_direction(line, position, forward, step):
    total = line.length()
    if total <= 0:
        return None

    if forward:
        start = max(0.0, position)
        end = min(total, position + step)
    else:
        start = max(0.0, position - step)
        end = min(total, position)

    first = _point_at(line, start)
    second = _point_at(line, end)
    if first is None or second is None:
        return None

    dx = second.x() - first.x()
    dy = second.y() - first.y()
    if not forward:
        dx, dy = -dx, -dy

    length = math.hypot(dx, dy)
    if length <= 0:
        return None
    return dx / length, dy / length


def _angle_at_line_position(line, position):
    total = line.length()
    if total <= 0:
        return 180.0

    step = max(min(total * 0.02, 5.0), 0.5)
    back = _local_direction(line, position, False, step)
    forward = _local_direction(line, position, True, step)

    if back is None or forward is None:
        return 180.0

    dot = max(
        -1.0,
        min(1.0, back[0] * forward[0] + back[1] * forward[1]),
    )
    return math.degrees(math.acos(dot))


def _analyse_dc(
    temp_dc,
    pole_records,
    pole_index,
    source_to_analysis,
    pass_tolerance_units,
    turn_angle,
):
    passing = {}
    turning = {}

    for feature_index, feature in enumerate(temp_dc.getFeatures(), start=1):
        _pump_ui(feature_index, 50)
        source_geom = feature.geometry()
        if source_geom is None or source_geom.isEmpty():
            continue

        geom = _transform_geometry(
            source_geom, source_to_analysis
        )
        dc_fid = int(feature.id())

        for points in _line_parts(geom):
            line = QgsGeometry.fromPolylineXY(points)
            if line.isEmpty() or line.length() <= 0:
                continue

            rect = QgsRectangle(line.boundingBox())
            rect.setXMinimum(rect.xMinimum() - pass_tolerance_units)
            rect.setXMaximum(rect.xMaximum() + pass_tolerance_units)
            rect.setYMinimum(rect.yMinimum() - pass_tolerance_units)
            rect.setYMaximum(rect.yMaximum() + pass_tolerance_units)

            for idx in pole_index.intersects(rect):
                if idx < 0 or idx >= len(pole_records):
                    continue

                record = pole_records[idx]
                distance = line.distance(
                    QgsGeometry.fromPointXY(record["point"])
                )
                if distance > pass_tolerance_units:
                    continue

                location = _safe_line_locate(
                    line, record["point"]
                )
                if location is None:
                    continue

                key = record["key"]
                passing.setdefault(key, set()).add(dc_fid)

                angle = _angle_at_line_position(line, location)
                if angle <= turn_angle:
                    turning.setdefault(key, set()).add(dc_fid)

    return passing, turning


def _ensure_and_write_fields(
    pole_layer_ids,
    passing,
    turning,
):
    project = QgsProject.instance()
    total_turn = 0
    total_no = 0
    passing_poles = 0

    for layer_id in pole_layer_ids:
        layer = project.mapLayer(layer_id)
        if layer is None:
            continue

        no_idx = layer.fields().indexOf("DC No")
        turn_idx = layer.fields().indexOf("DC TURN")
        own_edit = not layer.isEditable()

        try:
            if own_edit and not layer.startEditing():
                raise RuntimeError(
                    "无法编辑杆路图层：%s" % layer.name()
                )

            if no_idx < 0:
                if not layer.addAttribute(
                    QgsField("DC No", QVariant.Int)
                ):
                    raise RuntimeError(
                        "无法新增字段 DC No：%s" % layer.name()
                    )
                layer.updateFields()
                no_idx = layer.fields().indexOf("DC No")

            if turn_idx < 0:
                if not layer.addAttribute(
                    QgsField("DC TURN", QVariant.Int)
                ):
                    raise RuntimeError(
                        "无法新增字段 DC TURN：%s" % layer.name()
                    )
                layer.updateFields()
                turn_idx = layer.fields().indexOf("DC TURN")

            if no_idx < 0 or turn_idx < 0:
                raise RuntimeError(
                    "杆路图层 %s 中无法找到 DC No / DC TURN 字段。"
                    % layer.name()
                )

            for feature_index, feature in enumerate(layer.getFeatures(), start=1):
                _pump_ui(feature_index, 250)
                key = (layer_id, int(feature.id()))
                dc_no = len(passing.get(key, set()))
                dc_turn = len(turning.get(key, set()))

                if dc_no > 0:
                    passing_poles += 1
                total_no += dc_no
                total_turn += dc_turn

                if feature[no_idx] != dc_no:
                    if not layer.changeAttributeValue(
                        feature.id(), no_idx, dc_no
                    ):
                        raise RuntimeError(
                            "无法写入%s的DC No，FID=%s"
                            % (layer.name(), feature.id())
                        )

                if feature[turn_idx] != dc_turn:
                    if not layer.changeAttributeValue(
                        feature.id(), turn_idx, dc_turn
                    ):
                        raise RuntimeError(
                            "无法写入%s的DC TURN，FID=%s"
                            % (layer.name(), feature.id())
                        )

            if own_edit and not layer.commitChanges():
                raise RuntimeError(
                    "提交杆路图层失败：%s" % layer.name()
                )
        except Exception:
            if own_edit:
                layer.rollBack()
            raise

    return {
        "passing_poles": passing_poles,
        "dc_turn": total_turn,
        "dc_no_sum": total_no,
    }


class _PoleSelectorMixin:
    def _populate_poles(self, widget):
        widget.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if layer.type() != QgsMapLayerType.VectorLayer:
                continue
            if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PointGeometry:
                continue

            item = QtWidgets.QListWidgetItem(layer.name())
            item.setData(QtCore.Qt.UserRole, layer.id())
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Unchecked)
            widget.addItem(item)


class DistributionCablePoleDialog(QtWidgets.QDialog, _PoleSelectorMixin):
    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("Distribution Cable归杆与统计")
        self.resize(590, 480)

        layout = QtWidgets.QVBoxLayout(self)

        layout.addWidget(QtWidgets.QLabel("杆路图层（可多选）"))
        self.poles = QtWidgets.QListWidget()
        self.poles.setMinimumHeight(180)
        layout.addWidget(self.poles)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Distribution Cable图层"))
        self.dc_layer = QtWidgets.QComboBox()
        row.addWidget(self.dc_layer, 1)
        layout.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("归杆距离（m）"))
        self.snap_distance = QtWidgets.QDoubleSpinBox()
        self.snap_distance.setRange(0.01, 1000.0)
        self.snap_distance.setDecimals(2)
        self.snap_distance.setValue(5.0)
        row.addWidget(self.snap_distance)
        row.addStretch()
        layout.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("拐角判定角度（≤此角度为拐角）"))
        self.turn_angle = QtWidgets.QDoubleSpinBox()
        self.turn_angle.setRange(1.0, 179.9)
        self.turn_angle.setDecimals(1)
        self.turn_angle.setValue(135.0)
        row.addWidget(self.turn_angle)
        row.addStretch()
        layout.addLayout(row)

        note = QtWidgets.QLabel(
            "一次运行完成：Distribution Cable归杆 → Pole字段统计 → 汇总报告。\\n"
            "归杆距离始终按真实米数计算；135°为真实角度。\\n"
            "原Distribution Cable图层不修改；生成DC_归杆_临时_xxx临时图层。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        layout.addWidget(note)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        self.run_btn = buttons.button(QtWidgets.QDialogButtonBox.Ok)
        self.cancel_btn = buttons.button(QtWidgets.QDialogButtonBox.Cancel)
        self.run_btn.setText("运行")
        buttons.accepted.connect(self._run)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._populate_poles(self.poles)
        self._populate_dc_layers()

    def _populate_dc_layers(self):
        self.dc_layer.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if layer.type() != QgsMapLayerType.VectorLayer:
                continue
            if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.LineGeometry:
                continue

            name = layer.name().lower()
            if (
                "distribution cable" in name
                or "distribution_cable" in name
                or name == "dc"
            ):
                self.dc_layer.addItem(layer.name(), layer.id())

        if self.dc_layer.count() == 0:
            for layer in QgsProject.instance().mapLayers().values():
                if layer.type() != QgsMapLayerType.VectorLayer:
                    continue
                if QgsWkbTypes.geometryType(layer.wkbType()) == QgsWkbTypes.LineGeometry:
                    self.dc_layer.addItem(layer.name(), layer.id())

    def _selected_poles(self):
        return [
            self.poles.item(i).data(QtCore.Qt.UserRole)
            for i in range(self.poles.count())
            if self.poles.item(i).checkState() == QtCore.Qt.Checked
        ]

    def _run(self):
        pole_ids = self._selected_poles()
        dc_layer = QgsProject.instance().mapLayer(
            self.dc_layer.currentData()
        )

        if not pole_ids:
            QtWidgets.QMessageBox.warning(
                self,
                "DC处理",
                "请至少选择一个杆路图层。",
            )
            return
        if dc_layer is None:
            QtWidgets.QMessageBox.warning(
                self,
                "DC处理",
                "请选择Distribution Cable图层。",
            )
            return

        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        try:
            result = run_distribution_cable_stats(
                pole_ids,
                dc_layer,
                self.snap_distance.value(),
                self.turn_angle.value(),
                self.iface,
            )
            report = (
                "DC缆通过的杆子数量（只算杆子数量不论一杆杆通过几条缆只算1）：%s\\n"
                "DC缆拐角（所有杆图层DC TURN字段之和）：%s\\n"
                "DC缆通过的杆子*DC缆数量（DC No字段之和）：%s"
                % (
                    result["passing_poles"],
                    result["dc_turn"],
                    result["dc_no_sum"],
                )
            )
            QtWidgets.QMessageBox.information(
                self,
                "Distribution Cable汇总报告",
                report,
            )
            self.accept()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self,
                "DC处理失败",
                str(exc),
            )
            self.run_btn.setEnabled(True)
            self.cancel_btn.setEnabled(True)


def run_distribution_cable_stats(
    pole_layer_ids,
    dc_layer,
    snap_tolerance=5.0,
    turn_angle=135.0,
    iface=None,
):
    if snap_tolerance <= 0:
        raise RuntimeError("DC归杆距离必须大于0。")
    if not 0 < turn_angle < 180:
        raise RuntimeError(
            "DC拐角判定角度必须在0°到180°之间。"
        )

    analysis_crs, source_to_analysis, analysis_to_source, scale = (
        _analysis_context(dc_layer)
    )
    pole_records = _collect_poles(
        pole_layer_ids, analysis_crs
    )
    pole_index = _build_point_index(pole_records)

    tolerance_units = (
        snap_tolerance
        if source_to_analysis is not None
        else snap_tolerance * scale
    )

    temp_dc, snap_info = _snap_distribution_cable(
        dc_layer,
        pole_records,
        pole_index,
        source_to_analysis,
        analysis_to_source,
        tolerance_units,
    )

    passing, turning = _analyse_dc(
        temp_dc,
        pole_records,
        pole_index,
        source_to_analysis,
        tolerance_units,
        turn_angle,
    )

    summary = _ensure_and_write_fields(
        pole_layer_ids,
        passing,
        turning,
    )

    if iface:
        iface.messageBar().pushSuccess(
            "ODN Tools Pro",
            (
                "Distribution Cable处理完成：临时DC %s条；"
                "通过杆 %s根；DC TURN总数 %s；DC No总和 %s。"
            )
            % (
                snap_info["feature_count"],
                summary["passing_poles"],
                summary["dc_turn"],
                summary["dc_no_sum"],
            ),
            duration=8,
        )

    return {
        **summary,
        "temp_layer_id": temp_dc.id(),
        "temp_layer_name": temp_dc.name(),
        "snap_info": snap_info,
    }


class DistributionCableTools:
    def __init__(self, iface):
        self.iface = iface

    def run(self):
        dialog = DistributionCablePoleDialog(
            self.iface,
            self.iface.mainWindow(),
        )
        return dialog.exec_()
