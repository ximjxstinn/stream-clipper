# -*- coding: utf-8 -*-
"""
xImJxstinn Stream Clipper – Kern-Logik
Findet die besten Stellen in Twitch-VODs (Chat-Hype + Lautstärke) und schneidet sie.
Benötigte Tools (lädt die App beim ersten Start selbst in den Ordner "tools"):
  - yt-dlp.exe              (VOD-Liste, Audio + Clip-Download)
  - ffmpeg.exe / ffprobe.exe (Lautstärke-Analyse, Schneiden, TikTok-Format)
  - TwitchDownloaderCLI.exe (Chat eines VODs herunterladen)
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
import zipfile
from bisect import insort, bisect_left

APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv and sys.argv[0] else __file__))
TOOLS_DIR = os.path.join(APP_DIR, "tools")
WORK_DIR = os.path.join(APP_DIR, "cache")
IS_WIN = os.name == "nt"
EXE = ".exe" if IS_WIN else ""

BIN = 5  # Sekunden pro Analyse-Abschnitt

DEFAULT_KEYWORDS = (
    "kekw, lul, lulw, omegalul, icant, lmao, lol, xd, haha, hahaha, w, ww, www, "
    "pog, poggers, pogchamp, letsgo, lets go, let's go, clip, clip it, !clip, ???, "
    "wtf, holy, omg, krass, digga, alter, nein, ehrenlos, ehre, gg, sheesh, hä"
)
GTA_KEYWORDS = ("cops, polizei, bullen, wanted, sterne, flucht, verfolgung, crash, unfall, geklaut, klauen, "
                "auto, wasted, busted, explosion, boom, 💀, 😂, 😭, 🔥")


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- #
#  Hilfsfunktionen
# --------------------------------------------------------------------------- #
def fmt_time(sec, sep=":"):
    sec = max(0, int(round(sec)))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}{sep}{m:02d}{sep}{s:02d}"


def twitch_t(sec):
    sec = max(0, int(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m}m{s}s"


def parse_vod_id(text):
    text = (text or "").strip()
    m = re.search(r"videos/(\d+)", text)
    if m:
        return m.group(1)
    m = re.fullmatch(r"v?(\d{5,})", text)
    return m.group(1) if m else None


def vod_url(vod_id):
    return f"https://www.twitch.tv/videos/{vod_id}"


def tool(name):
    """Pfad zu einem Tool: erst tools-Ordner, dann PATH."""
    local = os.path.join(TOOLS_DIR, name + EXE)
    if os.path.isfile(local):
        return local
    return shutil.which(name)


class Runner:
    """Startet externe Programme, liest die Ausgabe live und kann abbrechen."""

    def __init__(self, log):
        self.log = log
        self.cancel_event = threading.Event()
        self.proc = None

    def cancel(self):
        self.cancel_event.set()
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    def check(self):
        if self.cancel_event.is_set():
            raise Cancelled()

    def run(self, cmd, cwd=None, on_line=None, quiet=False):
        self.check()
        kw = {}
        if IS_WIN:
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, **kw)
        tail = []
        for raw in iter(self.proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            tail.append(line)
            tail = tail[-15:]
            if on_line:
                on_line(line)
            elif not quiet:
                self.log(line)
            if self.cancel_event.is_set():
                break
        code = self.proc.wait()
        self.proc = None
        self.check()
        if code != 0:
            raise RuntimeError(f"{os.path.basename(cmd[0])} ist fehlgeschlagen (Code {code}):\n" + "\n".join(tail[-6:]))
        return code


# --------------------------------------------------------------------------- #
#  Tools herunterladen
# --------------------------------------------------------------------------- #
def _download(url, dest, log, runner, label):
    req = urllib.request.Request(url, headers={"User-Agent": "xImJxstinn-StreamClipper"})
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, -10
        with open(dest + ".part", "wb") as f:
            while True:
                runner.check()
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = int(done * 100 / total)
                    if pct >= last + 10:
                        last = pct
                        log(f"   {label}: {pct}%")
    _safe_replace(dest + ".part", dest, log)


def _safe_replace(src, dest, log):
    """Ersetzt eine Datei. Ist die alte gerade in Benutzung (Windows sperrt laufende .exe), bleibt sie."""
    try:
        os.replace(src, dest)
    except PermissionError:
        try:
            os.remove(src)
        except OSError:
            pass
        if os.path.isfile(dest):
            log(f"   ℹ {os.path.basename(dest)} wird gerade benutzt – behalte die vorhandene Version.")
        else:
            raise


def _extract(zip_path, wanted, log):
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            base = os.path.basename(info.filename)
            if base.lower() in wanted:
                target = os.path.join(TOOLS_DIR, base)
                with z.open(info) as src, open(target + ".new", "wb") as dst:
                    shutil.copyfileobj(src, dst)
                _safe_replace(target + ".new", target, log)
                log(f"   {base} bereit")
    os.remove(zip_path)


_TOOLS_LOCK = threading.Lock()


def ensure_tools(log, runner, force=False):
    # nur EIN Download gleichzeitig (Live-Clipper + Analyse wollten sonst parallel dieselbe Datei schreiben)
    with _TOOLS_LOCK:
        _ensure_tools(log, runner, force)


def _ensure_tools(log, runner, force=False):
    os.makedirs(TOOLS_DIR, exist_ok=True)
    if force or not tool("yt-dlp"):
        log("⬇ Lade yt-dlp …")
        _download("https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp" + EXE,
                  os.path.join(TOOLS_DIR, "yt-dlp" + EXE), log, runner, "yt-dlp")
    if force or not (tool("ffmpeg") and tool("ffprobe")):
        log("⬇ Lade ffmpeg (ca. 130 MB, nur beim ersten Mal) …")
        z = os.path.join(TOOLS_DIR, "ffmpeg.zip")
        _download("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
                  z, log, runner, "ffmpeg")
        _extract(z, {"ffmpeg.exe", "ffprobe.exe"}, log)
    if force or not tool("TwitchDownloaderCLI"):
        log("⬇ Lade TwitchDownloaderCLI (für den Chat) …")
        req = urllib.request.Request("https://api.github.com/repos/lay295/TwitchDownloader/releases/latest",
                                     headers={"User-Agent": "xImJxstinn-StreamClipper"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rel = json.load(r)
        asset = next((a for a in rel.get("assets", [])
                      if "cli" in a["name"].lower() and "windows-x64" in a["name"].lower()
                      and a["name"].lower().endswith(".zip")), None)
        if not asset:
            raise RuntimeError("TwitchDownloaderCLI-Download nicht gefunden.")
        z = os.path.join(TOOLS_DIR, "tdcli.zip")
        _download(asset["browser_download_url"], z, log, runner, "TwitchDownloaderCLI")
        _extract(z, {"twitchdownloadercli.exe"}, log)
    log("✔ Tools bereit")


# --------------------------------------------------------------------------- #
#  VOD-Liste eines Kanals
# --------------------------------------------------------------------------- #
def list_vods(channel, runner, limit=15):
    channel = channel.strip().lstrip("@").lower()
    out = []

    def collect(line):
        out.append(line)

    runner.run([tool("yt-dlp"), "--flat-playlist", "-J", "--playlist-end", str(limit),
                f"https://www.twitch.tv/{channel}/videos?filter=archives&sort=time"], on_line=collect)
    line = next((l for l in out if l.lstrip().startswith("{")), "")
    data = json.loads(line) if line else {}
    vods = []
    for e in data.get("entries") or []:
        vid = parse_vod_id(str(e.get("id") or e.get("url") or ""))
        if vid:
            dur = e.get("duration")
            vods.append({"id": vid, "title": e.get("title") or f"VOD {vid}",
                         "duration": dur, "date": e.get("upload_date") or ""})
    return vods


def is_live(channel, runner):
    """True, wenn der Kanal gerade live ist (dann ist das neueste VOD noch nicht fertig)."""
    try:
        runner.run([tool("yt-dlp"), "--skip-download", "--print", "is_live",
                    f"https://www.twitch.tv/{channel.strip().lstrip('@').lower()}"], quiet=True)
        return True
    except RuntimeError:
        return False


# --------------------------------------------------------------------------- #
#  Daten sammeln: Chat + Audio
# --------------------------------------------------------------------------- #
def download_chat(vod_id, workdir, runner, log):
    path = os.path.join(workdir, "chat.json")
    if os.path.isfile(path) and os.path.getsize(path) > 100:
        log("✔ Chat schon im Cache")
        return path
    td = tool("TwitchDownloaderCLI")
    if not td:
        raise RuntimeError("TwitchDownloaderCLI fehlt")
    log("💬 Lade Chat vom VOD …")
    last = [""]

    def on_line(l):
        m = re.search(r"(\d{1,3})%", l)
        if m and m.group(1) != last[0] and int(m.group(1)) % 10 == 0:
            last[0] = m.group(1)
            log(f"   Chat: {m.group(1)}%")

    runner.run([td, "chatdownload", "--id", vod_id, "-o", path], cwd=workdir, on_line=on_line)
    return path


BOTS = {"nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "wizebot", "sery_bot", "soundalerts",
        "botrixoficial", "kofistreambot", "pokemoncommunitygame", "commanderroot", "blerp", "own3d", "deepbot",
        "phantombot", "coebot", "vivbot", "streamcaptainbot", "lolrankbot", "frostytoolsdotcom"}
GREETING = re.compile(r"^\W*(hi+|hey+|hallo+|moin+|servus|hello|huhu|na+|yo+|guten (morgen|tag|abend)|n'?abend|"
                      r"gn+|gute nacht|bye+|tschüss|ciao|lurk|bin da|wb|welcome)\b", re.I)


def load_chat(path):
    """Lädt den Chat und wirft alles raus, was kein echter Hype ist."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    streamer = ((data.get("streamer") or {}).get("name") or "").lower()
    msgs, raid_msgs, clip_reqs, removed = [], [], [], 0
    for c in data.get("comments", []):
        t = c.get("content_offset_seconds")
        if t is None:
            continue
        m = c.get("message") or {}
        body = (m.get("body") or "").strip()
        com = c.get("commenter") or {}
        user = (com.get("name") or com.get("display_name") or "?").lower()
        low = body.lower()
        if "raid" in low or "raider" in low:
            raid_msgs.append(float(t))
        if user not in BOTS and re.search(r"(^|\s)!?(clip|clippen|clip it|clip das|clipt)(\s|$|!)", low):
            clip_reqs.append(float(t))
        if (user in BOTS or user.endswith("bot") or user == streamer
                or (m.get("user_notice_params") or {}).get("msg-id")          # Subs/Gifts/Raids-Meldungen
                or (low.startswith("!") and not low.startswith("!clip"))      # Befehle
                or (GREETING.match(low) and len(low) < 40)                    # Begrüßungen
                or "raid" in low):
            removed += 1
            continue
        msgs.append((float(t), user, body))
    msgs.sort(key=lambda x: x[0])
    # Raid = mind. 3 Raid-Nachrichten innerhalb von 60 s
    raid_msgs.sort()
    raids = []
    for k in range(len(raid_msgs) - 2):
        if raid_msgs[k + 2] - raid_msgs[k] <= 60 and (not raids or raid_msgs[k] - raids[-1] > 300):
            raids.append(raid_msgs[k])
    length = None
    v = data.get("video") or {}
    if v.get("length"):
        length = float(v["length"])
    elif v.get("end"):
        length = float(v["end"])
    return msgs, length, {"raids": raids, "removed": removed, "streamer": streamer, "clips": clip_reqs}


def download_audio(vod_id, workdir, runner, log):
    for f in os.listdir(workdir):
        if f.startswith("audio.") and not f.endswith(".part") and not f.endswith(".ytdl"):
            log("✔ Audio schon im Cache")
            return os.path.join(workdir, f)
    log("🔊 Lade Audiospur vom VOD (für die Lautstärke-Analyse) …")
    last = [-10]

    def on_line(l):
        m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", l)
        if m:
            p = int(float(m.group(1)))
            if p >= last[0] + 10:
                last[0] = p
                log(f"   Audio: {p}%")
        elif "ERROR" in l:
            log(l)

    runner.run([tool("yt-dlp"), "--newline", "--no-part", "-N", "8",
                "-f", "Audio_Only/audio_only/bestaudio/worst",
                "--ffmpeg-location", os.path.dirname(tool("ffmpeg")),
                "-o", os.path.join(workdir, "audio.%(ext)s"), vod_url(vod_id)], on_line=on_line)
    for f in os.listdir(workdir):
        if f.startswith("audio."):
            return os.path.join(workdir, f)
    raise RuntimeError("Audio-Download fehlgeschlagen")


def media_duration(path, runner):
    out = []
    runner.run([tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", path], on_line=out.append)
    try:
        return float(out[-1])
    except Exception:
        return None


def analyze_loudness(media_path, workdir, runner, log, duration=None):
    """Lautstärke (RMS in dB) pro Sekunde über ffmpeg/astats."""
    cache = os.path.join(workdir, "voice_v3.json")
    if os.path.isfile(cache):
        log("✔ Lautstärke schon im Cache")
        with open(cache) as f:
            return json.load(f)
    log("📈 Messe deine Stimme (Sprach-Frequenzen, ohne Bass/Explosionen) …")
    out_file = "rms.txt"
    rate = 8000
    last = [-10]

    def on_line(l):
        if duration and l.startswith("out_time_ms="):
            try:
                p = int(int(l.split("=")[1]) / 1e6 / duration * 100)
            except ValueError:
                return
            if p >= last[0] + 10:
                last[0] = p
                log(f"   Lautstärke: {min(p, 100)}%")

    # Wie viele Tonspuren? (OBS nimmt Mikro oft auf einer eigenen Spur auf)
    tracks = []
    try:
        runner.run([tool("ffprobe"), "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                    "-of", "csv=p=0", media_path], on_line=tracks.append)
    except Exception:
        tracks = ["0"]
    n = len([t for t in tracks if t.strip()])
    chain = (f"aresample={rate},highpass=f=250,lowpass=f=3400,asetnsamples={rate},astats=metadata=1:reset=1,"
             f"ametadata=print:key=lavfi.astats.Overall.RMS_level:file={out_file}")
    if n > 1:
        log(f"   🎚 {n} Tonspuren gefunden – höre alle zusammen ab (damit dein Mikro sicher dabei ist)")
        mix = "".join(f"[0:a:{k}]" for k in range(n)) + f"amix=inputs={n}:normalize=0,pan=mono|c0=c0,"
        audio_args = ["-filter_complex", mix + chain]
    else:
        audio_args = ["-ac", "1", "-af", chain]
    if n == 0:
        raise RuntimeError("Die Aufnahme hat keine Tonspur.")
    runner.run([tool("ffmpeg"), "-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1",
                "-i", media_path, "-vn", *audio_args, "-f", "null", "-"], cwd=workdir, on_line=on_line)
    db = parse_rms_file(os.path.join(workdir, out_file))
    with open(cache, "w") as f:
        json.dump(db, f)
    return db


def parse_rms_file(path):
    vals = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "RMS_level=" in line:
                v = line.split("=", 1)[1].strip()
                try:
                    x = float(v)
                    if math.isinf(x) or math.isnan(x):
                        x = -90.0
                except ValueError:
                    x = -90.0
                vals.append(max(-90.0, x))
    return vals


# --------------------------------------------------------------------------- #
#  Szenen-Erkennung im Bild: wie viel passiert pro Sekunde?
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#  GTA-Szenen im Bild: Polizei (Minimap blinkt rot/blau) + Explosionen (heller, warmer Blitz)
# --------------------------------------------------------------------------- #
MM_BOX = (0.008, 0.172, 0.82, 0.96)  # Minimap unten links (x0, x1, y0, y1) als Anteil vom Bild


def minimap_colors(pixels):
    """pixels: Liste (r, g, b) aus der Minimap. Gibt (rot_anteil, blau_anteil) zurück."""
    n = max(1, len(pixels))
    red = sum(1 for r, g, b in pixels if r > 90 and r - max(g, b) > 40)
    blue = sum(1 for r, g, b in pixels if b > 110 and b - max(r, g) > 40)
    return red / n, blue / n


def police_score(mm):
    """mm: Liste (rot, blau) der letzten ~4 s (4 pro Sekunde). 1 = Minimap blinkt rot/blau = Cops sind hinter dir."""
    reds = [r > 0.35 for r, _ in mm]
    blues = [b > 0.35 for _, b in mm]
    nr, nb = sum(reds), sum(blues)
    if nr < 2 or nb < 2:
        return 0.0
    state = ["r" if r else ("b" if b else "") for r, b in zip(reds, blues)]
    seq = [x for x in state if x]
    switches = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
    return 1.0 if switches >= 2 else 0.0


def frame_look(pixels):
    """pixels: (r, g, b) eines verkleinerten Bildes → (helligkeit, wärme, sättigung, weiß-anteil)."""
    n = max(1, len(pixels))
    r = sum(p[0] for p in pixels) / n
    g = sum(p[1] for p in pixels) / n
    b = sum(p[2] for p in pixels) / n
    sat = sum(max(p) - min(p) for p in pixels) / n
    white = sum(1 for p in pixels if min(p) > 200) / n
    return (r * 77 + g * 150 + b * 29) / 256, r - b, sat, white


# 🔫 Waffen-Anzeige oben rechts (Munition „30 240“ + Waffen-Symbol) = du hast eine Waffe gezogen → Kampf/Raub
HUD_BOX = (0.789, 1.0, 0.0, 0.25)   # oben rechts (x0, x1, y0, y1) als Anteil vom Bild
HUD_W, HUD_H = 60, 40               # so groß wird der Ausschnitt ausgewertet


def ammo_hud(crop):
    """crop: HUD_H x HUD_W x 3 (numpy, uint8/int). 1 = Munitions-Zahlen sichtbar (Waffe in der Hand), sonst 0."""
    import numpy as np
    fr = np.asarray(crop, dtype=np.int16).reshape(HUD_H, HUD_W, 3)
    mx = fr.max(2)
    mn = fr.min(2)
    br = (mx > 135) & (mx - mn < 60)   # helle, graue/weiße Schrift
    for r in range(3, 24):
        band = br[r:r + 3, 34:59]
        n = int(band.sum())
        if n < 4 or n > 40:
            continue
        if br[max(0, r - 2):r, 34:59].sum() > 3 or br[r + 3:r + 5, 34:59].sum() > 6:
            continue  # Zahlen stehen frei (kein heller Himmel/Wand)
        idx = np.where(band.any(0))[0]
        L, R = int(idx[0]), int(idx[-1])
        if R < 17 or R - L < 8 or R - L > 20:
            continue  # rechtsbündig, zwei Zahlen breit
        cols = band.any(0)[L:R + 1]
        run = gaps = 0
        for c in cols:
            run = 0 if c else run + 1
            if run == 2:
                gaps += 1
        if gaps < 1:
            continue  # Lücke zwischen Magazin und Reserve
        if mx[r:r + 3, 34:59][~band].mean() > 110:
            continue
        win = fr[max(0, r - 1):r + 4, 34 + L:34 + R + 1]
        if ((win[..., 0] > 100) & (win[..., 0] - np.maximum(win[..., 1], win[..., 2]) > 50)).sum() >= 3:
            continue  # rot-weiße Status-Symbole (Fahndung o. ä.), keine Zahlen
        if br[r:r + 3, 24:34].sum() > 2:
            continue  # Text läuft nach links weiter = Geld-Anzeige ($208,139), keine Munition
        up = br[max(0, r - 6):max(0, r - 1), 24:59]
        if up.any():
            ui = np.where(up.any(0))[0]
            if ui[-1] - ui[0] > 9 and up.sum() >= 6:
                continue  # zweite Textzeile direkt darüber = Geld (Bargeld + Bank), keine Munition
        return 1
    return 0


def flash_score(looks):
    """Explosion im Bild: in ≤0,75 s deutlich heller, ORANGE (warm + farbig) und nicht weiß überstrahlt.
    looks: frame_look()-Werte der letzten ~2 s (4 pro Sekunde). Weiße Überblendungen (Figurwechsel,
    Himmel, Ladebildschirm) zählen nicht."""
    if len(looks) < 4:
        return 0.0
    peak = max(range(len(looks)), key=lambda k: looks[k][0])
    before = [x[0] for x in looks[max(0, peak - 3):peak]]
    if not before:
        return 0.0
    luma, warm, sat, white = looks[peak]
    jump = luma - min(before)
    if jump >= 45 and warm >= 25 and 110 <= luma <= 205 and sat >= 55 and white <= 0.28:
        return min(3.0, jump / 45)
    return 0.0


def boom_near(low, sec, before=4):
    """low: Tiefbass-Pegel (dB) alle 0,1 s. Gibt True, wenn um Sekunde `sec` ein lauter Bass-Knall ist."""
    if not low:
        return True  # ohne Ton nicht blockieren
    k = int(sec * 10)
    pre = low[max(0, k - before * 10):max(0, k - 10)]
    post = low[max(0, k - 10):k + 20]
    if not pre or not post:
        return False
    pre_s = sorted(pre)
    return max(post) - pre_s[len(pre_s) // 2] >= 20 and max(post) >= -42


def analyze_bass(media, workdir, runner):
    """Tiefbass (<150 Hz) alle 0,1 s in dB – für Explosionen/Crashes."""
    out = "low.txt"
    runner.run([tool("ffmpeg"), "-hide_banner", "-nostats", "-loglevel", "error", "-i", media, "-vn", "-af",
                "aresample=8000,lowpass=f=150,lowpass=f=150,asetnsamples=n=800:p=0,astats=metadata=1:reset=1,"
                f"ametadata=print:key=lavfi.astats.Overall.RMS_level:file={out}", "-f", "null", "-"], cwd=workdir)
    vals = []
    try:
        with open(os.path.join(workdir, out), encoding="utf-8", errors="replace") as f:
            for line in f:
                if "RMS_level=" in line:
                    v = line.split("=", 1)[1].strip()
                    try:
                        vals.append(max(-120.0, float(v)))
                    except ValueError:
                        vals.append(-120.0)
    except OSError:
        pass
    return vals


def analyze_scenes(src, workdir, runner, log, duration=None):
    """Pro Sekunde: (polizei 0/1, explosion 0..3). Nutzt das Mini-Video der Bild-Analyse."""
    cache = os.path.join(workdir, "scenes_v1.json")
    if os.path.isfile(cache):
        with open(cache) as f:
            return json.load(f)
    if src["kind"] == "vod":
        media = next((os.path.join(workdir, f) for f in os.listdir(workdir) if f.startswith("lowres.")), None)
        if not media:
            return []
        pre = []
    else:
        media, pre = src["path"], ["-hwaccel", "auto"]
    log("🚔 Suche Polizei-Verfolgungen und Explosionen im Bild …")
    W, H = 128, 72
    x0, x1 = int(MM_BOX[0] * W), int(MM_BOX[1] * W)
    y0, y1 = int(MM_BOX[2] * H), int(MM_BOX[3] * H)
    mw, mh = x1 - x0, y1 - y0
    vf = (f"fps=4,scale={W}:{H},split[a][b];[a]crop={mw}:{mh}:{x0}:{y0}[m];"
          f"[b]scale=16:{mh}[f];[m][f]hstack")
    kw = {"creationflags": 0x08000000} if IS_WIN else {}
    proc = subprocess.Popen([tool("ffmpeg"), "-v", "error", *pre, "-i", media, "-an", "-filter_complex", vf,
                             "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], stdout=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, **kw)
    fw = mw + 16
    size = fw * mh * 3
    frames = []
    last = [-10]
    try:
        while True:
            runner.check()
            buf = proc.stdout.read(size)
            if not buf or len(buf) < size:
                break
            mm, full = [], []
            for y in range(mh):
                row = buf[y * fw * 3:(y + 1) * fw * 3]
                for x in range(fw):
                    px = (row[3 * x], row[3 * x + 1], row[3 * x + 2])
                    (mm if x < mw else full).append(px)
            red, blue = minimap_colors(mm)
            frames.append((red, blue, frame_look(full)))
            if duration and len(frames) % 400 == 0:
                pc = int(len(frames) / 4 / duration * 100)
                if pc >= last[0] + 20:
                    last[0] = pc
                    log(f"   Szenen: {min(pc, 100)}%")
    finally:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        low = analyze_bass(media, workdir, runner)
    except Cancelled:
        raise
    except Exception:
        low = []
    out = []
    for sec in range(len(frames) // 4):
        win = frames[max(0, sec * 4 - 8):sec * 4 + 8]
        pol = police_score([(f[0], f[1]) for f in win])
        fw2 = frames[max(0, sec * 4 - 4):sec * 4 + 4]
        boom = flash_score([f[2] for f in fw2])
        if boom and not boom_near(low, sec):
            boom = 0.0  # heller Blitz ohne Knall = Überblendung/Szenenwechsel, keine Explosion
        out.append([pol, round(boom, 2)])
    with open(cache, "w") as f:
        json.dump(out, f)
    secs_p = sum(1 for p, _ in out if p)
    secs_b = sum(1 for _, b in out if b)
    log(f"   🚔 {secs_p // 60} Min. Polizei-Action · 💥 {secs_b} Explosions-Blitze erkannt")
    return out


def analyze_motion(src, workdir, runner, log, duration=None):
    """Bewegung im Bild pro Sekunde (0 = Standbild, hoch = Action/Verfolgung/Kampf/Kameraschwenk)."""
    cache = os.path.join(workdir, "motion_v1.json")
    if os.path.isfile(cache):
        log("✔ Bild-Analyse schon im Cache")
        with open(cache) as f:
            return json.load(f)
    if src["kind"] == "vod":
        log("🎬 Lade das VOD in Mini-Qualität für die Szenen-Erkennung …")
        last = [-10]

        def on_dl(l):
            m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", l)
            if m and int(float(m.group(1))) >= last[0] + 20:
                last[0] = int(float(m.group(1)))
                log(f"   Bild-Download: {last[0]}%")
        runner.run([tool("yt-dlp"), "--newline", "--no-part", "-N", "8",
                    "-f", "160p/160p30/worst[vcodec!=none]/worst", "--ffmpeg-location",
                    os.path.dirname(tool("ffmpeg")), "-o", os.path.join(workdir, "lowres.%(ext)s"),
                    vod_url(src["id"])], on_line=on_dl)
        media = next((os.path.join(workdir, f) for f in os.listdir(workdir) if f.startswith("lowres.")), None)
        if not media:
            raise RuntimeError("Mini-Video-Download fehlgeschlagen")
        pre = []
    else:
        media, pre = src["path"], ["-hwaccel", "auto"]
    log("🎬 Analysiere das Bild (Action, Verfolgungen, Kämpfe, Szenenwechsel) …")
    out = "motion.txt"
    last = [-10]

    def on_line(l):
        if duration and l.startswith("out_time_ms="):
            try:
                p = int(int(l.split("=")[1]) / 1e6 / duration * 100)
            except ValueError:
                return
            if p >= last[0] + 10:
                last[0] = p
                log(f"   Bild: {min(p, 100)}%")
    runner.run([tool("ffmpeg"), "-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1", *pre,
                "-i", media, "-an", "-vf", "fps=1,scale=96:54,format=gray,tblend=all_mode=difference,signalstats,"
                f"metadata=print:key=lavfi.signalstats.YAVG:file={out}", "-f", "null", "-"],
               cwd=workdir, on_line=on_line)
    vals = []
    with open(os.path.join(workdir, out), encoding="utf-8", errors="replace") as f:
        for line in f:
            if "YAVG=" in line:
                try:
                    vals.append(round(float(line.split("=", 1)[1]), 2))
                except ValueError:
                    vals.append(0.0)
    with open(cache, "w") as f:
        json.dump(vals, f)
    return vals


# --------------------------------------------------------------------------- #
#  Highlight-Erkennung v2
# --------------------------------------------------------------------------- #
LEARN_FILE = os.path.join(APP_DIR, "lernen.json")
FEATURES = ["Chat-Hype", "Hype-Wörter", "Deine Stimme", "Viele Leute"]


def rolling_median(values, half):
    """Gleitender Median (Fenster ±half) – Basiswert 'wie viel ist normal hier'."""
    n = len(values)
    if n == 0:
        return []
    out = [0.0] * n
    window = []
    lo = hi = 0
    for i in range(n):
        want_lo, want_hi = max(0, i - half), min(n, i + half + 1)
        while hi < want_hi:
            insort(window, values[hi]); hi += 1
        while lo < want_lo:
            del window[bisect_left(window, values[lo])]; lo += 1
        k = len(window)
        out[i] = window[k // 2] if k % 2 else (window[k // 2 - 1] + window[k // 2]) / 2
    return out


def smooth(values, radius):
    n = len(values)
    pre = [0.0]
    for v in values:
        pre.append(pre[-1] + v)
    return [(pre[min(n, i + radius + 1)] - pre[max(0, i - radius)]) / (min(n, i + radius + 1) - max(0, i - radius))
            for i in range(n)]


def percentile(values, p):
    s = sorted(values)
    if not s:
        return 0.0
    return s[min(len(s) - 1, int(len(s) * p))]


def parse_keywords(text):
    return [k.strip().lower() for k in (text or "").split(",") if k.strip()]


def count_keywords(msg, keywords):
    low = msg.lower()
    tokens = set(re.findall(r"[\wäöüß!?']+", low))
    hits = 0
    for k in keywords:
        if " " in k or not re.fullmatch(r"[\wäöüß']+", k):
            if k in low:
                hits += 1
        elif k in tokens:
            hits += 1
    if re.search(r"(ha){3,}|(xd){2,}|\?{3,}|!{3,}|(kekw\s*){2,}|(lul\s*){2,}", low):
        hits += 1
    return min(hits, 3)


# ---- Lernen aus deinen 👍/👎 ------------------------------------------------
def load_learning():
    try:
        with open(LEARN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"examples": []}


def save_feedback(features, good):
    data = load_learning()
    data["examples"].append({"f": [round(x, 4) for x in features], "good": bool(good)})
    data["examples"] = data["examples"][-500:]
    with open(LEARN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return len(data["examples"])


def train_model(examples):
    """Kleine logistische Regression: lernt, welche Signale DIR wichtig sind."""
    pos = sum(1 for e in examples if e["good"])
    neg = len(examples) - pos
    if pos < 2 or neg < 2:
        return None
    k = len(examples[0]["f"])
    w = [0.0] * (k + 1)
    wp, wn = len(examples) / (2 * pos), len(examples) / (2 * neg)  # Klassen ausgleichen
    for _ in range(400):
        grad = [0.0] * (k + 1)
        for e in examples:
            x = e["f"] + [1.0]
            z = sum(a * b for a, b in zip(w, x))
            p = 1 / (1 + math.exp(-max(-30, min(30, z))))
            err = (p - (1 if e["good"] else 0)) * (wp if e["good"] else wn)
            for j in range(k + 1):
                grad[j] += err * x[j]
        for j in range(k + 1):
            w[j] -= 0.3 * (grad[j] / len(examples) + 0.01 * w[j])
    return w


def model_prob(w, f):
    z = sum(a * b for a, b in zip(w, f + [1.0]))
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def find_highlights(msgs=None, loudness=None, duration=None, *, count=10, pre=18, post=10,
                    min_gap=90, skip_start=120, w_chat=0.65, keywords=DEFAULT_KEYWORDS,
                    raids=None, clip_reqs=None, max_len=60, log=None, motion=None, maximal=False, scenes=None):
    """Gibt (highlights, kurve) zurück. kurve = Liste (zeit, score) für die Grafik."""
    log = log or (lambda *_: None)
    msgs = msgs or []
    loudness = loudness or []
    raids = raids or []
    clip_reqs = clip_reqs or []
    kws = parse_keywords(keywords)
    if not duration:
        duration = max([m[0] for m in msgs[-1:]] + [len(loudness)] + [1])
    nb = int(duration // BIN) + 1

    # --- Chat -----------------------------------------------------------------
    uniq = [set() for _ in range(nb)]
    kw = [0.0] * nb
    for t, user, body in msgs:
        b = int(t // BIN)
        if 0 <= b < nb:
            uniq[b].add(user)
            kw[b] += count_keywords(body, kws)
    u = [len(s) for s in uniq]
    minutes = max(1.0, duration / 60)
    chatters_per_min = sum(u) / minutes
    has_chat = sum(u) >= 30

    chat_score = [0.0] * nb
    kw_score = [0.0] * nb
    crowd = [0.0] * nb
    if has_chat:
        # 15-Sekunden-Fenster: Reaktionen verteilen sich über ein paar Sekunden
        u3 = [sum(u[max(0, i - 1):i + 2]) for i in range(nb)]
        k3 = [sum(kw[max(0, i - 1):i + 2]) for i in range(nb)]
        base = rolling_median(u3, 60)
        mean_u3 = sum(u3) / nb
        min_people = max(3, int(round(mean_u3 * 2.5)))  # echter Hype = deutlich mehr Leute als sonst
        for i in range(nb):
            if u3[i] < min_people or u3[i] < 2 * base[i] + 1:
                continue
            chat_score[i] = (u3[i] - base[i]) / math.sqrt(base[i] + 2)
            kw_score[i] = k3[i] / max(1, u3[i])  # Anteil Hype-Nachrichten
            crowd[i] = u3[i] / max(1.0, mean_u3)
        # Raids verfälschen alles (Begrüßungs-Spam) → 2 Min danach ignorieren
        for rt in raids:
            for i in range(int(rt // BIN) - 2, int((rt + 120) // BIN)):
                if 0 <= i < nb:
                    chat_score[i] = kw_score[i] = crowd[i] = 0.0

    # --- Stimme (Lautstärke im Sprach-Frequenzbereich) --------------------------
    voice = [0.0] * nb
    voice_db = [None] * nb
    has_audio = len(loudness) > 30
    if has_audio:
        sustained = [-90.0] * nb
        for i in range(nb):
            seg = loudness[i * BIN:(i + 1) * BIN + 1]
            if len(seg) > 1:
                # mind. 2 Sekunden AM STÜCK laut (Schreien/Lachen) – einzelne Knaller zählen nicht
                sustained[i] = max(min(seg[k], seg[k + 1]) for k in range(len(seg) - 1))
            elif seg:
                sustained[i] = seg[0]
        base = rolling_median(sustained, 60)
        for i in range(nb):
            diff = sustained[i] - base[i]
            voice_db[i] = diff
            voice[i] = max(0.0, diff - 4.0) / 8.0  # ab +4 dB über normal, +12 dB = 1.0
        voice = smooth(voice, 1)

    # --- Gewichtung automatisch anpassen -----------------------------------------
    wc = w_chat
    if not has_chat:
        wc = 0.0
        log("ℹ Kaum Chat – es zählt vor allem deine Stimme.")
    elif chatters_per_min < 1.5 and has_audio:
        wc = min(wc, 0.25)
        log(f"ℹ Ruhiger Chat ({chatters_per_min:.1f} Chatter/Min) – Stimme wird stärker gewichtet.")
    elif chatters_per_min < 4 and has_audio:
        wc = min(wc, 0.45)
    if not has_audio:
        wc = 1.0
    wa = 1 - wc

    def norm(arr, floor):
        p = max(floor, percentile([a for a in arr if a > 0], 0.9) if any(arr) else 0)
        return [min(a / p, 3.0) for a in arr]

    cn = norm(chat_score, 2.0)
    kn = [min(k / 0.5, 2.0) for k in kw_score]  # 50 % Hype-Nachrichten = 1.0
    an = [min(v, 3.0) for v in voice]
    crn = [min(c / 4.0, 2.0) for c in crowd]
    combined = [wc * (0.7 * cn[i] + 0.3 * kn[i] * min(1.0, cn[i] * 2)) + wa * an[i] for i in range(nb)]
    # --- Szenen im Bild: viel Bewegung im Vergleich zum Rest = Action ---------------
    vn = [0.0] * nb
    if motion and len(motion) > 30:
        mb = [0.0] * nb
        for i in range(nb):
            seg = motion[i * BIN:(i + 1) * BIN]
            mb[i] = sum(seg) / len(seg) if seg else 0.0
        base = rolling_median(mb, 60)
        raw = [0.0 if mb[i] < 1.5 else max(0.0, (mb[i] - base[i]) / (base[i] * 0.6 + 1.0)) for i in range(nb)]
        raw = smooth(raw, 1)
        vn = norm(raw, 0.8)
        wv = 0.6 if maximal else 0.35
        if not has_chat and not has_audio:
            wv = 1.0
        combined = [combined[i] + wv * vn[i] for i in range(nb)]
    # --- GTA-Szenen: Polizei hinter dir (Minimap blinkt) + Explosionen = die wichtigsten Momente --------
    pn = [0.0] * nb
    bn = [0.0] * nb
    if scenes and len(scenes) > 30:
        for i in range(nb):
            seg = scenes[i * BIN:(i + 1) * BIN]
            if seg:
                pn[i] = sum(1 for p_, _ in seg if p_) / len(seg)
                bn[i] = max(b_ for _, b_ in seg)
        bn = [max(bn[max(0, i - 1):i + 2]) for i in range(nb)]  # Explosion strahlt auf die Nachbar-Abschnitte
        combined = [combined[i] + 1.4 * pn[i] + 0.9 * min(bn[i], 2.0) for i in range(nb)]
        # Langes Gerede ohne Action (Stimme laut, aber Bild ruhig, keine Cops/Explosion) abwerten
        if any(pn) or any(bn):
            # Es gab echte GTA-Action → die hat Vorrang. Reden/Lachen ohne Cops/Explosion zählt viel weniger.
            near_act = [max(pn[max(0, i - 6):i + 7] + bn[max(0, i - 6):i + 7]) for i in range(nb)]
            for i in range(nb):
                if near_act[i] == 0:
                    combined[i] *= 0.3
    # "clip" / "!clip" im Chat (auch von dir) = jemand fand die Stelle gut genug zum Clippen
    clip_bins = [0] * nb
    for ct in clip_reqs:
        b = int(ct // BIN)
        for j in range(b - 2, b + 1):
            if 0 <= j < nb:
                clip_bins[j] += 1
    for i in range(nb):
        if clip_bins[i]:
            combined[i] += 0.8 + 0.2 * min(clip_bins[i], 4)

    long_rec = duration >= 15 * 60  # Anfang/Ende nur bei langen Streams abwerten
    skip_bins = int(skip_start // BIN) if long_rec else 0
    for i in range(nb):
        if i < skip_bins:
            combined[i] = 0.0
        elif long_rec and i > nb - 24:  # letzte 2 Min = "bye chat"
            combined[i] *= 0.3

    # --- Kandidaten (mit Mindestabstand) ----------------------------------------
    order = sorted(range(nb), key=lambda i: combined[i], reverse=True)
    gap_bins = max(1, int(min_gap // BIN))
    cands = []
    for i in order:
        if combined[i] < (0.06 if maximal else 0.15) or len(cands) >= count * 3:
            break
        if all(abs(i - j) >= gap_bins for j in cands):
            cands.append(i)
    weak = False
    if len(cands) < min(3, count):
        # Nichts Starkes gefunden → mildere Suche: relativ lauteste/aktivste Momente der Aufnahme
        weak = True
        soft = [0.0] * nb
        for i in range(nb):
            if i < skip_bins:
                continue
            a = max(0.0, voice_db[i]) / 12.0 if (has_audio and voice_db[i] is not None) else 0.0
            soft[i] = combined[i] + 0.5 * a + 0.3 * (cn[i] if has_chat else 0.0)
        soft = smooth(soft, 1)
        for i in sorted(range(nb), key=lambda k: soft[k], reverse=True):
            if soft[i] <= 0.02 or len(cands) >= count * 3:
                break
            if all(abs(i - j) >= gap_bins for j in cands):
                cands.append(i)
                combined[i] = max(combined[i], soft[i])
        if cands:
            log("ℹ Keine richtig lauten/hypen Stellen – nehme die relativ stärksten Momente (eher ruhige Aufnahme).")
    if not cands:
        return [], [(i * BIN, combined[i]) for i in range(nb)]

    # --- Gelernter Geschmack -----------------------------------------------------
    model = train_model(load_learning()["examples"])
    feats = {i: [cn[i], kn[i], an[i], crn[i]] for i in cands}
    if model:
        final = {i: combined[i] * (0.4 + 1.2 * model_prob(model, feats[i])) for i in cands}
    else:
        final = {i: combined[i] for i in cands}
    ranked = sorted(cands, key=lambda i: final[i], reverse=True)
    top = final[ranked[0]] or 1e-9
    # Lieber weniger, dafür gute Clips: schwache Kandidaten fliegen raus
    ref = final[ranked[min(2, len(ranked) - 1)]]  # dritt-bester als Maßstab, damit ein Mega-Moment nicht alles killt
    chosen = [i for n, i in enumerate(ranked[:count]) if maximal or weak or n < 3 or final[i] >= 0.45 * ref]
    if len(chosen) < len(ranked[:count]):
        log(f"ℹ Nur {len(chosen)} Stellen waren wirklich stark – den Rest lasse ich weg.")

    highlights = []
    for i in chosen:
        t = i * BIN + BIN / 2
        chat_led = has_chat and wc * cn[i] > 0 and wc * cn[i] >= wa * an[i]
        # Den eigentlichen Moment finden: Chat reagiert 5-20 s später (Stream-Delay + Reaktionszeit)
        visual_led = vn[i] > 0.3 and vn[i] >= an[i] and not chat_led
        boom_secs = [(scenes[k][1], k) for k in range(max(0, i * BIN - BIN), min(len(scenes or []), i * BIN + 2 * BIN))
                     if scenes[k][1] > 0] if scenes else []
        if boom_secs:
            moment = max(boom_secs)[1] + 3  # Explosion: Clip so legen, dass der Knall gut drin ist
        elif pn[i] > 0.5 and not chat_led:
            moment = t
        elif visual_led:
            moment = t  # Bild-Action: der Moment ist da, wo das Bild am meisten los ist
        elif has_audio:
            lo_s = int(max(0, t - 25)) if chat_led else int(max(0, i * BIN - BIN))
            hi_s = int(t + 2) if chat_led else int(i * BIN + 2 * BIN)
            window = [(loudness[s], s) for s in range(lo_s, min(hi_s, len(loudness)))]
            moment = max(window)[1] if window else t - 10
            if chat_led and window and max(window)[0] - (sum(w for w, _ in window) / len(window)) < 4:
                moment = t - 10  # keine klare Stimm-Spitze → Standard-Verzögerung
        else:
            moment = t - 10 if chat_led else t
        start = max(0.0, moment - pre)
        end = moment + post
        # Clip verlängern, solange der Hype anhält
        j = int(end // BIN)
        while j < nb and combined[j] >= 0.5 * combined[i] and end - start < max_len:
            end += BIN
            j += 1
        end = min(duration, end)

        reasons = []
        if has_chat and cn[i] > 0:
            reasons.append(f"{sum(u[max(0, i - 1):i + 2])} Leute im Chat")
            k = int(sum(kw[max(0, i - 1):i + 2]))
            if k:
                reasons.append(f"{k}× Hype-Wörter")
        if has_audio:
            near = [voice_db[j] for j in range(max(0, i - 1), min(nb, i + 2)) if voice_db[j] is not None]
            if near and max(near) > 4:
                reasons.append(f"Stimme +{max(near):.0f} dB")
        if clip_bins[i]:
            reasons.insert(0, "✂ jemand wollte das clippen")
        near_v = max(vn[max(0, i - 1):i + 2]) if vn else 0.0
        if near_v > 0.6:
            reasons.append("🎬 viel Action im Bild")
        if pn[i] > 0:
            reasons.insert(0, "🚔 Cops hinter dir")
        if bn[i] > 0:
            reasons.insert(0, "💥 Explosion")
        if model and model_prob(model, feats[i]) > 0.65:
            reasons.append("👍 passt zu deinem Geschmack")
        sample = _top_messages([m[2] for m in msgs if start <= m[0] <= end + 15])
        highlights.append({
            "time": moment, "start": start, "end": end,
            "score": min(100, round(100 * final[i] / top)),
            "reason": " · ".join(reasons) or ("relativ stärkster Moment" if weak else "Spitze"),
            "chat": sample, "features": feats[i],
        })
    curve = [(i * BIN, combined[i]) for i in range(nb)]
    return highlights, curve


# --------------------------------------------------------------------------- #
#  Bild-Check: Menüs / Einstellungen / Pause erkennen (kaum Bewegung im Bild)
# --------------------------------------------------------------------------- #
def measure_motion(src, start, end, workdir, runner):
    """Gibt (durchschnittliche Bewegung, Anteil fast stehender Bilder) für den Bereich zurück."""
    dur = max(2.0, end - start)
    os.makedirs(workdir, exist_ok=True)
    if src["kind"] == "vod":
        seg = os.path.join(workdir, f"motion_{int(start)}_{int(end)}.mp4")
        if not os.path.isfile(seg):
            runner.run([tool("yt-dlp"), "--newline", "--no-part", "-f", "worst[vcodec!=none]/worst",
                        "--download-sections", f"*{start:.1f}-{end:.1f}",
                        "--ffmpeg-location", os.path.dirname(tool("ffmpeg")),
                        "-o", seg, vod_url(src["id"])], quiet=True)
        inp = ["-i", seg]
    else:
        inp = ["-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", src["path"]]
    out = f"motion_{int(start)}.txt"
    runner.run([tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *inp, "-an",
                "-vf", "fps=2,scale=160:90,format=gray,tblend=all_mode=difference,signalstats,"
                       f"metadata=print:key=lavfi.signalstats.YAVG:file={out}",
                "-f", "null", "-"], cwd=workdir, quiet=True)
    vals = []
    with open(os.path.join(workdir, out), encoding="utf-8", errors="replace") as f:
        for line in f:
            if "YAVG=" in line:
                try:
                    vals.append(float(line.split("=", 1)[1]))
                except ValueError:
                    pass
    if not vals:
        return None, None
    vals = vals[1:] or vals  # erstes Differenzbild ist oft verfälscht
    mean = sum(vals) / len(vals)
    static = sum(1 for v in vals if v < 1.0) / len(vals)
    return mean, static


def looks_like_menu(mean, static):
    """Spiel = ganzes Bild bewegt sich. Menü/Einstellungen/Pause/BRB = fast alles steht still."""
    if mean is None:
        return False
    return static >= 0.6 or mean < 2.0


# --------------------------------------------------------------------------- #
#  Qualitäts-Check eines fertigen Clips (vor dem Hochladen)
# --------------------------------------------------------------------------- #
def check_clip(path, expected_dur, runner, tall=False):
    """Prüft einen geschnittenen Clip. Gibt (ok, probleme, info) zurück."""
    problems = []
    info = {}
    if not path or not os.path.isfile(path):
        return False, ["Datei fehlt"], info
    # Streams + Länge + Auflösung
    out = []
    try:
        runner.run([tool("ffprobe"), "-v", "error", "-show_entries",
                    "stream=codec_type,width,height:format=duration", "-of", "json", path], on_line=out.append)
        meta = json.loads("\n".join(out))
    except Exception:
        return False, ["Datei ist kaputt (lässt sich nicht lesen)"], info
    streams = meta.get("streams", [])
    v = next((x for x in streams if x.get("codec_type") == "video"), None)
    a = next((x for x in streams if x.get("codec_type") == "audio"), None)
    dur = float((meta.get("format") or {}).get("duration") or 0)
    info["dauer"] = round(dur, 1)
    if not v:
        return False, ["Kein Bild im Clip"], info
    if not a:
        problems.append("Kein Ton")
    h, w = int(v.get("height") or 0), int(v.get("width") or 0)
    info["aufloesung"] = f"{w}x{h}"
    if (tall and h < 1280) or (not tall and h < 700):
        problems.append(f"niedrige Auflösung ({w}x{h})")
    if dur < 5:
        problems.append(f"zu kurz ({dur:.0f} s)")
    elif expected_dur and abs(dur - expected_dur) > max(4.0, expected_dur * 0.25):
        problems.append(f"Länge stimmt nicht ({dur:.0f} s statt {expected_dur:.0f} s)")
    # Schwarzbild, Stille, Standbild in einem Durchgang
    lines = []
    runner.run([tool("ffmpeg"), "-hide_banner", "-nostats", "-i", path,
                "-vf", "fps=4,scale=160:-2,blackdetect=d=0.5:pix_th=0.10,freezedetect=n=0.003:d=2",
                "-af", "volumedetect", "-f", "null", "-"], on_line=lines.append)
    black = sum(float(m) for l in lines for m in re.findall(r"black_duration:\s*([\d.]+)", l))
    # freezedetect meldet Start/Ende; geht das Standbild bis zum Schluss, fehlt das Ende
    starts = [float(m) for l in lines for m in re.findall(r"freeze_start:\s*([\d.]+)", l)]
    ends = [float(m) for l in lines for m in re.findall(r"freeze_end:\s*([\d.]+)", l)]
    frozen = sum((ends[k] if k < len(ends) else dur) - st for k, st in enumerate(starts))
    mean_vol = next((float(m) for l in lines for m in re.findall(r"mean_volume:\s*(-?[\d.]+) dB", l)), None)
    if dur > 0:
        info["schwarz"] = round(black / dur * 100)
        info["standbild"] = round(frozen / dur * 100)
        if black / dur > 0.3:
            problems.append(f"{info['schwarz']} % schwarzes Bild")
        if frozen / dur > 0.5:
            problems.append(f"{info['standbild']} % Standbild (Menü/Pause?)")
    if mean_vol is not None:
        info["lautstaerke"] = mean_vol
        if a and mean_vol < -45:
            problems.append("fast kein Ton")
    return not problems, problems, info


def make_preview(path, out_png, runner, height=120):
    """3 Vorschaubilder (Anfang, Mitte, Ende) nebeneinander als PNG."""
    if os.path.isfile(out_png) and os.path.getmtime(out_png) >= os.path.getmtime(path):
        return out_png
    out = []
    runner.run([tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
                path], on_line=out.append)
    try:
        dur = max(1.0, float(out[-1]))
    except Exception:
        dur = 10.0
    runner.run([tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{dur * 0.1:.2f}", "-i", path,
                "-vf", f"fps={3 / (dur * 0.85):.4f},scale=-2:{height},tile=3x1:padding=4:color=0x18181b",
                "-frames:v", "1", out_png], quiet=True)
    return out_png


def _top_messages(msgs, n=4):
    counts = {}
    for m in msgs:
        key = m.strip()
        if key:
            counts[key] = counts.get(key, 0) + 1
    best = sorted(counts.items(), key=lambda kv: (-kv[1], len(kv[0])))[:n]
    return " | ".join(f"{k[:40]}" + (f" ×{c}" if c > 1 else "") for k, c in best)


# --------------------------------------------------------------------------- #
#  Schneiden
# --------------------------------------------------------------------------- #
VERTICAL_FILTERS = {
    "blur": ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24:4,"
             "eq=brightness=-0.08[bg];[0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"),
    "crop": "[0:v]crop=ih*9/16:ih,scale=1080:1920,format=yuv420p[v]",
}


def cut_from_vod(vod_id, start, end, out_base, quality, runner, log):
    q = {"1080p": "best[height<=1080]/best", "720p": "best[height<=720]/best",
         "Beste": "best"}.get(quality, "best[height<=1080]/best")
    last = [-25]

    def on_line(l):
        m = re.search(r"(\d+(?:\.\d+)?)%", l)
        if m and "[download]" in l:
            p = int(float(m.group(1)))
            if p >= last[0] + 25:
                last[0] = p
                log(f"   {p}%")
        elif "ERROR" in l:
            log(l)

    runner.run([tool("yt-dlp"), "--newline", "--no-part", "-f", q,
                "--download-sections", f"*{start:.2f}-{end:.2f}", "--force-keyframes-at-cuts",
                "--ffmpeg-location", os.path.dirname(tool("ffmpeg")),
                "--remux-video", "mp4", "-o", out_base + ".%(ext)s", vod_url(vod_id)], on_line=on_line)
    for ext in (".mp4", ".mkv", ".ts", ".webm"):
        if os.path.isfile(out_base + ext):
            return out_base + ext
    raise RuntimeError("Clip wurde nicht gespeichert")


def cut_from_file(src, start, end, out_path, runner):
    runner.run([tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{start:.2f}", "-i", src, "-t", f"{end - start:.2f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", out_path], quiet=True)
    return out_path


def make_vertical(src, out_path, style, runner):
    runner.run([tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                "-filter_complex", VERTICAL_FILTERS.get(style, VERTICAL_FILTERS["blur"]),
                "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                "-r", "30", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out_path], quiet=True)
    return out_path


def make_bestof(clips, out_path, runner):
    """Alle Clips (chronologisch) zu einem Best-of-Video zusammenfügen."""
    inputs = []
    for c in clips:
        inputs += ["-i", c]
    n = len(clips)
    parts = "".join(
        f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
        f"fps=30,format=yuv420p,setsar=1[v{i}];[{i}:a]aresample=48000[a{i}];" for i in range(n))
    concat = "".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
    runner.run([tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *inputs,
                "-filter_complex", parts + concat, "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", out_path], quiet=True)
    return out_path


def safe_name(text, maxlen=40):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text or "").strip()
    text = re.sub(r"\s+", "_", text)
    return text[:maxlen] or "stream"
