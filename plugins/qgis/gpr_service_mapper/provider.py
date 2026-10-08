import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .algorithm import InterpretGprSurvey


class GprProvider(QgsProcessingProvider):
    def loadAlgorithms(self):  # noqa: N802
        self.addAlgorithm(InterpretGprSurvey())

    def id(self):
        return "gprmapper"

    def name(self):
        return "GPR Service Mapper"

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))

    def longName(self):  # noqa: N802
        return self.name()
