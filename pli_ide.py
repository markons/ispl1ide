#!/usr/local/bin/python3.10
"""
Iron Spring PL/I IDE
A simple GUI to edit, compile, link, and run PL/I programs.
"""

import sys, os
if sys.version_info < (3, 10):
    _py = "/usr/local/bin/python3.10"
    if os.path.exists(_py):
        os.execv(_py, [_py] + sys.argv)
    sys.exit(f"Python 3.10+ required (have {sys.version.split()[0]}); "
             f"install it or run:  /usr/local/bin/python3.10 {sys.argv[0]}")

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font
import subprocess
import threading
import os
import sys
import tempfile
import re

HOME   = os.path.expanduser("~")
PLIC   = os.path.join(HOME, "bin", "plic")
ISPP   = "/usr/bin/ispp"
LIBDIR = os.path.join(HOME, "lib")
INCDIR = os.path.join(HOME, "lib", "pli-include")

HELLO_PLI = """ hello: procedure options(main);
   put list('Hello, PL/I world!');
   put skip;
 end hello;
"""

# ── colours ────────────────────────────────────────────────────────────────
BG        = "#1e1e2e"
BG2       = "#181825"
FG        = "#cdd6f4"
ACCENT    = "#89b4fa"
GREEN     = "#a6e3a1"
RED       = "#f38ba8"
YELLOW    = "#f9e2af"
GREY      = "#45475a"
TOOLBAR   = "#313244"
MAGENTA   = "#cba6f7"
TEAL      = "#94e2d5"
PEACH     = "#fab387"
COMMENT   = "#6c7086"
FONT_MONO = ("Consolas", 11)
FONT_UI   = ("Segoe UI", 10)

# Statements that ispp passes through unchanged to plic (not executed by ispp)
_ISPP_PASSTHRU_RE = re.compile(
    r'^\s*%(replace|include|page|skip)\b', re.IGNORECASE)

# Lines in the ispp insource (.ins) listing that have source line content
_INS_SRC_RE = re.compile(r'^\s{1,4}(\d+)\s{2}(.*)', re.DOTALL)

# ── PL/I syntax-highlight tables ───────────────────────────────────────────
_PL1_KEYWORDS = frozenset("""
    PROCEDURE PROC BEGIN END DO TO BY WHILE UNTIL REPEAT
    IF THEN ELSE SELECT WHEN OTHERWISE OTHER
    GO GOTO CALL RETURN STOP EXIT LEAVE ITERATE
    ON SIGNAL REVERT
    DECLARE DCL FORMAT PUT GET OPEN CLOSE READ WRITE REWRITE DELETE
    LIST DATA EDIT SKIP PAGE LINE COLUMN COL
    FILE STREAM RECORD INPUT OUTPUT UPDATE TITLE SET INTO FROM
    ENDFILE ENDPAGE ERROR FINISH OVERFLOW UNDERFLOW
    ZERODIVIDE CONVERSION SUBSCRIPTRANGE STRINGRANGE STRINGSIZE
    UNDEFINEDFILE CONDITION
""".split())

_PL1_TYPES = frozenset("""
    FIXED FLOAT DECIMAL DEC BINARY BIN BIT CHARACTER CHAR PICTURE PIC
    POINTER PTR AREA OFFSET LABEL ENTRY COMPLEX REAL SIGNED UNSIGNED
    VARYING NONVARYING ALIGNED UNALIGNED STATIC AUTOMATIC AUTO
    CONTROLLED CTL BASED DEFINED LIKE UNION DIMENSION DIM
    EXTERNAL EXT INTERNAL OPTIONS RETURNS RECURSIVE IRREDUCIBLE REDUCIBLE
    INITIAL INIT VALUE SEQUENTIAL DIRECT KEYED ENVIRONMENT ENV STRING
    BUFFERED UNBUFFERED CONNECTED MAIN REORDER NOREORDER
    BYADDR BYVALUE HANDLES
""".split())

_PL1_BUILTINS = frozenset("""
    ABS ADDR ACOS ASIN ATAN ATAND ATANH ALLOCATION ALLOC ALLOCATE FREE
    BOOL CEIL COLLATE COMPLETION COPY COS COSH DATE DATETIME DIVIDE
    EXP FLOOR HEX HIGH INDEX LBOUND HBOUND LENGTH LOG LOG2 LOG10 LOW
    MAX MAXLENGTH MIN MOD MULTIPLY NULL SYSNULL NULLSYS
    OFFSET ONCHAR ONCODE ONFILE ONKEY ONSOURCE ONLOC
    POINTER PROD RANK REM REVERSE ROUND SIGN SIN SINH
    SIZE SQRT STORAGE SUBSTR SUM TAN TANH TIME
    TRANSLATE TRIM TRUNC TYPE UNSPEC VALID VERIFY
    ACTUALCOUNT CURRENTSIZE EMPTY WCHARVAL
""".split())

# Master tokeniser: comment > string > preprocessor token > number > identifier
_HL_RE = re.compile(r"""
    (?P<comment>/\*.*?\*/)                              |
    (?P<string>'(?:''|[^'\n])*'|"(?:""|[^"\n])*")      |
    (?P<preproc>%[A-Za-z_]\w*)                          |
    (?P<number>\b[0-9]+(?:\.[0-9]+)?(?:[Ee][+-]?[0-9]+)?\b)  |
    (?P<ident>\b[A-Za-z$#@_][A-Za-z0-9$#@_]*\b)
""", re.VERBOSE | re.DOTALL | re.IGNORECASE)


def _compute_idx_map(text: str, offsets) -> dict[int, str]:
    """Convert a collection of char offsets to tkinter 'line.col' indices in one pass."""
    result: dict[int, str] = {}
    sorted_offs = sorted(set(offsets))
    if not sorted_offs:
        return result
    line, col, pos = 1, 0, 0
    for target in sorted_offs:
        while pos < target:
            if text[pos] == '\n':
                line += 1
                col = 0
            else:
                col += 1
            pos += 1
        result[target] = f"{line}.{col}"
    return result


class FindBar(tk.Frame):
    """Inline find-and-replace bar that lives at the bottom of the editor pane."""

    def __init__(self, master, editor: tk.Text, **kw):
        super().__init__(master, bg=TOOLBAR, **kw)
        self.editor   = editor
        self._matches: list[tuple[str, str]] = []
        self._current = -1
        self._after_id = None
        self._build()

    # ── layout ────────────────────────────────────────────────────────────
    def _build(self):
        def btn(parent, text, cmd, fg=FG, width=0):
            b = tk.Button(
                parent, text=text, command=cmd,
                bg=TOOLBAR, fg=fg, activebackground=GREY,
                activeforeground=FG, relief="flat",
                font=FONT_UI, padx=6, pady=1, cursor="hand2",
            )
            if width:
                b.config(width=width)
            b.pack(side="left", padx=2)
            return b

        # ── row 1: Find ──────────────────────────────────────────────────
        r1 = tk.Frame(self, bg=TOOLBAR, padx=4, pady=3)
        r1.pack(fill="x")

        tk.Label(r1, text="Find:", bg=TOOLBAR, fg=GREY,
                 font=FONT_UI, width=8, anchor="e").pack(side="left")

        self._find_var = tk.StringVar()
        self._find_entry = tk.Entry(
            r1, textvariable=self._find_var,
            bg=BG2, fg=FG, insertbackground=FG,
            font=FONT_MONO, relief="flat", width=30,
        )
        self._find_entry.pack(side="left", padx=(4, 0), ipady=3)
        self._find_var.trace_add("write", lambda *_: self._schedule())
        self._find_entry.bind("<Return>",       lambda e: (self._step(+1), "break"))
        self._find_entry.bind("<Shift-Return>", lambda e: (self._step(-1), "break"))
        self._find_entry.bind("<Escape>",       lambda e: self.hide())

        btn(r1, "▲", lambda: self._step(-1))
        btn(r1, "▼", lambda: self._step(+1))

        self._case_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            r1, text="Aa", variable=self._case_var,
            bg=TOOLBAR, fg=GREY, activebackground=TOOLBAR, activeforeground=FG,
            selectcolor=BG2, font=("Segoe UI", 9),
            command=self._schedule,
        ).pack(side="left", padx=6)

        self._count_var = tk.StringVar(value="")
        tk.Label(r1, textvariable=self._count_var,
                 bg=TOOLBAR, fg=GREY, font=("Segoe UI", 9),
                 width=14, anchor="w").pack(side="left")

        btn(r1, "✕", self.hide, fg=GREY)

        # ── row 2: Replace (hidden until Ctrl+H) ────────────────────────
        self._r2 = tk.Frame(self, bg=TOOLBAR, padx=4, pady=2)
        tk.Label(self._r2, text="Replace:", bg=TOOLBAR, fg=GREY,
                 font=FONT_UI, width=8, anchor="e").pack(side="left")
        self._repl_var = tk.StringVar()
        self._repl_entry = tk.Entry(
            self._r2, textvariable=self._repl_var,
            bg=BG2, fg=FG, insertbackground=FG,
            font=FONT_MONO, relief="flat", width=30,
        )
        self._repl_entry.pack(side="left", padx=(4, 4), ipady=3)
        self._repl_entry.bind("<Return>", lambda e: self._replace_one())
        btn(self._r2, "Replace",     self._replace_one)
        btn(self._r2, "Replace All", self._replace_all)

    # ── public API ─────────────────────────────────────────────────────────
    def show(self, replace=False):
        # Place bar in row 1 of the parent editor_area grid
        self.grid(row=1, column=0, sticky="ew")
        if replace:
            self._r2.pack(fill="x")
        else:
            self._r2.pack_forget()
        self._find_entry.focus_set()
        self._find_entry.select_range(0, "end")
        self._schedule()

    def hide(self):
        self._clear()
        self.grid_remove()      # removes from grid but remembers row/col for next show()
        self.editor.focus_set()

    # ── internals ─────────────────────────────────────────────────────────
    def _schedule(self):
        if self._after_id:
            self.after_cancel(self._after_id)
        self._after_id = self.after(120, self._search)

    def _search(self):
        self._clear()
        needle = self._find_var.get()
        if not needle:
            self._count_var.set("")
            self._find_entry.config(bg=BG2)
            return

        flags  = 0 if self._case_var.get() else re.IGNORECASE
        text   = self.editor.get("1.0", "end-1c")
        spans  = [(m.start(), m.end())
                  for m in re.finditer(re.escape(needle), text, flags)]

        if not spans:
            self._find_entry.config(bg="#3b1a1a")
            self._count_var.set("No matches")
            return

        self._find_entry.config(bg=BG2)
        idx = _compute_idx_map(text, [o for s, e in spans for o in (s, e)])
        self._matches = [(idx[s], idx[e]) for s, e in spans]
        for s, e in self._matches:
            self.editor.tag_add("find_match", s, e)

        # jump to the first match at or after the cursor
        cursor = self.editor.index("insert")
        self._current = -1
        for i, (s, _) in enumerate(self._matches):
            if self.editor.compare(s, ">=", cursor):
                self._current = i - 1
                break
        self._step(+1)

    def _step(self, delta: int):
        if not self._matches:
            return
        n = len(self._matches)
        if 0 <= self._current < n:
            s, e = self._matches[self._current]
            self.editor.tag_remove("find_current", s, e)
            self.editor.tag_add("find_match",   s, e)
        self._current = (self._current + delta) % n
        s, e = self._matches[self._current]
        self.editor.tag_remove("find_match",   s, e)
        self.editor.tag_add("find_current", s, e)
        self.editor.see(s)
        self.editor.mark_set("insert", s)
        self._count_var.set(f"{self._current + 1} / {n}")

    def _clear(self):
        self.editor.tag_remove("find_match",   "1.0", "end")
        self.editor.tag_remove("find_current", "1.0", "end")
        self._matches = []
        self._current = -1
        self._count_var.set("")

    def _replace_one(self):
        if not (0 <= self._current < len(self._matches)):
            return
        s, e = self._matches[self._current]
        self.editor.delete(s, e)
        self.editor.insert(s, self._repl_var.get())
        self._schedule()

    def _replace_all(self):
        self._search()
        if not self._matches:
            return
        repl = self._repl_var.get()
        for s, e in reversed(self._matches):
            self.editor.delete(s, e)
            self.editor.insert(s, repl)
        self._clear()
        self._count_var.set("Replaced all")


class PreprocessorView(tk.Toplevel):
    """Non-modal window showing ispp insource listing and preprocessed output."""

    def __init__(self, master, filename: str, ins_text: str, dek_text: str):
        super().__init__(master)
        self.title(f"Preprocessor — {os.path.basename(filename)}")
        self.geometry("980x700")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.lift()
        self._build(ins_text, dek_text)

    # ── shared text-pane factory ───────────────────────────────────────────
    @staticmethod
    def _make_pane(parent) -> tk.Text:
        hscroll = tk.Scrollbar(parent, orient="horizontal")
        hscroll.pack(side="bottom", fill="x")
        vscroll = tk.Scrollbar(parent, orient="vertical")
        vscroll.pack(side="right", fill="y")
        t = tk.Text(
            parent,
            bg=BG, fg=FG, insertbackground=FG,
            font=FONT_MONO, state="normal",
            wrap="none", cursor="arrow",
            yscrollcommand=vscroll.set,
            xscrollcommand=hscroll.set,
            relief="flat", padx=6, pady=4,
        )
        t.pack(fill="both", expand=True)
        vscroll.config(command=t.yview)
        hscroll.config(command=t.xview)
        return t

    def _build(self, ins_text: str, dek_text: str):
        # ── legend bar ──────────────────────────────────────────────────────
        leg = tk.Frame(self, bg=BG2, padx=8, pady=2)
        leg.pack(fill="x", side="top")
        for color, label in (
            (MAGENTA, "  ▪ ispp statement  "),
            (GREEN,   "  ▪ output source  "),
            (YELLOW,  "  ▪ warning  "),
            (RED,     "  ▪ error  "),
            (GREY,    "  ▪ informational / header  "),
        ):
            tk.Label(leg, text=label, bg=BG2, fg=color,
                     font=("Segoe UI", 8)).pack(side="left")

        # ── notebook with two tabs ───────────────────────────────────────────
        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("PP.TNotebook",        background=TOOLBAR, borderwidth=0)
        style.configure("PP.TNotebook.Tab",    background=TOOLBAR, foreground=GREY,
                        padding=[10, 3], font=("Segoe UI", 9))
        style.map("PP.TNotebook.Tab",
                  background=[("selected", BG)],
                  foreground=[("selected", FG)])

        nb = ttk.Notebook(self, style="PP.TNotebook")
        nb.pack(fill="both", expand=True)

        ins_frame = tk.Frame(nb, bg=BG)
        dek_frame = tk.Frame(nb, bg=BG)
        nb.add(ins_frame, text="  Insource listing (.ins)  ")
        nb.add(dek_frame, text="  Preprocessed output (.dek)  ")

        # ── insource tab ─────────────────────────────────────────────────────
        ins_widget = self._make_pane(ins_frame)
        ins_widget.tag_config("hdr",    foreground=GREY)
        ins_widget.tag_config("pp",     foreground=MAGENTA)
        ins_widget.tag_config("src",    foreground=FG)
        ins_widget.tag_config("error",  foreground=RED)
        ins_widget.tag_config("warn",   foreground=YELLOW)
        ins_widget.tag_config("info",   foreground=GREY)

        _DIAG = re.compile(r'^\s*\d+\s+\((ERR|WRN|INF)(\d+)\)')
        for raw in ins_text.splitlines():
            line = raw.rstrip()
            m = _INS_SRC_RE.match(raw)
            if m:
                content = m.group(2).rstrip()
                s = content.strip()
                if s.startswith('%') and not _ISPP_PASSTHRU_RE.match(s):
                    ins_widget.insert("end", line + "\n", "pp")
                else:
                    ins_widget.insert("end", line + "\n", "src")
            elif _DIAG.match(raw):
                kind = _DIAG.match(raw).group(1)
                tag = "error" if kind == "ERR" else ("warn" if kind == "WRN" else "info")
                ins_widget.insert("end", line + "\n", tag)
            else:
                ins_widget.insert("end", line + "\n", "hdr")
        ins_widget.config(state="disabled")

        # ── dek tab ──────────────────────────────────────────────────────────
        dek_widget = self._make_pane(dek_frame)
        dek_widget.tag_config("src", foreground=GREEN)
        for i, raw in enumerate(dek_text.splitlines(), 1):
            dek_widget.insert("end", f"{i:4d}  {raw.rstrip()}\n", "src")
        dek_widget.config(state="disabled")


class LineNumbers(tk.Canvas):
    """Canvas that draws line numbers and diagnostic markers alongside a Text widget."""

    def __init__(self, master, textwidget, **kw):
        super().__init__(master, **kw)
        self.textwidget = textwidget
        self.diagnostics: dict[int, str] = {}   # lineno → "error" | "warn"
        self.config(bg=BG2, highlightthickness=0)

    def redraw(self):
        self.delete("all")
        i = self.textwidget.index("@0,0")
        while True:
            dline = self.textwidget.dlineinfo(i)
            if dline is None:
                break
            y      = dline[1]
            h      = dline[3]
            lineno = int(str(i).split(".")[0])
            sev    = self.diagnostics.get(lineno)

            # coloured marker strip on the left edge
            if sev == "error":
                self.create_rectangle(0, y, 4, y + h, fill=RED,    outline="")
                num_colour = RED
            elif sev == "warn":
                self.create_rectangle(0, y, 4, y + h, fill=YELLOW, outline="")
                num_colour = YELLOW
            else:
                num_colour = GREY

            self.create_text(
                6, y, anchor="nw", text=str(lineno),
                fill=num_colour, font=FONT_MONO
            )
            next_i = self.textwidget.index(f"{i}+1line")
            if next_i == i:   # couldn't advance — we're past the last line
                break
            i = next_i


class PliIDE(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Iron Spring PL/I IDE")
        self.geometry("1000x720")
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.current_file: str | None = None
        self.modified = False
        self._work_dir = tempfile.mkdtemp(prefix="pli_ide_")
        self._proc: subprocess.Popen | None = None
        self._diagnostics: dict[int, str] = {}   # lineno → "error" | "warn"
        self._hl_after: str | None = None

        self._build_ui()
        self._new_file()
        self.after(100, self._update_line_numbers)

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_toolbar()
        self._build_panes()
        self._build_statusbar()

    def _build_toolbar(self):
        tb = tk.Frame(self, bg=TOOLBAR, pady=4)
        tb.pack(fill="x", side="top")

        def btn(text, cmd, color=FG, tip=""):
            b = tk.Button(
                tb, text=text, command=cmd,
                bg=TOOLBAR, fg=color, activebackground=GREY,
                activeforeground=FG, relief="flat",
                font=FONT_UI, padx=10, pady=2, cursor="hand2",
            )
            b.pack(side="left", padx=2)
            return b

        btn("  New  ",       self._new_file)
        btn("  Open  ",      self._open_file)
        btn("  Save  ",      self._save_file)

        tk.Frame(tb, bg=GREY, width=1).pack(side="left", fill="y", padx=6, pady=4)

        btn("  Preprocess  ", self._preprocess, color=MAGENTA)

        tk.Frame(tb, bg=GREY, width=1).pack(side="left", fill="y", padx=6, pady=4)

        btn("  Compile  ",   self._compile,       color=YELLOW)
        btn("  Link  ",      self._link,           color=ACCENT)
        btn("  Run  ",       self._run,            color=GREEN)

        tk.Frame(tb, bg=GREY, width=1).pack(side="left", fill="y", padx=6, pady=4)

        btn("  Build & Run  ", self._build_and_run, color=GREEN)

        tk.Frame(tb, bg=GREY, width=1).pack(side="left", fill="y", padx=6, pady=4)

        btn("  Kill  ",      self._kill_proc,      color=RED)

        # version label on right
        tk.Label(
            tb, text="Iron Spring PL/I 1.4.1",
            bg=TOOLBAR, fg=GREY, font=("Segoe UI", 9)
        ).pack(side="right", padx=12)

    def _build_panes(self):
        paned = tk.PanedWindow(self, orient="vertical", bg=BG,
                               sashrelief="flat", sashwidth=4,
                               sashpad=0)
        paned.pack(fill="both", expand=True)

        # ── editor frame ──────────────────────────────────────────────────
        edit_frame = tk.Frame(paned, bg=BG)
        paned.add(edit_frame, minsize=200)

        # Line-number gutter on the far left (pack)
        self.line_numbers = LineNumbers(edit_frame, None, width=46)
        self.line_numbers.pack(side="left", fill="y")

        # editor_area uses grid so FindBar can be inserted/removed cleanly
        #
        #   col 0 (weight=1)  | col 1 (weight=0)
        #   ──────────────────┼──────────────────
        #   editor  (row 0)   │  v-scrollbar
        #   FindBar (row 1)   │  (hidden until Ctrl+F)
        #   h-scrollbar (row 2, columnspan 2)
        #
        editor_area = tk.Frame(edit_frame, bg=BG)
        editor_area.pack(side="left", fill="both", expand=True)
        editor_area.grid_rowconfigure(0, weight=1)
        editor_area.grid_rowconfigure(1, weight=0)
        editor_area.grid_rowconfigure(2, weight=0)
        editor_area.grid_columnconfigure(0, weight=1)
        editor_area.grid_columnconfigure(1, weight=0)

        editor_scroll  = tk.Scrollbar(editor_area, orient="vertical")
        editor_hscroll = tk.Scrollbar(editor_area, orient="horizontal")
        editor_scroll.grid( row=0, column=1, sticky="ns")
        editor_hscroll.grid(row=2, column=0, columnspan=2, sticky="ew")

        self._editor_scroll = editor_scroll

        self.editor = tk.Text(
            editor_area,
            bg=BG, fg=FG, insertbackground=FG,
            selectbackground=ACCENT, selectforeground=BG2,
            font=FONT_MONO, undo=True,
            wrap="none",
            yscrollcommand=self._on_editor_scroll,
            xscrollcommand=editor_hscroll.set,
            relief="flat", padx=6, pady=4,
            tabs=("28",),
        )
        self.editor.grid(row=0, column=0, sticky="nsew")
        editor_scroll.config(command=self._on_editor_yscroll)
        editor_hscroll.config(command=self.editor.xview)

        self.line_numbers.textwidget = self.editor
        self.editor.bind("<<Modified>>",       self._on_text_modified)
        self.editor.bind("<KeyRelease>",        lambda e: self._update_line_numbers())
        self.editor.bind("<ButtonRelease>",     lambda e: self._update_line_numbers())
        self.editor.bind("<Configure>",         lambda e: self._update_line_numbers())
        self.editor.bind("<Control-s>",         lambda e: self._save_file())
        self.editor.bind("<Control-z>",         lambda e: self.editor.edit_undo())
        self.editor.bind("<Control-y>",         lambda e: self.editor.edit_redo())
        self.editor.bind("<Tab>",               self._insert_tab)
        self.editor.bind("<Control-f>",         lambda e: self._show_find()    or "break")
        self.editor.bind("<Control-h>",         lambda e: self._show_replace() or "break")
        self.editor.bind("<F3>",                lambda e: self.find_bar._step(+1) or "break")
        self.editor.bind("<Shift-F3>",          lambda e: self.find_bar._step(-1) or "break")
        self.editor.bind("<Escape>",            lambda e: self.find_bar.hide()
                                                          if self.find_bar.winfo_ismapped()
                                                          else None)

        # FindBar lives in row 1 of editor_area (hidden until Ctrl+F / Ctrl+H)
        self.find_bar = FindBar(editor_area, self.editor)

        # ── syntax-highlight tags (lowest priority) ───────────────────────
        self.editor.tag_config("hl_comment", foreground=COMMENT,
                               font=(*FONT_MONO, "italic"))
        self.editor.tag_config("hl_string",  foreground=GREEN)
        self.editor.tag_config("hl_preproc", foreground=YELLOW)
        self.editor.tag_config("hl_number",  foreground=PEACH)
        self.editor.tag_config("hl_keyword", foreground=ACCENT)
        self.editor.tag_config("hl_type",    foreground=MAGENTA)
        self.editor.tag_config("hl_builtin", foreground=TEAL)

        # ── output frame ──────────────────────────────────────────────────
        out_frame = tk.Frame(paned, bg=BG2)
        paned.add(out_frame, minsize=120)

        hdr = tk.Frame(out_frame, bg=TOOLBAR)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Output", bg=TOOLBAR, fg=GREY,
                 font=("Segoe UI", 9, "bold"), padx=8, pady=3).pack(side="left")
        tk.Button(hdr, text="Clear", command=self._clear_output,
                  bg=TOOLBAR, fg=GREY, activebackground=GREY,
                  activeforeground=FG, relief="flat",
                  font=("Segoe UI", 9), padx=6, pady=1,
                  cursor="hand2").pack(side="right", padx=4, pady=2)

        out_scroll = tk.Scrollbar(out_frame, orient="vertical")
        out_scroll.pack(side="right", fill="y")

        self.output = tk.Text(
            out_frame,
            bg=BG2, fg=FG, insertbackground=FG,
            font=FONT_MONO, state="disabled",
            wrap="word",
            yscrollcommand=out_scroll.set,
            relief="flat", padx=6, pady=4,
        )
        self.output.pack(fill="both", expand=True)
        out_scroll.config(command=self.output.yview)

        # colour tags for output
        self.output.tag_config("info",    foreground=ACCENT)
        self.output.tag_config("ok",      foreground=GREEN)
        self.output.tag_config("error",   foreground=RED)
        self.output.tag_config("warn",    foreground=YELLOW)
        self.output.tag_config("prog",    foreground=FG)
        self.output.tag_config("cmd",     foreground=GREY)
        self.output.tag_config("section", foreground=ACCENT, font=("Consolas", 11, "bold"))
        self.output.tag_config("diag_link_err",  foreground=RED,    underline=True)
        self.output.tag_config("diag_link_warn", foreground=YELLOW, underline=True)

        # diagnostic tags (medium priority — over syntax highlight)
        self.editor.tag_config("err_line",  background="#3b1a1a")
        self.editor.tag_config("warn_line", background="#2e2a12")
        # find tags (highest priority — over everything)
        self.editor.tag_config("find_match",    background="#544544", foreground=FG)
        self.editor.tag_config("find_current",  background=YELLOW,    foreground=BG2)

    def _build_statusbar(self):
        sb = tk.Frame(self, bg=TOOLBAR, pady=2)
        sb.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Ready")
        tk.Label(sb, textvariable=self.status_var,
                 bg=TOOLBAR, fg=GREY, font=("Segoe UI", 9),
                 anchor="w", padx=8).pack(side="left")
        self.cursor_var = tk.StringVar(value="Ln 1, Col 1")
        tk.Label(sb, textvariable=self.cursor_var,
                 bg=TOOLBAR, fg=GREY, font=("Segoe UI", 9),
                 padx=12).pack(side="right")
        self.editor.bind("<KeyRelease>",    lambda e: self._update_cursor(), add="+")
        self.editor.bind("<ButtonRelease>", lambda e: self._update_cursor(), add="+")

    # ── scrolling helpers ──────────────────────────────────────────────────

    def _on_editor_scroll(self, *args):
        self._editor_scroll.set(*args)
        self.line_numbers.redraw()

    def _on_editor_yscroll(self, *args):
        self.editor.yview(*args)
        self.line_numbers.redraw()

    # ── text helpers ───────────────────────────────────────────────────────

    def _insert_tab(self, event):
        self.editor.insert("insert", "   ")
        return "break"

    def _on_text_modified(self, event):
        if self.editor.edit_modified():
            self.modified = True
            self._update_title()
            self.editor.edit_modified(False)
            self._update_line_numbers()
            self._schedule_highlight()

    def _update_line_numbers(self):
        self.line_numbers.redraw()

    def _update_title(self):
        name = os.path.basename(self.current_file) if self.current_file else "untitled.pli"
        mark = "● " if self.modified else ""
        self.title(f"{mark}{name} — Iron Spring PL/I IDE")

    def _update_cursor(self):
        pos = self.editor.index("insert")
        ln, col = pos.split(".")
        self.cursor_var.set(f"Ln {ln}, Col {int(col)+1}")

    # ── find / replace ─────────────────────────────────────────────────────

    def _show_find(self):
        self.find_bar.show(replace=False)

    def _show_replace(self):
        self.find_bar.show(replace=True)

    # ── syntax highlighting ────────────────────────────────────────────────

    def _schedule_highlight(self):
        if self._hl_after:
            self.after_cancel(self._hl_after)
        self._hl_after = self.after(200, self._highlight_all)

    def _highlight_all(self):
        text = self.editor.get("1.0", "end-1c")
        for tag in ("hl_comment", "hl_string", "hl_preproc",
                    "hl_number",  "hl_keyword", "hl_type", "hl_builtin"):
            self.editor.tag_remove(tag, "1.0", "end")

        items: list[tuple[int, int, str]] = []
        for m in _HL_RE.finditer(text):
            kind = m.lastgroup
            if kind == "comment":
                tag = "hl_comment"
            elif kind == "string":
                tag = "hl_string"
            elif kind == "preproc":
                tag = "hl_preproc"
            elif kind == "number":
                tag = "hl_number"
            elif kind == "ident":
                w = m.group().upper()
                if   w in _PL1_KEYWORDS: tag = "hl_keyword"
                elif w in _PL1_TYPES:    tag = "hl_type"
                elif w in _PL1_BUILTINS: tag = "hl_builtin"
                else: continue
            else:
                continue
            items.append((m.start(), m.end(), tag))

        if not items:
            return
        idx = _compute_idx_map(text, {o for s, e, _ in items for o in (s, e)})
        for s, e, tag in items:
            self.editor.tag_add(tag, idx[s], idx[e])

    # ── output helpers ─────────────────────────────────────────────────────

    def _write(self, text, tag="prog"):
        self.output.config(state="normal")
        self.output.insert("end", text, tag)
        self.output.see("end")
        self.output.config(state="disabled")

    def _writeln(self, text, tag="prog"):
        self._write(text + "\n", tag)

    def _clear_output(self):
        self.output.config(state="normal")
        self.output.delete("1.0", "end")
        self.output.config(state="disabled")

    def _section(self, title):
        self._writeln(f"\n── {title} {'─' * (48 - len(title))}", "section")

    def _set_status(self, msg):
        self.status_var.set(msg)
        self.update_idletasks()

    # ── file operations ────────────────────────────────────────────────────

    def _new_file(self):
        if not self._confirm_discard():
            return
        self.current_file = None
        self.modified = False
        self._clear_diagnostics()
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", HELLO_PLI)
        self.editor.edit_modified(False)
        self.modified = False
        self._update_title()
        self._update_line_numbers()
        self._schedule_highlight()

    def _open_file(self):
        if not self._confirm_discard():
            return
        path = filedialog.askopenfilename(
            title="Open PL/I source",
            filetypes=[("PL/I source", "*.pli *.pl1 *.PLI"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path) as f:
                src = f.read()
        except OSError as e:
            messagebox.showerror("Open error", str(e))
            return
        self.current_file = path
        self.modified = False
        self._clear_diagnostics()
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", src)
        self.editor.edit_modified(False)
        self._update_title()
        self._update_line_numbers()
        self._schedule_highlight()

    def _save_file(self, ask=False):
        if ask or not self.current_file:
            path = filedialog.asksaveasfilename(
                title="Save PL/I source",
                defaultextension=".pli",
                filetypes=[("PL/I source", "*.pli *.pl1"), ("All files", "*.*")],
            )
            if not path:
                return False
            self.current_file = path
        try:
            with open(self.current_file, "w") as f:
                f.write(self.editor.get("1.0", "end-1c"))
        except OSError as e:
            messagebox.showerror("Save error", str(e))
            return False
        self.modified = False
        self._update_title()
        return True

    def _confirm_discard(self):
        if not self.modified:
            return True
        ans = messagebox.askyesnocancel(
            "Unsaved changes",
            "Save changes before continuing?",
        )
        if ans is None:
            return False
        if ans:
            return self._save_file()
        return True

    # ── build helpers ──────────────────────────────────────────────────────

    def _ensure_saved(self) -> str | None:
        """Return the source file path, saving if necessary."""
        if self.modified or not self.current_file:
            if not self._save_file():
                return None
        return self.current_file

    def _stem(self, path: str) -> str:
        return os.path.splitext(path)[0]

    def _run_cmd(self, cmd: list[str], cwd: str, tag_out="prog", tag_err="error") -> tuple[int, str, str]:
        """Run a command synchronously, streaming to the output pane."""
        env = os.environ.copy()
        env["PATH"] = os.path.join(HOME, "bin") + ":" + env.get("PATH", "")
        self._writeln("$ " + " ".join(cmd), "cmd")
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, env=env,
                capture_output=True, text=True,
            )
        except FileNotFoundError as e:
            self._writeln(str(e), "error")
            return -1, "", str(e)

        if proc.stdout:
            for line in proc.stdout.splitlines():
                tag = "warn" if ("warning" in line.lower() or "W:" in line) else tag_out
                self._writeln(line, tag)
        if proc.stderr:
            for line in proc.stderr.splitlines():
                tag = "warn" if ("warning" in line.lower()) else tag_err
                self._writeln(line, tag)
        return proc.returncode, proc.stdout, proc.stderr

    # ── preprocessor ──────────────────────────────────────────────────────

    def _needs_ispp(self, source: str) -> bool:
        """Return True if source contains ispp-only statements (not just %replace/%include)."""
        for line in source.splitlines():
            s = line.strip()
            if s.startswith('%') and not _ISPP_PASSTHRU_RE.match(s):
                return True
        return False

    def _build_ispp_linemap(self, ins_path: str) -> dict[int, int]:
        """
        Parse an ispp insource (.ins) listing and return a mapping
        dek_lineno → orig_lineno.

        ispp removes its own statements from the output but passes through
        %replace / %include / %page / %skip unchanged.  We reconstruct
        which original lines appear in the .dek (and in what order) so that
        plic diagnostic line numbers can be remapped to the original source.
        """
        dek_map: dict[int, int] = {}
        dek_line = 0
        try:
            with open(ins_path, errors="replace") as f:
                for raw in f:
                    m = _INS_SRC_RE.match(raw)
                    if not m:
                        continue
                    orig_lineno = int(m.group(1))
                    content = m.group(2).rstrip()
                    s = content.strip()
                    # ispp consumes its own statements; passthru ones stay in .dek
                    if s.startswith('%') and not _ISPP_PASSTHRU_RE.match(s):
                        continue
                    dek_line += 1
                    dek_map[dek_line] = orig_lineno
        except OSError:
            pass
        return dek_map

    def _preprocess(self):
        threading.Thread(target=self._do_preprocess, daemon=True).start()

    def _do_preprocess(self):
        src = self._ensure_saved()
        if not src:
            return
        src_dir  = os.path.dirname(src)
        src_base = os.path.basename(src)
        stem     = self._stem(src_base)
        dek_base = stem + ".dek"
        ins_path = os.path.join(src_dir, stem + ".ins")

        self._section("Preprocess")
        self._set_status("Preprocessing…")
        self._clear_diagnostics()

        cmd = [ISPP, "-li", f"-i{INCDIR}", src_base, "-o", dek_base]
        rc, _, _ = self._run_cmd(cmd, cwd=src_dir)

        # show ispp errors/warnings in editor (they reference original line numbers)
        ispp_diags = [d for d in self._parse_listing(ins_path)
                      if d[1] in ("error", "warn")]
        if ispp_diags:
            self._apply_diagnostics(ispp_diags)

        if rc <= 4:
            try:
                ins_text = open(ins_path, errors="replace").read()
                dek_text = open(os.path.join(src_dir, dek_base),
                                errors="replace").read()
            except OSError as e:
                self._writeln(str(e), "error")
                self._set_status("Preprocess error")
                return
            warns = sum(1 for d in ispp_diags if d[1] == "warn")
            self._writeln(
                f"✔  Preprocessed → {dek_base}"
                + (f"  ({warns} warning{'s' if warns != 1 else ''})" if warns else ""),
                "ok",
            )
            self._set_status(f"Preprocessed OK{f'  ({warns}W)' if warns else ''}")
            self.after(0, lambda: PreprocessorView(self, src, ins_text, dek_text))
        else:
            self._writeln("✘  Preprocessing failed", "error")
            self._set_status("Preprocess failed")

    # ── build actions (run in thread so UI stays responsive) ───────────────

    def _compile(self):
        threading.Thread(target=self._do_compile, daemon=True).start()

    def _link(self):
        threading.Thread(target=self._do_link, daemon=True).start()

    def _run(self):
        threading.Thread(target=self._do_run, daemon=True).start()

    def _build_and_run(self):
        threading.Thread(target=self._do_build_and_run, daemon=True).start()

    # ── diagnostic helpers ─────────────────────────────────────────────────

    # Matches:  "2 (ERR208)message"  or  "2 (WRN557)message"
    _DIAG_RE = re.compile(r"^\s*(\d+)\s+\((ERR|WRN)(\d+)\)(.*)")

    def _parse_listing(self, lst_path: str) -> list[tuple[int, str, str, str]]:
        """Return list of (lineno, 'error'|'warn', code, message) from a .lst file."""
        results = []
        try:
            with open(lst_path, errors="replace") as f:
                for raw in f:
                    line = raw.rstrip("\r\n")
                    m = self._DIAG_RE.match(line)
                    if m:
                        lineno = int(m.group(1))
                        sev    = "error" if m.group(2) == "ERR" else "warn"
                        code   = m.group(2) + m.group(3)
                        msg    = m.group(4).strip()
                        results.append((lineno, sev, code, msg))
        except OSError:
            pass
        return results

    def _clear_diagnostics(self):
        self._diagnostics.clear()
        self.line_numbers.diagnostics.clear()
        self.editor.tag_remove("err_line",  "1.0", "end")
        self.editor.tag_remove("warn_line", "1.0", "end")
        self.line_numbers.redraw()

    def _apply_diagnostics(self, items: list[tuple[int, str, str, str]]):
        """Highlight lines in editor, mark gutter, and write clickable output lines."""
        self._clear_diagnostics()
        if not items:
            return
        for lineno, sev, code, msg in items:
            # track worst severity per line
            prev = self._diagnostics.get(lineno)
            if prev != "error":
                self._diagnostics[lineno] = sev
            # highlight editor line
            tag = "err_line" if sev == "error" else "warn_line"
            self.editor.tag_add(tag, f"{lineno}.0", f"{lineno}.0 lineend+1c")
        # push to gutter
        self.line_numbers.diagnostics = dict(self._diagnostics)
        self.line_numbers.redraw()
        # write clickable diagnostics to output
        self._writeln("\nDiagnostics:", "section")
        for lineno, sev, code, msg in items:
            link_tag  = "diag_link_err" if sev == "error" else "diag_link_warn"
            prefix    = f"  {'E' if sev == 'error' else 'W'} ({code}) "
            loc_text  = f"line {lineno}"
            rest_text = f":  {msg}\n"
            self.output.config(state="normal")
            self.output.insert("end", prefix, link_tag)
            # make "line N" clickable
            tag_name = f"goto_{lineno}"
            start = self.output.index("end-1c")
            self.output.insert("end", loc_text, (link_tag, tag_name))
            self.output.tag_bind(tag_name, "<Button-1>",
                                 lambda e, n=lineno: self._goto_line(n))
            self.output.insert("end", rest_text,
                               "error" if sev == "error" else "warn")
            self.output.see("end")
            self.output.config(state="disabled")

    def _goto_line(self, lineno: int):
        self.editor.mark_set("insert", f"{lineno}.0")
        self.editor.see(f"{lineno}.0")
        self.editor.focus_set()

    # ── build steps ────────────────────────────────────────────────────────

    def _do_compile(self) -> bool:
        src = self._ensure_saved()
        if not src:
            return False
        src_dir  = os.path.dirname(src)
        src_base = os.path.basename(src)
        stem     = self._stem(src_base)

        self._section("Compile")
        self._set_status("Compiling…")
        self._clear_diagnostics()

        source_text = self.editor.get("1.0", "end-1c")
        if self._needs_ispp(source_text):
            # ── step 1: ispp ────────────────────────────────────────────────
            dek_base = stem + ".dek"
            ins_path = os.path.join(src_dir, stem + ".ins")
            cmd = [ISPP, "-li", f"-i{INCDIR}", src_base, "-o", dek_base]
            rc_pp, _, _ = self._run_cmd(cmd, cwd=src_dir)
            if rc_pp > 4:
                self._writeln("✘  Preprocessing failed", "error")
                self._set_status("Preprocess failed")
                return False
            self._writeln(f"  ↳ preprocessed → {dek_base}", "cmd")
            compile_input = dek_base
            lst_path      = os.path.join(src_dir, stem + ".lst")
            linemap       = self._build_ispp_linemap(ins_path)
        else:
            compile_input = src_base
            lst_path      = os.path.join(src_dir, stem + ".lst")
            linemap       = {}

        # ── step 2: plic ────────────────────────────────────────────────────
        cmd = [PLIC, "-C", "-ew", f"-i{INCDIR}", compile_input, "-o", stem + ".o"]
        rc, _, _ = self._run_cmd(cmd, cwd=src_dir)

        # parse .lst, remapping line numbers if ispp was used
        raw_diags = self._parse_listing(lst_path)
        if linemap:
            diags = [(linemap.get(ln, ln), sev, code, msg)
                     for ln, sev, code, msg in raw_diags]
        else:
            diags = raw_diags
        self._apply_diagnostics(diags)

        errors = sum(1 for _, s, _, _ in diags if s == "error")
        warns  = sum(1 for _, s, _, _ in diags if s == "warn")

        if rc <= 4:
            self._writeln(
                f"✔  Compiled → {stem}.o"
                + (f"  ({warns} warning{'s' if warns != 1 else ''})" if warns else ""),
                "ok",
            )
            self._set_status(f"Compiled OK{f'  ({warns}W)' if warns else ''}")
            return True
        else:
            self._writeln(
                f"✘  Compile failed  ({errors} error{'s' if errors != 1 else ''}"
                + (f", {warns} warning{'s' if warns != 1 else ''}" if warns else "") + ")",
                "error",
            )
            self._set_status(f"Compile failed  ({errors}E{f', {warns}W' if warns else ''})")
            return False

    def _do_link(self) -> bool:
        src = self._ensure_saved()
        if not src:
            return False
        obj = self._stem(src) + ".o"
        exe = self._stem(src)

        if not os.path.exists(obj):
            self._section("Link")
            self._writeln(f"Object file not found: {obj}", "error")
            self._writeln("Run Compile first.", "warn")
            return False

        self._section("Link")
        self._set_status("Linking…")
        cmd = [
            "gcc", "-m32",
            "-o", exe,
            obj,
            f"-L{LIBDIR}",
            "-lprf",
            "-Wl,-zmuldefs",
        ]
        rc, _, _ = self._run_cmd(cmd, cwd=os.path.dirname(src))
        if rc == 0:
            self._writeln(f"✔  Linked → {os.path.basename(exe)}", "ok")
            self._set_status("Linked OK")
            return True
        else:
            self._writeln("✘  Link failed", "error")
            self._set_status("Link failed")
            return False

    def _do_run(self):
        src = self._ensure_saved()
        if not src:
            return
        exe = self._stem(src)
        if not os.path.exists(exe):
            self._section("Run")
            self._writeln(f"Executable not found: {exe}", "error")
            self._writeln("Run Compile + Link first.", "warn")
            return

        self._section("Run")
        self._set_status("Running…")
        env = os.environ.copy()
        env["PATH"] = os.path.join(HOME, "bin") + ":" + env.get("PATH", "")
        self._writeln(f"$ {exe}", "cmd")

        try:
            self._proc = subprocess.Popen(
                [exe], cwd=os.path.dirname(src), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True,
            )
            for line in self._proc.stdout:
                self._writeln(line.rstrip(), "prog")
            self._proc.wait()
            rc = self._proc.returncode
        except Exception as e:
            self._writeln(str(e), "error")
            self._set_status("Run error")
            return
        finally:
            self._proc = None

        if rc == 0:
            self._writeln(f"\n✔  Exited with code {rc}", "ok")
            self._set_status("Done")
        else:
            self._writeln(f"\n✘  Exited with code {rc}", "error")
            self._set_status(f"Exited {rc}")

    def _do_build_and_run(self):
        if self._do_compile() and self._do_link():
            self._do_run()

    def _kill_proc(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            self._writeln("\n[process killed]", "error")
            self._set_status("Killed")
        else:
            self._set_status("No running process")

    # ── close ──────────────────────────────────────────────────────────────

    def _on_close(self):
        if self._confirm_discard():
            import shutil
            shutil.rmtree(self._work_dir, ignore_errors=True)
            self.destroy()


if __name__ == "__main__":
    app = PliIDE()
    app.mainloop()
