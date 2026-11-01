"""Where Claude Duo's files live, on macOS and Windows.

HERE: the program's own files (pages, styles). Inside the Windows .exe that is PyInstaller's unpack folder.
DATA: your settings and saved state. Next to the code when run from a folder (as on the Mac), and in
%APPDATA%\\Claude Duo for the Windows .exe, whose own folder is temporary."""
import os, sys

WIN = os.name == "nt"
MAC = sys.platform == "darwin"
FROZEN = bool(getattr(sys, "frozen", False))
HERE = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("CLAUDE_DUO_DATA") or (
    os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Claude Duo") if FROZEN else HERE)
os.makedirs(DATA, exist_ok=True)
