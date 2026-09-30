# AppImage packaging

The GUI is **PySide6** (Qt 6), so packaging is straightforward: PyInstaller
bundles Python, Qt and its plugins, and Pillow into a single self-contained
binary. There is no Tcl/Tk and no host Python requirement. (The GUI was ported
from Tkinter to PySide6 specifically to make this clean — Tkinter links the
host's Tcl/Tk, which prebuilt CPython distributions cannot carry into a
bundle.)

## Building

```bash
./build-appimage.sh
```

That is a normal one-command build. It needs **PyInstaller** in a Python 3.10+
that also has **PySide6** and **Pillow** (`pip install PySide6 Pillow
pyinstaller`), and it finds `appimagetool` on `PATH`, at
`~/.local/opt/appimagetool/appimagetool.AppImage`, or wherever `APPIMAGETOOL=`
points. Override the interpreter with `PYTHON=/path/to/python`.

```bash
mkdir -p ~/.local/opt/appimagetool
curl -L -o ~/.local/opt/appimagetool/appimagetool.AppImage \
  https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
chmod +x ~/.local/opt/appimagetool/appimagetool.AppImage
```

The script:

1. freezes `packaging/frozen_main.py` with PyInstaller (`--onedir`), bundling
   the engine and the settings/ABS modules as data files,
2. lays the frozen tree, the desktop entry, AppStream metadata and the generated
   icons into an AppDir,
3. smoke-tests the frozen binary headlessly (`--self-test` builds the real window
   under the offscreen Qt platform) so an image that cannot start never gets
   packaged,
4. runs `appimagetool` to produce the `.AppImage`.

Output: `dist/epub-shrink-x86_64.AppImage` (override with `APPIMAGE=...`).

### Two things worth knowing

**`--onedir`, not `--onefile`.** An AppImage is already a compressed read-only
squashfs, so packing the bundle into one executable would only add an unpack of
several hundred megabytes into `/tmp` on every launch. Inside the image the
tree costs nothing.

**The build tree is on local disk.** `BUILD=` defaults to a `/tmp` directory, not
to the project folder. This project lives on an SMB share that does not support
symlinks, and PyInstaller's collect step symlinks Qt's shared libraries; set
`BUILD=` explicitly if you need it elsewhere. Only the finished `.AppImage` is
written back into the project.

Other overrides: `DIST=`, `APPIMAGETOOL=`, `PYTHON=`, and `KEEP_APPDIR=1` to
keep the intermediate AppDir for debugging.

## Dependencies in the image

- **PySide6 / Qt 6** — the window, tree, dialogs and the QPixmap preview.
- **Pillow** — the engine's image codec and the preview thumbnails.
- The engine, settings and Audiobookshelf client are pure stdlib on top of that
  (`urllib`, `json`, `zipfile`); nothing else is pulled in.

## Layout produced

```
EpubCompressor.AppDir/
├── AppRun                              runs the frozen binary
├── epubshrink.desktop  desktop entry (root copy)
├── epubshrink.png      256px icon, embedded in the image
└── usr/
    ├── bin/epub-shrink-gui             the frozen application
    │   └── _internal/                  Python, Qt, plugins, Pillow
    ├── share/applications/…desktop     the extracted-image desktop entry
    ├── share/metainfo/…metainfo.xml    AppStream description
    └── share/icons/hicolor/*/apps/…png 16-512px
```

The icon is generated at build time from `icon.webp` (or `icon.png` /
`icon.ico`) in the project root at every standard hicolor size, so the artwork
can be re-exported at any resolution without touching the scripts. AppStream
metadata is included only when `appstreamcli` validates it; warnings such as a
missing homepage do not drop it.

## Checking the result

```bash
./EpubCompressor.AppDir/AppRun --self-test   # headless: proves it opens a window
./EpubCompressor.AppDir/AppRun book.epub     # real run
./dist/epub-shrink-x86_64.AppImage --self-test
```

The real proof is to run the built image on a machine that has no Python, no
Pillow and no Qt installed — that is the only test that demonstrates the bundle
is genuinely self-contained.
