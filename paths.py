import sys as _sys
from pathlib import Path as _P
if getattr(_sys, "frozen", False):  # running as StickmanStudio.exe
    RES = _P(getattr(_sys, "_MEIPASS", _P(_sys.executable).parent))
    BASE = _P.home() / "Documents" / "Stickman Studio"
else:
    RES = BASE = _P(__file__).resolve().parent
BASE.mkdir(parents=True, exist_ok=True)
