import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# L1: the test run never reads or writes the user's ~/.config/LabDesk
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="labdesk-tests-")
