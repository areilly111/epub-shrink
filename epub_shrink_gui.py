#!/usr/bin/env python3
"""Run the epub-shrink GUI from wherever this folder happens to live.

This is a thin launcher. It imports the real Qt window module and calls main()
rather than re-executing the file, so the GUI stays importable and testable.
"""

import os
import sys

HERE = os.path.dirname(os.path.realpath(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from epub_shrink_gui_qt import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
