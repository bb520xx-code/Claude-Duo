"""Where Claude Duo's files live, on macOS and Windows.

HERE: the program's own files (pages, styles). Inside the Windows .exe that is PyInstaller's unpack folder.
DATA: your settings and saved state. Next to the code when run from a folder, in %APPDATA%\\Claude Duo for
the Windows .exe (whose own folder is temporary), and in ~/Library/Application Support/Claude Duo for the
downloaded Mac app (which shouldn't write inside itself)."""
import os, sys

WIN = os.name == "nt"
MAC = sys.platform == "darwin"
FROZEN = bool(getattr(sys, "frozen", False))
HERE = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))


def _data():
    if os.environ.get("CLAUDE_DUO_DATA"):
        return os.environ["CLAUDE_DUO_DATA"]
    if not FROZEN:
        return HERE
    if MAC:
        return os.path.expanduser("~/Library/Application Support/Claude Duo")
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Claude Duo")


DATA = _data()
os.makedirs(DATA, exist_ok=True)
