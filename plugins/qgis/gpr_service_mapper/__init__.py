def classFactory(iface):  # noqa: N802 (QGIS API name)
    from .plugin import GprServiceMapperPlugin
    return GprServiceMapperPlugin(iface)
