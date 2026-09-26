# -*- coding: utf-8 -*-
"""QGIS plugin package entry point for ODN Tools Pro."""


def classFactory(iface):
    """Load the ODN Tools Pro plugin with the six supported modules."""
    from . import connection_point_engine
    from . import connection_point_engine_v4
    connection_point_engine.run_connection_point_naming_v2 = (
        connection_point_engine_v4.run_connection_point_naming_v2
    )

    from .site_co_design_impl import SiteCoDesign
    from .pole_trace_connect import PoleTraceDialog
    from .overlength_pole import OverlengthPoleDialog

    import os

    class ODNToolsPro(SiteCoDesign):
        """Main QGIS plugin controller exposing only the six supported tools."""

        def initGui(self):
            super().initGui()
            main_window = self.iface.mainWindow()
            icon_path = os.path.join(self.plugin_dir, 'poleTraceConnect.svg')

            self.add_action(
                icon_path,
                self.tr('杆路轨迹自动连线'),
                self.pole_trace_connect,
                parent=main_window,
            )
            self.add_action(
                icon_path,
                self.tr('超距增点'),
                self.overlength_pole,
                parent=main_window,
            )

        def pole_trace_connect(self):
            PoleTraceDialog(self.iface, self.iface.mainWindow()).exec_()

        def overlength_pole(self):
            OverlengthPoleDialog(self.iface, self.iface.mainWindow()).exec_()

    return ODNToolsPro(iface)
