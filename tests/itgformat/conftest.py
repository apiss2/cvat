"""Load the independent modules without importing CVAT's registry package."""
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUNDLE = HERE.parent / "overlay/cvat/apps/dataset_manager/formats/itgformat"
INSTALLED = HERE.parents[1] / "cvat/apps/dataset_manager/formats/itgformat"
SOURCE = BUNDLE if BUNDLE.is_dir() else INSTALLED
package = types.ModuleType("itgformat_core")
package.__path__ = [str(SOURCE)]
sys.modules[package.__name__] = package
