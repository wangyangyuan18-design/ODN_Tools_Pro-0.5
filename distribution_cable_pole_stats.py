# -*- coding: utf-8 -*-
"""Distribution Cable pole snapping and pole statistics.

The source Distribution Cable layer is never edited.  A snapped temporary
memory layer is generated and used for pole passage / turn analysis.
"""
import math

from qgis.PyQt import QtWidgets, QtCore
from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMapLayerType,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant


TEMP_PREFIX = "DC_归杆_临时_"


def _point_from_feature(feat):
    geom = feat.geometry()
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


def _transform_point(point, src_crs, dst_crs):
    if src_crs == dst_crs:
        return QgsPointXY(point)
    transform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
    return QgsPointXY(transform.transform(point))


def _nearest_pole(point, pole_records, spatial_index, tolerance):
    """Return the nearest pole record inside tolerance."""
    candidate_ids = spatial_index.nearestNeighbor(point, 8)
    best = None
    best_distance = float("inf")
    for idx in candidate_ids:
        if idx < 0 or idx >= len(pole_records):
            continue
        rec = pole_records[idx]
        distance = point.distance(rec["point"])
        if distance <= tolerance and distance < best_distance:
            best = rec
            best_distance = distance
    return best, best_distance


def _snap_geometry(geom, pole_records, spatial_index, tolerance):
    """Snap every line vertex to its nearest pole inside tolerance."""
    parts = _line_parts(geom)
    if not parts:
        return None, 0, 0, 0

    output_parts = []
    snapped_vertices = 0
    unsnapped_vertices = 0
    collapsed_parts = 0

    for points in parts:
        output = []
        last_key = None
        part_collapsed = False

        for point in points:
            rec, _ = _nearest_pole(point, pole_records, spatial_index, tolerance)
            if rec is not None:
                target = QgsPointXY(rec["point"])
                key = rec["key"]
                snapped_vertices += 1
            else:
                target = QgsPointXY(point)
                key = None
                unsnapped_vertices += 1

            if output and target.distance(output[-1]) <= 1e-9:
                if key is not None:
                    last_key = key
                part_collapsed = True
                continue

            output.append(target)
            last_key = key

        if part_collapsed:
            collapsed_parts += 1
        if len(output) >= 2:
            output_parts.append(output)

    if not output_parts:
        return None, snapped_vertices, unsnapped_vertices, collapsed_parts

    if geom.isMultipart():
        out_geom = QgsGeometry.fromMultiPolylineXY(output_parts)
    else:
        out_geom = QgsGeometry.fromPolylineXY(output_parts[0])
    return out_geom, snapped_vertices, unsnapped_vertices, collapsed_parts


def _safe_line_locate(line, point):
    try:
        location = line.lineLocatePoint(QgsGeometry.fromPointXY(point))
        return location if location >= 0 else None
    except Exception:
        return None


def _point_at(line, distance):
    try:
        point = line.interpolate(distance).asPoint()
        return QgsPointXY(point)
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

    p1 = _point_at(line, start)
    p2 = _point_at(line, end)
    if p1 is None or p2 is None:
        return None

    dx = p2.x() - p1.x()
    dy = p2.y() - p1.y()
    if not forward:
        dx, dy = -dx, -dy

    length = math.hypot(dx, dy)
    return (dx / length, dy / length) if length > 0 else None


def _angle_at_line_position(line, position):
    """Angle between the two directions leaving the pole.

    180 degrees means straight; smaller values are increasingly sharp turns.
    """
    total = line.length()
    if total <= 0:
        return 180.0

    step = max(min(total * 0.02, 5.0), 0.5)
    back = _local_direction(line, position, False, step)
    forward = _local_direction(line, position, True, step)
    if back is None or forward is None:
        return 180.0

    dot = max(-1.0, min(1.0, back[0] * forward[0] + back[1] * forward[1]))
    return math.degrees(math.acos(dot))


def _inflate_rect(rect, tolerance):
    result = QgsRectangle(rect)
    result.setXMinimum(result.xMinimum() - tolerance)
    result.setXMaximum(result.xMaximum() + tolerance)
    result.setYMinimum(result.yMinimum() - tolerance)
    result.setYMaximum(result.yMaximum() + tolerance)
    return result


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
        self.resize(570, 470)

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
        row.addWidget(QtWidgets.QLabel("DC归杆距离（m）"))
        self.snap_distance = QtWidgets.QDoubleSpinBox()
        self.snap_distance.setRange(0.01, 1000.0)
        self.snap_distance.setDecimals(2)
        self.snap_distance.setValue(5.0)
        row.addWidget(self.snap_distance)
        row.addStretch()
        layout.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("DC拐角判定角度（≤此角度为拐角）"))
        self.turn_angle = QtWidgets.QDoubleSpinBox()
        self.turn_angle.setRange(1.0, 179.9)
        self.turn_angle.setDecimals(1)
        self.turn_angle.setValue(135.0)
        row.addWidget(self.turn_angle)
        row.addStretch()
        layout.addLayout(row)

        note = QtWidgets.QLabel(
            "运行后先复制DC到临时内存图层并归杆，原Distribution Cable图层不修改。\n"
            "DC No：每根杆经过的不同DC要素条数。\n"
            "DC TURN：在该杆形成拐角的不同DC要素条数；同一DC重复经过同一杆只计1次。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        layout.addWidget(note)

        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        button_box.accepted.connect(self._run)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

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
            if "distribution cable" in name or "distribution_cable" in name or name == "dc":
                self.dc_layer.addItem(layer.name(), layer.id())

        # Fallback: if no obvious DC layer was found, show all line layers.
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
        dc_layer = QgsProject.instance().mapLayer(self.dc_layer.currentData())

        if not pole_ids:
            QtWidgets.QMessageBox.warning(self, "DC归杆与统计", "请至少选择一个杆路图层。")
            return
        if dc_layer is None:
            QtWidgets.QMessageBox.warning(self, "DC归杆与统计", "请选择Distribution Cable图层。")
            return

        try:
            result = run_distribution_cable_stats(
                pole_ids,
                dc_layer,
                self.snap_distance.value(),
                self.turn_angle.value(),
                self.iface,
            )

            report = (
                f"DC缆通过的杆子数量（只算杆子数量不论一杆杆通过几条缆只算1）：{result['passing_poles']}\n"
                f"DC缆拐角（所有杆图层DC TURN字段之和）：{result['dc_turn']}\n"
                f"DC缆通过的杆子*DC缆数量（DC No字段之和）：{result['dc_no_sum']}"
            )
            QtWidgets.QMessageBox.information(self, "Distribution Cable汇总报告", report)
            self.accept()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "DC归杆与统计失败", str(exc))


def _collect_poles(pole_layer_ids, analysis_crs):
    project = QgsProject.instance()
    records = []

    for layer_id in pole_layer_ids:
        layer = project.mapLayer(layer_id)
        if layer is None:
            continue

        for feature in layer.getFeatures():
            point = _point_from_feature(feature)
            if point is None:
                continue
            try:
                point = _transform_point(point, layer.crs(), analysis_crs)
            except Exception as exc:
                raise RuntimeError(
                    f"杆路图层 {layer.name()} 坐标转换失败：{exc}"
                )

            records.append({
                "layer_id": layer_id,
                "fid": int(feature.id()),
                "key": (layer_id, int(feature.id())),
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


def _make_temp_layer(source_layer):
    project = QgsProject.instance()
    name = TEMP_PREFIX + source_layer.name()

    for old in list(project.mapLayersByName(name)):
        project.removeMapLayer(old.id())

    if QgsWkbTypes.isMultiType(source_layer.wkbType()):
        geometry_type = "MultiLineString"
    else:
        geometry_type = "LineString"

    result = QgsVectorLayer(geometry_type, name, "memory")
    result.setCrs(source_layer.crs())
    provider = result.dataProvider()
    provider.addAttributes(list(source_layer.fields()))
    provider.addAttributes([
        QgsField("SNAP_VERTS", QVariant.Int),
        QgsField("UNSNAPPED", QVariant.Int),
        QgsField("COLLAPSED", QVariant.Int),
    ])
    result.updateFields()
    project.addMapLayer(result)
    return result


def _snap_distribution_cable(source_layer, pole_records, pole_index, tolerance):
    output = _make_temp_layer(source_layer)
    provider = output.dataProvider()

    total = 0
    snapped_features = 0
    snapped_vertices = 0
    unsnapped_vertices = 0
    collapsed_features = 0

    for source_feature in source_layer.getFeatures():
        geom = source_feature.geometry()
        if geom is None or geom.isEmpty():
            continue

        total += 1
        new_geom, snap_count, unsnap_count, collapsed_parts = _snap_geometry(
            geom,
            pole_records,
            pole_index,
            tolerance,
        )
        if new_geom is None:
            continue

        feature = QgsFeature(output.fields())
        attributes = list(source_feature.attributes())
        attributes.extend([snap_count, unsnap_count, collapsed_parts])
        feature.setAttributes(attributes)
        feature.setGeometry(new_geom)
        if not provider.addFeature(feature):
            raise RuntimeError(
                f"无法写入临时Distribution Cable图层，FID={source_feature.id()}"
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
        "snapped_features": snapped_features,
        "snapped_vertices": snapped_vertices,
        "unsnapped_vertices": unsnapped_vertices,
        "collapsed_features": collapsed_features,
    }


def _analyse_dc_passage(temp_dc, pole_records, pole_index, pass_tolerance, turn_angle):
    """Return per-pole sets of DC FIDs passing and turning.

    A DC feature counts only once per pole, even if multipart or repeated.
    """
    passing = {}
    turning = {}

    for feature in temp_dc.getFeatures():
        geom = feature.geometry()
        if geom is None or geom.isEmpty():
            continue

        dc_fid = int(feature.id())
        seen_poles = set()

        for points in _line_parts(geom):
            line = QgsGeometry.fromPolylineXY(points)
            if line.isEmpty() or line.length() <= 0:
                continue

            rect = _inflate_rect(line.boundingBox(), pass_tolerance)
            candidate_ids = pole_index.intersects(rect)

            for idx in candidate_ids:
                if idx < 0 or idx >= len(pole_records):
                    continue
                record = pole_records[idx]
                key = record["key"]

                distance = line.distance(QgsGeometry.fromPointXY(record["point"]))
                if distance > pass_tolerance:
                    continue

                location = _safe_line_locate(line, record["point"])
                if location is None:
                    continue

                # Passage is counted once per DC feature and pole, but every
                # occurrence is still checked for a turn.  This matters when
                # a multipart/looped DC passes the same pole more than once.
                if key not in seen_poles:
                    seen_poles.add(key)
                    passing.setdefault(key, set()).add(dc_fid)

                angle = _angle_at_line_position(line, location)
                if angle <= turn_angle:
                    turning.setdefault(key, set()).add(dc_fid)

        # One DC feature may have multiple parts.  A pole is counted once
        # because sets are keyed by the DC feature id.
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
    errors = []

    for layer_id in pole_layer_ids:
        layer = project.mapLayer(layer_id)
        if layer is None:
            continue

        no_idx = layer.fields().indexOf("DC No")
        turn_idx = layer.fields().indexOf("DC TURN")
        own_edit = not layer.isEditable()

        try:
            if own_edit and not layer.startEditing():
                raise RuntimeError(f"无法编辑杆路图层：{layer.name()}")

            if no_idx < 0:
                if not layer.addAttribute(QgsField("DC No", QVariant.Int)):
                    raise RuntimeError(f"无法新增字段 DC No：{layer.name()}")
                layer.updateFields()
                no_idx = layer.fields().indexOf("DC No")

            if turn_idx < 0:
                if not layer.addAttribute(QgsField("DC TURN", QVariant.Int)):
                    raise RuntimeError(f"无法新增字段 DC TURN：{layer.name()}")
                layer.updateFields()
                turn_idx = layer.fields().indexOf("DC TURN")

            if no_idx < 0 or turn_idx < 0:
                raise RuntimeError(f"杆路图层 {layer.name()} 中无法找到 DC No / DC TURN 字段。")

            for feature in layer.getFeatures():
                key = (layer_id, int(feature.id()))
                dc_no = len(passing.get(key, set()))
                dc_turn = len(turning.get(key, set()))

                if dc_no > 0:
                    passing_poles += 1
                total_no += dc_no
                total_turn += dc_turn

                if feature[no_idx] != dc_no:
                    if not layer.changeAttributeValue(feature.id(), no_idx, dc_no):
                        raise RuntimeError(
                            f"无法写入 {layer.name()} 的 DC No，FID={feature.id()}"
                        )

                if feature[turn_idx] != dc_turn:
                    if not layer.changeAttributeValue(feature.id(), turn_idx, dc_turn):
                        raise RuntimeError(
                            f"无法写入 {layer.name()} 的 DC TURN，FID={feature.id()}"
                        )

            if own_edit and not layer.commitChanges():
                raise RuntimeError(f"提交杆路图层失败：{layer.name()}")
        except Exception as exc:
            if own_edit:
                layer.rollBack()
            errors.append(str(exc))

    if errors:
        raise RuntimeError("DC No / DC TURN字段写入失败：\n" + "\n".join(errors))

    return {
        "passing_poles": passing_poles,
        "dc_turn": total_turn,
        "dc_no_sum": total_no,
    }


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
        raise RuntimeError("DC拐角判定角度必须在0°到180°之间。")

    pole_records = _collect_poles(pole_layer_ids, dc_layer.crs())
    pole_index = _build_point_index(pole_records)

    temp_dc, snap_info = _snap_distribution_cable(
        dc_layer,
        pole_records,
        pole_index,
        snap_tolerance,
    )

    passing, turning = _analyse_dc_passage(
        temp_dc,
        pole_records,
        pole_index,
        snap_tolerance,
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
                f"Distribution Cable处理完成：临时DC {temp_dc.featureCount()}条；"
                f"通过杆 {summary['passing_poles']}根；"
                f"DC TURN总数 {summary['dc_turn']}；"
                f"DC No总和 {summary['dc_no_sum']}。"
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
        dialog.exec_()
