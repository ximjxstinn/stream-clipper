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
from tkinter import ttk, filedialog, messagebox, simpledialog

import clipper_core as core
import updater

try:
    import uploader
except ImportError:  # Update von 2.1: alter Updater kannte uploader.py noch nicht → selbst nachladen
    try:
        with open(os.path.join(core.APP_DIR, "uploader.py"), "wb") as _f:
            _f.write(updater._get(updater.REPO_RAW + "uploader.py"))
        import uploader
    except Exception:
        uploader = None

try:
    import live_clipper
except ImportError:  # ältere Updater kennen live_clipper.py noch nicht → selbst nachladen
    try:
        with open(os.path.join(core.APP_DIR, "live_clipper.py"), "wb") as _f:
            _f.write(updater._get(updater.REPO_RAW + "live_clipper.py"))
        import live_clipper
    except Exception:
        live_clipper = None

VERSION = "3.9"

# Fertige Live-Profile für GTA 6 (jederzeit umschaltbar, gilt sofort – auch mitten im Stream)
_ALL = ("siren", "shots", "explosion", "bluelight", "vision", "game", "voice", "chat", "clipreq")


def _trig(*on):
    return {k: (k in on) for k in _ALL}


PROFILE_KEYS = ("live_trig", "live_game", "live_vis", "live_voice", "live_gap", "live_hour", "live_shots",
                "live_rate", "live_rate_min", "live_len")
GTA_PROFILES = {
    "🎮 GTA 6 – Standard (empfohlen)": {
        "live_trig": _trig("siren", "shots", "explosion", "bluelight", "vision", "game", "voice", "clipreq"),
        "live_game": 7, "live_vis": 1.8, "live_voice": 15, "live_gap": 60, "live_hour": 25, "live_shots": 4,
        "live_rate": True, "live_rate_min": 35, "live_len": 30},
    "🚔 GTA 6 – Polizei & Verfolgungsjagd": {
        "live_trig": _trig("siren", "bluelight", "shots", "explosion", "vision", "game", "clipreq"),
        "live_game": 6, "live_vis": 1.6, "live_voice": 0, "live_gap": 45, "live_hour": 40, "live_shots": 4,
        "live_rate": True, "live_rate_min": 30, "live_len": 30},
    "💰 GTA 6 – Überfall / Einbruch / Schießerei": {
        "live_trig": _trig("shots", "explosion", "siren", "bluelight", "vision", "game", "voice", "clipreq"),
        "live_game": 6, "live_vis": 1.6, "live_voice": 15, "live_gap": 40, "live_hour": 60, "live_shots": 3,
        "live_rate": True, "live_rate_min": 30, "live_len": 30},
    "🔥 GTA 6 – Nur die krassesten Momente": {
        "live_trig": _trig("shots", "explosion", "siren", "bluelight", "clipreq"),
        "live_game": 10, "live_vis": 2.5, "live_voice": 0, "live_gap": 120, "live_hour": 10, "live_shots": 8,
        "live_rate": True, "live_rate_min": 50, "live_len": 30},
    "😎 GTA 6 – Chillen / Freeroam": {
        "live_trig": _trig("explosion", "shots", "siren", "vision", "voice", "clipreq"),
        "live_game": 10, "live_vis": 2.2, "live_voice": 18, "live_gap": 180, "live_hour": 8, "live_shots": 6,
        "live_rate": True, "live_rate_min": 45, "live_len": 30},
}
CUSTOM_PROFILE = "✏ Eigene Einstellungen"
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

VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".flv", ".ts", ".webm", ".avi", ".m4v")
MODES = ["Normal (nur die besten)", "Maximal (alles Spannende)"]
FORMATS = ["16:9 (YouTube/Twitch)", "9:16 (TikTok/Shorts)", "Beides"]
VSTYLES = {"Blur-Hintergrund": "blur", "Mitte zuschneiden": "crop"}
QUALITIES = ["1080p", "720p", "Beste"]

DEFAULTS = {
    "channel": "ximjxstinn", "count": 10, "pre": 18, "post": 10, "min_gap": 90, "skip_start": 2,
    "w_chat": 65, "audio": True, "fmt": FORMATS[2], "vstyle": "Blur-Hintergrund", "quality": "1080p",
    "bestof": False, "nomenu": True, "vis": True, "mode": "Maximal (alles Spannende)", "live_times": [], "keywords": core.DEFAULT_KEYWORDS,
    "outdir": os.path.join(os.path.expanduser("~"), "Videos", "Stream Clips"),
    "auto": False, "seen": [], "autostart": False, "autoupdate": True, "last_session": "", "tray": True, "tray_hint_shown": False, "live_popup": True, "live_voice": 12, "live_trig": {}, "live_game": 6, "live_vis": 1.6, "live_gap": 60, "live_hour": 30, "live_shots": 4, "live_rate": True, "live_rate_min": 35, "live_drop": "keep", "live_auto": "approve", "live_profile": "", "live_profiles": {}, "live": False, "live_sens": "Maximal (alles Spannende)", "live_len": 30, "live_max": 100, "win_zoomed": True, "win_geo": "",
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
        if not self.s.get("mig_31"):
            # Einmalig: Live-Clipper auf "so viel wie möglich" + GTA-Wörter (Cops, Autos klauen …)
            self.s["live_sens"] = "Maximal (alles Spannende)"
            self.s["live_max"] = 100
            kw = core.parse_keywords(self.s.get("keywords", ""))
            extra = [k for k in core.parse_keywords(core.GTA_KEYWORDS) if k not in kw]
            if extra:
                self.s["keywords"] = (self.s.get("keywords", "").rstrip(", ") + ", " + ", ".join(extra)).strip(", ")
            self.s["mig_31"] = True
            self.save()
        if not self.s.get("mig_38"):
            self.s["mig_38"] = True
            self.root.after(1500, lambda: self.apply_live_profile("🎮 GTA 6 – Standard (empfohlen)"))
        if not self.s.get("mig_35"):
            if int(self.s.get("live_voice", 12) or 0) < 12:
                self.s["live_voice"] = 12  # normales Reden soll nicht mehr clippen
            self.s["mig_35"] = True
            self.save()
        if not self.s.get("mig_332"):
            self.s["vstyle"] = "Mitte zuschneiden"
            if int(self.s.get("live_len", 30) or 0) < 20:
                self.s["live_len"] = 30
            self.s["mig_332"] = True
            self.save()
        if not self.s.get("mig_33"):
            # Einmalig: auch beim Analysieren alles Spannende + Szenen-Erkennung im Bild
            self.s["mode"] = MODES[1]
            self.s["vis"] = True
            self.s["mig_33"] = True
            self.save()
        self.q = queue.Queue()
        self.runner = None
        self.busy = False
        self.highlights = []
        self.curve = []
        self.meta = None
        self.vods = []
        self.auto_job = None

        root.title(f"xImJxstinn Stream Clipper v{VERSION}")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        geo = self.s.get("win_geo") or f"{min(1500, sw - 60)}x{min(960, sh - 90)}+20+20"
        root.geometry(geo)
        root.minsize(min(900, sw - 40), min(600, sh - 80))
        self._last_state = "zoomed" if self.s.get("win_zoomed", True) else "normal"
        if self._last_state == "zoomed":
            try:
                root.state("zoomed")
            except tk.TclError:
                pass
        root.bind("<Configure>", self._on_configure, add="+")
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
        self.live = None
        if self.s.get("live"):
            self.root.after(4000, self._toggle_live)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_button)
        self.tray = None
        self.root.bind("<Unmap>", self._on_unmap)
        threading.Thread(target=self._setup_tray, daemon=True).start()
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
        # Linke Spalte scrollbar machen (für kleine Bildschirme / nicht maximiert)
        lwrap = ttk.Frame(body, style="Panel.TFrame")
        lwrap.pack(side="left", fill="y")
        lcan = tk.Canvas(lwrap, bg=PANEL, highlightthickness=0, width=420)
        lsb = ttk.Scrollbar(lwrap, orient="vertical", command=lcan.yview)
        lcan.configure(yscrollcommand=lsb.set)
        lcan.pack(side="left", fill="y")
        left = ttk.Frame(lcan, style="Panel.TFrame", padding=14)
        lcan.create_window((0, 0), window=left, anchor="nw")

        def fit_left(_=None):
            lcan.configure(scrollregion=(0, 0, left.winfo_reqwidth(), left.winfo_reqheight()),
                           width=left.winfo_reqwidth())
            if left.winfo_reqheight() > lcan.winfo_height() + 2:
                if not lsb.winfo_ismapped():
                    lsb.pack(side="left", fill="y")
            elif lsb.winfo_ismapped():
                lsb.pack_forget()
                lcan.yview_moveto(0)
        left.bind("<Configure>", fit_left)
        lcan.bind("<Configure>", fit_left)

        def wheel(e):
            if not lsb.winfo_ismapped():
                return
            w = self.root.winfo_containing(e.x_root, e.y_root)
            while w is not None and w is not lcan:
                if isinstance(w, tk.Text):
                    return  # Textfeld scrollt selbst
                w = w.master
            if w is lcan:
                lcan.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")
        self.root.bind_all("<MouseWheel>", wheel, add="+")
        # Mausrad soll NIE Werte in Auswahlfeldern/Zahlenfeldern verstellen (passierte beim Scrollen)
        for cls in ("TCombobox", "TSpinbox", "Spinbox"):
            for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                try:
                    self.root.unbind_class(cls, ev)
                except Exception:
                    pass
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
        self.v_vis = tk.BooleanVar(value=s.get("vis", True))
        ttk.Checkbutton(left, text="Szenen-Erkennung im Bild (Action, Verfolgungen, Kämpfe) – auch live",
                        variable=self.v_vis).grid(row=r, column=0, columnspan=3, sticky="w", pady=(2, 0))
        r += 1
        ttk.Label(left, text="Modus (auch live)", style="Panel.TLabel").grid(row=r, column=0, sticky="w", pady=2)
        self.v_mode = tk.StringVar(value=s.get("mode", MODES[1]))
        ttk.Combobox(left, textvariable=self.v_mode, values=MODES, state="readonly", width=22).grid(
            row=r, column=1, columnspan=2, sticky="we", pady=2)
        r += 1
        self.v_nomenu = tk.BooleanVar(value=s.get("nomenu", True))
        ttk.Checkbutton(left, text="Menüs, Einstellungen & Pause rausfiltern (Bildanalyse)",
                        variable=self.v_nomenu).grid(row=r, column=0, columnspan=3, sticky="w", pady=(2, 0))
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
        self.v_tray = tk.BooleanVar(value=s.get("tray", True))
        ttk.Checkbutton(left, text="Schließen/Minimieren → unten rechts im Infobereich weiterlaufen",
                        variable=self.v_tray, command=self.settings).grid(row=r, column=0, columnspan=3,
                                                                          sticky="w", pady=(2, 0))
        r += 1
        self.v_autoupdate = tk.BooleanVar(value=s.get("autoupdate", True))
        ttk.Checkbutton(left, text="Updates automatisch laden & installieren", variable=self.v_autoupdate,
                        command=self.settings).grid(row=r, column=0, columnspan=3, sticky="w", pady=(2, 0))
        r += 1
        ttk.Button(left, text="⬆ Upload zu YouTube & TikTok einrichten …", style="Accent.TButton",
                   command=self.upload_settings).grid(row=r, column=0, columnspan=3, sticky="we", pady=(10, 0))
        r += 1
        # ---- Live-Clipper ----
        ttk.Label(left, text="🔴 Live-Clipper (PC & PS5)", style="Sec.TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(14, 2))
        r += 1
        self.v_live = tk.BooleanVar(value=s.get("live", False))
        ttk.Checkbutton(left, text="Während ich live bin automatisch Twitch-Clips machen", variable=self.v_live,
                        command=self._toggle_live).grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        self.v_live_state = tk.StringVar(value="aus")
        ttk.Label(left, text="Status:", style="Panel.TLabel").grid(row=r, column=0, sticky="w")
        ttk.Label(left, textvariable=self.v_live_state, style="Muted.TLabel").grid(row=r, column=1, columnspan=2,
                                                                                    sticky="w")
        r += 1
        # Empfindlichkeit kommt jetzt aus „Modus“ oben (gilt für VOD + live)
        self.v_live_sens = tk.StringVar(value=s.get("live_sens", "Mittel"))
        ttk.Label(left, text="Clip-Länge (Sek.)", style="Panel.TLabel").grid(row=r, column=0, sticky="w", pady=2)
        self.v_live_len = tk.IntVar(value=s.get("live_len", 30))
        ttk.Spinbox(left, from_=10, to=60, textvariable=self.v_live_len, width=6).grid(row=r, column=2, sticky="e")
        r += 1
        ttk.Label(left, text="Stimme clippt ab +dB (0 = aus)", style="Panel.TLabel").grid(row=r, column=0,
                                                                                         columnspan=2, sticky="w", pady=2)
        self.v_live_voice = tk.IntVar(value=s.get("live_voice", 12))
        sb_voice = ttk.Spinbox(left, from_=0, to=30, textvariable=self.v_live_voice, width=6, command=self.settings)
        sb_voice.grid(row=r, column=2, sticky="e")
        sb_voice.bind("<Return>", lambda e: self.settings())
        sb_voice.bind("<FocusOut>", lambda e: self.settings())
        r += 1
        ttk.Label(left, text="5 = schon lautes Reden · 10 = Rufen/Lachen · 15 = nur Schreien · 0 = Stimme egal",
                  style="Muted.TLabel", wraplength=380).grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        ttk.Label(left, text="Profil", style="Panel.TLabel").grid(row=r, column=0, sticky="w", pady=2)
        self.v_live_profile = tk.StringVar(value=s.get("live_profile") or CUSTOM_PROFILE)
        self.cb_live_profile = ttk.Combobox(left, textvariable=self.v_live_profile, state="readonly", width=30,
                                            values=self._profile_names())
        self.cb_live_profile.grid(row=r, column=1, columnspan=2, sticky="we", pady=2)
        self.cb_live_profile.bind("<<ComboboxSelected>>", lambda e: self.apply_live_profile(self.v_live_profile.get()))
        r += 1
        ttk.Button(left, text="⚙ Live-Auslöser einstellen (was & wie oft) …", command=self.live_trigger_settings).grid(
            row=r, column=0, columnspan=3, sticky="we", pady=(4, 2))
        r += 1
        self.v_live_popup = tk.BooleanVar(value=s.get("live_popup", True))
        ttk.Checkbutton(left, text="Vorschau-Popup bei jedem neuen Live-Clip", variable=self.v_live_popup,
                        command=self.settings).grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        self.v_tw_status = tk.StringVar(value=self._twitch_status())
        ttk.Button(left, text="🟣 Mit Twitch verbinden …", command=self.twitch_connect).grid(
            row=r, column=0, columnspan=2, sticky="we", pady=(4, 0))
        ttk.Label(left, textvariable=self.v_tw_status, style="Muted.TLabel").grid(row=r, column=2, sticky="e")
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
        topbar = ttk.Frame(right)
        topbar.pack(fill="x")
        act = ttk.Frame(topbar)
        act.grid(row=0, column=0, sticky="w")
        act2 = ttk.Frame(topbar)
        topbar.columnconfigure(1, weight=1)

        def place_act2(_=None):
            need = act.winfo_reqwidth() + act2.winfo_reqwidth() + 20
            if topbar.winfo_width() < need:
                act2.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
            else:
                act2.grid(row=0, column=1, sticky="e", pady=0)
        topbar.bind("<Configure>", place_act2)
        self.b_an = ttk.Button(act, text="🔍 1. Analysieren", style="Accent.TButton", command=self.analyze)
        self.b_an.pack(side="left")
        self.b_clip = ttk.Button(act, text="✂ 2. Ausgewählte clippen", command=lambda: self.clip(False))
        self.b_clip.pack(side="left", padx=6)
        self.b_all = ttk.Button(act, text="✂ Alle clippen", command=lambda: self.clip(True))
        self.b_all.pack(side="left")
        self.b_auto = ttk.Button(act, text="⚡ Alles automatisch", style="Accent.TButton", command=self.full_auto)
        self.b_auto.pack(side="left", padx=6)
        self.b_view = ttk.Button(act, text="▶ Ansehen", command=self.view_clip)
        self.b_view.pack(side="left", padx=(0, 6))
        self.b_up = ttk.Button(act, text="✅ Freigeben & hochladen", command=self.upload_selected)
        self.b_up.pack(side="left")
        self.b_cancel = ttk.Button(act, text="Abbrechen", command=self.cancel, state="disabled")
        self.b_cancel.pack(side="left", padx=6)
        ttk.Label(act2, text="Bewerten:", foreground=MUTED).pack(side="left")
        ttk.Button(act2, text="👍", width=4, command=lambda: self.rate(True)).pack(side="left", padx=4)
        ttk.Button(act2, text="👎", width=4, command=lambda: self.rate(False)).pack(side="left", padx=(0, 10))
        ttk.Button(act2, text="💾 Speichern", command=self.save_dialog).pack(side="left", padx=(0, 4))
        ttk.Button(act2, text="📂 Laden", command=self.load_dialog).pack(side="left", padx=(0, 4))
        ttk.Button(act2, text="📁 Ordner", command=self._open_out).pack(side="left")

        # ---- Hype-Kurve ----
        self.canvas = tk.Canvas(right, height=150, bg=PANEL, highlightthickness=0)
        self.canvas.pack(fill="x", pady=(10, 0))
        # Vorschau des ausgewählten Clips
        self.prev_bar = tk.Frame(right, bg=PANEL)
        self.prev_img_lbl = tk.Label(self.prev_bar, bg=PANEL)
        self.prev_img_lbl.pack(side="left", padx=8, pady=6)
        self.prev_txt = tk.Label(self.prev_bar, bg=PANEL, fg=TEXT, justify="left", anchor="nw",
                                 font=("Segoe UI", 10), wraplength=360)
        self.prev_txt.pack(side="left", fill="both", expand=True, padx=8, pady=6)
        ttk.Button(self.prev_bar, text="▶ Abspielen", command=self.view_clip).pack(side="right", padx=8)
        self._prev_photo = None
        self.canvas.bind("<Configure>", lambda e: self._draw_curve())
        self.canvas.bind("<Button-1>", self._curve_click)

        # ---- Ergebnisliste ----
        tf = ttk.Frame(right)
        tf.pack(fill="both", expand=True, pady=(10, 0))
        cols = ("rank", "time", "score", "status", "reason", "chat")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", selectmode="extended")
        for c, t, w, a in [("rank", "#", 36, "center"), ("time", "Zeit im Stream", 110, "center"),
                           ("score", "Hype", 60, "center"), ("status", "Status", 190, "w"),
                           ("reason", "Warum", 240, "w"),
                           ("chat", "Chat sagt …", 380, "w")]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a, stretch=c in ("reason", "chat"))
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", self._open_moment)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.tag_configure("done", foreground=GOOD)
        self.tree.tag_configure("good", background="#123d2c")
        self.tree.tag_configure("bad", foreground="#6b6b75")
        self.tree.tag_configure("warn", foreground="#ffb347")
        self.tree.tag_configure("pending", foreground="#7fd3ff")
        self.tree.bind("+", lambda e: self.rate(True))
        self.tree.bind("-", lambda e: self.rate(False))
        ttk.Label(right, text="Tipp: Doppelklick öffnet die Stelle auf Twitch, „▶ Ansehen“ spielt den fertigen Clip ab. "
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
            "nomenu": bool(self.v_nomenu.get()), "vis": bool(self.v_vis.get()), "mode": self.v_mode.get(),
            "keywords": self.t_kw.get("1.0", "end").strip(), "outdir": self.v_out.get().strip() or DEFAULTS["outdir"],
            "auto": bool(self.v_auto.get()), "autostart": bool(self.v_autostart.get()),
            "autoupdate": bool(self.v_autoupdate.get()), "tray": bool(self.v_tray.get()),
            "live": bool(self.v_live.get()),
            "live_sens": ("Maximal (alles Spannende)" if self.v_mode.get().startswith("Maximal") else "Mittel"),
            "live_popup": bool(self.v_live_popup.get()), "live_voice": num(self.v_live_voice, 12), "live_len": num(self.v_live_len, 30),
        })
        self.save()
        if getattr(self, "live", None):
            self.live.st.update(self._live_settings())
        if hasattr(self, "v_live_profile"):
            self._mark_custom()
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
        for b in (self.b_an, self.b_clip, self.b_all, self.b_auto, self.b_vods, self.b_up):
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

    def _status_text(self, h):
        up = h.get("uploaded") or {}
        if up:
            return "⬆ hochgeladen (" + " + ".join({"yt": "YouTube", "tt": "TikTok"}[p] for p in up) + ")"
        qc = h.get("qc")
        th = h.get("thrill") or {}
        if h.get("dropped"):
            return f"🗑 aussortiert – langweilig ({th.get('score', 0)}/100)"
        if h.get("skipped"):
            return "– nicht unter den besten"
        if h.get("boring"):
            return f"💤 langweilig ({th.get('score', 0)}/100) – kein Upload"
        if qc and not qc.get("ok"):
            return "⚠ " + "; ".join(qc.get("problems", []))[:60]
        if h.get("pending"):
            return "⏳ wartet auf deine Freigabe"
        if qc and qc.get("ok"):
            return "✅ geprüft – ok" + (f" · 🎯 {th['score']}/100" if th else "")
        if h.get("clipped"):
            return "✂ geschnitten"
        return ""

    def _refresh_row(self, i):
        iid = str(i)
        if not self.tree.exists(iid) or i >= len(self.highlights):
            return
        h = self.highlights[i]
        vals = list(self.tree.item(iid, "values"))
        if len(vals) >= 4:
            vals[3] = self._status_text(h)
            self.tree.item(iid, values=vals)
        tags = [t for t in self.tree.item(iid, "tags") if t not in ("done", "warn", "pending")]
        qc = h.get("qc")
        if qc and not qc.get("ok"):
            tags.append("warn")
        elif h.get("pending"):
            tags.append("pending")
        elif h.get("clipped"):
            tags.append("done")
        self.tree.item(iid, tags=tags)

    def view_clip(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Ansehen", "Wähl einen Clip in der Liste aus.")
            return
        h = self.highlights[int(sel[0])]
        files = h.get("files") or {}
        path = next((p for p in (files.get("tall"), files.get("wide")) if p and os.path.isfile(p)), None)
        if not path:
            messagebox.showinfo("Ansehen", "Dieser Clip ist noch nicht geschnitten. Doppelklick zeigt die Stelle auf Twitch.")
            return
        try:
            os.startfile(path)
        except Exception:
            webbrowser.open(path)

    def _mark_done(self, i):
        self._refresh_row(i)
        self.save_session()

    # ------------------------------------------------------------ Live-Clipper --
    def _twitch_status(self):
        if not live_clipper:
            return "Modul fehlt"
        c = live_clipper.load_conf()
        return f"✅ {c.get('login')}" if c.get("refresh_token") else "nicht verbunden"

    def _live_settings(self):
        st = dict(self.s)
        st["vstyle_key"] = VSTYLES.get(self.s.get("vstyle"), "blur")
        return st

    def twitch_connect(self):
        if not live_clipper:
            messagebox.showerror("Live-Clipper", "live_clipper.py fehlt – bitte „Nach Updates suchen“.")
            return
        c = live_clipper.load_conf()
        win = tk.Toplevel(self.root)
        win.title("Mit Twitch verbinden")
        win.configure(bg=BG)
        win.transient(self.root)
        f = ttk.Frame(win, style="Panel.TFrame", padding=16)
        f.pack(fill="both", expand=True, padx=12, pady=12)
        ttk.Label(f, text="Damit die App live echte Twitch-Clips machen darf, braucht sie einmalig Zugriff auf "
                          "„Clips erstellen“ in deinem Twitch-Konto.", style="Panel.TLabel", wraplength=480).pack(anchor="w")
        ttk.Label(f, text="Client-ID deiner Twitch-App:", style="Panel.TLabel").pack(anchor="w", pady=(12, 2))
        cid = tk.StringVar(value=c.get("client_id", ""))
        ttk.Entry(f, textvariable=cid, width=50).pack(fill="x")
        ttk.Label(f, text="Client-Geheimnis (nur bei Client-Typ „Vertraulich“ nötig):",
                  style="Panel.TLabel").pack(anchor="w", pady=(10, 2))
        csec = tk.StringVar(value=c.get("client_secret", ""))
        ttk.Entry(f, textvariable=csec, width=50, show="•").pack(fill="x")
        ttk.Label(f, text="⚠ Bei einer App, die schon ein anderes Programm (z. B. dein Bot) nutzt, NICHT auf "
                          "„Neues Geheimnis“ klicken – sonst geht das andere Programm nicht mehr. Nimm das "
                          "Geheimnis, das du schon hast.", style="Muted.TLabel", wraplength=480).pack(anchor="w")
        ttk.Button(f, text="Twitch-App anlegen (dev.twitch.tv) …",
                   command=lambda: webbrowser.open("https://dev.twitch.tv/console/apps/create")).pack(anchor="w", pady=6)
        status = tk.StringVar(value=self._twitch_status())
        ttk.Label(f, textvariable=status, style="Muted.TLabel").pack(anchor="w", pady=(4, 8))

        def go():
            status.set("⏳ Browser geöffnet – dort auf „Aktivieren“/„Autorisieren“ klicken …")
            tw = live_clipper.Twitch()

            def job():
                try:
                    name = tw.device_login(cid.get(), self.log, webbrowser.open, client_secret=csec.get())
                    self.log(f"✅ Twitch verbunden: {name}")
                    self.ui(lambda: (status.set(f"✅ verbunden als {name}"), self.v_tw_status.set(f"✅ {name}")))
                    if self.v_live.get():
                        self.ui(self._restart_live)
                except Exception as e:
                    self.log(f"❌ {e}")
                    self.ui(lambda: status.set("❌ fehlgeschlagen – siehe Log"))
            threading.Thread(target=job, daemon=True).start()
        ttk.Button(f, text="Verbinden", style="Accent.TButton", command=go).pack(anchor="e")

    def _profile_names(self):
        return list(GTA_PROFILES) + list(self.s.get("live_profiles") or {}) + [CUSTOM_PROFILE]

    def _profile(self, name):
        return GTA_PROFILES.get(name) or (self.s.get("live_profiles") or {}).get(name)

    def apply_live_profile(self, name, quiet=False):
        prof = self._profile(name)
        if not prof:
            self.s["live_profile"] = ""
            self.save()
            return
        for k in PROFILE_KEYS:
            if k in prof:
                self.s[k] = json.loads(json.dumps(prof[k]))
        self.s["live_profile"] = name
        if hasattr(self, "v_live_voice"):
            self.v_live_voice.set(int(self.s.get("live_voice", 12)))
            self.v_live_len.set(int(self.s.get("live_len", 30)))
            self.v_live_profile.set(name)
        self.settings()
        if not quiet:
            self.log(f"🎛 Profil „{name}“ aktiv – gilt sofort.")

    def _mark_custom(self):
        """Nach Hand-Änderungen: passt nicht mehr zum Profil → „Eigene Einstellungen“."""
        name = self.s.get("live_profile")
        prof = self._profile(name) if name else None
        if prof and any(self.s.get(k) != prof.get(k) for k in PROFILE_KEYS if k in prof):
            self.s["live_profile"] = ""
            self.v_live_profile.set(CUSTOM_PROFILE)
            self.save()

    def live_trigger_settings(self):
        trig_names = live_clipper.TRIGGERS if live_clipper else {}
        cur = dict(self.s.get("live_trig") or {})
        win = tk.Toplevel(self.root)
        win.title("Live-Auslöser")
        win.configure(bg=BG)
        win.transient(self.root)
        f = ttk.Frame(win, style="Panel.TFrame", padding=16)
        f.pack(fill="both", expand=True, padx=12, pady=12)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Was soll einen Live-Clip auslösen?", style="Sec.TLabel").grid(row=0, column=0, columnspan=3,
                                                                                         sticky="w", pady=(0, 6))
        vars_ = {}
        r = 1
        for key, name in trig_names.items():
            vars_[key] = tk.BooleanVar(value=cur.get(key, True))
            ttk.Checkbutton(f, text=name, variable=vars_[key]).grid(row=r, column=0, columnspan=3, sticky="w")
            r += 1
        ttk.Label(f, text="Wie empfindlich / wie oft?", style="Sec.TLabel").grid(row=r, column=0, columnspan=3,
                                                                                  sticky="w", pady=(12, 6))
        r += 1
        fields = [
            ("Spiel lauter als normal ab (+dB)", "live_game", 6, 2, 20, "kleiner = mehr Clips"),
            ("Bewegung im Bild ab (× normal, ×10)", "live_vis", 16, 12, 40, "16 = ×1,6 · 25 = ×2,5"),
            ("Stimme ab (+dB, 0 = aus)", "live_voice", 12, 0, 30, "12 = Rufen · 15 = Schreien"),
            ("Schüsse: mindestens so viele in 4 Sek.", "live_shots", 4, 2, 20, "Sturmgewehr-Salve = 8–10+"),
            ("Mindestabstand zwischen Clips (Sek.)", "live_gap", 60, 20, 600, "größer = weniger Clips"),
            ("Höchstens Clips pro Stunde", "live_hour", 30, 1, 200, ""),
        ]
        ents = {}
        for label, key, default, lo, hi, hint in fields:
            val = self.s.get(key, default)
            if key == "live_vis":
                val = int(round(float(val) * 10))
            ents[key] = tk.IntVar(value=int(val))
            ttk.Label(f, text=label, style="Panel.TLabel").grid(row=r, column=0, sticky="w", pady=2)
            ttk.Spinbox(f, from_=lo, to=hi, textvariable=ents[key], width=6).grid(row=r, column=1, sticky="w",
                                                                                 padx=8)
            ttk.Label(f, text=hint, style="Muted.TLabel").grid(row=r, column=2, sticky="w")
            r += 1

        ttk.Label(f, text="Nach dem Clip – automatisch", style="Sec.TLabel").grid(row=r, column=0, columnspan=3,
                                                                                    sticky="w", pady=(12, 6))
        r += 1
        v_rate = tk.BooleanVar(value=self.s.get("live_rate", True))
        ttk.Checkbutton(f, text="🎯 Jeden Clip auf Spannung prüfen – nur spannende behalten", variable=v_rate).grid(
            row=r, column=0, columnspan=3, sticky="w")
        r += 1
        ents["live_rate_min"] = tk.IntVar(value=int(self.s.get("live_rate_min", 35)))
        ttk.Label(f, text="Mindestens so spannend (0–100 Punkte)", style="Panel.TLabel").grid(row=r, column=0,
                                                                                             sticky="w", pady=2)
        ttk.Spinbox(f, from_=0, to=100, increment=5, textvariable=ents["live_rate_min"], width=6).grid(
            row=r, column=1, sticky="w", padx=8)
        ttk.Label(f, text="Schüsse/Explosion/Sirene/Action = Punkte", style="Muted.TLabel").grid(row=r, column=2,
                                                                                               sticky="w")
        r += 1
        drops = {"keep": "Behalten, aber nicht hochladen", "delete": "Sofort löschen"}
        v_drop = tk.StringVar(value=drops.get(self.s.get("live_drop", "keep"), drops["keep"]))
        ttk.Label(f, text="Langweilige Clips", style="Panel.TLabel").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(f, values=list(drops.values()), textvariable=v_drop, state="readonly", width=30).grid(
            row=r, column=1, columnspan=2, sticky="w", padx=8)
        r += 1
        autos = {"approve": "Erst mir zeigen (Freigabe)", "end": "Nach Streamende automatisch hochladen",
                 "now": "Sofort automatisch hochladen"}
        v_auto = tk.StringVar(value=autos.get(self.s.get("live_auto", "approve"), autos["approve"]))
        ttk.Label(f, text="Spannende Clips hochladen", style="Panel.TLabel").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Combobox(f, values=list(autos.values()), textvariable=v_auto, state="readonly", width=34).grid(
            row=r, column=1, columnspan=2, sticky="w", padx=8)
        r += 1
        ttk.Label(f, text="Hochgeladen wird auf die Plattformen, die unter „⬆ Upload einrichten“ auf Auto stehen "
                          "(TikTok als Entwurf, YouTube wie dort eingestellt).", style="Muted.TLabel",
                  wraplength=460).grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1

        def preset(level):
            vals = {"wenig": (10, 25, 15, 180, 10, 8), "mittel": (6, 16, 12, 60, 30, 4),
                    "viel": (4, 14, 8, 30, 80, 3)}[level]
            for (key, v) in zip(("live_game", "live_vis", "live_voice", "live_gap", "live_hour", "live_shots"), vals):
                ents[key].set(v)
        pf = ttk.Frame(f, style="Panel.TFrame")
        pf.grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(pf, text="Schnell-Auswahl:", style="Panel.TLabel").pack(side="left")
        for lvl, txt in (("wenig", "Wenig (nur Krasses)"), ("mittel", "Mittel"), ("viel", "Viel")):
            ttk.Button(pf, text=txt, command=lambda l=lvl: preset(l)).pack(side="left", padx=4)
        r += 1

        def load_prof(name):
            prof = self._profile(name)
            if not prof:
                return
            for k, var in vars_.items():
                var.set(bool((prof.get("live_trig") or {}).get(k, True)))
            for k in ("live_game", "live_voice", "live_gap", "live_hour", "live_shots", "live_rate_min"):
                if k in prof and k in ents:
                    ents[k].set(int(prof[k]))
            if "live_vis" in prof:
                ents["live_vis"].set(int(round(float(prof["live_vis"]) * 10)))
            v_rate.set(bool(prof.get("live_rate", True)))
        gf = ttk.Frame(f, style="Panel.TFrame")
        gf.grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(gf, text="Werte aus Profil laden:", style="Panel.TLabel").pack(side="left")
        v_lp = tk.StringVar(value="")
        cb = ttk.Combobox(gf, textvariable=v_lp, state="readonly", width=36,
                          values=[n for n in self._profile_names() if n != CUSTOM_PROFILE])
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: load_prof(v_lp.get()))
        r += 1

        def save():
            def gi(k, d):
                try:
                    return int(float(ents[k].get()))
                except Exception:
                    return d
            self.s["live_trig"] = {k: bool(v.get()) for k, v in vars_.items()}
            self.s["live_game"] = gi("live_game", 6)
            self.s["live_vis"] = gi("live_vis", 16) / 10.0
            self.s["live_voice"] = gi("live_voice", 12)
            self.s["live_gap"] = gi("live_gap", 60)
            self.s["live_hour"] = gi("live_hour", 30)
            self.s["live_shots"] = gi("live_shots", 4)
            self.s["live_rate"] = bool(v_rate.get())
            self.s["live_rate_min"] = max(0, min(100, gi("live_rate_min", 35)))
            self.s["live_drop"] = next((k for k, t in drops.items() if t == v_drop.get()), "keep")
            self.s["live_auto"] = next((k for k, t in autos.items() if t == v_auto.get()), "approve")
            self.v_live_voice.set(self.s["live_voice"])
            self.settings()
            self._mark_custom()
            self.log("✔ Live-Auslöser gespeichert – gilt sofort, auch mitten im Stream.")
            win.destroy()

        def save_as():
            name = simpledialog.askstring("Als Profil speichern", "Name für dein Profil (z. B. „Mein GTA Heist“):",
                                          parent=win)
            if not name or not name.strip():
                return
            name = "⭐ " + name.strip()[:40]
            save()
            profs = dict(self.s.get("live_profiles") or {})
            profs[name] = {k: json.loads(json.dumps(self.s.get(k))) for k in PROFILE_KEYS}
            self.s["live_profiles"] = profs
            self.s["live_profile"] = name
            self.cb_live_profile.configure(values=self._profile_names())
            self.v_live_profile.set(name)
            self.save()
            self.log(f"⭐ Profil „{name}“ gespeichert – oben bei „Profil“ jederzeit auswählbar.")

        def delete_prof():
            name = self.s.get("live_profile")
            if name not in (self.s.get("live_profiles") or {}):
                messagebox.showinfo("Profil löschen", "Nur eigene ⭐-Profile können gelöscht werden.", parent=win)
                return
            if messagebox.askyesno("Profil löschen", f"„{name}“ löschen?", parent=win):
                self.s["live_profiles"].pop(name, None)
                self.s["live_profile"] = ""
                self.cb_live_profile.configure(values=self._profile_names())
                self.v_live_profile.set(CUSTOM_PROFILE)
                self.save()
        bf = ttk.Frame(f, style="Panel.TFrame")
        bf.grid(row=r, column=0, columnspan=3, sticky="we", pady=(12, 0))
        ttk.Button(bf, text="🗑 Eigenes Profil löschen", command=delete_prof).pack(side="left")
        ttk.Button(bf, text="⭐ Als Profil speichern …", command=save_as).pack(side="left", padx=6)
        ttk.Button(bf, text="Speichern (gilt sofort)", style="Accent.TButton", command=save).pack(side="right")

    def _toggle_live(self):
        on = bool(self.v_live.get())
        self.settings()
        if not on:
            if self.live:
                self.live.stop()
                self.live = None
            self.v_live_state.set("aus")
            self.log("Live-Clipper aus.")
            return
        if not live_clipper:
            self.log("❌ live_clipper.py fehlt – bitte „Nach Updates suchen“.")
            return
        tw = live_clipper.Twitch()
        if not tw.connected():
            self.v_live_state.set("erst mit Twitch verbinden")
            self.twitch_connect()
            return
        self._restart_live()

    def _restart_live(self):
        if self.live:
            self.live.stop()

        def threaded_ensure():
            try:
                core.ensure_tools(self.log, core.Runner(self.log))
            except Exception as e:
                self.log(f"⚠ Tools: {e}")
            self.ui(start)

        def start():
            self.live = live_clipper.LiveClipper(
                live_clipper.Twitch(), self._live_settings(), self.log,
                on_clip=lambda stream, h: self.ui(lambda: self._add_live_clip(stream, h)),
                on_state=lambda txt: self.ui(lambda: self.v_live_state.set(txt)),
                notify=self.notify)
            self.live.on_end = lambda stream: self.ui(lambda: self._live_stream_ended(stream))
            self.live.start()
        threading.Thread(target=threaded_ensure, daemon=True).start()

    def _add_live_clip(self, stream, h):
        sid = str(stream.get("id", "live"))
        if not (self.meta and self.meta.get("kind") == "live" and str(self.meta.get("id")) == sid):
            self.save_session()
            title = stream.get("title") or "Live"
            self._show({"kind": "live", "id": sid, "title": title}, [], [], autosave=False)
            self.log(f"🔴 Neue Live-Liste für „{title}“ – Live-Clips landen hier.")
        self.highlights.append(h)
        if self.s.get("live_list_id") != sid:
            self.s["live_list_id"], self.s["live_times"] = sid, []
        self.s.setdefault("live_times", []).append(round(h["time"], 1))
        i = len(self.highlights) - 1
        self.tree.insert("", "end", iid=str(i), values=(
            i + 1, core.fmt_time(h["time"]), h["score"], self._status_text(h), h["reason"], h["chat"]))
        self._refresh_row(i)
        self.tree.see(str(i))
        self.save_session()
        self._live_popup(i)
        if self.s.get("live_auto") == "now" and not h.get("boring") and (h.get("qc") or {}).get("ok", True):
            src = dict(self.meta)
            h["auto_queued"] = True
            threading.Thread(target=lambda: self._auto_upload(src, [(i, h)], direct=False, force=True),
                             daemon=True).start()

    def _live_stream_ended(self, stream):
        if not (self.meta and self.meta.get("kind") == "live" and self.highlights):
            return
        items = list(enumerate(self.highlights))
        mode = self.s.get("live_auto", "approve")
        if mode == "now":
            items = [x for x in items if not (x[1].get("uploaded") or {}) and not x[1].get("auto_queued")]
        threading.Thread(target=lambda: self._auto_upload(self.meta, items, direct=False, force=(mode != "approve")),
                         daemon=True).start()

    # ------------------------------------------------------- Infobereich (Tray) --
    def _pip(self, *pkgs):
        """Installiert fehlende Python-Pakete einmalig (ohne Konsolenfenster)."""
        import subprocess, importlib, site
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", "--disable-pip-version-check",
                        *pkgs], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       stdin=subprocess.DEVNULL, timeout=300, **kw)
        importlib.invalidate_caches()
        try:
            up = site.getusersitepackages()
            if up not in sys.path:
                sys.path.append(up)
        except Exception:
            pass

    # ---------------------------------------------------- Drag & Drop (Aufnahme reinziehen) --
    def _setup_dnd(self):
        if os.name != "nt":
            return
        try:
            import windnd
        except ImportError:
            try:
                self.log("⬇ Installiere einmalig Drag & Drop (windnd) …")
                self._pip("windnd")
                import windnd
            except Exception as e:
                self.log(f"⚠ Drag & Drop nicht verfügbar ({e}) – nimm stattdessen „Datei: …“.")
                return
        self.ui(lambda: self._hook_dnd(windnd))

    def _hook_dnd(self, windnd):
        try:
            windnd.hook_dropfiles(self.root, func=self._on_drop, force_unicode=True)
            self.log("✔ Drag & Drop bereit – zieh eine Aufnahme einfach ins Fenster.")
        except Exception as e:
            self.log(f"⚠ Drag & Drop ging nicht ({e}) – nimm stattdessen „Datei: …“.")

    def _on_drop(self, files):
        # kommt direkt aus Windows → erst danach in Ruhe in Tk weiterarbeiten
        paths = [f.decode("utf-8", "replace") if isinstance(f, bytes) else str(f) for f in files]
        self.root.after(50, lambda: self.handle_drop(paths))

    def handle_drop(self, paths):
        vids = [p for p in paths if os.path.isfile(p) and p.lower().endswith(VIDEO_EXTS)]
        if not vids:
            messagebox.showinfo("Reinziehen", "Das ist keine Video-Datei.\nErlaubt: " + ", ".join(VIDEO_EXTS))
            return
        if self.busy:
            messagebox.showinfo("Moment", "Die App arbeitet gerade noch. Klick erst auf „Abbrechen“ "
                                          "und zieh die Aufnahme dann nochmal rein.")
            return
        path = vids[0]
        if len(vids) > 1:
            self.log(f"ℹ Mehrere Dateien – nehme die erste: {os.path.basename(path)}")
        # alte Analyse ist schon gespeichert (Ordner „gespeichert“) → Liste leeren und ersetzen
        self.save_session()
        self.highlights, self.curve, self.meta = [], [], None
        self.tree.delete(*self.tree.get_children())
        self._draw_curve()
        self.v_file.set(path)
        self.src_kind.set("file")
        self.show_window()
        self.log(f"📥 Neue Aufnahme: {os.path.basename(path)} – alte Clips aus der Liste ersetzt, analysiere …")
        self.analyze()

    def _setup_tray(self):
        """Tray-Icon unten rechts. Braucht pystray + Pillow – werden beim ersten Mal automatisch installiert."""
        self._setup_dnd()  # zuerst Drag & Drop (gleicher Thread → keine zwei pip-Installationen gleichzeitig)
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            try:
                self.log("⬇ Installiere einmalig das Infobereich-Modul (pystray) …")
                self._pip("pystray", "pillow")
                import pystray
                from PIL import Image, ImageDraw
            except Exception as e:
                self.log(f"⚠ Infobereich nicht verfügbar ({e}) – App schließt dann normal.")
                return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(145, 71, 255, 255))
        # Schere
        d.ellipse((10, 34, 26, 50), outline="white", width=4)
        d.ellipse((10, 14, 26, 30), outline="white", width=4)
        d.line((24, 26, 54, 46), fill="white", width=5)
        d.line((24, 38, 54, 18), fill="white", width=5)
        self._tray_img = img

        def on_ui(fn):
            return lambda icon, item: self.ui(fn)

        menu = pystray.Menu(
            pystray.MenuItem("Öffnen", on_ui(self.show_window), default=True),
            pystray.MenuItem("Neuesten Stream jetzt clippen", on_ui(self._tray_clip_latest)),
            pystray.MenuItem("Auto-Modus", on_ui(self._tray_toggle_auto),
                             checked=lambda item: bool(self.s.get("auto"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Beenden", on_ui(self._quit)),
        )
        self.tray = pystray.Icon("StreamClipper", img, f"Stream Clipper v{VERSION}", menu)
        self.ui(lambda: self.root.iconphoto(True, self._tk_icon()) if self._tk_icon() else None)
        self.log("✔ Infobereich bereit – Schließen/Minimieren schickt die App nach unten rechts.")
        try:
            self.tray.run()
        except Exception as e:
            self.tray = None
            self.log(f"⚠ Infobereich-Icon ist abgestürzt ({e}) – App schließt dann normal.")

    def _tk_icon(self):
        try:
            from PIL import ImageTk
            if not hasattr(self, "_tkimg"):
                self._tkimg = ImageTk.PhotoImage(self._tray_img)
            return self._tkimg
        except Exception:
            return None

    def _use_tray(self):
        return self.tray is not None and bool(self.v_tray.get())

    def hide_to_tray(self):
        self.root.withdraw()
        if not self.s.get("tray_hint_shown"):
            self.notify("Stream Clipper läuft weiter",
                        "Ich bin hier unten im Infobereich. Doppelklick zum Öffnen, Rechtsklick → Beenden.")
            self.s["tray_hint_shown"] = True
            self.save()

    def show_window(self):
        self.root.deiconify()
        try:
            self.root.state(self._last_state)  # war es maximiert, kommt es maximiert zurück
        except tk.TclError:
            self.root.state("normal")
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def _on_configure(self, event):
        if event.widget is not self.root:
            return
        st = self.root.state()
        if st in ("normal", "zoomed"):
            self._last_state = st
            self.s["win_zoomed"] = st == "zoomed"
            if st == "normal":
                self.s["win_geo"] = self.root.geometry()

    def _on_unmap(self, event):
        if event.widget is self.root and self._use_tray():
            self.root.after(50, lambda: self.hide_to_tray() if self.root.state() == "iconic" else None)

    def _on_close_button(self):
        if self._use_tray():
            self.settings()
            self.save_session()
            self.hide_to_tray()
        elif self.v_tray.get() and (self.busy or self.v_auto.get()):
            # Infobereich noch nicht bereit, aber es läuft etwas → nicht beenden, nur minimieren
            self.root.iconify()
            self.log("ℹ Infobereich noch nicht bereit – App wurde in die Taskleiste minimiert.")
        else:
            self._quit()

    def notify(self, title, msg):
        """Windows-Benachrichtigung unten rechts (nur wenn das Fenster versteckt ist oder es wichtig ist)."""
        try:
            if self.tray:
                self.tray.notify(msg[:250], title[:60])
        except Exception:
            pass

    def _tray_clip_latest(self):
        if self.busy:
            self.notify("Stream Clipper", "Ich arbeite gerade schon an etwas.")
            return
        st = self.settings()

        def job():
            core.ensure_tools(self.log, self.runner)
            vods = core.list_vods(st["channel"], self.runner, limit=1)
            if not vods:
                self.notify("Stream Clipper", "Kein VOD gefunden.")
                return
            v = vods[0]
            self.notify("Stream Clipper", f"Clippe jetzt: {v['title']}")
            self._full_auto_job({"kind": "vod", "id": v["id"], "title": v["title"]}, st)
        self._work(job)

    def _tray_toggle_auto(self):
        self.v_auto.set(not self.v_auto.get())
        self._toggle_auto()
        if self.tray:
            self.tray.update_menu()
        self.notify("Auto-Modus", "an – neue Streams werden automatisch geclippt" if self.v_auto.get() else "aus")

    def _quit(self):
        self.settings()
        self.save_session()
        if self.live:
            self.live.stop()
        if self.runner:
            self.runner.cancel()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    _close = _quit

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
            if loud:
                srt = sorted(loud)
                self.log(f"   Aufnahme: {core.fmt_time(length or len(loud))} · Ton: leise {srt[len(srt)//10]:.0f} dB, "
                         f"normal {srt[len(srt)//2]:.0f} dB, laut {srt[int(len(srt)*0.99)]:.0f} dB")
        motion = []
        if st.get("vis", True):
            try:
                motion = core.analyze_motion(src, work, self.runner, self.log, length)
            except core.Cancelled:
                raise
            except Exception as e:
                self.log(f"⚠ Szenen-Erkennung im Bild ging nicht ({str(e)[:100]}) – mache ohne weiter.")
        if not msgs and not loud and not motion:
            raise RuntimeError("Keine Daten zum Analysieren (weder Chat noch Ton noch Bild).")
        maximal = st.get("mode", "").startswith("Maximal")
        want = st["count"]
        gap = st["min_gap"]
        if maximal:
            want = max(want, max(20, int((length or 3600) / 240)))  # ~1 Clip pro 4 Min. Stream, mind. 20
            gap = min(gap, 45)
            self.log(f"🧠 Maximal-Modus: suche bis zu {want} spannende Szenen (Abstand ≥ {gap} s) …")
        else:
            self.log("🧠 Suche die besten Stellen …")
        hl, curve = core.find_highlights(
            msgs, loud, length, count=want * 3 if st.get("nomenu", True) else want, pre=st["pre"], post=st["post"],
            min_gap=gap, skip_start=st["skip_start"] * 60, w_chat=st["w_chat"] / 100, keywords=st["keywords"],
            raids=info.get("raids"), clip_reqs=info.get("clips"), log=self.log, motion=motion, maximal=maximal)
        if maximal and src.get("kind") == "vod" and self.s.get("live_times"):
            # Szenen, die der Live-Clipper schon geclippt hat, nicht doppelt schneiden
            lt = self.s["live_times"]
            before = len(hl)
            hl = [h for h in hl if all(abs(h["time"] - t) > 25 for t in lt)]
            if len(hl) < before:
                self.log(f"   ↪ {before - len(hl)} Szene(n) hat der Live-Clipper schon – übersprungen.")
        n_learn = len(core.load_learning()["examples"])
        if n_learn:
            self.log(f"🧠 Nutze {n_learn} Bewertungen von dir, um nach deinem Geschmack zu sortieren.")
        if hl and st.get("nomenu", True):
            hl = self._filter_menus(src, hl, want, os.path.join(work, "bildcheck"))
        hl = hl[:want]
        if not hl:
            raise RuntimeError("Keine auffälligen Stellen gefunden.")
        self.log(f"✔ {len(hl)} Highlights gefunden – Doppelklick zum Ansehen, dann clippen.")
        self.ui(lambda: self._show(src, hl, curve))
        return src, hl

    def _filter_menus(self, src, hl, want, workdir):
        """Schaut sich jeden Kandidaten kurz an und wirft Menüs/Einstellungen/Pause raus."""
        self.log("🎮 Prüfe das Bild der Kandidaten (Menüs & Einstellungen raus) …")
        keep, dropped = [], 0
        for n, h in enumerate(hl, 1):
            if len(keep) >= want:
                break
            self.runner.check()
            try:
                mean, static = core.measure_motion(src, h["start"], h["end"], workdir, self.runner)
            except core.Cancelled:
                raise
            except Exception as e:
                self.log(f"   ⚠ Bild-Check für {core.fmt_time(h['time'])} ging nicht ({str(e)[:80]}) – behalte die Stelle")
                keep.append(h)
                continue
            if core.looks_like_menu(mean, static):
                dropped += 1
                self.log(f"   ✖ {core.fmt_time(h['time'])} verworfen – sieht nach Menü/Einstellungen aus "
                         f"({int(static * 100)} % Standbild)")
            else:
                h["motion"] = round(mean, 1)
                keep.append(h)
        if not keep and hl:
            self.log("   ⚠ Die Bildanalyse hätte alles aussortiert – zeige dir die besten Stellen trotzdem.")
            return hl[:want]
        if dropped:
            self.log(f"   → {dropped} Menü-Stelle(n) aussortiert und durch die nächstbesten ersetzt.")
        else:
            self.log("   ✔ Alles Gameplay, nichts aussortiert.")
        return keep

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
                i + 1, core.fmt_time(h["time"]), h["score"], self._status_text(h), h["reason"], h["chat"]))
            self._refresh_row(i)
        self._draw_curve()
        if autosave:
            self.save_session()

    # --------------------------------------------------------------- Speichern --
    def _session_file(self, src):
        if src["kind"] == "vod":
            key = src["id"]
        elif src["kind"] == "live":
            key = "live_" + str(src.get("id", ""))
        else:
            key = "datei_" + core.safe_name(src.get("title", ""), 50)
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
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.after(300, self.root.destroy)

    def _draw_curve(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if not self.curve:
            c.create_text(w / 2, h / 2, text="📥 Zieh eine Aufnahme hier rein – oder wähle links ein VOD und klick auf Analysieren",
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
        if h.get("clip_url"):
            webbrowser.open(h["clip_url"])
        elif self.meta["kind"] == "vod":
            webbrowser.open(f"{core.vod_url(self.meta['id'])}?t={core.twitch_t(h['start'])}")
        else:
            messagebox.showinfo("Stelle", f"Spring in deiner Aufnahme zu {core.fmt_time(h['start'])}.")

    def rate(self, good, iids=None):
        sel = iids or self.tree.selection()
        if not sel or not self.highlights:
            messagebox.showinfo("Bewerten", "Wähl zuerst einen Clip in der Liste aus.")
            return
        n = len(core.load_learning()["examples"])
        for iid in sel:
            h = self.highlights[int(iid)]
            if h.get("rated") is not None or "features" not in h:
                continue
            h["rated"] = good
            if not good and h.get("pending"):
                h["pending"] = False  # 👎 = nicht hochladen
                self._refresh_row(int(iid))
            n = core.save_feedback(h["features"], good)
            tags = [t for t in self.tree.item(iid, "tags") if t not in ("good", "bad")] + ["good" if good else "bad"]
            self.tree.item(iid, tags=tags)
        self.log(f"{'👍' if good else '👎'} gespeichert – die App kennt jetzt {n} Bewertungen. "
                 "Beim nächsten Analysieren wird danach sortiert.")
        self.save_session()
        if iids:
            return
        nxt = self.tree.next(sel[-1])
        if nxt:
            self.tree.selection_set(nxt)
            self.tree.see(nxt)

    # ---------------------------------------------------------------- Vorschau --
    def _clip_file(self, h):
        files = h.get("files") or {}
        return next((p for p in (files.get("wide"), files.get("tall")) if p and os.path.isfile(p)), None)

    def _preview_async(self, h, done):
        """Erzeugt die Vorschau im Hintergrund und ruft done(png) im UI-Thread auf."""
        path = self._clip_file(h)
        if not path:
            return

        def job():
            try:
                png = core.make_preview(path, os.path.splitext(path)[0] + "_vorschau.png", core.Runner(lambda *_: None))
                self.ui(lambda: done(png))
            except Exception:
                pass
        threading.Thread(target=job, daemon=True).start()

    def _on_select(self, _=None):
        sel = self.tree.selection()
        if len(sel) != 1 or not self.highlights:
            self.prev_bar.pack_forget()
            return
        i = int(sel[0])
        h = self.highlights[i]
        if not self._clip_file(h):
            self.prev_bar.pack_forget()
            return
        self.prev_txt.configure(text=f"#{i + 1} · {core.fmt_time(h['time'])} · Hype {h['score']}\n{h['reason']}\n"
                                     f"{self._status_text(h)}")
        self.prev_img_lbl.configure(image="", text="⏳ Vorschau …", fg=MUTED)
        self.prev_bar.pack(fill="x", pady=(8, 0), after=self.canvas)

        def show(png):
            if self.tree.selection() != sel:
                return
            try:
                self._prev_photo = tk.PhotoImage(file=png)
                self.prev_img_lbl.configure(image=self._prev_photo, text="")
            except Exception:
                self.prev_img_lbl.configure(text="(keine Vorschau)")
        self._preview_async(h, show)

    def _live_popup(self, i):
        """Kleines Vorschau-Fenster unten rechts bei jedem neuen LIVE-Clip."""
        if not self.s.get("live_popup", True) or i >= len(self.highlights):
            return
        h = self.highlights[i]

        def show(png):
            try:
                win = tk.Toplevel(self.root)
                win.overrideredirect(True)
                win.attributes("-topmost", True)
                try:
                    win.attributes("-alpha", 0.96)
                except tk.TclError:
                    pass
                win.configure(bg=ACCENT)
                box = tk.Frame(win, bg=PANEL)
                box.pack(padx=2, pady=2)
                top = tk.Frame(box, bg=PANEL)
                top.pack(fill="x", padx=10, pady=(8, 4))
                tk.Label(top, text=f"✂ Neuer LIVE-Clip #{i + 1}", bg=PANEL, fg=TEXT,
                         font=("Segoe UI Semibold", 11)).pack(side="left")
                tk.Button(top, text="✕", bg=PANEL, fg=MUTED, bd=0, activebackground=PANEL,
                          command=win.destroy).pack(side="right")
                photo = tk.PhotoImage(file=png)
                lbl = tk.Label(box, image=photo, bg=PANEL)
                lbl.image = photo
                lbl.pack(padx=10)
                tk.Label(box, text=h["reason"] + ("" if (h.get("qc") or {}).get("ok", True)
                                                  else "\n⚠ " + ", ".join(h["qc"]["problems"])),
                         bg=PANEL, fg=MUTED, wraplength=photo.width(), justify="left",
                         font=("Segoe UI", 9)).pack(padx=10, pady=4, anchor="w")
                btns = tk.Frame(box, bg=PANEL)
                btns.pack(fill="x", padx=10, pady=(0, 10))

                def play():
                    p = self._clip_file(h)
                    if p:
                        try:
                            os.startfile(p)
                        except Exception:
                            webbrowser.open(p)

                def rate(good):
                    self.rate(good, iids=(str(i),))
                    win.destroy()
                for txt, cmd in (("▶ Abspielen", play), ("👍", lambda: rate(True)), ("👎", lambda: rate(False))):
                    tk.Button(btns, text=txt, command=cmd, bg=FIELD, fg=TEXT, bd=0, padx=10, pady=4,
                              activebackground=ACCENT, activeforeground="white").pack(side="left", padx=(0, 6))
                win.update_idletasks()
                w, hgt = win.winfo_width(), win.winfo_height()
                sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
                win.geometry(f"+{sw - w - 16}+{sh - hgt - 64}")
                state = {"hover": False}
                win.bind("<Enter>", lambda e: state.update(hover=True))
                win.bind("<Leave>", lambda e: state.update(hover=False))

                def auto_close(left=15):
                    if not win.winfo_exists():
                        return
                    if state["hover"]:
                        left = 6
                    if left <= 0:
                        win.destroy()
                    else:
                        win.after(1000, lambda: auto_close(left - 1))
                auto_close()
            except Exception as e:
                self.log(f"⚠ Vorschau-Popup ging nicht: {e}")
        self._preview_async(h, show)

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
        if self.meta and self.meta.get("kind") == "live":
            messagebox.showinfo("Live-Clips", "Live-Clips sind schon fertig geschnitten. Mit „▶ Ansehen“ anschauen "
                                              "und mit „✅ Freigeben & hochladen“ hochladen.")
            return
        idx = list(range(len(self.highlights))) if everything else [int(i) for i in self.tree.selection()]
        if not idx:
            messagebox.showinfo("Nichts ausgewählt", "Wähle in der Liste Clips aus (Strg/Shift für mehrere) "
                                                     "oder nimm \"Alle clippen\".")
            return
        st = self.settings()
        self._work(self._clip_job, self.meta, [(i, self.highlights[i]) for i in idx], st, everything)

    def _clip_job(self, src, items, st, best_only=False):
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
        target = max(1, int(st.get("count", 10)))
        if best_only:
            # Nur die besten: nach Hype sortiert schneiden, langweilige aussortieren, bis genug spannende da sind
            items = sorted(items, key=lambda x: -x[1]["score"])
            self.log(f"🏆 Nur die besten: ich schneide nach Hype sortiert, sortiere Langweiliges aus und höre "
                     f"bei {target} spannenden Clips auf.")
        kept = []
        for n, (i, h) in enumerate(items, 1):
            self.runner.check()
            if best_only and len(kept) >= target:
                h["skipped"] = True
                self.ui(lambda i=i: self._refresh_row(i))
                continue
            h.pop("skipped", None)
            h.pop("dropped", None)
            name = f"{i + 1:02d}_{core.fmt_time(h['start'], '-')}_hype{h['score']}"
            base = os.path.join(folder, name)
            self.log(f"✂ Clip {n}/{len(items)}: {core.fmt_time(h['start'])} – {core.fmt_time(h['end'])}")
            if src["kind"] == "vod":
                wide = core.cut_from_vod(src["id"], h["start"], h["end"], base, st["quality"], self.runner, self.log)
            else:
                wide = core.cut_from_file(src["path"], h["start"], h["end"], base + ".mp4", self.runner)
            made_wide.append((h["start"], wide))
            tall = None
            if want_tall:
                self.log("   📱 TikTok-Version …")
                tall = core.make_vertical(wide, base + "_tiktok.mp4", vstyle, self.runner)
            h["files"] = {"wide": wide if want_wide else None, "tall": tall, "vstyle": vstyle}
            # Qualitäts-Check
            check = tall or wide
            try:
                ok, probs, qinfo = core.check_clip(check, h["end"] - h["start"], self.runner, tall=bool(tall))
            except core.Cancelled:
                raise
            except Exception as e:
                ok, probs, qinfo = True, [], {"fehler": str(e)[:80]}
            h["qc"] = {"ok": ok, "problems": probs, "info": qinfo}
            self.log("   🔎 Check: " + ("✅ alles ok" if ok else "⚠ " + ", ".join(probs)))
            if live_clipper and st.get("live_rate", True) and wide and os.path.isfile(wide):
                try:
                    pts, why = live_clipper.rate_clip(wide, min_shots=max(2, int(st.get("live_shots", 4))))
                    keep = pts >= int(st.get("live_rate_min", 35))
                    h["thrill"] = {"score": pts, "why": why, "keep": keep}
                    h["boring"] = not keep
                    self.log(f"   🎯 Spannung: {pts}/100" + (" · " + ", ".join(why) if why else "")
                             + ("" if keep else " → 💤 zu langweilig, kein Upload"))
                except Exception as e:
                    self.log(f"   ⚠ Spannungs-Check ging nicht ({str(e)[:80]})")
            if best_only and h.get("boring"):
                for fp in (wide, tall):
                    try:
                        if fp and os.path.isfile(fp):
                            os.remove(fp)
                    except OSError:
                        pass
                made_wide = [x for x in made_wide if x[1] != wide]
                h["files"], h["dropped"], h["clipped"] = {}, True, False
                self.log("   🗑 aussortiert – nicht spannend genug")
                self.ui(lambda i=i: self._refresh_row(i))
                continue
            kept.append((i, h))
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
        dropped = len([1 for _, h in items if h.get("dropped")])
        self.log(f"✅ Fertig! {len(kept)} Clips gespeichert in: {folder}"
                 + (f" ({dropped} langweilige aussortiert)" if dropped else ""))
        if best_only and not kept and dropped:
            self.log("💤 Kein Clip war spannend genug. Tipp: „⚙ Live-Auslöser einstellen“ → Mindestpunktzahl "
                     "niedriger stellen (z. B. 25).")
        self.notify("✂ Clips fertig", f"{len(kept)} Clips aus „{src.get('title', 'Stream')}“ sind fertig.")
        self._auto_upload(src, kept, direct=True)
        return folder

    def _auto_upload(self, src, items, direct=True, force=False):
        """Nach dem Clippen: beste geprüfte Clips zur Freigabe vormerken oder direkt hochladen."""
        if not uploader:
            return
        conf = uploader.load_conf()
        plats = [p for p, k in (("yt", "yt_auto"), ("tt", "tt_auto")) if conf.get(k)]
        if not plats:
            return
        boring = [x for x in items if x[1].get("boring")]
        items = [x for x in items if not x[1].get("boring")]
        if boring:
            self.log(f"💤 {len(boring)} langweilige Clip(s) werden nicht hochgeladen.")
        items = [x for x in items if x[1].get("files") and not x[1].get("dropped") and not x[1].get("skipped")]
        good = [x for x in items if (x[1].get("qc") or {}).get("ok", True) and not (x[1].get("uploaded") or {})]
        bad = len([x for x in items if not (x[1].get("qc") or {}).get("ok", True)])
        if bad:
            self.log(f"⚠ {bad} Clip(s) haben den Check nicht bestanden und werden nicht hochgeladen.")
        best = sorted(good, key=lambda x: -((x[1].get("thrill") or {}).get("score", 0) * 10 + x[1]["score"]))
        if not force:
            best = best[:max(1, int(conf.get("auto_max", 3)))]
        if not best:
            self.log("⬆ Kein Clip hat den Check bestanden – nichts hochgeladen.")
        elif conf.get("approve", True) and not force:
            for i, h in best:
                h["pending"] = True
                self.ui(lambda i=i: self._refresh_row(i))
            self.ui(self.save_session)
            self.log(f"⏳ {len(best)} Clip(s) warten auf deine Freigabe: auswählen → „▶ Ansehen“ → "
                     "„✅ Freigeben & hochladen“ (oder 👎 wenn er schlecht ist).")
            self.notify("⏳ Clips warten auf Freigabe",
                        f"{len(best)} geprüfte Clips sind bereit. Öffne die App, schau sie dir an und gib sie frei.")
        else:
            self.log(f"⬆ Auto-Upload der besten {len(best)} geprüften Clips …")
            if direct:
                self._upload_job(src, best, plats)
            else:
                self.ui(lambda: self._queue_upload(src, best, plats))

    def _queue_upload(self, src, items, plats):
        if self.busy:
            self.root.after(60 * 1000, lambda: self._queue_upload(src, items, plats))
            return
        self._work(self._upload_job, src, items, plats)

    # ----------------------------------------------------------------- Upload --
    def upload_selected(self):
        if not uploader:
            messagebox.showerror("Upload", "uploader.py fehlt – bitte 'Nach Updates suchen'.")
            return
        sel = [int(i) for i in self.tree.selection()]
        if not sel:
            messagebox.showinfo("Hochladen", "Wähl in der Liste die Clips aus, die hochgeladen werden sollen.")
            return
        missing = [i + 1 for i in sel if not self.highlights[i].get("files")]
        if missing:
            messagebox.showinfo("Erst clippen", f"Clip(s) #{', #'.join(map(str, missing))} sind noch nicht "
                                                "geschnitten. Klick zuerst auf 'Ausgewählte clippen'.")
            return
        conf = uploader.load_conf()
        plats = []
        if conf.get("yt_token"):
            plats.append("yt")
        if conf.get("tt_token"):
            plats.append("tt")
        if not plats:
            self.upload_settings()
            return
        names = " und ".join({"yt": "YouTube", "tt": "TikTok"}[p] for p in plats)
        warn = [f"#{i + 1}: " + ", ".join(self.highlights[i]["qc"]["problems"]) for i in sel
                if self.highlights[i].get("qc") and not self.highlights[i]["qc"].get("ok")]
        if warn and not messagebox.askyesno("Achtung", "Diese Clips haben beim Check Probleme:\n\n" + "\n".join(warn)
                                            + "\n\nTrotzdem hochladen?"):
            return
        if not messagebox.askyesno("Freigeben & hochladen", f"{len(sel)} Clip(s) zu {names} hochladen?"):
            return
        self._work(self._upload_job, self.meta, [(i, self.highlights[i]) for i in sel], plats)

    def _upload_job(self, src, items, plats):
        conf = uploader.load_conf()
        cancel = lambda: self.runner.cancel_event.is_set()
        done = 0
        for i, h in items:
            self.runner.check()
            files = h.get("files") or {}
            up = h.setdefault("uploaded", {})
            for plat in plats:
                if up.get(plat):
                    self.log(f"   #{i + 1} ist schon auf {'YouTube' if plat == 'yt' else 'TikTok'} – übersprungen")
                    continue
                wide_mode = plat == "yt" and conf.get("yt_what") == "wide"
                path = files.get("wide") if wide_mode else files.get("tall")
                if not path or not os.path.isfile(path):
                    src_wide = files.get("wide")
                    if wide_mode and files.get("tall") and os.path.isfile(files["tall"]):
                        path = files["tall"]
                    elif src_wide and os.path.isfile(src_wide):
                        self.log(f"   📱 Erstelle Hochkant-Version für #{i + 1} …")
                        path = core.make_vertical(src_wide, os.path.splitext(src_wide)[0] + "_tiktok.mp4",
                                                  files.get("vstyle", "blur"), self.runner)
                        files["tall"] = path
                    else:
                        self.log(f"⚠ Datei für Clip #{i + 1} nicht gefunden – übersprungen")
                        continue
                title, desc, tags = uploader.build_text(conf, src.get("title"), i + 1, h.get("reason", ""),
                                                        short=not wide_mode)
                try:
                    if plat == "yt":
                        self.log(f"▶ YouTube: lade #{i + 1} hoch – „{title}“")
                        vid, privacy = uploader.yt_upload(conf, path, title, desc, tags, self.log, cancel)
                        up["yt"] = vid
                        note = ""
                        if privacy == "private" and conf.get("yt_privacy") != "private":
                            note = " (noch privat – Google schaltet öffentlich erst nach der API-Prüfung frei)"
                        self.log(f"   ✅ https://youtu.be/{vid}{note}")
                    else:
                        self.log(f"♪ TikTok: lade #{i + 1} hoch …")
                        pid, status = uploader.tt_upload(conf, path, f"{title} {tags}", self.log, cancel)
                        up["tt"] = pid
                        self.log("   ✅ " + ("In deiner TikTok-App als Entwurf – Benachrichtigung öffnen und posten!"
                                           if status == "SEND_TO_USER_INBOX" else f"Gepostet ({status})"))
                    done += 1
                except uploader.UploadError as e:
                    if "Abgebrochen" in str(e):
                        raise core.Cancelled()
                    self.log(f"   ❌ {e}")
                if up:
                    h["pending"] = False
                self.ui(lambda i=i: self._refresh_row(i))
                self.ui(self.save_session)
        self.log(f"⬆ Upload fertig – {done} Upload(s) erfolgreich.")
        if done:
            self.notify("⬆ Upload fertig", f"{done} Upload(s) erfolgreich" +
                        (" – TikTok-Entwürfe warten in deiner App!" if "tt" in plats else "."))

    def upload_settings(self):
        if not uploader:
            messagebox.showerror("Upload", "uploader.py fehlt – bitte 'Nach Updates suchen'.")
            return
        conf = uploader.load_conf()
        win = tk.Toplevel(self.root)
        win.title("Upload zu YouTube & TikTok")
        win.configure(bg=BG)
        win.geometry("640x760")
        win.transient(self.root)
        f = ttk.Frame(win, style="Panel.TFrame", padding=16)
        f.pack(fill="both", expand=True, padx=12, pady=12)
        f.columnconfigure(1, weight=1)
        r = 0

        def sec(text):
            nonlocal r
            ttk.Label(f, text=text, style="Sec.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", pady=(12, 4))
            r += 1

        def row(label, widget, btn=None):
            nonlocal r
            ttk.Label(f, text=label, style="Panel.TLabel").grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
            widget.grid(row=r, column=1, sticky="we", pady=3)
            if btn:
                btn.grid(row=r, column=2, sticky="e", padx=(6, 0))
            r += 1

        # ---- YouTube ----
        sec("▶ YouTube")
        yt_file = tk.StringVar(value=os.path.basename(conf.get("yt_client_file", "")) or "keine Datei")
        yt_status = tk.StringVar(value="✅ verbunden" if conf.get("yt_token") else "nicht verbunden")

        def pick_yt():
            p = filedialog.askopenfilename(parent=win, title="client_secret….json von Google wählen",
                                           filetypes=[("JSON", "*.json")])
            if p:
                try:
                    uploader.yt_load_client_file(conf, p)
                    yt_file.set(os.path.basename(p))
                except Exception as e:
                    messagebox.showerror("YouTube", str(e), parent=win)

        def connect(fn, var):
            store()
            var.set("⏳ Browser geöffnet – bitte anmelden …")

            def job():
                try:
                    name = fn(conf, self.log)
                    self.ui(lambda: var.set(f"✅ {name}"))
                except Exception as e:
                    self.log(f"❌ {e}")
                    self.ui(lambda: var.set("❌ fehlgeschlagen – siehe Log"))
            threading.Thread(target=job, daemon=True).start()

        row("OAuth-Datei", ttk.Label(f, textvariable=yt_file, style="Muted.TLabel"),
            ttk.Button(f, text="Datei …", command=pick_yt))
        row("Status", ttk.Label(f, textvariable=yt_status, style="Muted.TLabel"),
            ttk.Button(f, text="Verbinden", command=lambda: connect(uploader.yt_connect, yt_status)))
        privs = {"Öffentlich": "public", "Nicht gelistet": "unlisted", "Privat": "private"}
        yt_priv = tk.StringVar(value=next(k for k, v in privs.items() if v == conf.get("yt_privacy", "public")))
        row("Sichtbarkeit", ttk.Combobox(f, textvariable=yt_priv, values=list(privs), state="readonly"))
        whats = {"Shorts (Hochkant 9:16)": "shorts", "Normale Videos (16:9)": "wide"}
        yt_what = tk.StringVar(value=next(k for k, v in whats.items() if v == conf.get("yt_what", "shorts")))
        row("Hochladen als", ttk.Combobox(f, textvariable=yt_what, values=list(whats), state="readonly"))
        yt_auto = tk.BooleanVar(value=conf.get("yt_auto", False))
        ttk.Checkbutton(f, text="Nach dem Clippen automatisch auf YouTube hochladen", variable=yt_auto).grid(
            row=r, column=0, columnspan=3, sticky="w"); r += 1

        # ---- TikTok ----
        sec("♪ TikTok")
        tt_key = tk.StringVar(value=conf.get("tt_client_key", ""))
        tt_sec = tk.StringVar(value=conf.get("tt_client_secret", ""))
        tt_status = tk.StringVar(value="✅ verbunden" if conf.get("tt_token") else "nicht verbunden")
        row("Client Key", ttk.Entry(f, textvariable=tt_key))
        row("Client Secret", ttk.Entry(f, textvariable=tt_sec, show="•"))
        row("Status", ttk.Label(f, textvariable=tt_status, style="Muted.TLabel"),
            ttk.Button(f, text="Verbinden", command=lambda: connect(uploader.tt_connect, tt_status)))
        modes = {"Als Entwurf in die TikTok-App (klappt sofort)": "draft",
                 "Direkt posten (erst nach TikTok-Prüfung öffentlich)": "direct"}
        tt_mode = tk.StringVar(value=next(k for k, v in modes.items() if v == conf.get("tt_mode", "draft")))
        row("Modus", ttk.Combobox(f, textvariable=tt_mode, values=list(modes), state="readonly"))
        tt_auto = tk.BooleanVar(value=conf.get("tt_auto", False))
        ttk.Checkbutton(f, text="Nach dem Clippen automatisch zu TikTok hochladen", variable=tt_auto).grid(
            row=r, column=0, columnspan=3, sticky="w"); r += 1

        # ---- Texte ----
        sec("✏ Titel & Beschreibung")
        title = tk.StringVar(value=conf.get("title"))
        row("Titel", ttk.Entry(f, textvariable=title))
        ttk.Label(f, text="{stream} = Streamtitel · {nr} = Platz in der Highlight-Liste", style="Muted.TLabel").grid(
            row=r, column=1, sticky="w"); r += 1
        desc = tk.Text(f, height=4, bg=FIELD, fg=TEXT, insertbackground=TEXT, relief="flat", font=("Segoe UI", 9))
        desc.insert("1.0", conf.get("description", ""))
        row("Beschreibung", desc)
        tags = tk.StringVar(value=conf.get("hashtags"))
        row("Hashtags", ttk.Entry(f, textvariable=tags))
        amax = tk.IntVar(value=conf.get("auto_max", 3))
        row("Auto-Upload: beste", ttk.Spinbox(f, from_=1, to=15, textvariable=amax, width=5))
        approve = tk.BooleanVar(value=conf.get("approve", True))
        ttk.Checkbutton(f, text="Ich will jeden Clip vorher ansehen und freigeben (empfohlen)",
                        variable=approve).grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 0)); r += 1
        ttk.Label(f, text="Clips mit Fehlern (schwarzes Bild, kein Ton, Standbild, falsche Länge) werden nie "
                          "automatisch hochgeladen.", style="Muted.TLabel", wraplength=560).grid(
            row=r, column=0, columnspan=3, sticky="w"); r += 1

        def store():
            conf.update({"yt_privacy": privs[yt_priv.get()], "yt_what": whats[yt_what.get()],
                         "yt_auto": bool(yt_auto.get()), "tt_client_key": tt_key.get().strip(),
                         "tt_client_secret": tt_sec.get().strip(), "tt_mode": modes[tt_mode.get()],
                         "tt_auto": bool(tt_auto.get()), "title": title.get(),
                         "description": desc.get("1.0", "end").strip(), "hashtags": tags.get(),
                         "approve": bool(approve.get())})
            try:
                conf["auto_max"] = int(amax.get())
            except Exception:
                pass
            uploader.save_conf(conf)

        bf = ttk.Frame(f, style="Panel.TFrame")
        bf.grid(row=r, column=0, columnspan=3, sticky="we", pady=(16, 0))

        def open_help():
            p = os.path.join(core.APP_DIR, "UPLOAD-EINRICHTEN.txt")
            if not os.path.isfile(p):
                try:
                    with open(p, "wb") as hf:
                        hf.write(updater._get(updater.REPO_RAW + "UPLOAD-EINRICHTEN.txt"))
                except Exception:
                    webbrowser.open("https://github.com/ximjxstinn/stream-clipper/blob/main/UPLOAD-EINRICHTEN.txt")
                    return
            try:
                os.startfile(p)
            except Exception:
                webbrowser.open(p)

        ttk.Button(bf, text="❓ So richtest du das ein", command=open_help).pack(side="left")
        ttk.Button(bf, text="Speichern", style="Accent.TButton",
                   command=lambda: (store(), self.log("✔ Upload-Einstellungen gespeichert"), win.destroy())).pack(
            side="right")

    def full_auto(self):
        src = self._source()
        if src:
            st = self.settings()
            self._work(self._full_auto_job, src, st)

    def _full_auto_job(self, src, st):
        src, hl = self._analyze_job(src, st)
        self._clip_job(src, list(enumerate(hl)), st, best_only=True)

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
        if self.s.get("live") and not st.get("mode", "").startswith("Maximal"):
            # Der Live-Clipper hat diese Streams schon live geclippt → nicht doppelt aus dem VOD clippen
            self.s.setdefault("seen", []).extend(v["id"] for v in new)
            self.s["seen"] = self.s["seen"][-100:]
            self.save()
            self.log(f"Auto-Modus: {len(new)} neues VOD – wurde schon vom Live-Clipper geclippt, überspringe.")
            return
        if core.is_live(st["channel"], self.runner):
            self.log("Auto-Modus: du bist gerade live – clippe, wenn der Stream vorbei ist.")
            return
        for v in reversed(new):
            self.log(f"⚡ Auto-Modus: neuer Stream gefunden – {v['title']}")
            self.notify("⚡ Neuer Stream gefunden", f"Clippe jetzt automatisch: {v['title']}")
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
    app = App(root)
    if "--minimized" in sys.argv:
        root.withdraw()  # direkt in den Infobereich
        # Falls das Tray-Modul nicht lädt, nach 20 s wenigstens in der Taskleiste zeigen
        root.after(20000, lambda: root.iconify() if app.tray is None else None)
    root.mainloop()


if __name__ == "__main__":
    main()
