#!/usr/bin/env python3
"""Frozen entry point for the packaged epub-shrink AppImage.

PyInstaller needs one real script to freeze. It hides the importlib machinery
the normal launcher uses and calls main() directly, so the bundled binary has
no dependency on the project folder layout.
"""

import os
import sys

HERE = os.path.dirname(os.path.realpath(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from epub_shrink_gui_qt import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
