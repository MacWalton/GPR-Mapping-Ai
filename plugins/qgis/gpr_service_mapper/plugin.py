"""QGIS plug-in entry point: registers the Processing provider and adds a toolbar button."""
import os

from qgis.core import QgsApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .provider import GprProvider

ALG_ID = "gprmapper:interpretsurvey"


class GprServiceMapperPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.action = None

    def initProcessing(self):  # noqa: N802
        self.provider = GprProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):  # noqa: N802
        self.initProcessing()
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))
        self.action = QAction(icon, "Interpret GPR survey…", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&GPR Service Mapper", self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginMenu("&GPR Service Mapper", self.action)
            self.iface.removeToolBarIcon(self.action)
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)

    def run(self):
        import processing
        processing.execAlgorithmDialog(ALG_ID, {})
