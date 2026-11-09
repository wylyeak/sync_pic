"""Read-only container startup gate. No database, ADB, scan or unlink calls."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'controller'))
from file_sync import ensure_storage
from target_guard import TargetDirectory, PROTECTED_SOURCE, ALLOWED_TARGET

ensure_storage(PROTECTED_SOURCE)
with TargetDirectory(PROTECTED_SOURCE, ALLOWED_TARGET):
    pass
print('Validated non-overlapping source/target on the same mounted filesystem')
