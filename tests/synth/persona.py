"""Compatibility shim for the invented demo generators."""
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from caduceus.demo.persona import *  # noqa: F401,F403
from caduceus.demo.persona import _filler, _skip_markers

if __name__ == "__main__":
    main()
