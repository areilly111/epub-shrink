# EPUB Compressor

Reduce the size of EPUB books so they fit on a Kindle, then send them to an
Audiobookshelf server.

## Why this exists

Audiobookshelf is a fine way to get books onto a Kindle. But it can only send a
book the Kindle will accept, and the limit is on file size. When a book is
over that limit, Audiobookshelf itself cannot help — the EPUB is simply too big,
and it fails before it ever reaches the device.

That was the problem here: a book that had to be emailed rather than synced.
Compressing the artwork brought it under the limit and it went across without
trouble.

The cause is almost always artwork rather than text. EPUBs routinely contain
multi-megabyte PNGs of painted covers and illustrations, sometimes stored as
32-bit RGBA even with nothing transparent in them, which roughly doubles the
file for no visible benefit. Shrinking those images gets a book under the limit
without touching a single word of it.

So this is the tool for the step Audiobookshelf cannot do for you: **shrink the
EPUB, then upload it.**

## What it does

- Recompresses oversized artwork inside the EPUB, and only when doing so
  actually makes the file smaller.
- **Never touches text.** Words, metadata, styles and reading order are left
  exactly as they were, and the output is a spec-compliant EPUB.
- Presets for common e-readers, or a direct size target such as `5MB`.
- Works in place, archiving the untouched original to a `Backups` folder and
  restorable at any time, or writes new files and leaves the originals alone.
- Uploads straight to Audiobookshelf with the title, author, series and the rest
  read out of the book itself.
- A command line tool as well as a window.

## Getting it

Download the AppImage, mark it executable, and run it:

```bash
chmod +x epub-shrink-x86_64.AppImage
./epub-shrink-x86_64.AppImage
```

Nothing needs installing: Python, Qt and Pillow all travel inside the image. A
64-bit Linux kernel and the FUSE runtime AppImages use are the only
requirements.

To run from source instead:

```bash
pip install PySide6 Pillow
./install.sh
epub-shrink-gui
```

## Using the window

Open it and the list is empty, as it should be. Use **Add files...** or **Add
folder...**, or drop files and folders onto the window. Those four list buttons
and **Upload selected to Audiobookshelf** sit directly beneath the list, above
the options panel. The bottom row holds **Restore**, **Preview**, **Save
report**, **Log**, **Cancel** and **Compress**.

Pick a preset (or set the numbers yourself), press **Compress**, and watch the
report. Books that get smaller are listed with before and after sizes; anything
skipped says why.

Finished books can go straight to the server with **Upload selected to
Audiobookshelf**, whether or not they were just compressed. Each book's metadata
is read from its own EPUB and shown in an editable table — title, author,
series, series number and the filename it will be stored under — so a wrong
author can be fixed before anything is sent. Two books that would land on the
same filename are called out, because the server keeps one file per name.

## Connecting to Audiobookshelf

**Settings...** takes the server URL and either an API key or a username and
password. An API key on its own is enough and is the better choice.

Press **Test connection & load libraries** once. The library list, your
selection and the time of the test are saved, so every later visit to the dialog
shows the real library and folder path immediately without contacting the
server again. The status line says where the current list came from and when.
The folder used for a library is remembered per library, so switching between
libraries and back returns to where you were.

The metadata sent is what the EPUB declares: title, subtitle, authors,
narrators, series and its position, publisher, publication year and date,
language, description, ISBN/ASIN, genres and tags. Absent values are omitted
rather than blanked, so a sparse EPUB does not wipe metadata the server already
had. Every upload reports honestly whether the file was sent, indexed and given
its metadata; a failed upload never takes the compression job down with it.

Credentials are stored only in the settings file, written `0600` under
`~/.config/epub-shrink/`. Nothing is written to the source, and no credentials
travel with the AppImage.

## Using the command line

```bash
epub-shrink book.epub                        # writes book_compressed.epub
epub-shrink book.epub -o small.epub -q 65
epub-shrink *.epub --dry-run                 # report only, writes nothing
epub-shrink ~/Books                          # every EPUB under a folder
epub-shrink ~/Books --in-place               # originals archived to Backups/
epub-shrink ~/Books --in-place --backup-dir /mnt/backup
epub-shrink ~/Books --preset oasis           # Kindle Oasis-grade numbers
epub-shrink book.epub --target 5MB           # keep the output under 5 MB
epub-shrink ~/Books --jobs 4                 # compress 4 books in parallel
epub-shrink ~/Books --in-place --restore     # swap back the archived originals
epub-shrink ~/Books --report-file run.json   # also write the full report
```

## Credits

This project stands on the work of others. None of it is copied into the source,
but all of it shaped the result.

- **[Audiobookshelf](https://github.com/advplyr/audiobookshelf)** (GPL-3.0) — the
  server this uploads to, and the reference for its HTTP API. The metadata
  parser deliberately matches the behaviour of
  `server/utils/parsers/parseOpfMetadata.js` so that an upload arrives with at
  least the detail the server would have worked out for itself. That is a
  behavioural reference only: the implementation is independent, written on top
  of `xml.etree.ElementTree` against the OPF specification, and no
  Audiobookshelf code is used or imported.
- **[Python](https://www.python.org)** (PSF-2.0) and the standard library.
- **[PySide6 / Qt 6](https://doc.qt.io/qtforpython/)** (LGPL-3.0) — the window,
  tree, dialogs and preview.
- **[Pillow](https://python-pillow.org)** (MIT-CMU) — the image codec and
  preview thumbnails.
- **[PyInstaller](https://pyinstaller.org)** (GPL-2.0-or-later with a bundling
  exception) and **[appimagetool](https://github.com/AppImage/AppImageKit)** —
  used to build the AppImage.
- **[EPUB 3.3 specification](https://www.w3.org/TR/epub-33/)** — what defines a
  valid EPUB, including the `mimetype` entry an archive has to store first and
  uncompressed.
- **[Kindle size limits](https://www.amazon.com/gp/help/customer/display.html?nodeId=GKMQC26VQQMM8XSW)** —
  the target this was built to meet.

## License

[GPL-3.0-or-later](LICENSE).

GPL-3.0 was chosen deliberately rather than a permissive license: the metadata
parser tracks Audiobookshelf's behaviour closely, and keeping the same license
removes any question about what that obliges. If you fork this, GPL-3.0-or-later
is the straightforward choice.
