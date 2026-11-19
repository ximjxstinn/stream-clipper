# -*- coding: utf-8 -*-
"""xImJxstinn Stream Clipper – findet automatisch die besten Stellen in deinen Twitch-Streams und schneidet sie."""
import datetime
import json
import os
import queue
import threading
import traceback
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import clipper_core as core
import updater

VERSION = "2.1"
import sys
SETTINGS_FILE = os.path.join(core.APP_DIR, "settings.json")
SESSION_DIR = os.path.join(core.APP_DIR, "gespeichert")

BG = "#0e0e10"      # Twitch-Dunkel
PANEL = "#18181b"
FIELD = "#26262c"
ACCENT = "#9147ff"  # Twitch-Lila
ACCENT2 = "#bf94ff"
TEXT = "#efeff1"
MUTED = "#adadb8"
GOOD = "#00f593"

FORMATS = ["16:9 (YouTube/Twitch)", "9:16 (TikTok/Shorts)", "Beides"]
VSTYLES = {"Blur-Hintergrund": "blur", "Mitte zuschneiden": "crop"}
QUALITIES = ["1080p", "720p", "Beste"]

DEFAULTS = {
    "channel": "ximjxstinn", "count": 10, "pre": 18, "post": 10, "min_gap": 90, "skip_start": 2,
    "w_chat": 65, "audio": True, "fmt": FORMATS[2], "vstyle": "Blur-Hintergrund", "quality": "1080p",
    "bestof": False, "keywords": core.DEFAULT_KEYWORDS,
    "outdir": os.path.join(os.path.expanduser("~"), "Videos", "Stream Clips"),
    "auto": False, "seen": [], "autostart": False, "autoupdate": True, "last_session": "",
}


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    return s


class App:
    def __init__(self, root):
        self.root = root
        self.s = load_settings()
        self.q = queue.Queue()
        self.runner = None
        self.busy = False
        self.highlights = []
        self.curve = []
        self.meta = None
        self.vods = []
        self.auto_job = None

        root.title(f"xImJxstinn Stream Clipper v{VERSION}")
        root.geometry("1180x800")
        root.minsize(980, 680)
        root.configure(bg=BG)
        ico = os.path.join(core.APP_DIR, "icon.ico")
        if os.path.isfile(ico):
            try:
                root.iconbitmap(ico)
            except Exception:
                pass
        self._style()
        self._build()
        self.root.after(100, self._poll)
        self.root.after(400, self._restore_last)
        if self.s.get("autoupdate", True):
            self.root.after(2500, lambda: self.check_updates(silent=True))
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        if self.s.get("auto"):
            self.root.after(3000, self._auto_tick)

    # ------------------------------------------------------------------ UI --
    def _style(self):
        st = ttk.Style()
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=TEXT, fieldbackground=FIELD, font=("Segoe UI", 10))
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=TEXT)
        st.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
        st.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        st.configure("Head.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 18))
        st.configure("Sub.TLabel", background=BG, foreground=ACCENT2, font=("Segoe UI", 10))
        st.configure("Sec.TLabel", background=PANEL, foreground=ACCENT2, font=("Segoe UI Semibold", 11))
        st.configure("TButton", background=FIELD, foreground=TEXT, borderwidth=0, padding=(10, 6))
        st.map("TButton", background=[("active", "#3a3a44"), ("disabled", PANEL)],
               foreground=[("disabled", "#5c5c66")])
        st.configure("Accent.TButton", background=ACCENT, foreground="white", font=("Segoe UI Semibold", 10))
        st.map("Accent.TButton", background=[("active", "#772ce8"), ("disabled", "#3d2a66")])
        st.configure("TCheckbutton", background=PANEL, foreground=TEXT)
        st.map("TCheckbutton", background=[("active", PANEL)])
        st.configure("TRadiobutton", background=PANEL, foreground=TEXT)
        st.map("TRadiobutton", background=[("active", PANEL)])
        st.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT, borderwidth=0)
        st.configure("TSpinbox", fieldbackground=FIELD, foreground=TEXT, arrowcolor=TEXT, borderwidth=0)
        st.configure("TCombobox", fieldbackground=FIELD, foreground=TEXT, arrowcolor=TEXT, borderwidth=0)
        st.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=26, borderwidth=0)
        st.configure("Treeview.Heading", background=FIELD, foreground=MUTED, borderwidth=0)
        st.map("Treeview", background=[("selected", ACCENT)])
        st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD, borderwidth=0)
        st.configure("Horizontal.TScale", background=PANEL, troughcolor=FIELD)

    def _build(self):
        s = self.s
        top = ttk.Frame(self.root, padding=(16, 12, 16, 4))
        top.pack(fill="x")
        ttk.Label(top, text="✂ Stream Clipper", style="Head.TLabel").pack(side="left")
        ttk.Label(top, text="   findet automatisch die besten Stellen in deinen Streams",
                  style="Sub.TLabel").pack(side="left", pady=(8, 0))

        body = ttk.Frame(self.root, padding=(16, 6))
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, style="Panel.TFrame", padding=14)
        left.pack(side="left", fill="y")
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        # ---- Quelle ----
        ttk.Label(left, text="Quelle", style="Sec.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        self.src_kind = tk.StringVar(value="vod")
        ttk.Radiobutton(left, text="Twitch-VOD", variable=self.src_kind, value="vod").grid(row=1, column=0, sticky="w")
        ttk.Radiobutton(left, text="Eigene Aufnahme (Datei)", variable=self.src_kind, value="file").grid(
            row=1, column=1, columnspan=2, sticky="w")

        ttk.Label(left, text="Kanal", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.v_channel = tk.StringVar(value=s["channel"])
        ttk.Entry(left, textvariable=self.v_channel, width=18).grid(row=2, column=1, sticky="we", pady=(8, 0))
        self.b_vods = ttk.Button(left, text="VODs laden", command=self.load_vods)
        self.b_vods.grid(row=2, column=2, sticky="e", padx=(6, 0), pady=(8, 0))

        self.v_vodsel = tk.StringVar()
        self.cb_vods = ttk.Combobox(left, textvariable=self.v_vodsel, state="readonly", width=44)
        self.cb_vods.grid(row=3, column=0, columnspan=3, sticky="we", pady=4)
        self.cb_vods.bind("<<ComboboxSelected>>", self._vod_picked)

        ttk.Label(left, text="oder Link:", style="Panel.TLabel").grid(row=4, column=0, sticky="w")
        self.v_url = tk.StringVar()
        ttk.Entry(left, textvariable=self.v_url).grid(row=4, column=1, columnspan=2, sticky="we")

        ttk.Label(left, text="Datei:", style="Panel.TLabel").grid(row=5, column=0, sticky="w", pady=4)
        self.v_file = tk.StringVar()
        ttk.Entry(left, textvariable=self.v_file).grid(row=5, column=1, sticky="we", pady=4)
        ttk.Button(left, text="…", width=3, command=self._pick_file).grid(row=5, column=2, sticky="e", padx=(6, 0))

        # ---- Einstellungen ----
        ttk.Label(left, text="Einstellungen", style="Sec.TLabel").grid(row=6, column=0, columnspan=3, sticky="w",
                                                                       pady=(14, 2))
        self.v_count = tk.IntVar(value=s["count"])
        self.v_pre = tk.IntVar(value=s["pre"])
        self.v_post = tk.IntVar(value=s["post"])
        self.v_gap = tk.IntVar(value=s["min_gap"])
        self.v_skip = tk.IntVar(value=s["skip_start"])
        rows = [("Anzahl Clips", self.v_count, 1, 50),
                ("Sekunden vor dem Moment", self.v_pre, 3, 120),
                ("Sekunden nach dem Moment", self.v_post, 3, 120),
                ("Mindestabstand (Sek.)", self.v_gap, 10, 1800),
                ("Stream-Anfang ignorieren (Min.)", self.v_skip, 0, 60)]
        r = 7
        for label, var, lo, hi in rows:
            ttk.Label(left, text=label, style="Panel.TLabel").grid(row=r, column=0, columnspan=2, sticky="w", pady=2)
            ttk.Spinbox(left, from_=lo, to=hi, textvariable=var, width=6).grid(row=r, column=2, sticky="e")
            r += 1

        ttk.Label(left, text="Gewichtung", style="Panel.TLabel").grid(row=r, column=0, sticky="w", pady=(6, 0))
        self.v_w = tk.IntVar(value=s["w_chat"])
        self.l_w = ttk.Label(left, style="Muted.TLabel")
        self.l_w.grid(row=r, column=1, columnspan=2, sticky="e", pady=(6, 0))
        r += 1
        ttk.Scale(left, from_=0, to=100, variable=self.v_w, command=lambda e: self._upd_w()).grid(
            row=r, column=0, columnspan=3, sticky="we")
        self._upd_w()
        r += 1
        self.v_audio = tk.BooleanVar(value=s["audio"])
        ttk.Checkbutton(left, text="Lautstärke mit analysieren (Schreie, Lacher)", variable=self.v_audio).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))
        r += 1

        ttk.Label(left, text="Format", style="Panel.TLabel").grid(row=r, column=0, sticky="w", pady=(8, 2))
        self.v_fmt = tk.StringVar(value=s["fmt"])
        ttk.Combobox(left, textvariable=self.v_fmt, values=FORMATS, state="readonly", width=22).grid(
            row=r, column=1, columnspan=2, sticky="we", pady=(8, 2))
        r += 1
        ttk.Label(left, text="TikTok-Stil", style="Panel.TLabel").grid(row=r, column=0, sticky="w", pady=2)
        self.v_vstyle = tk.StringVar(value=s["vstyle"])
        ttk.Combobox(left, textvariable=self.v_vstyle, values=list(VSTYLES), state="readonly", width=22).grid(
            row=r, column=1, columnspan=2, sticky="we", pady=2)
        r += 1
        ttk.Label(left, text="Qualität", style="Panel.TLabel").grid(row=r, column=0, sticky="w", pady=2)
        self.v_quality = tk.StringVar(value=s["quality"])
        ttk.Combobox(left, textvariable=self.v_quality, values=QUALITIES, state="readonly", width=22).grid(
            row=r, column=1, columnspan=2, sticky="we", pady=2)
        r += 1
        self.v_bestof = tk.BooleanVar(value=s["bestof"])
        ttk.Checkbutton(left, text="Zusätzlich Best-of-Video zusammenschneiden", variable=self.v_bestof).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))
        r += 1

        ttk.Label(left, text="Hype-Wörter im Chat (mit Komma)", style="Panel.TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(8, 2))
        r += 1
        self.t_kw = tk.Text(left, height=3, width=40, bg=FIELD, fg=TEXT, insertbackground=TEXT, relief="flat",
                            wrap="word", font=("Segoe UI", 9))
        self.t_kw.insert("1.0", s["keywords"])
        self.t_kw.grid(row=r, column=0, columnspan=3, sticky="we")
        r += 1

        ttk.Label(left, text="Speichern in", style="Panel.TLabel").grid(row=r, column=0, sticky="w", pady=(8, 0))
        self.v_out = tk.StringVar(value=s["outdir"])
        ttk.Entry(left, textvariable=self.v_out).grid(row=r, column=1, sticky="we", pady=(8, 0))
        ttk.Button(left, text="…", width=3, command=self._pick_out).grid(row=r, column=2, sticky="e", padx=(6, 0),
                                                                        pady=(8, 0))
        r += 1
        self.v_auto = tk.BooleanVar(value=s["auto"])
        ttk.Checkbutton(left, text="Auto-Modus: neue Streams selbst clippen (alle 30 Min prüfen)",
                        variable=self.v_auto, command=self._toggle_auto).grid(row=r, column=0, columnspan=3,
                                                                               sticky="w", pady=(10, 0))
        r += 1
        self.v_autostart = tk.BooleanVar(value=s.get("autostart", False))
        ttk.Checkbutton(left, text="Mit Windows starten (läuft im Hintergrund)", variable=self.v_autostart,
                        command=self._toggle_autostart).grid(row=r, column=0, columnspan=3, sticky="w", pady=(2, 0))
        r += 1
        self.v_autoupdate = tk.BooleanVar(value=s.get("autoupdate", True))
        ttk.Checkbutton(left, text="Updates automatisch laden & installieren", variable=self.v_autoupdate,
                        command=self.settings).grid(row=r, column=0, columnspan=3, sticky="w", pady=(2, 0))
        r += 1
        bf = ttk.Frame(left, style="Panel.TFrame")
        bf.grid(row=r, column=0, columnspan=3, sticky="we", pady=(10, 0))
        ttk.Button(bf, text="🔄 Nach Updates suchen", command=lambda: self.check_updates(False)).pack(
            side="left", fill="x", expand=True)
        ttk.Button(bf, text="Tools aktualisieren", command=self.update_tools).pack(side="left", fill="x",
                                                                                   expand=True, padx=(6, 0))
        r += 1
        ttk.Label(left, text=f"Version {VERSION}", style="Muted.TLabel").grid(row=r, column=0, columnspan=3,
                                                                             sticky="w", pady=(6, 0))
        left.columnconfigure(1, weight=1)

        # ---- Rechts: Aktionen ----
        act = ttk.Frame(right)
        act.pack(fill="x")
        self.b_an = ttk.Button(act, text="🔍 1. Analysieren", style="Accent.TButton", command=self.analyze)
        self.b_an.pack(side="left")
        self.b_clip = ttk.Button(act, text="✂ 2. Ausgewählte clippen", command=lambda: self.clip(False))
        self.b_clip.pack(side="left", padx=6)
        self.b_all = ttk.Button(act, text="✂ Alle clippen", command=lambda: self.clip(True))
        self.b_all.pack(side="left")
        self.b_auto = ttk.Button(act, text="⚡ Alles automatisch", style="Accent.TButton", command=self.full_auto)
        self.b_auto.pack(side="left", padx=6)
        self.b_cancel = ttk.Button(act, text="Abbrechen", command=self.cancel, state="disabled")
        self.b_cancel.pack(side="left")
        ttk.Button(act, text="📁 Ordner", command=self._open_out).pack(side="right")
        ttk.Button(act, text="📂 Laden", command=self.load_dialog).pack(side="right", padx=(0, 4))
        ttk.Button(act, text="💾 Speichern", command=self.save_dialog).pack(side="right", padx=(0, 4))
        ttk.Button(act, text="👎", width=4, command=lambda: self.rate(False)).pack(side="right", padx=(0, 10))
        ttk.Button(act, text="👍", width=4, command=lambda: self.rate(True)).pack(side="right", padx=4)
        ttk.Label(act, text="Bewerten:", foreground=MUTED).pack(side="right")

        # ---- Hype-Kurve ----
        self.canvas = tk.Canvas(right, height=150, bg=PANEL, highlightthickness=0)
        self.canvas.pack(fill="x", pady=(10, 0))
        self.canvas.bind("<Configure>", lambda e: self._draw_curve())
        self.canvas.bind("<Button-1>", self._curve_click)

        # ---- Ergebnisliste ----
        tf = ttk.Frame(right)
        tf.pack(fill="both", expand=True, pady=(10, 0))
        cols = ("rank", "time", "score", "reason", "chat")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="extended")
        for c, t, w, a in [("rank", "#", 36, "center"), ("time", "Zeit im Stream", 110, "center"),
                           ("score", "Hype", 60, "center"), ("reason", "Warum", 260, "w"),
                           ("chat", "Chat sagt …", 380, "w")]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a, stretch=c in ("reason", "chat"))
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", self._open_moment)
        self.tree.tag_configure("done", foreground=GOOD)
        self.tree.tag_configure("good", background="#123d2c")
        self.tree.tag_configure("bad", foreground="#6b6b75")
        self.tree.bind("+", lambda e: self.rate(True))
        self.tree.bind("-", lambda e: self.rate(False))
        ttk.Label(right, text="Tipp: Doppelklick öffnet die Stelle auf Twitch, damit du sie vorher ansehen kannst. "
                              "👍/👎 (oder +/−) bringt der App deinen Geschmack bei – je mehr, desto besser.", foreground=MUTED).pack(anchor="w", pady=(4, 0))

        # ---- Log ----
        self.prog = ttk.Progressbar(right, mode="indeterminate")
        self.prog.pack(fill="x", pady=(8, 4))
        self.log_box = tk.Text(right, height=9, bg=PANEL, fg=MUTED, relief="flat", font=("Consolas", 9),
                               state="disabled", wrap="word")
        self.log_box.pack(fill="x")
        self.log("Willkommen! Wähle ein VOD (\"VODs laden\") oder füg einen Link ein und klick auf Analysieren.")

    # --------------------------------------------------------------- helpers --
    def _upd_w(self):
        w = int(float(self.v_w.get()))
        self.l_w.configure(text=f"Chat {w}%  ·  Lautstärke {100 - w}%")

    def _pick_file(self):
        p = filedialog.askopenfilename(title="Aufnahme wählen",
                                       filetypes=[("Videos", "*.mp4 *.mkv *.mov *.flv *.ts *.webm"), ("Alle", "*.*")])
        if p:
            self.v_file.set(p)
            self.src_kind.set("file")

    def _pick_out(self):
        p = filedialog.askdirectory(title="Speicherort für Clips")
        if p:
            self.v_out.set(p)

    def _open_out(self):
        d = self.v_out.get()
        os.makedirs(d, exist_ok=True)
        try:
            os.startfile(d)
        except Exception:
            webbrowser.open(d)

    def _vod_picked(self, _=None):
        i = self.cb_vods.current()
        if 0 <= i < len(self.vods):
            self.v_url.set(core.vod_url(self.vods[i]["id"]))
            self.src_kind.set("vod")

    def settings(self):
        def num(var, default):
            try:
                return int(float(var.get()))
            except Exception:
                return default
        s = self.s
        s.update({
            "channel": self.v_channel.get().strip() or "ximjxstinn",
            "count": num(self.v_count, 10), "pre": num(self.v_pre, 20), "post": num(self.v_post, 12),
            "min_gap": num(self.v_gap, 90), "skip_start": num(self.v_skip, 2), "w_chat": num(self.v_w, 65),
            "audio": bool(self.v_audio.get()), "fmt": self.v_fmt.get(), "vstyle": self.v_vstyle.get(),
            "quality": self.v_quality.get(), "bestof": bool(self.v_bestof.get()),
            "keywords": self.t_kw.get("1.0", "end").strip(), "outdir": self.v_out.get().strip() or DEFAULTS["outdir"],
            "auto": bool(self.v_auto.get()), "autostart": bool(self.v_autostart.get()),
            "autoupdate": bool(self.v_autoupdate.get()),
        })
        self.save()
        return dict(s)

    def save(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.s, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def log(self, text):
        """Thread-sicher loggen."""
        self.q.put(("log", text))

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    stamp = datetime.datetime.now().strftime("%H:%M:%S")
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", f"[{stamp}] {data}\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                elif kind == "call":
                    data()
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def ui(self, fn):
        self.q.put(("call", fn))

    def _set_busy(self, busy):
        self.busy = busy
        for b in (self.b_an, self.b_clip, self.b_all, self.b_auto, self.b_vods):
            b.configure(state="disabled" if busy else "normal")
        self.b_cancel.configure(state="normal" if busy else "disabled")
        if busy:
            self.prog.start(12)
        else:
            self.prog.stop()

    def _work(self, fn, *args):
        """Startet fn im Hintergrund-Thread."""
        if self.busy:
            messagebox.showinfo("Moment", "Es läuft gerade schon etwas.")
            return False
        self._set_busy(True)
        self.runner = core.Runner(self.log)

        def wrap():
            try:
                fn(*args)
            except core.Cancelled:
                self.log("⏹ Abgebrochen.")
            except Exception as e:
                self.log(f"❌ Fehler: {e}")
                with open(os.path.join(core.APP_DIR, "fehler.log"), "a", encoding="utf-8") as f:
                    f.write(f"\n--- {datetime.datetime.now()} ---\n{traceback.format_exc()}")
            finally:
                self.ui(lambda: self._set_busy(False))

        threading.Thread(target=wrap, daemon=True).start()
        return True

    def cancel(self):
        if self.runner:
            self.runner.cancel()
            self.log("Breche ab …")

    def _mark_done(self, i):
        if self.tree.exists(str(i)):
            self.tree.item(str(i), tags=[t for t in self.tree.item(str(i), "tags") if t != "done"] + ["done"])
        self.save_session()

    def _close(self):
        self.settings()
        self.save_session()
        if self.runner:
            self.runner.cancel()
        self.root.destroy()

    # --------------------------------------------------------------- actions --
    def update_tools(self):
        self._work(lambda: core.ensure_tools(self.log, self.runner, force=True))

    def load_vods(self):
        st = self.settings()

        def job():
            core.ensure_tools(self.log, self.runner)
            self.log(f"Lade VODs von {st['channel']} …")
            vods = core.list_vods(st["channel"], self.runner)

            def show():
                self.vods = vods
                vals = []
                for v in vods:
                    d = v["date"]
                    d = f"{d[6:8]}.{d[4:6]}." if len(d) == 8 else ""
                    dur = f" ({core.fmt_time(v['duration'])})" if v.get("duration") else ""
                    vals.append(f"{d} {v['title']}{dur}".strip())
                self.cb_vods["values"] = vals
                if vals:
                    self.cb_vods.current(0)
                    self._vod_picked()
            self.ui(show)
            self.log(f"✔ {len(vods)} VODs gefunden" if vods else
                     "Keine VODs gefunden – sind vergangene Übertragungen in den Twitch-Einstellungen aktiviert?")
        self._work(job)

    def _source(self):
        if self.src_kind.get() == "file":
            p = self.v_file.get().strip()
            if not os.path.isfile(p):
                messagebox.showwarning("Datei fehlt", "Bitte wähle eine Aufnahme-Datei aus.")
                return None
            return {"kind": "file", "path": p, "title": os.path.splitext(os.path.basename(p))[0]}
        vid = core.parse_vod_id(self.v_url.get())
        if not vid:
            messagebox.showwarning("Kein VOD", "Bitte wähle ein VOD aus der Liste oder füg einen Link wie\n"
                                               "https://www.twitch.tv/videos/123456789 ein.")
            return None
        title = ""
        for v in self.vods:
            if v["id"] == vid:
                title = v["title"]
        return {"kind": "vod", "id": vid, "title": title or f"VOD {vid}"}

    def analyze(self):
        src = self._source()
        if src:
            st = self.settings()
            self._work(self._analyze_job, src, st)

    def _analyze_job(self, src, st):
        core.ensure_tools(self.log, self.runner)
        msgs, length, loud = [], None, []
        info = {"raids": [], "clips": []}
        if src["kind"] == "vod":
            work = os.path.join(core.WORK_DIR, src["id"])
            os.makedirs(work, exist_ok=True)
            try:
                chat_path = core.download_chat(src["id"], work, self.runner, self.log)
                msgs, length, info = core.load_chat(chat_path)
                try:
                    with open(chat_path, encoding="utf-8") as f:
                        t = (json.load(f).get("video") or {}).get("title")
                    if t:
                        src["title"] = t
                except Exception:
                    pass
                self.log(f"✔ {len(msgs)} echte Chat-Nachrichten ({info['removed']} Bots/Befehle/Begrüßungen rausgefiltert"
                         + (f", {len(info['raids'])} Raid(s) erkannt" if info['raids'] else "") + ")")
            except core.Cancelled:
                raise
            except Exception as e:
                self.log(f"⚠ Chat konnte nicht geladen werden ({e}). Mache nur mit Lautstärke weiter.")
            if st["audio"] or len(msgs) < 20:
                try:
                    audio = core.download_audio(src["id"], work, self.runner, self.log)
                    loud = core.analyze_loudness(audio, work, self.runner, self.log, length)
                except core.Cancelled:
                    raise
                except Exception as e:
                    self.log(f"⚠ Lautstärke-Analyse ging nicht: {e}")
        else:
            key = core.safe_name(os.path.basename(src["path"]), 60) + f"_{int(os.path.getmtime(src['path']))}"
            work = os.path.join(core.WORK_DIR, "datei_" + key)
            os.makedirs(work, exist_ok=True)
            length = core.media_duration(src["path"], self.runner)
            loud = core.analyze_loudness(src["path"], work, self.runner, self.log, length)
            self.log("ℹ Bei eigenen Aufnahmen gibt's keinen Chat – es wird nur nach Lautstärke gesucht.")
        if not msgs and not loud:
            raise RuntimeError("Keine Daten zum Analysieren (weder Chat noch Audio).")
        self.log("🧠 Suche die besten Stellen …")
        hl, curve = core.find_highlights(
            msgs, loud, length, count=st["count"], pre=st["pre"], post=st["post"], min_gap=st["min_gap"],
            skip_start=st["skip_start"] * 60, w_chat=st["w_chat"] / 100, keywords=st["keywords"],
            raids=info.get("raids"), clip_reqs=info.get("clips"), log=self.log)
        n_learn = len(core.load_learning()["examples"])
        if n_learn:
            self.log(f"🧠 Nutze {n_learn} Bewertungen von dir, um nach deinem Geschmack zu sortieren.")
        if not hl:
            raise RuntimeError("Keine auffälligen Stellen gefunden.")
        self.log(f"✔ {len(hl)} Highlights gefunden – Doppelklick zum Ansehen, dann clippen.")
        self.ui(lambda: self._show(src, hl, curve))
        return src, hl

    def _show(self, src, hl, curve, autosave=True):
        self.meta, self.highlights, self.curve = src, hl, curve
        self.tree.delete(*self.tree.get_children())
        for i, h in enumerate(hl):
            tags = []
            if h.get("clipped"):
                tags.append("done")
            if h.get("rated") is not None:
                tags.append("good" if h["rated"] else "bad")
            self.tree.insert("", "end", iid=str(i), tags=tags, values=(
                i + 1, core.fmt_time(h["time"]), h["score"], h["reason"], h["chat"]))
        self._draw_curve()
        if autosave:
            self.save_session()

    # --------------------------------------------------------------- Speichern --
    def _session_file(self, src):
        key = src["id"] if src["kind"] == "vod" else "datei_" + core.safe_name(src.get("title", ""), 50)
        return os.path.join(SESSION_DIR, f"{key}.json")

    def save_session(self, path=None):
        """Speichert die aktuelle Analyse (Highlights, Kurve, Bewertungen, was schon geclippt ist)."""
        if not self.meta or not self.highlights:
            return None
        os.makedirs(SESSION_DIR, exist_ok=True)
        path = path or self._session_file(self.meta)
        data = {"app": "StreamClipper", "version": VERSION,
                "saved": datetime.datetime.now().isoformat(timespec="seconds"),
                "source": self.meta, "highlights": self.highlights,
                "curve": [[round(t, 1), round(v, 4)] for t, v in self.curve]}
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(path + ".tmp", path)
            self.s["last_session"] = path
            self.save()
            return path
        except Exception as e:
            self.log(f"⚠ Speichern ging nicht: {e}")
            return None

    def load_session(self, path, quiet=False):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            src, hl = data["source"], data["highlights"]
            curve = [tuple(x) for x in data.get("curve", [])]
        except Exception as e:
            if not quiet:
                messagebox.showerror("Laden", f"Datei konnte nicht geladen werden:\n{e}")
            return
        if src.get("kind") == "vod":
            self.v_url.set(core.vod_url(src["id"]))
            self.src_kind.set("vod")
        elif src.get("path"):
            self.v_file.set(src["path"])
            self.src_kind.set("file")
        self._show(src, hl, curve, autosave=False)
        self.s["last_session"] = path
        self.save()
        self.log(f"📂 Analyse geladen: {src.get('title', '')} (gespeichert {data.get('saved', '?').replace('T', ' ')})")

    def _restore_last(self):
        p = self.s.get("last_session")
        if p and os.path.isfile(p):
            self.load_session(p, quiet=True)

    def save_dialog(self):
        if not self.highlights:
            messagebox.showinfo("Speichern", "Es gibt noch keine Analyse zum Speichern.")
            return
        os.makedirs(SESSION_DIR, exist_ok=True)
        p = filedialog.asksaveasfilename(
            title="Analyse speichern", initialdir=SESSION_DIR, defaultextension=".json",
            initialfile=os.path.basename(self._session_file(self.meta)), filetypes=[("Stream-Clipper-Analyse", "*.json")])
        if p and self.save_session(p):
            self.log(f"💾 Gespeichert: {p}")

    def load_dialog(self):
        os.makedirs(SESSION_DIR, exist_ok=True)
        p = filedialog.askopenfilename(title="Gespeicherte Analyse laden", initialdir=SESSION_DIR,
                                       filetypes=[("Stream-Clipper-Analyse", "*.json")])
        if p:
            self.load_session(p)

    # ----------------------------------------------------------------- Updates --
    def check_updates(self, silent=False):
        def job():
            try:
                info = updater.check(VERSION)
            except Exception as e:
                if not silent:
                    self.log(f"⚠ Update-Check ging nicht: {e}")
                return
            if not info:
                if not silent:
                    self.log(f"✔ Du hast die neueste Version ({VERSION}).")
                return
            self.ui(lambda: self._offer_update(info, silent))
        threading.Thread(target=job, daemon=True).start()
        if silent:  # alle 6 Stunden erneut (wichtig, wenn die App mit Windows dauerhaft läuft)
            self.root.after(6 * 3600 * 1000, lambda: self.check_updates(True) if self.v_autoupdate.get() else None)

    def _offer_update(self, info, silent):
        new = info.get("version")
        notes = info.get("notes", "")
        if self.busy:
            self.log(f"🔄 Update {new} ist da – wird installiert, sobald die App fertig ist.")
            self.root.after(60 * 1000, lambda: self._offer_update(info, silent))
            return
        if not silent and not messagebox.askyesno("Update", f"Version {new} ist da (du hast {VERSION}).\n\n"
                                                             f"{notes}\n\nJetzt installieren?"):
            return
        self.log(f"🔄 Installiere Update {new} …" + (f" – {notes}" if notes else ""))

        def job():
            try:
                updater.install(info, self.log)
            except Exception as e:
                self.log(f"❌ Update fehlgeschlagen (alte Version bleibt): {e}")
                return
            self.log(f"✅ Update {new} installiert – App startet neu …")
            self.ui(self._restart)
        threading.Thread(target=job, daemon=True).start()

    def _restart(self):
        self.settings()
        self.save_session()
        try:
            updater.restart()
        except Exception as e:
            self.log(f"Bitte App selbst neu starten ({e})")
            return
        self.root.after(300, self.root.destroy)

    def _draw_curve(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if not self.curve:
            c.create_text(w / 2, h / 2, text="Hier erscheint nach der Analyse die Hype-Kurve deines Streams",
                          fill=MUTED, font=("Segoe UI", 10))
            return
        pad = 22
        total = self.curve[-1][0] or 1
        # auf Pixelbreite verdichten (Maximum pro Pixel)
        cols = max(1, w - 2 * pad)
        buckets = [0.0] * cols
        for t, v in self.curve:
            x = min(cols - 1, int(t / total * cols))
            buckets[x] = max(buckets[x], v)
        top = max(buckets) or 1
        pts = [pad, h - pad]
        for x, v in enumerate(buckets):
            pts += [pad + x, h - pad - v / top * (h - 2 * pad)]
        pts += [pad + cols - 1, h - pad]
        c.create_polygon(*pts, fill="#2b1d4a", outline=ACCENT, width=1)
        for k in range(0, int(total) + 1, 1800 if total > 7200 else 600):
            x = pad + k / total * cols
            c.create_line(x, h - pad, x, h - pad + 4, fill=MUTED)
            c.create_text(x, h - 8, text=core.fmt_time(k)[:5], fill=MUTED, font=("Segoe UI", 8))
        for i, hl in enumerate(self.highlights):
            x = pad + hl["time"] / total * cols
            c.create_line(x, 14, x, h - pad, fill=GOOD, dash=(2, 3))
            c.create_oval(x - 9, 4, x + 9, 22, fill=GOOD, outline="")
            c.create_text(x, 13, text=str(i + 1), fill=BG, font=("Segoe UI Semibold", 8))

    def _curve_click(self, e):
        if not self.highlights or not self.curve:
            return
        pad = 22
        total = self.curve[-1][0] or 1
        t = (e.x - pad) / max(1, self.canvas.winfo_width() - 2 * pad) * total
        i = min(range(len(self.highlights)), key=lambda k: abs(self.highlights[k]["time"] - t))
        self.tree.selection_set(str(i))
        self.tree.see(str(i))

    def _open_moment(self, _=None):
        sel = self.tree.selection()
        if not sel or not self.meta:
            return
        h = self.highlights[int(sel[0])]
        if self.meta["kind"] == "vod":
            webbrowser.open(f"{core.vod_url(self.meta['id'])}?t={core.twitch_t(h['start'])}")
        else:
            messagebox.showinfo("Stelle", f"Spring in deiner Aufnahme zu {core.fmt_time(h['start'])}.")

    def rate(self, good):
        sel = self.tree.selection()
        if not sel or not self.highlights:
            messagebox.showinfo("Bewerten", "Wähl zuerst einen Clip in der Liste aus.")
            return
        n = len(core.load_learning()["examples"])
        for iid in sel:
            h = self.highlights[int(iid)]
            if h.get("rated") is not None or "features" not in h:
                continue
            h["rated"] = good
            n = core.save_feedback(h["features"], good)
            tags = [t for t in self.tree.item(iid, "tags") if t not in ("good", "bad")] + ["good" if good else "bad"]
            self.tree.item(iid, tags=tags)
        self.log(f"{'👍' if good else '👎'} gespeichert – die App kennt jetzt {n} Bewertungen. "
                 "Beim nächsten Analysieren wird danach sortiert.")
        self.save_session()
        nxt = self.tree.next(sel[-1])
        if nxt:
            self.tree.selection_set(nxt)
            self.tree.see(nxt)

    def _toggle_autostart(self):
        on = bool(self.v_autostart.get())
        startup = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs",
                               "Startup", "xImJxstinn Stream Clipper.bat")
        try:
            if on:
                pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                if not os.path.isfile(pyw):
                    pyw = sys.executable
                with open(startup, "w", encoding="utf-8") as f:
                    f.write(f'@echo off\r\ncd /d "{core.APP_DIR}"\r\nstart "" "{pyw}" '
                            f'"{os.path.join(core.APP_DIR, "StreamClipper.pyw")}" --minimized\r\n')
                self.v_auto.set(True)
                self._toggle_auto()
                self.log("✔ Startet jetzt mit Windows (minimiert) und clippt neue Streams automatisch.")
            elif os.path.isfile(startup):
                os.remove(startup)
                self.log("Autostart aus.")
        except Exception as e:
            self.log(f"❌ Autostart ging nicht: {e}")
        self.settings()

    # ---------------------------------------------------------------- clipping --
    def clip(self, everything):
        if not self.highlights:
            messagebox.showinfo("Erst analysieren", "Klick zuerst auf \"Analysieren\".")
            return
        idx = list(range(len(self.highlights))) if everything else [int(i) for i in self.tree.selection()]
        if not idx:
            messagebox.showinfo("Nichts ausgewählt", "Wähle in der Liste Clips aus (Strg/Shift für mehrere) "
                                                     "oder nimm \"Alle clippen\".")
            return
        st = self.settings()
        self._work(self._clip_job, self.meta, [(i, self.highlights[i]) for i in idx], st)

    def _clip_job(self, src, items, st):
        core.ensure_tools(self.log, self.runner)
        date = datetime.date.today().strftime("%Y-%m-%d")
        folder = os.path.join(st["outdir"], f"{date}_{core.safe_name(src['title'])}")
        os.makedirs(folder, exist_ok=True)
        fmt = st["fmt"]
        want_wide = fmt != FORMATS[1]
        want_tall = fmt != FORMATS[0]
        vstyle = VSTYLES.get(st["vstyle"], "blur")
        made_wide = []
        lines = [f"Highlights – {src['title']}", ""]
        for n, (i, h) in enumerate(items, 1):
            self.runner.check()
            name = f"{i + 1:02d}_{core.fmt_time(h['start'], '-')}_hype{h['score']}"
            base = os.path.join(folder, name)
            self.log(f"✂ Clip {n}/{len(items)}: {core.fmt_time(h['start'])} – {core.fmt_time(h['end'])}")
            if src["kind"] == "vod":
                wide = core.cut_from_vod(src["id"], h["start"], h["end"], base, st["quality"], self.runner, self.log)
            else:
                wide = core.cut_from_file(src["path"], h["start"], h["end"], base + ".mp4", self.runner)
            made_wide.append((h["start"], wide))
            if want_tall:
                self.log("   📱 TikTok-Version …")
                core.make_vertical(wide, base + "_tiktok.mp4", vstyle, self.runner)
            link = f"{core.vod_url(src['id'])}?t={core.twitch_t(h['start'])}" if src["kind"] == "vod" else ""
            lines.append(f"#{i + 1}  {core.fmt_time(h['start'])}  Hype {h['score']}  {h['reason']}  {link}")
            h["clipped"] = True
            self.ui(lambda i=i: self._mark_done(i))
        if st["bestof"] and len(made_wide) > 1:
            self.log("🎬 Schneide Best-of-Video zusammen …")
            core.make_bestof([p for _, p in sorted(made_wide)], os.path.join(folder, "_BEST-OF.mp4"), self.runner)
        if not want_wide:
            for _, p in made_wide:
                try:
                    os.remove(p)
                except OSError:
                    pass
        with open(os.path.join(folder, "highlights.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.log(f"✅ Fertig! {len(items)} Clips gespeichert in: {folder}")
        return folder

    def full_auto(self):
        src = self._source()
        if src:
            st = self.settings()
            self._work(self._full_auto_job, src, st)

    def _full_auto_job(self, src, st):
        src, hl = self._analyze_job(src, st)
        self._clip_job(src, list(enumerate(hl)), st)

    # ---------------------------------------------------------------- auto --
    def _toggle_auto(self):
        st = self.settings()
        if st["auto"]:
            self.log("⚡ Auto-Modus an: Ab jetzt werden neue Streams nach dem Ende automatisch geclippt "
                     "(App muss offen bleiben).")
            self.s["seen_init"] = False
            self.save()
            self._auto_tick()
        else:
            if self.auto_job:
                self.root.after_cancel(self.auto_job)
                self.auto_job = None
            self.log("Auto-Modus aus.")

    def _auto_tick(self):
        if self.auto_job:
            self.root.after_cancel(self.auto_job)
        self.auto_job = None
        if not self.v_auto.get():
            return
        self.auto_job = self.root.after(30 * 60 * 1000, self._auto_tick)
        if self.busy:
            return
        st = self.settings()
        self._work(self._auto_job, st)

    def _auto_job(self, st):
        core.ensure_tools(self.log, self.runner)
        vods = core.list_vods(st["channel"], self.runner, limit=5)
        seen = set(self.s.get("seen", []))
        if not self.s.get("seen_init"):
            # Beim Einschalten: alte VODs nicht nachträglich clippen
            self.s["seen"] = [v["id"] for v in vods]
            self.s["seen_init"] = True
            self.save()
            self.log(f"Auto-Modus: {len(vods)} bestehende VODs gemerkt – warte auf den nächsten Stream.")
            return
        new = [v for v in vods if v["id"] not in seen]
        if not new:
            self.log("Auto-Modus: kein neuer Stream.")
            return
        if core.is_live(st["channel"], self.runner):
            self.log("Auto-Modus: du bist gerade live – clippe, wenn der Stream vorbei ist.")
            return
        for v in reversed(new):
            self.log(f"⚡ Auto-Modus: neuer Stream gefunden – {v['title']}")
            src = {"kind": "vod", "id": v["id"], "title": v["title"]}
            try:
                self._full_auto_job(src, st)
            finally:
                self.s.setdefault("seen", []).append(v["id"])
                self.s["seen"] = self.s["seen"][-100:]
                self.save()


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    if "--minimized" in sys.argv:
        root.iconify()
    root.mainloop()


if __name__ == "__main__":
    main()
