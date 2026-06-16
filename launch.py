"""Frozen-app entry point.

PyInstaller needs a top-level SCRIPT (not a package module run as __main__,
which would break ``pdftexteditor.main``'s relative imports). This imports the
package normally and runs it.
"""

import sys

from pdftexteditor.main import main

if __name__ == "__main__":
    sys.exit(main())
