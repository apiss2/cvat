"""Load the independent modules without importing CVAT's registry package."""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "cvat/apps/dataset_manager/formats/itgformat"
package = types.ModuleType("itgformat_core")
package.__path__ = [str(SOURCE)]
sys.modules[package.__name__] = package
