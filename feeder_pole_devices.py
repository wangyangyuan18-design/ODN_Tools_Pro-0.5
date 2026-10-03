# -*- coding: utf-8 -*-
"""FEEDER pole snapping and TYPE J / UPB placement tools.

The source FEEDER layer is never edited. One Run performs both:
1) create a snapped temporary FEEDER layer;
2) analyse its pole sequence;
3) write TYPE J / UPB points;
4) update Pole.FEEDER.

Distance inputs are always real metres. Projected CRSs with non-metre units
are converted by their unit factor; geographic CRSs are analysed in a local
UTM CRS and results are transformed back to the source CRS.
"""
import math
from collections import defaultdict

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
    QgsSpatialIndex,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant


TEMP_PREFIX = "FEEDER_归杆_临时_"


def _pump_ui(counter, every=200):
    if counter and counter % every == 0:
        try:
            QtWidgets.QApplication.processEvents()
        except Exception:
            pass

def _notify_progress(progress_cb, percent, status=None):
    if progress_cb is None:
        return
    try:
        progress_cb(max(0.0, min(100.0, float(percent))), status)
    except Exception:
        pass


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
    """Choose a local UTM CRS from the layer extent."""
    source_crs = layer.crs()
    to_wgs84 = QgsCoordinateTransform(
        source_crs,
        QgsCoordinateReferenceSystem("EPSG:4326"),
        QgsProject.instance(),
    )
    extent = layer.extent()
    center = QgsPointXY((extent.xMinimum() + extent.xMaximum()) / 2.0,
                        (extent.yMinimum() + extent.yMaximum()) / 2.0)
    center_wgs84 = to_wgs84.transform(center)
    lon = max(-180.0, min(180.0, center_wgs84.x()))
    lat = max(-80.0, min(84.0, center_wgs84.y()))
    zone = int(math.floor((lon + 180.0) / 6.0) + 1)
    zone = max(1, min(60, zone))
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return QgsCoordinateReferenceSystem("EPSG:%d" % epsg)


def _analysis_context(source_layer):
    """Return (analysis_crs, source_to_analysis, analysis_to_source, tol_scale).

    tol_scale is the number of source/analysis CRS units per metre when the
    source CRS is projected. For geographic CRS the analysis CRS is metric
    and tol_scale is 1.
    """
    crs = source_layer.crs()

    if crs.isGeographic():
        analysis_crs = _utm_crs_for_layer(source_layer)
        source_to_analysis = QgsCoordinateTransform(
            crs, analysis_crs, QgsProject.instance()
        )
        analysis_to_source = QgsCoordinateTransform(
            analysis_crs, crs, QgsProject.instance()
        )
        return analysis_crs, source_to_analysis, analysis_to_source, 1.0

    try:
        factor = QgsUnitTypes.fromUnitToUnitFactor(
            crs.mapUnits(), QgsUnitTypes.DistanceMeters
        )
        if factor > 0:
            # factor = metres represented by one source CRS unit.
            return crs, None, None, 1.0 / factor
    except Exception:
        pass

    # Fallback for unusual projected units.
    analysis_crs = _utm_crs_for_layer(source_layer)
    source_to_analysis = QgsCoordinateTransform(
        crs, analysis_crs, QgsProject.instance()
    )
    analysis_to_source = QgsCoordinateTransform(
        analysis_crs, crs, QgsProject.instance()
    )
    return analysis_crs, source_to_analysis, analysis_to_source, 1.0


def _build_pole_records(pole_layer_ids, analysis_crs):
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


def _nearest_pole(point, pole_records, spatial_index, tolerance_units):
    candidates = spatial_index.nearestNeighbor(point, 8)
    best = None
    best_distance = float("inf")
    for idx in candidates:
        if idx < 0 or idx >= len(pole_records):
            continue
        record = pole_records[idx]
        distance = point.distance(record["point"])
        if distance <= tolerance_units and distance < best_distance:
            best = record
            best_distance = distance
    return best, best_distance


def _snap_geometry(geom, pole_records, spatial_index, tolerance_units):
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
                point, pole_records, spatial_index, tolerance_units
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
        result = QgsGeometry.fromMultiPolylineXY(output_parts)
    else:
        result = QgsGeometry.fromPolylineXY(output_parts[0])
    return result, snapped_vertices, unsnapped_vertices, collapsed_parts


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

    result = QgsVectorLayer(geometry_type, name, "memory")
    result.setCrs(source_layer.crs())
    provider = result.dataProvider()
    provider.addAttributes(list(source_layer.fields()))
    provider.addAttributes([
        QgsField("SNAP_START", QVariant.String),
        QgsField("SNAP_END", QVariant.String),
        QgsField("SNAP_VERTS", QVariant.Int),
        QgsField("UNSNAPPED", QVariant.Int),
        QgsField("COLLAPSED", QVariant.Int),
    ])
    result.updateFields()
    project.addMapLayer(result)
    return result


def _snap_feeder(
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

    project = QgsProject.instance()

    for source_index, source_feature in enumerate(source_layer.getFeatures(), start=1):
        _pump_ui(source_index, 100)
        source_geom = source_feature.geometry()
        if source_geom is None or source_geom.isEmpty():
            continue
        total += 1

        analysis_geom = _transform_geometry(source_geom, source_to_analysis)
        new_geom, snap_count, unsnap_count, collapsed_parts = _snap_geometry(
            analysis_geom,
            pole_records,
            pole_index,
            tolerance_units,
        )
        if new_geom is None:
            continue

        output_geom = _transform_geometry(new_geom, analysis_to_source)

        new_parts = _line_parts(new_geom)
        start_record = end_record = None
        if new_parts:
            start_record, _ = _nearest_pole(
                new_parts[0][0], pole_records, pole_index, 1e-9
            )
            end_record, _ = _nearest_pole(
                new_parts[-1][-1], pole_records, pole_index, 1e-9
            )

        feature = QgsFeature(output.fields())
        attributes = list(source_feature.attributes())
        attributes.extend([
            "%s:%s" % (start_record["layer_id"], start_record["fid"])
            if start_record else "",
            "%s:%s" % (end_record["layer_id"], end_record["fid"])
            if end_record else "",
            snap_count,
            unsnap_count,
            collapsed_parts,
        ])
        feature.setAttributes(attributes)
        feature.setGeometry(output_geom)

        if not provider.addFeature(feature):
            raise RuntimeError(
                "无法写入临时FEEDER图层，FID=%s" % source_feature.id()
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
        location = line.lineLocatePoint(QgsGeometry.fromPointXY(point))
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


def _analyse_feeder(feeder_layer, pole_records, pole_index, source_to_analysis, tolerance_units, corner_angle):
    line_hits = []
    pole_to_hits = defaultdict(list)
    pole_feeder_ids = defaultdict(set)

    for feeder_index, feeder_feature in enumerate(feeder_layer.getFeatures(), start=1):
        _pump_ui(feeder_index, 50)
        geom = feeder_feature.geometry()
        if geom is None or geom.isEmpty():
            continue

        geom = _transform_geometry(geom, source_to_analysis)
        parts = _line_parts(geom)
        for part_index, points in enumerate(parts):
            line = QgsGeometry.fromPolylineXY(points)
            if line.isEmpty() or line.length() <= 0:
                continue

            local = []
            query_rect = line.boundingBox()
            query_rect.setXMinimum(query_rect.xMinimum() - tolerance_units)
            query_rect.setXMaximum(query_rect.xMaximum() + tolerance_units)
            query_rect.setYMinimum(query_rect.yMinimum() - tolerance_units)
            query_rect.setYMaximum(query_rect.yMaximum() + tolerance_units)

            candidate_ids = pole_index.intersects(query_rect)
            for idx in candidate_ids:
                if idx < 0 or idx >= len(pole_records):
                    continue
                record = pole_records[idx]
                location = _safe_line_locate(line, record["point"])
                if location is None:
                    continue
                projected = _point_at(line, location)
                if projected is None:
                    continue
                if projected.distance(record["point"]) > tolerance_units:
                    continue
                local.append((location, record, projected))

            local.sort(key=lambda item: item[0])

            unique = []
            seen_keys = set()
            for location, record, projected in local:
                if record["key"] in seen_keys:
                    continue
                seen_keys.add(record["key"])
                unique.append((location, record, projected))

            if unique:
                line_hits.append(
                    (int(feeder_feature.id()), part_index, line, unique)
                )
                for location, record, projected in unique:
                    key = record["key"]
                    pole_to_hits[key].append((line, location, record))
                    pole_feeder_ids[key].add(int(feeder_feature.id()))

    pole_info = {}
    for key, hits in pole_to_hits.items():
        is_corner = False
        min_angle = 180.0
        for line, location, record in hits:
            angle = _angle_at_line_position(line, location)
            min_angle = min(min_angle, angle)
            if angle <= corner_angle:
                is_corner = True

        pole_info[key] = {
            "point": hits[0][2]["point"],
            "corner": is_corner,
            "angle": min_angle,
            "hits": hits,
        }

    return line_hits, pole_info, pole_feeder_ids


def _write_points_without_existing_check(layer, points_analysis_crs, points):
    if not points:
        return 0

    transform = None
    if layer.crs() != points_analysis_crs:
        transform = QgsCoordinateTransform(
            points_analysis_crs, layer.crs(), QgsProject.instance()
        )

    own_edit = not layer.isEditable()
    if own_edit and not layer.startEditing():
        raise RuntimeError("无法编辑图层：%s" % layer.name())

    added = 0
    try:
        for point_index, point in enumerate(points, start=1):
            _pump_ui(point_index, 50)
            target_point = _transform_point(point, transform)
            feature = QgsFeature(layer.fields())
            feature.setGeometry(QgsGeometry.fromPointXY(target_point))
            if not layer.addFeature(feature):
                raise RuntimeError("无法向图层写入要素：%s" % layer.name())
            added += 1

        if own_edit and not layer.commitChanges():
            layer.rollBack()
            raise RuntimeError("提交图层失败：%s" % layer.name())
        return added
    except Exception:
        if own_edit:
            layer.rollBack()
        raise


def _write_feeder_field(pole_layer_ids, pole_info, pole_feeder_ids):
    project = QgsProject.instance()
    field_updates = 0

    for layer_id in pole_layer_ids:
        layer = project.mapLayer(layer_id)
        if layer is None:
            continue

        field_idx = layer.fields().indexOf("FEEDER")
        own_edit = not layer.isEditable()
        try:
            if own_edit and not layer.startEditing():
                raise RuntimeError("无法编辑杆路图层：%s" % layer.name())

            if field_idx < 0:
                if not layer.addAttribute(
                    QgsField("FEEDER", QVariant.Int)
                ):
                    raise RuntimeError(
                        "无法新增FEEDER字段：%s" % layer.name()
                    )
                layer.updateFields()
                field_idx = layer.fields().indexOf("FEEDER")

            if field_idx < 0:
                raise RuntimeError(
                    "杆路图层 %s 中无法找到FEEDER字段。" % layer.name()
                )

            for feature_index, feature in enumerate(layer.getFeatures(), start=1):
                _pump_ui(feature_index, 250)
                key = (layer_id, int(feature.id()))
                value = len(pole_feeder_ids.get(key, set()))
                if feature[field_idx] != value:
                    if not layer.changeAttributeValue(
                        feature.id(), field_idx, value
                    ):
                        raise RuntimeError(
                            "无法写入%s的FEEDER字段，FID=%s"
                            % (layer.name(), feature.id())
                        )
                    field_updates += 1

            if own_edit and not layer.commitChanges():
                raise RuntimeError("提交杆路图层失败：%s" % layer.name())
        except Exception:
            if own_edit:
                layer.rollBack()
            raise

    return field_updates


def _build_device_keys(line_hits, pole_info, corner_angle):
    """Return TYPE J and UPB keys using per-FEEDER corner state."""
    typej_keys = set()
    upb_keys = set()
    processed_keys = set()

    for _, _, line, hits in line_hits:
        ordered = []
        seen = set()
        for location, record, _ in hits:
            key = record["key"]
            if key in seen:
                continue
            seen.add(key)
            ordered.append((location, record))

        segment = []
        for location, record in ordered:
            key = record["key"]
            if key in processed_keys:
                continue

            # Corner state belongs to this FEEDER passage, not globally
            # to the pole. A shared pole may be straight on one FEEDER and
            # a corner on another.
            is_corner = _angle_at_line_position(line, location) <= corner_angle
            if is_corner:
                for idx, (_, segment_record) in enumerate(segment):
                    segment_key = segment_record["key"]
                    if segment_key in processed_keys:
                        continue
                    if idx % 2 == 0:
                        typej_keys.add(segment_key)
                    else:
                        upb_keys.add(segment_key)
                    processed_keys.add(segment_key)

                segment = []
                upb_keys.add(key)
                processed_keys.add(key)
            else:
                segment.append((location, record))

        for idx, (_, record) in enumerate(segment):
            key = record["key"]
            if key in processed_keys:
                continue
            if idx % 2 == 0:
                typej_keys.add(key)
            else:
                upb_keys.add(key)
            processed_keys.add(key)

    # Any passed pole not selected as TYPE J must have UPB.
    upb_keys.update(set(pole_info.keys()) - typej_keys - upb_keys)
    return typej_keys, upb_keys


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


class FeederDeviceDialog(QtWidgets.QDialog, _PoleSelectorMixin):
    """Merged FEEDER snapping + TYPE J / UPB placement dialog."""

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("FEEDER归杆及杆上设备布置")
        self.resize(610, 520)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel("杆路图层（可多选）"))

        self.poles = QtWidgets.QListWidget()
        self.poles.setMinimumHeight(180)
        layout.addWidget(self.poles)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("FEEDER图层"))
        self.feeder = QtWidgets.QComboBox()
        row.addWidget(self.feeder, 1)
        layout.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("TYPE J图层"))
        self.typej = QtWidgets.QComboBox()
        row.addWidget(self.typej, 1)
        layout.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("UPB图层"))
        self.upb = QtWidgets.QComboBox()
        row.addWidget(self.upb, 1)
        layout.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("归杆距离（m）"))
        self.tolerance = QtWidgets.QDoubleSpinBox()
        self.tolerance.setRange(0.01, 1000.0)
        self.tolerance.setDecimals(2)
        self.tolerance.setValue(5.0)
        row.addWidget(self.tolerance)
        row.addStretch()
        layout.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("拐角判定角度（≤此角度为拐角）"))
        self.corner_angle = QtWidgets.QDoubleSpinBox()
        self.corner_angle.setRange(1.0, 179.9)
        self.corner_angle.setDecimals(1)
        self.corner_angle.setValue(135.0)
        row.addWidget(self.corner_angle)
        row.addStretch()
        layout.addLayout(row)

        note = QtWidgets.QLabel(
            "一次运行完成：FEEDER归杆 → TYPE J/UPB布置 → Pole.FEEDER更新。\n"
            "归杆距离始终按真实米数计算；135°为真实角度阈值。\n"
            "原FEEDER不修改；TYPE J/UPB写入时不检查目标图层原有数据。"
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
        self._populate_layers()

    def _populate_layers(self):
        self.feeder.clear()
        self.typej.clear()
        self.upb.clear()

        project = QgsProject.instance()
        for layer in project.mapLayers().values():
            if layer.type() != QgsMapLayerType.VectorLayer:
                continue

            geometry_type = QgsWkbTypes.geometryType(layer.wkbType())
            if geometry_type == QgsWkbTypes.LineGeometry:
                self.feeder.addItem(layer.name(), layer.id())
            elif geometry_type == QgsWkbTypes.PointGeometry:
                self.typej.addItem(layer.name(), layer.id())
                self.upb.addItem(layer.name(), layer.id())

    def _selected_poles(self):
        return [
            self.poles.item(i).data(QtCore.Qt.UserRole)
            for i in range(self.poles.count())
            if self.poles.item(i).checkState() == QtCore.Qt.Checked
        ]

    def _run(self):
        pole_ids = self._selected_poles()
        feeder = QgsProject.instance().mapLayer(self.feeder.currentData())
        typej = QgsProject.instance().mapLayer(self.typej.currentData())
        upb = QgsProject.instance().mapLayer(self.upb.currentData())

        if not pole_ids:
            QtWidgets.QMessageBox.warning(
                self, "FEEDER处理", "请至少选择一个杆路图层。"
            )
            return
        if feeder is None:
            QtWidgets.QMessageBox.warning(
                self, "FEEDER处理", "请选择FEEDER图层。"
            )
            return
        if typej is None or upb is None:
            QtWidgets.QMessageBox.warning(
                self, "FEEDER处理", "请选择TYPE J和UPB图层。"
            )
            return
        if typej.id() == upb.id():
            QtWidgets.QMessageBox.warning(
                self, "FEEDER处理", "TYPE J图层和UPB图层不能选择同一个图层。"
            )
            return

        try:
            self.run_btn.setEnabled(False)
            self.cancel_btn.setEnabled(False)
            result = run_feeder_devices(
                pole_ids,
                feeder,
                typej,
                upb,
                self.tolerance.value(),
                self.corner_angle.value(),
                self.iface,
            )
            if result:
                self.accept()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self, "FEEDER处理失败", str(exc)
            )
            self.run_btn.setEnabled(True)
            self.cancel_btn.setEnabled(True)


def run_feeder_snap(pole_layer_ids, feeder_layer, tolerance, iface=None):
    """Compatibility wrapper: only create the snapped temporary layer."""
    analysis_crs, source_to_analysis, analysis_to_source, scale = _analysis_context(
        feeder_layer
    )
    pole_records = _build_pole_records(pole_layer_ids, analysis_crs)
    pole_index = _build_point_index(pole_records)

    tolerance_units = (
        tolerance if source_to_analysis is not None else tolerance * scale
    )
    output, info = _snap_feeder(
        feeder_layer,
        pole_records,
        pole_index,
        source_to_analysis,
        analysis_to_source,
        tolerance_units,
    )

    if iface:
        iface.messageBar().pushSuccess(
            "ODN Tools Pro",
            "FEEDER归杆完成：原线 %s 条，临时线 %s 条，归杆顶点 %s 个。"
            % (info["total"], info["feature_count"], info["snapped_vertices"]),
            duration=8,
        )
    return output


def run_feeder_devices(
    pole_layer_ids,
    feeder_layer,
    typej_layer,
    upb_layer,
    pass_tolerance=5.0,
    corner_angle=135.0,
    iface=None,
):
    """Merged FEEDER snap + device placement + Pole.FEEDER update."""
    if pass_tolerance <= 0:
        raise RuntimeError("FEEDER归杆距离必须大于0。")
    if not 0 < corner_angle < 180:
        raise RuntimeError("FEEDER拐角判定角度必须在0°到180°之间。")
    if typej_layer.id() == upb_layer.id():
        raise RuntimeError("TYPE J图层和UPB图层不能选择同一个图层。")

    analysis_crs, source_to_analysis, analysis_to_source, scale = _analysis_context(
        feeder_layer
    )
    pole_records = _build_pole_records(pole_layer_ids, analysis_crs)
    pole_index = _build_point_index(pole_records)

    tolerance_units = (
        pass_tolerance if source_to_analysis is not None else pass_tolerance * scale
    )

    temp_layer, snap_info = _snap_feeder(
        feeder_layer,
        pole_records,
        pole_index,
        source_to_analysis,
        analysis_to_source,
        tolerance_units,
    )

    line_hits, pole_info, pole_feeder_ids = _analyse_feeder(
        temp_layer,
        pole_records,
        pole_index,
        source_to_analysis,
        tolerance_units,
        corner_angle,
    )

    typej_keys, upb_keys = _build_device_keys(
        line_hits, pole_info, corner_angle
    )

    typej_points = [pole_info[key]["point"] for key in typej_keys]
    upb_points = [pole_info[key]["point"] for key in upb_keys]

    # Intentionally do NOT inspect/filter existing TYPE J/UPB features.
    added_typej = _write_points_without_existing_check(
        typej_layer, analysis_crs, typej_points
    )
    added_upb = _write_points_without_existing_check(
        upb_layer, analysis_crs, upb_points
    )

    feeder_field_updates = _write_feeder_field(
        pole_layer_ids, pole_info, pole_feeder_ids
    )

    result = {
        "temp_layer_id": temp_layer.id(),
        "temp_layer_name": temp_layer.name(),
        "passed_poles": len(pole_info),
        "corner_poles": sum(
            1 for info in pole_info.values() if info["corner"]
        ),
        "typej_added": added_typej,
        "upb_added": added_upb,
        "feeder_field_updates": feeder_field_updates,
        "snap_info": snap_info,
    }

    if iface:
        iface.messageBar().pushSuccess(
            "ODN Tools Pro",
            (
                "FEEDER处理完成：临时FEEDER %s 条；经过杆 %s 根；"
                "拐角杆 %s 根；新增TYPE J %s 个；新增UPB %s 个；"
                "FEEDER字段更新 %s 根杆。"
            )
            % (
                snap_info["feature_count"],
                result["passed_poles"],
                result["corner_poles"],
                added_typej,
                added_upb,
                feeder_field_updates,
            ),
            duration=8,
        )

    return result


class FeederPoleDeviceTools:
    def __init__(self, iface):
        self.iface = iface

    def feeder_snap(self):
        return self.feeder_devices()

    def feeder_devices(self):
        dialog = FeederDeviceDialog(
            self.iface,
            self.iface.mainWindow(),
        )
        return dialog.exec_()


# Backward-compatible name for code that imported the old dialog.
FeederSnapDialog = FeederDeviceDialog
