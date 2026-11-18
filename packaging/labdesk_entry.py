"""PyInstaller entry point for LabDesk.exe (same as ``python -m labdesk_app``)."""
import sys

from labdesk_app.main import main

if __name__ == "__main__":
    sys.exit(main())
