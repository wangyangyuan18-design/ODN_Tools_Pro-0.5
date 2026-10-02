# -*- coding: utf-8 -*-
"""FEEDER pole snapping and TYPE J / UPB placement tools.

The source FEEDER layer is never edited. The first tool creates an in-memory
snapped copy. The second tool analyses that temporary copy and places devices
on the selected pole/device layers.
"""
import math
from collections import defaultdict

from qgis.PyQt import QtWidgets, QtCore
from qgis.core import (
    QgsFeature, QgsGeometry, QgsMapLayerType, QgsPointXY, QgsProject,
    QgsSpatialIndex, QgsWkbTypes, QgsCoordinateTransform, QgsRectangle, QgsField,
)
from qgis.PyQt.QtCore import QVariant


TEMP_PREFIX = "FEEDER_归杆_临时_"


def _point_from_feature(feat):
    g = feat.geometry()
    if g is None or g.isEmpty():
        return None
    try:
        if QgsWkbTypes.geometryType(g.wkbType()) == QgsWkbTypes.PointGeometry:
            return QgsPointXY(g.asPoint())
        return QgsPointXY(g.centroid().asPoint())
    except Exception:
        return None


def _line_parts(g):
    if g is None or g.isEmpty():
        return []
    try:
        if g.isMultipart():
            return [list(p) for p in g.asMultiPolyline() if len(p) >= 2]
        p = list(g.asPolyline())
        return [p] if len(p) >= 2 else []
    except Exception:
        return []


def _same_crs_transform(src, dst):
    if src == dst:
        return None
    return QgsCoordinateTransform(src, dst, QgsProject.instance())


def _transform_geometry(g, tr):
    if tr is None:
        return QgsGeometry(g)
    x = QgsGeometry(g)
    try:
        x.transform(tr)
        return x
    except Exception:
        return QgsGeometry(g)


def _nearest_pole(point, pole_records, tolerance):
    best = None
    best_d = float("inf")
    for rec in pole_records:
        d = point.distance(rec["point"])
        if d <= tolerance and d < best_d:
            best = rec
            best_d = d
    return best, best_d


def _line_endpoints(g):
    parts = _line_parts(g)
    if not parts:
        return None, None
    return QgsPointXY(parts[0][0]), QgsPointXY(parts[-1][-1])


def _snap_all_vertices_geometry(g, pole_records, tolerance):
    """Snap every FEEDER vertex to its nearest pole and remove duplicate
    consecutive vertices which collapse onto the same pole.

    The original FEEDER geometry is never changed.  For multipart lines each
    part is processed independently.  A vertex without a pole inside the
    configured tolerance is retained and reported to the caller.
    """
    parts = _line_parts(g)
    if not parts:
        return None, 0, 0

    out_parts = []
    snapped_vertices = 0
    unsnapped_vertices = 0

    for pts in parts:
        out = []
        last_key = None
        for pt in pts:
            rec, _ = _nearest_pole(pt, pole_records, tolerance)
            if rec is not None:
                target = QgsPointXY(rec["point"])
                key = (rec["layer_id"], rec["fid"])
                snapped_vertices += 1
            else:
                target = QgsPointXY(pt)
                key = None
                unsnapped_vertices += 1

            # If two adjacent vertices resolve to the same pole, keep only
            # one vertex. This is important for both geometry cleanliness and
            # the "one pole = one passage point" rule.
            if out and target.distance(out[-1]) <= 1e-9:
                if key is not None:
                    last_key = key
                continue

            out.append(target)
            last_key = key

        if len(out) >= 2:
            out_parts.append(out)

    if not out_parts:
        return None, snapped_vertices, unsnapped_vertices

    if g.isMultipart():
        return QgsGeometry.fromMultiPolylineXY(out_parts), snapped_vertices, unsnapped_vertices
    return QgsGeometry.fromPolylineXY(out_parts[0]), snapped_vertices, unsnapped_vertices


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


class FeederSnapDialog(QtWidgets.QDialog, _PoleSelectorMixin):
    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("FEEDER归杆")
        self.resize(520, 430)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel("杆路图层（可多选）"))
        self.poles = QtWidgets.QListWidget()
        self.poles.setMinimumHeight(180)
        lay.addWidget(self.poles)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("FEEDER图层"))
        self.feeder = QtWidgets.QComboBox()
        row.addWidget(self.feeder, 1)
        lay.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("端点归杆距离（m）"))
        self.tol = QtWidgets.QDoubleSpinBox()
        self.tol.setRange(0.01, 1000.0)
        self.tol.setDecimals(2)
        self.tol.setValue(5.0)
        row.addWidget(self.tol)
        row.addStretch()
        lay.addLayout(row)

        note = QtWidgets.QLabel(
            "只复制FEEDER到临时内存图层，不修改原FEEDER。\n"
            "同一条线两个端点若归到同一根杆，只计一次；中间顶点和线形保持不变。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        lay.addWidget(note)

        btn = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btn.accepted.connect(self._run)
        btn.rejected.connect(self.reject)
        lay.addWidget(btn)

        self._populate_poles(self.poles)
        self._populate_feeders()

    def _populate_feeders(self):
        self.feeder.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if layer.type() != QgsMapLayerType.VectorLayer:
                continue
            if QgsWkbTypes.geometryType(layer.wkbType()) == QgsWkbTypes.LineGeometry:
                self.feeder.addItem(layer.name(), layer.id())

    def _selected_poles(self):
        return [
            self.poles.item(i).data(QtCore.Qt.UserRole)
            for i in range(self.poles.count())
            if self.poles.item(i).checkState() == QtCore.Qt.Checked
        ]

    def _run(self):
        ids = self._selected_poles()
        feeder = QgsProject.instance().mapLayer(self.feeder.currentData())
        if not ids:
            QtWidgets.QMessageBox.warning(self, "FEEDER归杆", "请至少选择一个杆路图层。")
            return
        if feeder is None:
            QtWidgets.QMessageBox.warning(self, "FEEDER归杆", "请选择FEEDER图层。")
            return
        try:
            result = run_feeder_snap(ids, feeder, self.tol.value(), self.iface)
            if result:
                self.accept()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "FEEDER归杆失败", str(exc))


def run_feeder_snap(pole_layer_ids, feeder_layer, tolerance, iface=None):
    project = QgsProject.instance()
    poles_by_crs = defaultdict(list)

    # Use FEEDER CRS as the analysis CRS so the configured distance is applied
    # consistently to the source FEEDER and all selected pole layers.
    for lid in pole_layer_ids:
        layer = project.mapLayer(lid)
        if layer is None:
            continue
        tr = _same_crs_transform(layer.crs(), feeder_layer.crs())
        for f in layer.getFeatures():
            p = _point_from_feature(f)
            if p is None:
                continue
            if tr is not None:
                p = QgsPointXY(tr.transform(p))
            poles_by_crs[feeder_layer.crs().authid()].append({
                "layer_id": lid, "fid": int(f.id()), "point": p,
            })

    pole_records = poles_by_crs[feeder_layer.crs().authid()]
    if not pole_records:
        raise RuntimeError("选定杆路图层没有可用的点要素。")

    name = TEMP_PREFIX + feeder_layer.name()
    for old in list(project.mapLayersByName(name)):
        project.removeMapLayer(old.id())

    out = QgsVectorLayer(
        "LineString" if not QgsWkbTypes.isMultiType(feeder_layer.wkbType())
        else "MultiLineString",
        name,
        "memory",
    )
    out.setCrs(feeder_layer.crs())
    pr = out.dataProvider()
    pr.addAttributes(list(feeder_layer.fields()))
    pr.addAttributes([
        QgsField("SNAP_START", QVariant.String),
        QgsField("SNAP_END", QVariant.String),
        QgsField("SNAP_VERTS", QVariant.Int),
        QgsField("UNSNAPPED", QVariant.Int),
    ])
    out.updateFields()

    total = snapped = collapsed = unsnapped_vertices = 0
    for src in feeder_layer.getFeatures():
        g = src.geometry()
        if g is None or g.isEmpty():
            continue
        total += 1

        ng, snap_count, unsnap_count = _snap_all_vertices_geometry(
            g, pole_records, tolerance
        )
        if ng is None:
            continue
        unsnapped_vertices += unsnap_count

        # Record the actual first/last pole after all-vertex snapping.
        new_parts = _line_parts(ng)
        start_rec = end_rec = None
        if new_parts:
            sa, sb = new_parts[0][0], new_parts[-1][-1]
            start_rec, _ = _nearest_pole(sa, pole_records, 1e-9)
            end_rec, _ = _nearest_pole(sb, pole_records, 1e-9)

        nf = QgsFeature(out.fields())
        attrs = list(src.attributes())
        attrs.extend([
            f"{start_rec['layer_id']}:{start_rec['fid']}" if start_rec else "",
            f"{end_rec['layer_id']}:{end_rec['fid']}" if end_rec else "",
            snap_count,
            unsnap_count,
        ])
        nf.setAttributes(attrs)
        nf.setGeometry(ng)
        pr.addFeature(nf)

        if snap_count:
            snapped += 1
        # Count collapsed geometry where multiple source vertices were reduced
        # to fewer output vertices.
        src_vertex_count = sum(len(p) for p in _line_parts(g))
        out_vertex_count = sum(len(p) for p in _line_parts(ng))
        if out_vertex_count < src_vertex_count:
            collapsed += 1

    out.updateExtents()
    project.addMapLayer(out)

    msg = (
        f"FEEDER归杆完成：原线 {total} 条，生成临时线 {out.featureCount()} 条；"
        f"发生顶点归杆 {snapped} 条；合并多余顶点 {collapsed} 条；"
        f"未找到 {unsnapped_vertices} 个顶点的5m内杆位。"
    )
    if iface:
        iface.messageBar().pushSuccess("ODN Tools Pro", msg, duration=8)
    return out



class FeederDeviceDialog(QtWidgets.QDialog, _PoleSelectorMixin):
    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("FEEDER杆上设备布置")
        self.resize(560, 470)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel("杆路图层（可多选）"))
        self.poles = QtWidgets.QListWidget()
        self.poles.setMinimumHeight(150)
        lay.addWidget(self.poles)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("归杆后的FEEDER临时图层"))
        self.feeder = QtWidgets.QComboBox()
        row.addWidget(self.feeder, 1)
        lay.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("TYPE J图层"))
        self.typej = QtWidgets.QComboBox()
        row.addWidget(self.typej, 1)
        lay.addWidget(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("UPB图层"))
        self.upb = QtWidgets.QComboBox()
        row.addWidget(self.upb, 1)
        lay.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("拐角判定角度（≤此角度为拐角）"))
        self.angle = QtWidgets.QDoubleSpinBox()
        self.angle.setRange(1.0, 179.9)
        self.angle.setDecimals(1)
        self.angle.setValue(135.0)
        row.addWidget(self.angle)
        row.addStretch()
        lay.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("杆/FEEDER经过判定距离（m）"))
        self.pass_tol = QtWidgets.QDoubleSpinBox()
        self.pass_tol.setRange(0.01, 1000.0)
        self.pass_tol.setDecimals(2)
        self.pass_tol.setValue(5.0)
        row.addWidget(self.pass_tol)
        row.addStretch()
        lay.addLayout(row)

        note = QtWidgets.QLabel(
            "直线杆：符合“每隔一根杆”规则时放TYPE J；没有TYPE J的经过杆放UPB。\n"
            "拐角杆（局部角度≤设定值）不放TYPE J，直接按UPB规则处理。\n"
            "设备均落在杆的实际坐标，不修改FEEDER。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        lay.addWidget(note)

        btn = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btn.accepted.connect(self._run)
        btn.rejected.connect(self.reject)
        lay.addWidget(btn)

        self._populate_poles(self.poles)
        self._populate_layers()

    def _populate_layers(self):
        self.feeder.clear(); self.typej.clear(); self.upb.clear()
        project = QgsProject.instance()
        for layer in project.mapLayers().values():
            if layer.type() != QgsMapLayerType.VectorLayer:
                continue
            gt = QgsWkbTypes.geometryType(layer.wkbType())
            if gt == QgsWkbTypes.LineGeometry and layer.name().startswith(TEMP_PREFIX):
                self.feeder.addItem(layer.name(), layer.id())
            elif gt == QgsWkbTypes.PointGeometry:
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
            QtWidgets.QMessageBox.warning(self, "设备布置", "请至少选择一个杆路图层。")
            return
        if feeder is None:
            QtWidgets.QMessageBox.warning(self, "设备布置", "请先生成FEEDER归杆临时图层。")
            return
        if typej is None or upb is None:
            QtWidgets.QMessageBox.warning(self, "设备布置", "请选择TYPE J和UPB点图层。")
            return
        try:
            run_feeder_devices(
                pole_ids, feeder, typej, upb,
                self.angle.value(), self.pass_tol.value(), self.iface
            )
            self.accept()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "设备布置失败", str(exc))


def _project_position_on_line(line, point):
    try:
        ok, dist, _, _ = line.closestSegmentWithContext(point)
        if ok < 0:
            return None
        # closestSegmentWithContext returns the squared distance as first
        # value in some QGIS versions; using line.lineLocatePoint is safer.
        loc = line.lineLocatePoint(QgsGeometry.fromPointXY(point))
        if loc < 0:
            return None
        return loc
    except Exception:
        return None


def _point_at(line, distance):
    try:
        return QgsPointXY(line.interpolate(distance).asPoint())
    except Exception:
        return None


def _local_direction(line, at_dist, forward, step):
    total = line.length()
    if total <= 0:
        return None
    if forward:
        a = max(0.0, at_dist)
        b = min(total, at_dist + step)
    else:
        a = max(0.0, at_dist - step)
        b = min(total, at_dist)
    pa, pb = _point_at(line, a), _point_at(line, b)
    if pa is None or pb is None:
        return None
    dx, dy = pb.x() - pa.x(), pb.y() - pa.y()
    if not forward:
        dx, dy = -dx, -dy
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 0 else None


def _angle_at_line_position(line, dist):
    total = line.length()
    if total <= 0:
        return 180.0
    step = max(min(total * 0.02, 5.0), 0.5)
    # Use directions pointing away from the pole on both sides.
    back = _local_direction(line, dist, False, step)
    forward = _local_direction(line, dist, True, step)
    if back is None or forward is None:
        return 180.0
    dot = max(-1.0, min(1.0, back[0] * forward[0] + back[1] * forward[1]))
    return math.degrees(math.acos(dot))


def _filter_new_points(layer, points, tolerance=0.5):
    existing = []
    for f in layer.getFeatures():
        p = _point_from_feature(f)
        if p is not None:
            existing.append(p)
    out = []
    for point in points:
        if any(point.distance(p) <= tolerance for p in existing):
            continue
        if any(point.distance(p) <= tolerance for p in out):
            continue
        out.append(QgsPointXY(point))
    return out


def _write_devices(layer, points):
    if not points:
        return 0
    own_edit = not layer.isEditable()
    if own_edit and not layer.startEditing():
        raise RuntimeError(f"无法编辑图层：{layer.name()}")
    added = 0
    try:
        for point in points:
            f = QgsFeature(layer.fields())
            f.setGeometry(QgsGeometry.fromPointXY(point))
            if not layer.addFeature(f):
                raise RuntimeError(f"无法向图层写入要素：{layer.name()}")
            added += 1
        if own_edit and not layer.commitChanges():
            layer.rollBack()
            raise RuntimeError(f"提交图层失败：{layer.name()}")
        return added
    except Exception:
        if own_edit:
            layer.rollBack()
        raise


def run_feeder_devices(
    pole_layer_ids, feeder_layer, typej_layer, upb_layer,
    corner_angle=135.0, pass_tolerance=5.0, iface=None
):
    project = QgsProject.instance()

    # Pole coordinates are transformed into the temporary FEEDER CRS.
    pole_records = []
    for lid in pole_layer_ids:
        layer = project.mapLayer(lid)
        if layer is None:
            continue
        tr = _same_crs_transform(layer.crs(), feeder_layer.crs())
        for f in layer.getFeatures():
            p = _point_from_feature(f)
            if p is None:
                continue
            if tr is not None:
                p = QgsPointXY(tr.transform(p))
            pole_records.append({
                "key": (lid, int(f.id())),
                "point": p,
            })
    if not pole_records:
        raise RuntimeError("没有可用的杆子点。")

    # For each feeder, find poles close to its geometry and sort by distance
    # along the feeder. A pole occurring twice is counted once.
    line_hits = []
    pole_to_hits = defaultdict(list)

    for ff in feeder_layer.getFeatures():
        geom = ff.geometry()
        if geom is None or geom.isEmpty():
            continue
        parts = _line_parts(geom)
        for part_index, pts in enumerate(parts):
            line = QgsGeometry.fromPolylineXY(pts)
            if line.isEmpty() or line.length() <= 0:
                continue
            local = []
            for rec in pole_records:
                loc = _project_position_on_line(line, rec["point"])
                if loc is None:
                    continue
                q = _point_at(line, loc)
                if q is None or q.distance(rec["point"]) > pass_tolerance:
                    continue
                local.append((loc, rec, q))
            local.sort(key=lambda x: x[0])

            unique = []
            seen_keys = set()
            for loc, rec, q in local:
                if rec["key"] in seen_keys:
                    continue
                seen_keys.add(rec["key"])
                unique.append((loc, rec, q))

            if unique:
                line_hits.append((int(ff.id()), part_index, line, unique))
                for item in unique:
                    pole_to_hits[item[1]["key"]].append((line, item[0], item[1]))

    # Determine each pole's corner status from each feeder passage. If any
    # passage is a corner, it is treated as a corner for device placement.
    pole_info = {}
    for key, hits in pole_to_hits.items():
        corner = False
        min_angle = 180.0
        for line, loc, rec in hits:
            angle = _angle_at_line_position(line, loc)
            min_angle = min(min_angle, angle)
            if angle <= corner_angle:
                corner = True
        pole_info[key] = {
            "point": hits[0][2]["point"],
            "corner": corner,
            "angle": min_angle,
            "hits": hits,
        }

    # Process FEEDERs in source order.  Each FEEDER is split at corner
    # poles.  Every segment starts a fresh TYPE J sequence:
    #
    #   straight segment: J, UPB, J, UPB, ...
    #   corner pole: UPB, then reset the sequence after the corner
    #
    # A global processed-pole set is used across FEEDERs.  Therefore, if a
    # second FEEDER follows poles already handled by the first FEEDER, those
    # poles are skipped and calculation resumes from the first unprocessed
    # pole.  This is the required "write by line" behavior.
    typej_keys = set()
    upb_keys = set()
    processed_keys = set()

    for _, _, line, hits in line_hits:
        # A hit may be encountered more than once when multipart geometry or
        # multiple selected pole layers contain the same physical pole.
        ordered = []
        seen = set()
        for loc, rec, _ in hits:
            key = rec["key"]
            if key in seen:
                continue
            seen.add(key)
            ordered.append((loc, rec))

        # Split into fresh sequences at every corner.  A corner itself is
        # always UPB and is also a reset point.
        segment = []
        for loc, rec in ordered:
            key = rec["key"]

            if key in processed_keys:
                # Already written by an earlier FEEDER: do not create another
                # TYPE J/UPB and do not let the overlap affect this FEEDER's
                # new-device alternation.
                continue

            info = pole_info[key]
            if info["corner"]:
                # Finish the straight segment before this corner.
                for idx, (_, srec) in enumerate(segment):
                    skey = srec["key"]
                    if skey in processed_keys:
                        continue
                    if idx % 2 == 0:
                        typej_keys.add(skey)
                    else:
                        upb_keys.add(skey)
                    processed_keys.add(skey)
                segment = []

                # Corner itself is UPB and resets the sequence.
                upb_keys.add(key)
                processed_keys.add(key)
            else:
                segment.append((loc, rec))

        # Flush the final straight segment.
        for idx, (_, rec) in enumerate(segment):
            key = rec["key"]
            if key in processed_keys:
                continue
            if idx % 2 == 0:
                typej_keys.add(key)
            else:
                upb_keys.add(key)
            processed_keys.add(key)

    # Safety: every passed pole not selected as TYPE J gets UPB.  This also
    # covers poles whose corner status was discovered from another FEEDER.
    upb_keys.update(set(pole_info.keys()) - typej_keys - upb_keys)


    if typej_layer.id() == upb_layer.id():
        raise RuntimeError("TYPE J图层和UPB图层不能选择同一个图层。")

    typej_points = _filter_new_points(
        typej_layer, [pole_info[k]["point"] for k in typej_keys]
    )
    upb_points = _filter_new_points(
        upb_layer, [pole_info[k]["point"] for k in upb_keys]
    )
    added_j = _write_devices(typej_layer, typej_points)
    added_u = _write_devices(upb_layer, upb_points)

    corners = sum(1 for x in pole_info.values() if x["corner"])
    msg = (
        f"FEEDER杆上设备完成：经过杆 {len(pole_info)} 根；"
        f"拐角杆 {corners} 根；新增 TYPE J {added_j} 个；新增 UPB {added_u} 个。"
    )
    if iface:
        iface.messageBar().pushSuccess("ODN Tools Pro", msg, duration=7)
    return {"poles": len(pole_info), "corners": corners,
            "typej_added": added_j, "upb_added": added_u}


class FeederPoleDeviceTools:
    def __init__(self, iface):
        self.iface = iface

    def feeder_snap(self):
        dlg = FeederSnapDialog(self.iface, self.iface.mainWindow())
        dlg.exec_()

    def feeder_devices(self):
        dlg = FeederDeviceDialog(self.iface, self.iface.mainWindow())
        dlg.exec_()
