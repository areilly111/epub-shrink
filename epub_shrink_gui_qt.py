#!/usr/bin/env python3
"""PySide6 GUI for epub-shrink.

A ground-up Qt port of the Tkinter window, targeting feature parity: presets,
size target, cover quality, max-dim, strip-unused, parallel jobs, in-place with
backups, restore, report export, before/after preview, and Audiobookshelf
settings/upload. The engine (es) and the settings/ABS modules are shared
unchanged, so compression behaviour is identical to the CLI.

Relocatability is preserved: every module is found next to this file, so the
folder can be moved, symlinked, or packaged without edits. This Qt version
bundles cleanly into an AppImage (PyInstaller ships PySide6 with no host Tcl/Tk
requirement, unlike tkinter).

Launch via epub-shrink-gui (after ./install.sh) or python3 epub_shrink_gui.py.
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import queue
import sys
import threading
import traceback
from dataclasses import dataclass, field
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QPixmap, QTextCharFormat
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSlider, QSpinBox, QTableWidget, QTableWidgetItem, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

HERE = Path(__file__).resolve().parent
BACKEND_NAMES = ("epub_shrink.py", "epub-shrink", "epub_shrink/__init__.py")


def _load_module(mod_name: str, names: tuple[str, ...]) -> Any:
    """Import a sibling module by its normal name or a fallback filename."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    try:
        return __import__(mod_name)
    except ImportError:
        pass
    for name in names:
        path = HERE / name
        if path.is_file():
            spec = importlib.util.spec_from_loader(
                mod_name, SourceFileLoader(mod_name, str(path)))
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            return module
    raise ImportError(f"{mod_name} not found beside this script")


es = _load_module("epub_shrink", BACKEND_NAMES)
settings_mod = _load_module("epub_shrink_settings", ("epub_shrink_settings.py",))
abs_mod = _load_module("epub_shrink_abs", ("epub_shrink_abs.py",))

PRESETS = es.PRESETS
PRESET_ORDER = ["kindle-paperwhite", "kindle-basic", "kindle-oasis",
                "tablet", "smallest", "custom"]
PRESET_LABELS = {
    "kindle-paperwhite": "Kindle Paperwhite (default)",
    "kindle-basic": "Kindle (basic)",
    "kindle-oasis": "Kindle Oasis / Scribe",
    "tablet": "Tablet / iPad",
    "smallest": "Smallest possible",
    "custom": "Custom (as set below)",
}


def human(n: float) -> str:
    return es.human(n)


def resolve_meta(meta: dict, path: str, settings: dict) -> dict:
    """A book's own metadata with the gaps filled in from filename and defaults.

    EPUB metadata is frequently incomplete, and Audiobookshelf refuses an upload
    with no title at all, so the on-disk stem and the user's configured defaults
    stand in for anything the book failed to declare.
    """
    out = dict(meta)
    stem = os.path.splitext(os.path.basename(path))[0]
    if not str(out.get("title") or "").strip():
        out["title"] = stem
    if not str(out.get("author") or "").strip():
        fallback = str(settings.get("default_author") or "").strip()
        if fallback:
            out["author"] = fallback
            out["authors"] = [fallback]
    if not str(out.get("series") or "").strip():
        fallback = str(settings.get("default_series") or "").strip()
        if fallback:
            out["series"] = fallback
    return out


def library_cache(libraries: list[dict]) -> list[dict]:
    """Reduce a server library listing to what is worth keeping on disk.

    Only ids, names and folder paths survive. Nothing here is secret, but the
    cached shape is deliberately smaller than the server's: a settings file that
    mirrors the whole /api/libraries payload would rot as soon as the server
    adds a field, and there is no reason to store anything the GUI does not read.
    """
    out = []
    for lib in libraries or []:
        folders = [{"id": f.get("id", ""), "path": f.get("fullPath", "")}
                   for f in (lib.get("folders") or []) if f.get("id")]
        out.append({"id": lib.get("id", ""), "name": lib.get("name", ""),
                    "folders": folders})
    return [lib for lib in out if lib["id"]]


def libraries_from_cache(cache: Any) -> list[dict]:
    """Rebuild the listing shape fetch_libraries() returns, from the cache.

    A stored cache written by an older version, or hand-edited into something
    unexpected, yields an empty list rather than an exception: the settings
    dialog then simply asks for the connection to be tested again.
    """
    out = []
    for lib in cache if isinstance(cache, list) else []:
        if not isinstance(lib, dict) or not lib.get("id"):
            continue
        folders = [{"id": f.get("id", ""), "fullPath": f.get("path", "")}
                   for f in (lib.get("folders") or [])
                   if isinstance(f, dict) and f.get("id")]
        out.append({"id": lib["id"],
                    "name": lib.get("name") or lib["id"],
                    "folders": folders})
    return out


@dataclass
class Job:
    """One EPUB queued for compression."""
    path: str
    status: str = "Queued"
    before: int = 0
    after: int = 0
    output: str = ""
    problem: str = ""
    done: bool = False
    is_dry_run: bool = False
    report: Any = None          # the engine Report, held for preview/export
    upload_note: str = ""       # result of an Audiobookshelf upload, if any
    images_before: int = 0
    images_after: int = 0
    converted: int = 0
    renamed: int = 0
    psnr: list[tuple[str, float]] = field(default_factory=list)


class BackendThread(QThread):
    progress = Signal(str, object)
    finished = Signal()

    def __init__(self, jobs: list[Job], opts: Any, settings: dict):
        super().__init__()
        self.jobs = jobs
        self.opts = opts
        self.settings = settings
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        self.progress.emit("log", "Starting...\n")
        max_workers = max(1, min(int(getattr(self.opts, "jobs", 1) or 1), len(self.jobs)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(self._handle, i) for i in range(len(self.jobs))]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception:
                    self.progress.emit("trace", traceback.format_exc())
        self.progress.emit("finished", None)
        self.finished.emit()

    def _handle(self, index: int) -> None:
        job = self.jobs[index]
        if self._cancel.is_set():
            job.status = "Cancelled"
            self.progress.emit("update", job)
            return
        job.status = "Working..."
        self.progress.emit("log", f"{os.path.basename(job.path)}\n")
        self.progress.emit("update", job)
        try:
            rep = self._run_one(job)
        except Exception as exc:
            job.status = "Failed"
            job.problem = str(exc)
            self.progress.emit("log", f"  error: {exc}\n")
            self.progress.emit("trace", traceback.format_exc())
            self.progress.emit("update", job)
            return
        self._absorb(job, rep)
        self._maybe_upload(job)
        self.progress.emit("done", job)

    def _run_one(self, job: Job):
        opts = self.opts
        src = job.path
        if opts.dry_run:
            out = os.path.join(os.path.dirname(src), f".epub-shrink-dryrun-{os.getpid()}.epub")
        elif opts.in_place:
            out = src + ".shrinking"
        else:
            out = es.default_output(src, opts.suffix)
        try:
            rep = es.compress_epub(src, out, opts)
        except BaseException:
            for leftover in (out, f"{out}.partial"):
                if os.path.exists(leftover):
                    os.remove(leftover)
            raise
        job.report = rep
        if opts.dry_run:
            if os.path.exists(out):
                os.remove(out)
        elif opts.in_place:
            if rep.problems:
                if os.path.exists(out):
                    os.remove(out)
                job.problem = f"{len(rep.problems)} validation problems, original left untouched"
            else:
                backup = es.archive_original(src, opts.backup_dir)
                os.replace(out, src)
                rep.out_path = src
                self.progress.emit("backup", (os.path.basename(src), backup))
        return rep

    def _absorb(self, job: Job, rep) -> None:
        job.before = rep.src_bytes
        job.after = rep.out_bytes
        job.images_before = rep.image_before
        job.images_after = rep.image_after
        job.converted = sum(1 for i in rep.items if i.action == "png->jpeg")
        job.renamed = sum(1 for i in rep.items if i.action == "reencoded")
        job.psnr = [(i.new_name, i.psnr) for i in rep.items if i.psnr is not None]
        job.problem = "; ".join(rep.problems) if rep.problems else ""
        job.output = "(dry run - nothing written)" if job.is_dry_run else rep.out_path
        job.done = True
        if job.status != "Done (cancelled after this file)":
            if rep.problems:
                job.status = "Validation failed"
            elif job.is_dry_run:
                job.status = "Would compress"
            else:
                job.status = "Done"
        saved = rep.src_bytes - rep.out_bytes
        pct = (saved / rep.src_bytes * 100) if rep.src_bytes else 0
        self.progress.emit(
            "log",
            f"  images  {human(rep.image_before)} -> {human(rep.image_after)}\n"
            f"  file    {human(rep.src_bytes)} -> {human(rep.out_bytes)}"
            f"   (-{pct:.0f}%, {human(saved)} saved)\n",
        )
        for note in rep.notes:
            self.progress.emit("log", f"  note: {note}\n")
        for problem in rep.problems:
            self.progress.emit("log", f"  PROBLEM: {problem}\n")
        if not rep.problems:
            self.progress.emit("log", "  checks passed\n")

    def _maybe_upload(self, job: Job) -> None:
        if not (self.opts.abs_upload and job.done and not job.is_dry_run and not job.problem and job.report and not job.report.problems):
            return
        abs_conf = self.settings.get("abs") or {}
        library_id = (abs_conf.get("library_id") or "").strip()
        folder_id = (abs_conf.get("folder_id") or "").strip()
        if not library_id or not folder_id:
            job.upload_note = "Audiobookshelf: library and folder not chosen - open Settings and pick them"
            self.progress.emit("log", f"  {job.upload_note}\n")
            return
        try:
            client = abs_mod.build_client(self.settings)
            meta = resolve_meta(es.read_book_meta(job.output), job.output,
                                self.settings)
            with open(job.output, "rb") as fh:
                blob = fh.read()
            upload_name = es.derive_upload_name(
                meta, os.path.splitext(os.path.basename(job.output))[0])
            result = client.publish(
                library_id,
                folder_id,
                meta,
                blob,
                upload_name,
                scan_after=bool(abs_conf.get("scan_after", True)),
                progress=lambda msg: self.progress.emit("log", f"  abs: {msg}\n"),
            )
            job.upload_note = result.get("message", "")
            self.progress.emit("log", f"  abs: {job.upload_note}\n")
        except Exception as exc:
            job.upload_note = f"Audiobookshelf upload failed: {exc}"
            self.progress.emit("log", f"  abs error: {exc}\n")


class RestoreThread(QThread):
    """Swaps books back to their archived originals, off the UI thread."""

    progress = Signal(str, object)
    finished = Signal()

    def __init__(self, jobs: list[Job]):
        super().__init__()
        self.jobs = jobs
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        for job in self.jobs:
            if self._cancel.is_set():
                job.status = "Cancelled"
                self.progress.emit("update", job)
                continue
            try:
                es.restore_original(job.path, None)
                job.before = os.path.getsize(job.path)
                job.status = "Restored"
                job.upload_note = ""
                self.progress.emit("log", f"restored {os.path.basename(job.path)} from backup\n")
            except FileNotFoundError as exc:
                job.status = "No backup"
                self.progress.emit("log", f"  {exc}\n")
            except OSError as exc:
                job.status = "Restore failed"
                job.problem = str(exc)
                self.progress.emit("log", f"  restore error: {exc}\n")
            self.progress.emit("update", job)
            self.progress.emit("done", job)
        self.progress.emit("finished", None)
        self.finished.emit()


@dataclass
class UploadEntry:
    """One book on its way to Audiobookshelf.

    `meta` is the read_book_meta() dict with the dialog's edits applied;
    `filename` is the name it will be stored under on the server, which is
    separate from the local file and is shown to the user before they commit.
    """
    path: str
    meta: dict
    filename: str
    status: str = "Waiting"
    note: str = ""
    # The title the file declares for itself, kept before the user edits it.
    # Audiobookshelf indexes the book under its own title, not the one sent
    # with the request, so this is what the item has to be searched for after a
    # title correction in the dialog.
    disk_title: str = ""

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or "").strip()

    def search_terms(self) -> list[str]:
        """Names the book may be indexed under, for locating it post-upload."""
        terms = [self.disk_title, str(self.meta.get("subtitle") or ""),
                 str(self.meta.get("isbn") or "")]
        return [t.strip() for t in terms if t.strip() and t.strip() != self.title]


class UploadThread(QThread):
    """Uploads books to Audiobookshelf one at a time, off the UI thread.

    Deliberately sequential: each upload is a blocking multipart POST followed by
    a scan and a poll for the item to appear, and hammering the server with
    several of those at once mostly earns rate limiting rather than speed.
    """

    progress = Signal(str, object)     # ("log", text) | ("entry", UploadEntry)
    finished = Signal(object)           # list[UploadEntry]

    def __init__(self, entries: list[UploadEntry], library_id: str,
                 folder_id: str, settings: dict, scan_after: bool = True):
        super().__init__()
        self.entries = entries
        self.library_id = library_id
        self.folder_id = folder_id
        self.settings = settings
        self.scan_after = scan_after
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            client = abs_mod.build_client(self.settings)
        except Exception as exc:
            self.progress.emit("log", f"  abs error: {exc}\n")
            for entry in self.entries:
                entry.status = "Failed"
                entry.note = str(exc)
                self.progress.emit("entry", entry)
            self.finished.emit(self.entries)
            return

        for entry in self.entries:
            if self._cancel.is_set():
                entry.status = "Cancelled"
                entry.note = "cancelled before upload"
                self.progress.emit("entry", entry)
                continue
            entry.status = "Uploading"
            self.progress.emit("entry", entry)
            try:
                with open(entry.path, "rb") as fh:
                    blob = fh.read()
                result = client.publish(
                    self.library_id, self.folder_id, entry.meta, blob,
                    entry.filename, scan_after=self.scan_after,
                    progress=lambda msg: self.progress.emit("log", f"  {msg}\n"),
                    search_terms=entry.search_terms(),
                )
                entry.status = ("Uploaded" if result.get("status") == "ok"
                                else "Uploaded, not indexed")
                entry.note = result.get("message", "")
            except Exception as exc:
                entry.status = "Failed"
                entry.note = str(exc)
                self.progress.emit("log", f"  abs error: {exc}\n")
            self.progress.emit("entry", entry)
        self.finished.emit(self.entries)


class FileDropWidget(QWidget):
    """A widget that accepts file/folder drops from the desktop."""

    dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
        if paths:
            self.dropped.emit(paths)
        event.acceptProposedAction()


class App(QMainWindow):
    def __init__(self, argv: list[str] | None = None):
        super().__init__()
        self.argv = argv or []
        self.setWindowTitle("epub-shrink")
        self.resize(1000, 800)
        self.setMinimumSize(800, 620)

        self.jobs: list[Job] = []
        self.events: queue.Queue = queue.Queue()
        self.worker: BackendThread | None = None
        self.restorer: RestoreThread | None = None
        self.uploader: UploadThread | None = None
        self._upload_rows: UploadDialog | None = None
        self._upload_entries: list[UploadEntry] = []
        self.row_ids: dict[str, QTreeWidgetItem] = {}
        self.settings: dict = settings_mod.load_settings()
        # Seeded from the cached listing so the settings dialog shows the real
        # library and folder on first open, with no connection needed.
        self._abs_libraries: list[dict] = libraries_from_cache(
            (self.settings.get("abs") or {}).get("libraries"))
        self._applying_preset = False

        proto = es.build_parser().parse_args(["x.epub"])
        settings_mod.apply_settings_to_namespace(proto, self.settings)
        es.apply_preset_defaults(proto)

        self._build(proto)
        self._watch_customization()

        preset_key = proto.preset if proto.preset in PRESET_LABELS else "kindle-paperwhite"
        self.preset_combo.setCurrentText(PRESET_LABELS[preset_key])
        self._apply_preset(preset_key)
        self._sync_controls()

        for arg in self.argv:
            if os.path.isdir(arg):
                for found in es.find_epubs(arg, recursive=True):
                    self._enqueue(found)
            elif os.path.isfile(arg):
                self._enqueue(arg)
            else:
                print(f"not found: {arg}", file=sys.stderr)

    # -- layout ---------------------------------------------------------

    def _build(self, proto) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("epub-shrink")
        font = title.font()
        font.setPointSize(font.pointSize() + 4)
        font.setBold(True)
        title.setFont(font)
        header.addWidget(title)
        tag = QLabel("make your EPUBs small enough for the Kindle")
        tag.setStyleSheet("color: #90a0b0;")
        header.addWidget(tag)
        header.addStretch(1)
        root.addLayout(header)

        self.drop_zone = FileDropWidget()
        self.drop_zone.dropped.connect(self._on_drop_paths)
        drop_layout = QVBoxLayout()
        drop_layout.setContentsMargins(0, 0, 0, 0)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["File", "Size", "Images", "Status"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.tree.setEditTriggers(QTreeWidget.NoEditTriggers)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().resizeSection(0, 380)
        self.tree.header().resizeSection(1, 150)
        self.tree.header().resizeSection(2, 170)
        self.tree.header().resizeSection(3, 160)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemDoubleClicked.connect(lambda _i: self._preview_selected())
        drop_layout.addWidget(self.tree)
        self.drop_zone.setLayout(drop_layout)
        root.addWidget(self.drop_zone, 1)

        totals = QHBoxLayout()
        self.total_before = QLabel("0 B")
        self.total_after = QLabel("0 B")
        self.total_saved = QLabel("0 B (0%)")
        self.summary = QLabel("Drop EPUB files here to begin.")
        for text, label in (("Total before:", self.total_before),
                            ("Total after:", self.total_after),
                            ("Saved:", self.total_saved)):
            caption = QLabel(text)
            caption.setStyleSheet("color: #90a0b0;")
            totals.addWidget(caption)
            bold = label.font()
            bold.setBold(True)
            label.setFont(bold)
            totals.addWidget(label)
            totals.addSpacing(18)
        totals.addStretch(1)
        self.summary.setStyleSheet("color: #90a0b0;")
        totals.addWidget(self.summary)
        root.addLayout(totals)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        options = QGroupBox("Options")
        grid = QGridLayout(options)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)

        grid.addWidget(QLabel("Target device"), 0, 0)
        self.preset_combo = QComboBox()
        self.preset_combo.addItems([PRESET_LABELS[p] for p in PRESET_ORDER])
        self.preset_combo.currentIndexChanged.connect(self._on_preset_chosen)
        grid.addWidget(self.preset_combo, 0, 1)
        grid.addWidget(QLabel("Cover quality"), 0, 2)
        self.cover_quality_slider, self.cover_quality_value = self._slider(1, 95)
        grid.addWidget(self.cover_quality_slider, 0, 3)
        grid.addWidget(self.cover_quality_value, 0, 4)
        grid.addWidget(QLabel("Downscale over"), 0, 5)
        self.max_dim_spin = QSpinBox()
        self.max_dim_spin.setRange(0, 4000)
        self.max_dim_spin.setSuffix(" px")
        grid.addWidget(self.max_dim_spin, 0, 6)
        grid.addWidget(QLabel("Books at once"), 0, 7)
        self.jobs_spin = QSpinBox()
        self.jobs_spin.setRange(1, 8)
        grid.addWidget(self.jobs_spin, 0, 8)

        grid.addWidget(QLabel("Artwork quality"), 1, 0)
        self.quality_slider, self.quality_value = self._slider(1, 95)
        grid.addWidget(self.quality_slider, 1, 1)
        grid.addWidget(self.quality_value, 1, 2)
        grid.addWidget(QLabel("Existing JPEG quality"), 1, 3)
        self.jpeg_quality_slider, self.jpeg_quality_value = self._slider(1, 95)
        grid.addWidget(self.jpeg_quality_slider, 1, 4)
        grid.addWidget(self.jpeg_quality_value, 1, 5)

        grid.addWidget(QLabel("Convert PNGs at least"), 2, 0)
        self.min_dim_spin = QSpinBox()
        self.min_dim_spin.setRange(50, 10000)
        self.min_dim_spin.setSuffix(" px")
        grid.addWidget(self.min_dim_spin, 2, 1)
        grid.addWidget(QLabel("and"), 2, 2)
        self.min_png_kb_spin = QSpinBox()
        self.min_png_kb_spin.setRange(1, 100000)
        self.min_png_kb_spin.setSuffix(" KB")
        grid.addWidget(self.min_png_kb_spin, 2, 3)
        grid.addWidget(QLabel("Convert JPEGs at least"), 2, 4)
        self.min_jpeg_kb_spin = QSpinBox()
        self.min_jpeg_kb_spin.setRange(0, 100000)
        self.min_jpeg_kb_spin.setSuffix(" KB")
        grid.addWidget(self.min_jpeg_kb_spin, 2, 5)

        grid.addWidget(QLabel("Size target"), 3, 0)
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("off, or e.g. 5MB / 700k")
        self.target_edit.setMaximumWidth(140)
        grid.addWidget(self.target_edit, 3, 1)
        grid.addWidget(QLabel("Output suffix"), 3, 2)
        self.suffix_edit = QLineEdit()
        self.suffix_edit.setMaximumWidth(160)
        grid.addWidget(self.suffix_edit, 3, 3)

        checks = QHBoxLayout()
        self.dry_run_check = QCheckBox("Dry run")
        self.dry_run_check.toggled.connect(self._sync_controls)
        self.verbose_check = QCheckBox("Verbose")
        self.strip_unused_check = QCheckBox("Strip unused files")
        checks.addWidget(self.dry_run_check)
        checks.addWidget(self.verbose_check)
        checks.addWidget(self.strip_unused_check)
        checks.addStretch(1)
        self.upload_after_check = QCheckBox("Upload to Audiobookshelf")
        checks.addWidget(self.upload_after_check)
        self.settings_btn = QPushButton("Settings...")
        self.settings_btn.clicked.connect(self._open_settings)
        checks.addWidget(self.settings_btn)
        grid.addLayout(checks, 4, 0, 1, 9)

        inplace = QHBoxLayout()
        self.in_place_check = QCheckBox("Replace originals, archiving to")
        self.in_place_check.toggled.connect(self._sync_controls)
        inplace.addWidget(self.in_place_check)
        self.backup_edit = QLineEdit()
        self.backup_edit.setPlaceholderText("blank = a Backups folder beside each book")
        inplace.addWidget(self.backup_edit, 1)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._choose_backup_dir)
        inplace.addWidget(browse)
        grid.addLayout(inplace, 5, 0, 1, 9)

        # The list's own buttons sit directly above the options rather than in
        # the bottom bar: adding, removing and uploading are all operations on
        # the selected books, and keeping them next to the list keeps the
        # bottom bar for the actions that run over the whole list.
        list_bar = QHBoxLayout()
        self.add_files_btn = QPushButton("Add files...")
        self.add_files_btn.clicked.connect(self._add_files)
        self.folder_btn = QPushButton("Add folder...")
        self.folder_btn.clicked.connect(self._add_folder)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self._remove_selected)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._clear)
        for button in (self.add_files_btn, self.folder_btn, self.remove_btn,
                       self.clear_btn):
            list_bar.addWidget(button)
        self.upload_btn = QPushButton("Upload selected to Audiobookshelf...")
        self.upload_btn.clicked.connect(self._open_upload)
        list_bar.addWidget(self.upload_btn)
        list_bar.addStretch(1)
        root.addLayout(list_bar)

        root.addWidget(options)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(180)
        self.log.setStyleSheet("background:#141414; color:#e6e6e6;")
        root.addWidget(self.log)

        self.status = QLabel("Ready")
        self.status.setStyleSheet("color: #90a0b0;")
        root.addWidget(self.status)

        bar = QHBoxLayout()
        self.restore_btn = QPushButton("Restore originals...")
        self.restore_btn.clicked.connect(self._start_restore)
        self.preview_btn = QPushButton("Preview...")
        self.preview_btn.clicked.connect(self._preview_selected)
        self.report_btn = QPushButton("Save report...")
        self.report_btn.clicked.connect(self._export_report)
        for button in (self.restore_btn, self.preview_btn, self.report_btn):
            bar.addWidget(button)
        bar.addStretch(1)
        self.show_log_check = QCheckBox("Log")
        self.show_log_check.setChecked(True)
        self.show_log_check.toggled.connect(self._toggle_log)
        bar.addWidget(self.show_log_check)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        self.cancel_btn.setEnabled(False)
        bar.addWidget(self.cancel_btn)
        self.start_btn = QPushButton("Compress")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._start)
        self.start_btn.setEnabled(False)
        bar.addWidget(self.start_btn)
        root.addLayout(bar)

        self._set_saved_values(proto)
        self._update_buttons()

    def _set_saved_values(self, proto) -> None:
        for slider, value in (
            (self.cover_quality_slider, self.cover_quality_value),
            (self.quality_slider, self.quality_value),
            (self.jpeg_quality_slider, self.jpeg_quality_value),
        ):
            slider.blockSignals(True)
            slider.setValue(int(proto.cover_quality) if slider is self.cover_quality_slider
                            else int(proto.quality if slider is self.quality_slider
                                     else proto.jpeg_quality))
            slider.blockSignals(False)
            value.setText(str(slider.value()))
        for spin, number in (
            (self.max_dim_spin, int(proto.max_dim or 0)),
            (self.min_dim_spin, int(proto.min_dim)),
            (self.min_png_kb_spin, int(proto.min_png_bytes // 1024)),
            (self.min_jpeg_kb_spin, int(proto.min_jpeg_bytes // 1024)),
        ):
            spin.blockSignals(True)
            spin.setValue(number)
            spin.blockSignals(False)
        self.target_edit.setText(self.settings.get("target") or "")
        self.jobs_spin.setValue(max(1, int(self.settings.get("jobs", 2) or 2)))
        self.strip_unused_check.setChecked(bool(proto.strip_unused))
        self.in_place_check.setChecked(bool(proto.in_place))
        self.backup_edit.setText(proto.backup_dir or "")
        self.suffix_edit.setText(proto.suffix or "_compressed")
        abs_conf = self.settings.get("abs") or {}
        self.upload_after_check.setChecked(bool(abs_conf.get("upload_after", False)))

    def _slider(self, lo: int, hi: int) -> tuple[QSlider, QLabel]:
        slider = QSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        value = QLabel(str(lo))
        value.setMinimumWidth(30)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        slider.valueChanged.connect(lambda v, lbl=value: lbl.setText(str(v)))
        return slider, value

    # -- setting knobs -------------------------------------------------

    def _watch_customization(self) -> None:
        """Moving any slider/spinner turns a named preset into 'custom'."""
        for signal_widget in (self.quality_slider, self.jpeg_quality_slider,
                              self.cover_quality_slider, self.min_dim_spin,
                              self.min_png_kb_spin, self.min_jpeg_kb_spin,
                              self.max_dim_spin):
            signal_widget.valueChanged.connect(self._mark_custom)

    def _preset_key(self) -> str:
        """Label shown in the combo -> engine preset id."""
        label = self.preset_combo.currentText()
        for key, text in PRESET_LABELS.items():
            if text == label:
                return key
        return "custom"

    def _mark_custom(self) -> None:
        if self._applying_preset:
            return
        if self.preset_combo.currentText() in PRESET_LABELS.values():
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentText(PRESET_LABELS["custom"])
            self.preset_combo.blockSignals(False)

    def _on_preset_chosen(self, *_args) -> None:
        self._apply_preset(self._preset_key())

    def _apply_preset(self, name: str) -> None:
        if name not in PRESETS or name == "custom":
            return
        self._applying_preset = True
        try:
            values = PRESETS[name]
            for widget, number in (
                (self.quality_slider, values["quality"]),
                (self.jpeg_quality_slider, values["jpeg_quality"]),
                (self.cover_quality_slider, values["cover_quality"]),
                (self.min_dim_spin, values["min_dim"]),
                (self.min_png_kb_spin, max(1, values["min_png_bytes"] // 1024)),
                (self.min_jpeg_kb_spin, max(1, values["min_jpeg_bytes"] // 1024)),
                (self.max_dim_spin, values["max_dim"]),
            ):
                widget.blockSignals(True)
                widget.setValue(int(number))
                widget.blockSignals(False)
            for slider, value in (
                (self.quality_slider, self.quality_value),
                (self.jpeg_quality_slider, self.jpeg_quality_value),
                (self.cover_quality_slider, self.cover_quality_value),
            ):
                value.setText(str(slider.value()))
        finally:
            self._applying_preset = False

    def _sync_controls(self) -> None:
        in_place = self.in_place_check.isChecked()
        dry = self.dry_run_check.isChecked()
        self.suffix_edit.setEnabled(not (in_place or dry))
        self.backup_edit.setEnabled(in_place)
        if in_place:
            self.start_btn.setText("Replace originals")
        else:
            self.start_btn.setText("Dry run" if dry else "Compress")

    # -- queue management ---------------------------------------------

    def _add_files(self) -> None:
        chosen, _ = QFileDialog.getOpenFileNames(
            self, "Choose EPUB files", "", "EPUB books (*.epub);;All files (*)")
        for path in chosen:
            self._enqueue(path)

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose a folder (subfolders are searched too)")
        if not folder:
            return
        found = es.find_epubs(folder, recursive=True)
        if not found:
            self._log(f"No EPUB files found under {folder}\n", "err")
            return
        for path in found:
            self._enqueue(path)
        self._log(f"Found {len(found)} EPUB file(s) under {folder}\n", "mut")

    def _choose_backup_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose where originals are kept")
        if folder:
            self.backup_edit.setText(folder)

    def _on_drop_paths(self, paths: list[str]) -> None:
        for path in paths:
            if os.path.isdir(path):
                for found in es.find_epubs(path, recursive=True):
                    self._enqueue(found)
            else:
                self._enqueue(path)

    def _enqueue(self, path: str) -> None:
        path = os.path.abspath(path)
        if any(os.path.abspath(j.path) == path for j in self.jobs):
            self._log(f"Already queued: {os.path.basename(path)}\n", "mut")
            return
        if not zipfile_is_epub(path):
            self._log(f"Not an EPUB: {os.path.basename(path)}\n", "err")
            return
        job = Job(path=path, before=os.path.getsize(path))
        self.jobs.append(job)
        item = QTreeWidgetItem([os.path.basename(path), human(job.before), "-", job.status])
        self.tree.addTopLevelItem(item)
        self.row_ids[path] = item
        self._recalc_totals()
        self._update_buttons()
        self._log(f"Added {os.path.basename(path)}\n", "mut")

    def _remove_selected(self) -> None:
        if self._busy():
            return
        for item in self.tree.selectedItems():
            path = next((p for p, i in self.row_ids.items() if i is item), None)
            if not path:
                continue
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
            self.row_ids.pop(path, None)
            self.jobs = [j for j in self.jobs if os.path.abspath(j.path) != path]
        self._recalc_totals()
        self._update_buttons()

    def _clear(self) -> None:
        if self._busy():
            return
        self.tree.clear()
        self.jobs.clear()
        self.row_ids.clear()
        self._recalc_totals()
        self._update_buttons()
        self._write_log("")

    def _busy(self) -> bool:
        return bool((self.worker and self.worker.isRunning())
                    or (self.restorer and self.restorer.isRunning())
                    or (self.uploader and self.uploader.isRunning()))

    def _selected_jobs(self) -> list[Job]:
        """The queued books the user currently has selected, in list order.

        With nothing selected this is every queued book, so the upload button
        does something sensible after a file drop rather than appearing dead.
        """
        paths = [next((p for p, i in self.row_ids.items() if i is item), None)
                 for item in self.tree.selectedItems()]
        paths = [p for p in paths if p]
        if not paths:
            return list(self.jobs)
        return [j for j in self.jobs if os.path.abspath(j.path) in set(paths)]

    @staticmethod
    def _upload_source(job: Job) -> str:
        """Which file to send for this book.

        A book that has already been compressed uploads the compressed result —
        that is the file the user spent the wait for. Anything else uploads
        exactly what is on disk. Whether compression has run at all is
        irrelevant: a book that was never queued for it uploads fine.
        """
        output = job.output
        if output and not job.is_dry_run and os.path.isfile(output):
            return output
        return job.path

    # -- upload --------------------------------------------------------

    def _upload_target(self) -> str:
        """Where uploads will land, named rather than identified by UUID."""
        abs_conf = self.settings.get("abs") or {}
        return f"{abs_conf.get('library_name') or 'the library'}" \
               f" / {abs_conf.get('folder_name') or 'its folder'}"

    def _open_upload(self) -> None:
        if self._busy():
            return
        abs_conf = self.settings.get("abs") or {}
        library_id = str(abs_conf.get("library_id") or "").strip()
        folder_id = str(abs_conf.get("folder_id") or "").strip()
        if not abs_conf.get("enabled"):
            QMessageBox.information(
                self, "Audiobookshelf",
                "Audiobookshelf uploads are turned off.\n\n"
                "Turn them on in Settings -> Audiobookshelf first.")
            return
        if not library_id or not folder_id:
            QMessageBox.information(
                self, "Audiobookshelf",
                "Choose a library and a folder to upload into first.\n\n"
                "That is under Settings -> Audiobookshelf.")
            return

        jobs = self._selected_jobs()
        if not jobs:
            QMessageBox.information(self, "Nothing selected",
                                    "Add some EPUBs to the list first.")
            return

        books: list[tuple[str, dict]] = []
        for job in jobs:
            path = self._upload_source(job)
            try:
                meta = resolve_meta(es.read_book_meta(path), path, self.settings)
            except Exception as exc:
                # read_book_meta does not raise, but a book could vanish from
                # under us between being queued and now.
                meta = resolve_meta({}, path, self.settings)
                self._log(f"  could not read metadata from "
                         f"{os.path.basename(path)}: {exc}\n", "err")
            books.append((path, meta))

        target = self._upload_target()
        dialog = UploadDialog(self, books, target)
        if dialog.exec() != QDialog.Accepted:
            return
        # The dialog stays alive after exec() so its rows can show per-book
        # upload status while the worker thread runs.
        self._upload_rows = dialog
        self._start_upload(dialog.entries(), library_id, folder_id,
                           str(abs_conf.get("scan_after", True)))

    def _start_upload(self, entries: list[UploadEntry], library_id: str,
                      folder_id: str, scan_after: bool) -> None:
        self._save_settings()
        self._upload_entries = entries
        for entry in entries:
            self._log(f"Uploading {entry.filename}\n", "mut")
        self.show_log_check.setChecked(True)
        self._toggle_log()
        self.progress.setRange(0, len(entries))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.status.setText(f"Uploading 0 of {len(entries)} to Audiobookshelf")
        self._update_buttons()

        self.uploader = UploadThread(entries, library_id, folder_id,
                                     self.settings, scan_after)
        self.uploader.progress.connect(self._on_upload_progress)
        self.uploader.finished.connect(self._on_upload_finished)
        self.uploader.start()

    def _on_upload_progress(self, kind: str, payload) -> None:
        if kind == "log":
            self._log(str(payload), "mut")
            return
        entry: UploadEntry = payload
        self.progress.setValue(min(self.progress.value() + 1,
                                   self.progress.maximum()))
        self.status.setText(
            f"Uploading {self.progress.value()} of "
            f"{self.progress.maximum()} to Audiobookshelf")
        if entry.note:
            self._log(f"  {entry.filename}: {entry.note}\n",
                      "err" if entry.status == "Failed" else "mut")
        for row, candidate in enumerate(self._upload_entries):
            if candidate is entry:
                self._upload_rows.set_status(row, entry.status, entry.note)
                break

    def _on_upload_finished(self, entries: list[UploadEntry]) -> None:
        done = sum(1 for e in entries if e.status.startswith("Uploaded"))
        failed = sum(1 for e in entries if e.status == "Failed")
        self.progress.setVisible(False)
        summary = f"Uploaded {done} of {len(entries)} to Audiobookshelf"
        if failed:
            summary += f" · {failed} failed (see log)"
        self.status.setText(summary)
        self._log(f"{summary}\n", "err" if failed else "ok")
        self.uploader = None
        self._upload_entries = []
        self._update_buttons()

    # -- run -----------------------------------------------------------

    def _collect_options(self):
        opts = es.build_parser().parse_args(["placeholder.epub"])
        opts.quality = self.quality_slider.value()
        opts.jpeg_quality = self.jpeg_quality_slider.value()
        opts.min_dim = self.min_dim_spin.value()
        opts.min_png_bytes = self.min_png_kb_spin.value() * 1024
        opts.min_jpeg_bytes = self.min_jpeg_kb_spin.value() * 1024
        opts.max_dim = self.max_dim_spin.value()
        opts.cover_quality = self.cover_quality_slider.value()
        opts.in_place = self.in_place_check.isChecked()
        opts.backup_dir = self.backup_edit.text().strip() or None
        opts.no_backup = False
        opts.dry_run = self.dry_run_check.isChecked()
        opts.verbose = self.verbose_check.isChecked()
        opts.psnr = False
        opts.suffix = self.suffix_edit.text() or "_compressed"
        opts.strip_unused = self.strip_unused_check.isChecked()
        opts.jobs = self.jobs_spin.value()
        target = self.target_edit.text().strip()
        try:
            opts.target_bytes = es.parse_size(target) if target else 0
        except Exception:
            raise ValueError(f"'{target}' is not a size like 5MB or 700k")
        opts.preset = self._preset_key()
        opts.abs_upload = bool(self.upload_after_check.isChecked()
                               and (self.settings.get("abs") or {}).get("enabled"))
        return opts

    def _start(self) -> None:
        if not self.jobs or self._busy():
            return
        try:
            opts = self._collect_options()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid option", f"Check the option values: {exc}")
            return

        if self.upload_after_check.isChecked() and not (self.settings.get("abs") or {}).get("enabled"):
            reply = QMessageBox.question(
                self, "Audiobookshelf is disabled",
                "Uploads are turned off in Settings. Continue without uploading?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
            opts.abs_upload = False

        if opts.in_place:
            reply = QMessageBox.question(
                self, "Replace originals?",
                f"{len(self.jobs)} file(s) will be replaced in place, keeping their "
                f"current names and locations so library apps such as Audiobookshelf "
                f"still find them.\n\nThe untouched originals are moved to:\n"
                f"  {opts.backup_dir or 'a Backups folder beside each book'}\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return

        for job in self.jobs:
            job.status = "Queued"
            job.done = False
            job.is_dry_run = opts.dry_run
            job.upload_note = ""
            self._set_row(job)
        self._write_log("")
        self._set_running(True)
        self.status.setText("Compressing...")
        self.progress.setVisible(True)
        self.progress.setMaximum(len(self.jobs))
        self.worker = BackendThread(self.jobs, opts, self.settings)
        self.worker.progress.connect(self._on_event)
        self.worker.finished.connect(lambda: self._on_finished())
        self.worker.start()

    def _cancel(self) -> None:
        if self._busy():
            active = self.worker if (self.worker and self.worker.isRunning()) else self.restorer
            if active:
                active.cancel()
            self.status.setText("Cancelling after the current file...")
            self.cancel_btn.setEnabled(False)

    def _start_restore(self) -> None:
        if not self.jobs or self._busy():
            return
        reply = QMessageBox.question(
            self, "Restore originals?",
            f"Each queued book will be swapped back for the untouched original kept "
            f"in its Backups folder, byte for byte.\n\n{len(self.jobs)} file(s) will be "
            f"replaced. Continue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        for job in self.jobs:
            job.status = "Queued"
            job.done = False
            job.upload_note = ""
            self._set_row(job)
        self._write_log("")
        self._set_running(True)
        self.status.setText("Restoring...")
        self.restorer = RestoreThread(self.jobs)
        self.restorer.progress.connect(self._on_event)
        self.restorer.finished.connect(lambda: self._on_finished())
        self.restorer.start()

    def _on_event(self, kind: str, payload) -> None:
        if kind == "log":
            self._log(payload)
        elif kind == "trace":
            self._log(payload, "err")
        elif kind == "backup":
            _name, where = payload
            self._log(f"  original kept: {where}\n", "mut")
        elif kind == "update":
            self._set_row(payload)
        elif kind == "done":
            self._set_row(payload)
            self._recalc_totals()
            self.progress.setValue(self.progress.value() + 1)

    def _on_finished(self) -> None:
        self._set_running(False)
        self._summary()
        self.progress.setVisible(False)

    def _set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self.restore_btn.setEnabled(not running and bool(self.jobs))
        if not running:
            self.status.setText("Ready")
            self._update_buttons()

    def _summary(self) -> None:
        done = [j for j in self.jobs if j.done]
        failed = [j for j in self.jobs
                  if j.status in ("Failed", "Validation failed", "Restore failed")]
        pending = [j for j in self.jobs if not j.done]
        if pending:
            if done:
                self.status.setText(f"Cancelled: {len(done)} finished, {len(pending)} not started.")
            else:
                self.status.setText(f"Stopped: {len(pending)} file(s) not processed")
        elif failed:
            self.status.setText(f"Finished with problems in {len(failed)} file(s)")
        elif self.dry_run_check.isChecked():
            self.status.setText("Dry run finished. No files were written.")
        elif done and all(j.status == "Restored" for j in done):
            self.status.setText(f"Restored {len(done)} file(s) from backup.")
        else:
            self.status.setText(f"Finished. {len(done)} file(s) processed.")

    # -- display -------------------------------------------------------

    def _set_row(self, job: Job) -> None:
        item = self.row_ids.get(job.path)
        if not item:
            return
        if job.done and job.before:
            images = f"{human(job.images_before)} -> {human(job.images_after)}"
            size = f"{human(job.before)} -> {human(job.after)}"
        else:
            images = "-"
            size = human(job.before)
        item.setText(0, os.path.basename(job.path))
        item.setText(1, size)
        item.setText(2, images)
        item.setText(3, job.status)
        if job.status in ("Failed", "Validation failed", "Restore failed", "No backup"):
            item.setForeground(3, Qt.red)
        elif job.status in ("Working...", "Queued"):
            item.setForeground(3, Qt.yellow)
        elif job.status.startswith(("Done", "Restored", "Would compress")):
            item.setForeground(3, Qt.green)
        else:
            item.setForeground(3, Qt.gray)

    def _recalc_totals(self) -> None:
        before = sum(j.before for j in self.jobs)
        after = sum(j.after if j.done and not j.is_dry_run else 0 for j in self.jobs)
        pending = sum(1 for j in self.jobs if not j.done)
        self.total_before.setText(human(before))
        self.total_after.setText(human(after))
        if before and not pending:
            saved = before - after
            self.total_saved.setText(f"{human(saved)} ({saved / before * 100:.0f}%)")
        elif before:
            self.total_saved.setText(f"{human(before - after)} so far")
        else:
            self.total_saved.setText("0 B (0%)")
        self.summary.setText(f"{pending} file(s) pending" if pending
                             else f"{len(self.jobs)} file(s)")

    def _update_buttons(self) -> None:
        running = self._busy()
        selected_done = self._selected_done_job() is not None
        any_done = any(j.done and j.report for j in self.jobs)
        self.upload_btn.setEnabled(bool(self.jobs) and not running)
        self.remove_btn.setEnabled(not running)
        self.folder_btn.setEnabled(not running)
        self.clear_btn.setEnabled(not running and bool(self.jobs))
        self.start_btn.setEnabled(not running and bool(self.jobs))
        self.restore_btn.setEnabled(not running and bool(self.jobs))
        self.preview_btn.setEnabled(not running and selected_done)
        self.report_btn.setEnabled(not running and any_done)

    def _selected_done_job(self) -> Job | None:
        for item in self.tree.selectedItems():
            path = next((p for p, i in self.row_ids.items() if i is item), None)
            for job in self.jobs:
                if os.path.abspath(job.path) == path and job.done and job.report:
                    return job
        return None

    def _toggle_log(self) -> None:
        self.log.setVisible(self.show_log_check.isChecked())

    def _write_log(self, text: str) -> None:
        self.log.setPlainText(text)

    def _log(self, text: str, tag: str = "") -> None:
        if not self.show_log_check.isChecked():
            return
        colors = {"err": "#e06c6c", "mut": "#a0a0a0", "good": "#5cc98a"}
        cursor = self.log.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        color = colors.get(tag)
        if color:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            cursor.setCharFormat(fmt)
        cursor.insertText(text)
        self.log.setTextCursor(cursor)
        self.log.ensureCursorVisible()

    # -- preview, report, settings -------------------------------------

    def _preview_selected(self) -> None:
        job = self._selected_done_job()
        if job:
            dialog = PreviewDialog(self, job)
            dialog.exec()

    def _export_report(self) -> None:
        done = [j for j in self.jobs if j.done and j.report]
        if not done:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save report", "epub-shrink-report.txt", "Text file (*.txt);;All files (*)")
        if not path:
            return
        proto = es.build_parser().parse_args(["x.epub"])
        proto.verbose = True
        blocks = []
        for job in done:
            blocks.append(es.format_report(job.report, proto, verbose=True))
            if job.upload_note:
                blocks.append(f"  upload: {job.upload_note}")
            blocks.append("")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(blocks))
        except OSError as exc:
            QMessageBox.critical(self, "Report", f"Could not write report: {exc}")
            return
        self._log(f"report written to {path}\n", "mut")

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self)
        dialog.exec()

    def closeEvent(self, event) -> None:
        self._save_settings()
        super().closeEvent(event)

    def _save_settings(self) -> None:
        """Persist the current widget state so the next run starts here."""
        abs_conf = dict(self.settings.get("abs") or {})
        self.settings.update({
            "preset": self._preset_key(),
            "quality": self.quality_slider.value(),
            "jpeg_quality": self.jpeg_quality_slider.value(),
            "min_dim": self.min_dim_spin.value(),
            "min_png_bytes": self.min_png_kb_spin.value() * 1024,
            "min_jpeg_bytes": self.min_jpeg_kb_spin.value() * 1024,
            "max_dim": self.max_dim_spin.value(),
            "cover_quality": self.cover_quality_slider.value(),
            "target": self.target_edit.text().strip(),
            "strip_unused": self.strip_unused_check.isChecked(),
            "jobs": self.jobs_spin.value(),
            "in_place": self.in_place_check.isChecked(),
            "no_backup": False,
            "backup_dir": self.backup_edit.text().strip(),
            "suffix": self.suffix_edit.text().strip() or "_compressed",
        })
        abs_conf["upload_after"] = self.upload_after_check.isChecked()
        self.settings["abs"] = abs_conf
        try:
            settings_mod.save_settings(self.settings)
        except OSError as exc:
            self._log(f"could not save settings: {exc}\n", "err")


# --------------------------------------------------------------------------
# Audiobookshelf settings dialog
# --------------------------------------------------------------------------

class SettingsDialog(QDialog):
    """Connection and defaults for Audiobookshelf uploads.

    Credentials (API key or username/password) live in the settings file, which
    is written with 0600 permissions under the user's home.
    """

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.setWindowTitle("Audiobookshelf settings")
        self.setMinimumWidth(520)
        self.libraries: list[dict] = []

        abs_conf = dict(app.settings.get("abs") or {})
        # What the dialog was opened with. The library and folder combos are
        # populated from a cached listing, so they can legitimately be empty
        # (no test ever run, or the listing has since changed); saving must not
        # turn that into "forget the target", which would strand an existing
        # configuration.
        self._saved: dict = abs_conf

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.enabled_check = QCheckBox("Uploads enabled")
        self.enabled_check.setChecked(bool(abs_conf.get("enabled", False)))
        form.addRow(self.enabled_check)

        self.url_edit = QLineEdit(abs_conf.get("url", ""))
        form.addRow("URL", self.url_edit)

        self.api_key_edit = QLineEdit(abs_conf.get("api_key", ""))
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        form.addRow("API key", self.api_key_edit)
        hint = QLabel("An API key alone is enough; a username/password is only "
                      "used when the key is blank.")
        hint.setStyleSheet("color: #90a0b0;")
        hint.setWordWrap(True)
        form.addRow("", hint)

        self.username_edit = QLineEdit(abs_conf.get("username", ""))
        form.addRow("Username", self.username_edit)

        self.password_edit = QLineEdit(abs_conf.get("password", ""))
        self.password_edit.setEchoMode(QLineEdit.Password)
        form.addRow("Password", self.password_edit)

        self.insecure_check = QCheckBox("Accept self-signed TLS (insecure, for LAN setups)")
        self.insecure_check.setChecked(bool(abs_conf.get("insecure_tls", False)))
        form.addRow(self.insecure_check)

        meta_title = QLabel("Metadata defaults")
        meta_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(meta_title)
        self.default_author_edit = QLineEdit(app.settings.get("default_author", ""))
        form.addRow("Default author", self.default_author_edit)
        self.default_series_edit = QLineEdit(app.settings.get("default_series", ""))
        form.addRow("Default series", self.default_series_edit)

        lib_title = QLabel("Library")
        lib_title.setStyleSheet("font-weight: bold;")
        layout.addWidget(lib_title)
        self.lib_combo = QComboBox()
        self.lib_combo.currentIndexChanged.connect(self._library_chosen)
        form.addRow("Library", self.lib_combo)
        self.folder_combo = QComboBox()
        form.addRow("Folder", self.folder_combo)

        self.test_btn = QPushButton("Test connection & load libraries")
        self.test_btn.clicked.connect(self._load_libraries)
        layout.addWidget(self.test_btn)

        self.lib_status = QLabel()
        self.lib_status.setStyleSheet("color: #90a0b0;")
        self.lib_status.setWordWrap(True)
        layout.addWidget(self.lib_status)

        self.scan_after_check = QCheckBox("Ask the server to rescan after uploading")
        self.scan_after_check.setChecked(bool(abs_conf.get("scan_after", True)))
        layout.addWidget(self.scan_after_check)
        self.upload_after_check = QCheckBox("Upload to Audiobookshelf after compression")
        self.upload_after_check.setChecked(bool(abs_conf.get("upload_after", False)))
        layout.addWidget(self.upload_after_check)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setDefault(True)
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

        self._prefill()

    def _prefill(self) -> None:
        """Fill the library and folder combos without contacting the server.

        The listing comes from the cache written by the last successful test, so
        reopening this dialog shows the current target immediately instead of
        asking for a connection test on every visit.
        """
        libs = list(getattr(self.app, "_abs_libraries", []) or [])
        if not libs:
            libs = libraries_from_cache(self._saved.get("libraries"))
            self.app._abs_libraries = list(libs)
        if libs:
            self.libraries = libs
            self._repopulate_libraries(select=str(self._saved.get("library_id") or ""))
        self._show_lib_status()

    def _remembered_folder(self, lib_id: str) -> str:
        """The folder to select for `lib_id`.

        Only ever returns a folder that actually exists in that library, so a
        folder moved between libraries or deleted on the server cannot leave the
        combo pointing at nothing.
        """
        valid = {f.get("id") for f in self._lib(lib_id).get("folders", [])}
        current = self._folder_id()
        if current and current in valid:
            return current
        if lib_id == str(self._saved.get("library_id") or ""):
            saved = str(self._saved.get("folder_id") or "")
            if saved and saved in valid:
                return saved
        by_library = self._saved.get("folder_by_library")
        if isinstance(by_library, dict):
            remembered = str(by_library.get(lib_id) or "")
            if remembered and remembered in valid:
                return remembered
        return ""

    def _collect(self) -> dict:
        # An empty combo means "no listing to choose from", not "clear the
        # selection", so fall back to what is already saved.
        library_id = self._lib_id() or str(self._saved.get("library_id") or "")
        folder_id = self._folder_id() or str(self._saved.get("folder_id") or "")
        out = {
            "enabled": self.enabled_check.isChecked(),
            "url": self.url_edit.text().strip(),
            "api_key": self.api_key_edit.text().strip(),
            "username": self.username_edit.text().strip(),
            "password": self.password_edit.text(),
            "insecure_tls": self.insecure_check.isChecked(),
            "scan_after": self.scan_after_check.isChecked(),
            "upload_after": self.upload_after_check.isChecked(),
            "library_id": library_id,
            "folder_id": folder_id,
            "library_name": self._library_label(library_id),
            "folder_name": self._folder_name(folder_id),
            "libraries": library_cache(self.libraries),
            "folder_by_library": self._folder_memory(folder_id),
            "last_tested": str(self._saved.get("last_tested") or ""),
        }
        return out

    def _lib_id(self) -> str:
        return self.lib_combo.currentData() or ""

    def _folder_id(self) -> str:
        return self.folder_combo.currentData() or ""

    def _library_label(self, lib_id: str) -> str:
        """A library's display name, falling back to one already saved."""
        name = str(self._lib(lib_id).get("name") or "")
        if name and name != lib_id:
            return name
        return str(self._saved.get("library_name") or "")

    def _folder_name(self, folder_id: str) -> str:
        """A folder's path, preferring one already saved."""
        for lib in self.libraries:
            for folder in lib.get("folders", []):
                if folder.get("id") == folder_id:
                    return str(folder.get("fullPath") or "")
        return str(self._saved.get("folder_name") or "")

    def _folder_memory(self, folder_id: str) -> dict:
        """The library -> folder map, with the current choice recorded in it."""
        by_library = self._saved.get("folder_by_library")
        out = {}
        if isinstance(by_library, dict):
            out = {str(k): str(v) for k, v in by_library.items()
                   if isinstance(k, str) and v}
        if self._lib_id() and folder_id:
            out[self._lib_id()] = folder_id
        return out

    def _show_lib_status(self) -> None:
        """Say where the current listing came from, so a stale one is obvious."""
        target = f"{self._library_label(self._lib_id()) or 'unknown library'}" \
                 f"  /  {self._folder_name(self._folder_id()) or 'unknown folder'}"
        if not self.libraries:
            # No listing, but the ids are all an upload actually needs, so a
            # saved target still works: say so instead of implying otherwise.
            saved = str(self._saved.get("library_id") or "")
            self.lib_status.setText(
                f"{target}\nSaved from an earlier connection test. Uploads will "
                f"still work; press the button above to refresh this list."
                if saved else
                "No libraries loaded yet - press the button above to connect.")
            return
        when = str(self._saved.get("last_tested") or "")
        if when:
            try:
                stamp = datetime.datetime.fromisoformat(when).strftime(
                    "%Y-%m-%d %H:%M")
            except ValueError:
                stamp = when
            self.lib_status.setText(
                f"{target}\nSaved from the last connection test ({stamp}). "
                f"Test again to refresh it.")
        else:
            self.lib_status.setText(target)

    def _load_libraries(self) -> None:
        self.test_btn.setEnabled(False)
        self.test_btn.setText("Connecting...")
        QApplication.processEvents()
        try:
            client = abs_mod.build_client({"abs": self._collect()})
            name = client.validate()
            libs = list(client.fetch_libraries())
        except Exception as exc:
            self._show_lib_status()
            QMessageBox.critical(self, "Audiobookshelf", f"Could not connect:\n{exc}")
            return
        finally:
            self.test_btn.setEnabled(True)
            self.test_btn.setText("Test connection & load libraries")
        self.libraries = libs
        self.app._abs_libraries = libs

        # Repopulate first, then read the selection back out of the widgets:
        # the names and the remembered-folder map have to be captured from the
        # state the combos are actually in, not the state they were in before
        # the fresh listing arrived.
        self._repopulate_libraries(
            select=self._lib_id() or str(self._saved.get("library_id") or ""))

        # Keep the listing, the selection and the timestamp, and write them
        # through straight away. A connection test only reads, and persisting
        # it is the whole point: the next open of this dialog should not need
        # the server at all.
        collected = self._collect()
        collected["last_tested"] = datetime.datetime.now().replace(
            microsecond=0).isoformat()
        self.app.settings["abs"] = collected
        self._saved = dict(collected)
        try:
            settings_mod.save_settings(self.app.settings)
        except OSError as exc:
            self.app._log(f"could not save settings: {exc}\n", "err")

        self._show_lib_status()
        self.app._log(
            f"Audiobookshelf connected as {name!r}; "
            f"{len(libs)} librar{'y' if len(libs) == 1 else 'ies'} loaded\n", "mut")
        QMessageBox.information(
            self, "Audiobookshelf",
            f"Connected as {name!r}.\n{len(libs)} book "
            f"librar{'y' if len(libs) == 1 else 'ies'} found.\n\n"
            f"Uploading into:\n{collected['library_name'] or 'the library'}"
            f" / {collected['folder_name'] or 'no folder'}\n\n"
            "This has been saved; you will not need to test again.")

    def _repopulate_libraries(self, select: str = "") -> None:
        self.lib_combo.blockSignals(True)
        self.lib_combo.clear()
        for lib in self.libraries:
            self.lib_combo.addItem(f"{lib['name']}  ({lib['id'][:8]})", lib["id"])
        self.lib_combo.blockSignals(False)
        if not self.libraries:
            return
        index = self.lib_combo.findData(select) if select else 0
        if index < 0:
            index = 0
        self.lib_combo.setCurrentIndex(index)
        self._refresh_folders(self.lib_combo.currentData())

    def _lib(self, lib_id: str) -> dict:
        return next((l for l in self.libraries if l["id"] == lib_id),
                    {"id": lib_id, "name": lib_id, "folders": []})

    def _library_chosen(self, *_args) -> None:
        lib_id = self._lib_id()
        if lib_id:
            self._refresh_folders(lib_id)
        self._show_lib_status()

    def _refresh_folders(self, lib_id: str) -> None:
        lib = self._lib(lib_id)
        wanted = self._remembered_folder(lib_id)
        self.folder_combo.blockSignals(True)
        self.folder_combo.clear()
        for folder in lib.get("folders", []):
            self.folder_combo.addItem(
                f"{folder.get('fullPath', '?')}  ({folder['id'][:8]})", folder["id"])
        # Fall back to the first folder only when the remembered one is gone.
        # A library here can hold many folders, and silently landing uploads in
        # the wrong one is worse than making the user look.
        index = self.folder_combo.findData(wanted) if wanted else -1
        if index < 0:
            index = 0 if self.folder_combo.count() else -1
        if index >= 0:
            self.folder_combo.setCurrentIndex(index)
        self.folder_combo.blockSignals(False)

    def _save(self) -> None:
        self.app.settings["abs"] = self._collect()
        self.app.settings["default_author"] = self.default_author_edit.text().strip()
        self.app.settings["default_series"] = self.default_series_edit.text().strip()
        # Widgets first: _save_settings persists what the widgets hold, so
        # pushing the dialog values in before saving keeps them in sync.
        self.app.upload_after_check.setChecked(self.app.settings["abs"]["upload_after"])
        self.app._save_settings()
        self.app._log("Audiobookshelf settings saved\n", "mut")
        self.accept()


# --------------------------------------------------------------------------
# before/after preview
# --------------------------------------------------------------------------

class UploadDialog(QDialog):
    """Review and send books to Audiobookshelf.

    Every book's metadata is read out of its own EPUB before the dialog opens,
    and the row it will be stored under is derived from that. EPUB metadata is
    wrong often enough that the cells stay editable: correcting an author here
    is far cheaper than finding the mistake in the library afterwards.

    Books that have already been compressed upload the compressed file; books
    that have not upload exactly what is on disk. Compression state is
    irrelevant to whether a book can be uploaded.
    """

    COL_SOURCE, COL_TITLE, COL_AUTHOR, COL_SERIES, COL_INDEX, \
        COL_NAME, COL_STATUS = range(7)
    HEADERS = ["On disk", "Title", "Author", "Series", "#",
               "Uploads as", "Status"]

    def __init__(self, parent, books: list[tuple[str, dict]], target: str):
        """`books` is [(path, metadata)]; `target` describes the library folder."""
        super().__init__(parent)
        self.setWindowTitle("Upload to Audiobookshelf")
        self.resize(1000, 520)
        self._manual: set[int] = set()      # rows whose filename was typed by hand

        layout = QVBoxLayout(self)
        intro = QLabel(
            f"Uploading to {target}. Metadata is read from each book itself; "
            "edit anything that is wrong before sending.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #90a0b0;")
        layout.addWidget(intro)

        self.table = QTableWidget(len(books), len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setEditTriggers(QTableWidget.DoubleClicked
                                   | QTableWidget.SelectedClicked
                                   | QTableWidget.EditKeyPressed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_SOURCE, QHeaderView.Interactive)
        header.setSectionResizeMode(self.COL_TITLE, QHeaderView.Stretch)
        header.setSectionResizeMode(self.COL_AUTHOR, QHeaderView.Stretch)
        header.setSectionResizeMode(self.COL_SERIES, QHeaderView.Stretch)
        header.setSectionResizeMode(self.COL_INDEX, QHeaderView.Interactive)
        header.setSectionResizeMode(self.COL_NAME, QHeaderView.Stretch)
        header.setSectionResizeMode(self.COL_STATUS, QHeaderView.Interactive)
        self.table.setColumnWidth(self.COL_SOURCE, 190)
        self.table.setColumnWidth(self.COL_INDEX, 50)
        self.table.setColumnWidth(self.COL_STATUS, 170)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, 1)

        self._books = books
        # Filling the table fires itemChanged for every cell; the derived name
        # column does not exist yet for those, so the handler stays quiet until
        # the whole grid is in place.
        self._loading = True
        try:
            for row, (path, meta) in enumerate(books):
                self.table.setItem(row, self.COL_SOURCE,
                                   self._cell(os.path.basename(path),
                                              editable=False))
                self.table.setItem(row, self.COL_TITLE,
                                   self._cell(str(meta.get("title") or "")))
                self.table.setItem(row, self.COL_AUTHOR,
                                   self._cell(str(meta.get("author") or "")))
                self.table.setItem(row, self.COL_SERIES,
                                   self._cell(str(meta.get("series") or "")))
                self.table.setItem(row, self.COL_INDEX,
                                   self._cell(str(meta.get("series_index") or "")))
                self.table.setItem(row, self.COL_NAME,
                                   self._cell(self._derive(path, meta),
                                              editable=False))
                self.table.setItem(row, self.COL_STATUS,
                                   self._cell("", editable=False))
        finally:
            self._loading = False

        note = QLabel("Audiobookshelf files books into "
                      "<author>/<series>/<title>/ itself, and stores the file "
                      "under the name in “Uploads as”.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #90a0b0;")
        layout.addWidget(note)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_btn = QPushButton("Close")
        self.cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_btn)
        self.go_btn = QPushButton(f"Upload {len(books)} "
                                  f"{'book' if len(books) == 1 else 'books'}")
        self.go_btn.setDefault(True)
        self.go_btn.clicked.connect(self._on_go)
        buttons.addWidget(self.go_btn)
        layout.addLayout(buttons)

    @staticmethod
    def _cell(text: str, editable: bool = True) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setToolTip("Detected from the book; edit the columns to the "
                            "right to change what is sent.")
        return item

    @staticmethod
    def _derive(path: str, meta: dict) -> str:
        return es.derive_upload_name(
            meta, os.path.splitext(os.path.basename(path))[0])

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """Keep the derived filename in step with the metadata, unless the user
        has deliberately set that filename themselves."""
        if self._loading:
            return
        row = item.row()
        if item.column() == self.COL_NAME:
            if item.text().strip():
                self._manual.add(row)
                return
            # Emptying the name hands control back to the metadata rather than
            # leaving the book with nothing to upload as.
            self._manual.discard(row)
            self._refresh_name(row)
            return
        if item.column() not in (self.COL_TITLE, self.COL_AUTHOR,
                                 self.COL_SERIES, self.COL_INDEX):
            return
        self._refresh_name(row)

    def _refresh_name(self, row: int) -> None:
        """Recompute one row's upload name from its metadata."""
        if row in self._manual or row >= len(self._books):
            return
        name_cell = self.table.item(row, self.COL_NAME)
        if name_cell is None:
            return
        name_cell.setText(self._derive(self._books[row][0], self._row_meta(row)))

    def _row_meta(self, row: int) -> dict:
        """The metadata as currently edited in the table."""
        meta = dict(self._books[row][1])
        meta["title"] = self.table.item(row, self.COL_TITLE).text().strip()
        meta["author"] = self.table.item(row, self.COL_AUTHOR).text().strip()
        meta["authors"] = [meta["author"]] if meta["author"] else []
        meta["series"] = self.table.item(row, self.COL_SERIES).text().strip()
        meta["series_index"] = self.table.item(row, self.COL_INDEX).text().strip()
        return meta

    def entries(self) -> list[UploadEntry]:
        """The books to send, with the dialog's edits applied."""
        out = []
        for row, (path, original) in enumerate(self._books):
            out.append(UploadEntry(path=path,
                                   meta=self._row_meta(row),
                                   filename=self.table.item(
                                       row, self.COL_NAME).text().strip(),
                                   disk_title=str(original.get("title") or "").strip()))
        return out

    def duplicate_names(self) -> list[str]:
        """Upload names that appear more than once.

        Audiobookshelf overwrites a file that lands on an existing name, so two
        different books sharing one of these would mean the second silently
        replaces the first.
        """
        seen: dict[str, int] = {}
        for entry in self.entries():
            if entry.filename:
                seen[entry.filename] = seen.get(entry.filename, 0) + 1
        return sorted(name for name, count in seen.items() if count > 1)

    def set_status(self, row: int, status: str, note: str = "") -> None:
        self.table.blockSignals(True)
        try:
            self.table.item(row, self.COL_STATUS).setText(status)
            self.table.item(row, self.COL_STATUS).setToolTip(note)
        finally:
            self.table.blockSignals(False)

    def _on_go(self) -> None:
        blanks = [e for e in self.entries() if not e.title.strip()]
        if blanks:
            QMessageBox.warning(
                self, "Nothing to send",
                "Every book needs a title. Fill in or remove the blank row(s):\n\n"
                + "\n".join(f"· {os.path.basename(e.path)}" for e in blanks))
            return
        clashes = self.duplicate_names()
        if clashes:
            answer = QMessageBox.question(
                self, "Same upload name",
                "These names are used by more than one book. Audiobookshelf "
                "stores one file per name, so all but the last would be "
                "replaced:\n\n"
                + "\n".join(f"· {name}" for name in clashes)
                + "\n\nUpload anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        self.accept()


class PreviewDialog(QDialog):
    """Side-by-side original vs compressed for one book, with PSNR."""

    THUMB = (300, 340)

    def __init__(self, parent, job: Job):
        super().__init__(parent)
        self.job = job
        self.setWindowTitle(f"Preview - {os.path.basename(job.path)}")
        self.resize(880, 540)
        self._photos: list[QPixmap] = []

        layout = QVBoxLayout(self)
        caption = QLabel("Changed images (select one)")
        caption.setStyleSheet("font-weight: bold;")
        layout.addWidget(caption)

        self.list = QTreeWidget()
        self.list.setColumnCount(4)
        self.list.setHeaderLabels(["Image", "Before", "After", "PSNR"])
        self.list.setRootIsDecorated(False)
        self.list.setEditTriggers(QTreeWidget.NoEditTriggers)
        self.list.header().setStretchLastSection(False)
        self.list.header().resizeSection(0, 330)
        self.list.header().resizeSection(1, 100)
        self.list.header().resizeSection(2, 100)
        self.list.header().resizeSection(3, 100)
        self.list.setMaximumHeight(200)
        self.list.currentItemChanged.connect(self._on_select)
        layout.addWidget(self.list)

        pics = QWidget()
        pics_layout = QHBoxLayout(pics)
        self.before_lbl = QLabel()
        self.before_lbl.setAlignment(Qt.AlignCenter)
        self.before_lbl.setMinimumHeight(self.THUMB[1])
        self.after_lbl = QLabel()
        self.after_lbl.setAlignment(Qt.AlignCenter)
        self.after_lbl.setMinimumHeight(self.THUMB[1])
        pics_layout.addWidget(self.before_lbl, 1)
        pics_layout.addWidget(self.after_lbl, 1)
        layout.addWidget(pics, 1)

        self.before_cap = QLabel("before")
        self.after_cap = QLabel("after")
        self.before_cap.setAlignment(Qt.AlignCenter)
        self.after_cap.setAlignment(Qt.AlignCenter)
        for label in (self.before_cap, self.after_cap):
            label.setStyleSheet("color: #90a0b0;")
        caps = QHBoxLayout()
        caps.addWidget(self.before_cap, 1)
        caps.addWidget(self.after_cap, 1)
        layout.addLayout(caps)

        rep = job.report
        changed = [i for i in rep.items if i.changed]
        if not changed:
            empty = QLabel("No images were changed in this run.")
            empty.setStyleSheet("color: #90a0b0;")
            layout.addWidget(empty)
        for item in changed:
            psnr = f"{item.psnr:.1f} dB" if item.psnr is not None else "n/a"
            row = QTreeWidgetItem([
                os.path.basename(item.arcname), human(item.orig_bytes),
                human(item.new_bytes), psnr])
            row.setData(0, Qt.UserRole, item.arcname)
            self.list.addTopLevelItem(row)
        if changed:
            self.list.setCurrentItem(self.list.topLevelItem(0))
            self._on_select()

    def _on_select(self, *_args) -> None:
        row = self.list.currentItem()
        if row is None:
            return
        arcname = row.data(0, Qt.UserRole)
        item = next((i for i in self.job.report.items if i.arcname == arcname), None)
        if not item:
            return
        before = self._read_archive(self.job.path, os.path.basename(item.arcname))
        after = self._read_archive(
            self.job.output, os.path.basename(item.new_name or item.arcname))
        extra = []
        if item.scaled:
            extra.append(f"downscaled {item.width}x{item.height}")
        if item.is_cover:
            extra.append("cover quality")
        cap = f"{os.path.basename(item.arcname)}  {human(item.orig_bytes)}"
        self.before_cap.setText(cap)
        self.after_cap.setText(f"{os.path.basename(item.new_name or item.arcname)}  "
                               f"{human(item.new_bytes)}"
                               + (f"  ({', '.join(extra)})" if extra else ""))
        self._set_pic(self.before_lbl, before)
        self._set_pic(self.after_lbl, after)

    def _read_archive(self, archive: str, basename: str) -> bytes | None:
        import zipfile

        if not archive or not os.path.isfile(archive):
            return None
        try:
            with zipfile.ZipFile(archive) as zf:
                for name in zf.namelist():
                    if os.path.basename(name).lower() == basename.lower():
                        return zf.read(name)
        except (zipfile.BadZipFile, OSError):
            return None
        return None

    def _set_pic(self, label: QLabel, data: bytes | None) -> None:
        label.clear()
        self._photos.clear()
        if not data:
            label.setText("(no image available)")
            return
        try:
            import io

            from PIL import Image

            im = Image.open(io.BytesIO(data))
            im.thumbnail(self.THUMB, Image.LANCZOS)
            pixmap = QPixmap()
            buffer = io.BytesIO()
            im.convert("RGBA").save(buffer, format="PNG")
            pixmap.loadFromData(buffer.getvalue(), "PNG")
            self._photos.append(pixmap)
            label.setPixmap(pixmap)
        except Exception:
            label.setText("(cannot display)")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def zipfile_is_epub(path: str) -> bool:
    """Cheap check that a file is a zip with an EPUB mimetype entry."""
    import zipfile

    try:
        with zipfile.ZipFile(path) as zf:
            return "mimetype" in zf.namelist()
    except Exception:
        return False


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in ("--version", "-V"):
        print("epub-shrink-gui 2.0 (PySide6)")
        return 0
    if args and args[0] == "--self-test":
        # Verifies the bundle can import everything and build the real window
        # without a display. The AppImage build runs this to fail early rather
        # than shipping an image that cannot start.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([sys.argv[0]])
        window = App(argv=[])
        window.show()
        app.processEvents()
        print("self-test OK: window built with", len(window.jobs), "queued jobs")
        window.close()
        return 0
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("epub-shrink")
    window = App(argv=args)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
