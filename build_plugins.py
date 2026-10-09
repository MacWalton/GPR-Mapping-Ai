"""Bundle the gpr_agent core into the QGIS plug-in and the ArcGIS Pro toolbox and zip them.

python build_plugins.py   ->  dist/gpr_service_mapper_qgis.zip, dist/GPR_Service_Mapper_ArcGIS.zip
"""
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
CORE = ROOT / "gpr_agent"
DIST = ROOT / "dist"
SKIP = {"__pycache__", "synthetic.py"}  # the forward model is a test tool, not needed in the GIS


def vendor(dest: Path):
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(CORE, dest, ignore=lambda d, names: [n for n in names if n in SKIP or n.endswith(".pyc")])


def zipdir(src: Path, zip_path: Path, arc_root: str):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if "__pycache__" in p.parts or p.suffix == ".pyc":
                continue
            z.write(p, Path(arc_root) / p.relative_to(src))


def main():
    DIST.mkdir(exist_ok=True)
    q = ROOT / "plugins" / "qgis" / "gpr_service_mapper"
    vendor(q / "gpr_agent")
    zipdir(q, DIST / "gpr_service_mapper_qgis.zip", "gpr_service_mapper")

    a = ROOT / "plugins" / "arcgis" / "GPR_Service_Mapper"   # ongoing testing - use at own risk
    vendor(a / "gpr_agent")
    zipdir(a, DIST / "GPR_Service_Mapper_ArcGIS.zip", "GPR_Service_Mapper")

    for z in DIST.glob("*.zip"):
        print(z, z.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
