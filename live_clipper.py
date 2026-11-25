# -*- coding: utf-8 -*-
"""
Live-Clipper: Während du live bist (PC oder PS5 – egal), liest die App den Twitch-Chat mit,
hört deinen Stream-Ton ab und erstellt bei Hype-Momenten automatisch echte Twitch-Clips.
Die Clips werden heruntergeladen, als TikTok-Version gebaut, geprüft und an die App übergeben.

Twitch-Zugang: eigene Twitch-App (Client-Typ "Öffentlich") + Geräte-Code-Anmeldung, nur Scope clips:edit.
Daten bleiben lokal in twitch.json.
"""
import base64
import calendar
import hashlib
import shutil
import json
import math
import os
import re
import socket
import sys
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

import clipper_core as core

CONF_FILE = os.path.join(core.APP_DIR, "twitch.json")
HELIX = "https://api.twitch.tv/helix"
SCOPES = "clips:edit"
IS_WIN = os.name == "nt"

MAXIMAL = "Maximal (alles Spannende)"
TRIGGERS = {  # Schlüssel: Anzeigename
    "cops": "🚔 Cops hinter dir (Minimap blinkt rot/blau)",
    "siren": "🚨 Sirene / Alarm / Polizei (Ton)",
    "shots": "🔫 Schüsse (mehrere Schüsse / Dauerfeuer)",
    "explosion": "💥 Explosionen (Feuerball im Bild + Knall)",
    "bluelight": "🚨 Blaulicht im Bild",
    "vision": "🎬 Action im Bild (viel Bewegung)",
    "game": "🎮 Spiel wird deutlich lauter",
    "voice": "🗣 Du schreist / rastest aus",
    "chat": "💬 Chat-Hype (mehrere reagieren)",
    "clipreq": "✂ Jemand schreibt „clip“ / „!clip“",
}
SENSITIVITY = {  # (Chat-Score, Stimme +dB, Spiel-Action +LU, min. Leute im Chat)
    MAXIMAL: (1.5, 10.0, 6.0, 2),
    "Hoch (mehr Clips)": (2.5, 7.0, 7.0, 3),
    "Mittel": (4.0, 9.0, 10.0, 3),
    "Niedrig (nur große Momente)": (6.0, 13.0, 99.0, 3),
}


class LiveError(Exception):
    pass


# --------------------------------------------------------------------------- #
#  Twitch-Anmeldung + API
# --------------------------------------------------------------------------- #
def load_conf():
    try:
        with open(CONF_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_conf(c):
    with open(CONF_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2)
    os.replace(CONF_FILE + ".tmp", CONF_FILE)


def _http(url, data=None, headers=None, method=None, timeout=20):
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"message": raw.decode("utf-8", "replace")[:300]}


class Twitch:
    def __init__(self):
        self.c = load_conf()
        self.lock = threading.Lock()

    @property
    def client_id(self):
        return self.c.get("client_id", "")

    def connected(self):
        return bool(self.c.get("refresh_token") and self.client_id)

    def _secret(self, d):
        """Vertrauliche Twitch-Apps brauchen das Client-Geheimnis, öffentliche nicht."""
        sec = self.c.get("client_secret", "")
        if sec:
            d["client_secret"] = sec
        return d

    def device_login(self, client_id, log, open_url, cancel=lambda: False, client_secret=None):
        """Anmeldung per Gerätecode: Browser öffnet sich, du klickst nur auf 'Autorisieren'."""
        client_id = client_id.strip()
        if client_secret is not None:
            self.c["client_secret"] = client_secret.strip()
        if not client_id:
            raise LiveError("Bitte zuerst die Client-ID deiner Twitch-App eintragen.")
        st, d = _http("https://id.twitch.tv/oauth2/device", {"client_id": client_id, "scopes": SCOPES})
        if st != 200 or "device_code" not in d:
            raise LiveError(f"Twitch-Anmeldung ging nicht: {d.get('message', d)} "
                            "(Ist die Client-ID richtig und der Client-Typ 'Öffentlich'?)")
        log(f"🔑 Twitch-Code: {d['user_code']} – im Browser bestätigen …")
        open_url(d["verification_uri"])
        interval = max(2, int(d.get("interval", 5)))
        end = time.time() + int(d.get("expires_in", 1800))
        while time.time() < end:
            if cancel():
                raise LiveError("Abgebrochen")
            time.sleep(interval)
            st, t = _http("https://id.twitch.tv/oauth2/token", self._secret({
                "client_id": client_id, "scopes": SCOPES, "device_code": d["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}))
            if st == 200 and t.get("access_token"):
                self.c.update({"client_id": client_id, "access_token": t["access_token"],
                               "refresh_token": t.get("refresh_token", ""),
                               "expires_at": time.time() + int(t.get("expires_in", 3600)) - 120})
                me = self.api("GET", "/users")
                u = (me.get("data") or [{}])[0]
                self.c["login"] = u.get("login", "")
                self.c["user_id"] = u.get("id", "")
                save_conf(self.c)
                return self.c["login"]
            msg = str(t.get("message", ""))
            if "pending" in msg:
                continue
            if "slow_down" in msg:
                interval += 2
                continue
            if "secret" in msg.lower() or "invalid client" in msg.lower():
                raise LiveError("Twitch will das Client-Geheimnis deiner App (Client-Typ 'Vertraulich'). "
                                "Trag es im Fenster 'Mit Twitch verbinden' ein.")
            raise LiveError(f"Twitch-Anmeldung abgelehnt: {msg or t}")
        raise LiveError("Zeit für die Twitch-Anmeldung abgelaufen.")

    def _token(self):
        with self.lock:
            if self.c.get("access_token") and time.time() < self.c.get("expires_at", 0):
                return self.c["access_token"]
            if not self.c.get("refresh_token"):
                raise LiveError("Twitch ist nicht verbunden.")
            st, t = _http("https://id.twitch.tv/oauth2/token", self._secret({
                "client_id": self.client_id, "grant_type": "refresh_token",
                "refresh_token": self.c["refresh_token"]}))
            if st != 200 or not t.get("access_token"):
                raise LiveError("Twitch-Anmeldung abgelaufen – bitte im Live-Clipper neu verbinden.")
            self.c.update({"access_token": t["access_token"],
                           "refresh_token": t.get("refresh_token", self.c["refresh_token"]),
                           "expires_at": time.time() + int(t.get("expires_in", 3600)) - 120})
            save_conf(self.c)
            return self.c["access_token"]

    def api(self, method, path, params=None, retry=True):
        url = HELIX + path + ("?" + urllib.parse.urlencode(params) if params else "")
        st, d = _http(url, b"" if method == "POST" else None, method=method,
                      headers={"Authorization": "Bearer " + self._token(), "Client-Id": self.client_id})
        if st == 401 and retry:
            with self.lock:
                self.c["expires_at"] = 0
            return self.api(method, path, params, retry=False)
        if st >= 400:
            raise LiveError(f"Twitch-API {path}: HTTP {st} – {d.get('message', d)}")
        return d

    def user_id(self, login):
        d = self.api("GET", "/users", {"login": login.lower()})
        data = d.get("data") or []
        if not data:
            raise LiveError(f"Twitch-Kanal '{login}' nicht gefunden.")
        return data[0]["id"]

    def stream(self, user_id):
        d = self.api("GET", "/streams", {"user_id": user_id})
        data = d.get("data") or []
        return data[0] if data else None

    def create_clip(self, broadcaster_id, duration=30, title=""):
        params = {"broadcaster_id": broadcaster_id, "has_delay": "false", "duration": str(int(duration))}
        if title:
            params["title"] = title[:100]
        d = self.api("POST", "/clips", params)
        data = d.get("data") or []
        if not data:
            raise LiveError(f"Twitch hat keinen Clip erstellt: {d}")
        return data[0]["id"]

    def wait_clip(self, clip_id, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(5)
            d = self.api("GET", "/clips", {"id": clip_id})
            data = d.get("data") or []
            if data:
                return data[0]
        return None


# --------------------------------------------------------------------------- #
#  Live-Chat (anonym über IRC – braucht keinen Login)
# --------------------------------------------------------------------------- #
PRIVMSG = re.compile(r"^:(\w+)!\S+ PRIVMSG #\w+ :(.*)$")


def parse_irc(line):
    m = PRIVMSG.match(line)
    return (m.group(1).lower(), m.group(2)) if m else None


class ChatReader(threading.Thread):
    def __init__(self, channel, on_msg, stop, log):
        super().__init__(daemon=True)
        self.channel, self.on_msg, self.stop, self.log = channel.lower().lstrip("#@"), on_msg, stop, log

    def run(self):
        while not self.stop.is_set():
            try:
                raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=10)
                s = ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
                s.settimeout(2)
                s.sendall(f"NICK justinfan{int(time.time()) % 100000}\r\nJOIN #{self.channel}\r\n".encode())
                buf = b""
                while not self.stop.is_set():
                    try:
                        chunk = s.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    while b"\r\n" in buf:
                        line, buf = buf.split(b"\r\n", 1)
                        text = line.decode("utf-8", "replace")
                        if text.startswith("PING"):
                            s.sendall(b"PONG :tmi.twitch.tv\r\n")
                            continue
                        msg = parse_irc(text)
                        if msg:
                            self.on_msg(time.time(), *msg)
                s.close()
            except Exception as e:
                if not self.stop.is_set():
                    self.log(f"   ⚠ Chat-Verbindung unterbrochen ({e}) – verbinde neu …")
            self.stop.wait(5)


# --------------------------------------------------------------------------- #
#  Live-Ton: Stimme, Spiel-Lautstärke und SIRENEN – rohe Tonspur, in Python ausgewertet
# --------------------------------------------------------------------------- #
import array

SR = 8000                     # Abtastrate für die Analyse
FRAME = SR // 10              # 0,1 s
SIREN_N = 160                 # 20-ms-Fenster für die Tonhöhe (Auflösung 50 Hz)
SIREN_FREQS = list(range(450, 1901, 50))
_GOERTZEL = [(f, 2 * math.cos(2 * math.pi * f / SR)) for f in SIREN_FREQS]


def tone_peak(samples):
    """Stärkste Tonhöhe 450–1900 Hz und ihr Anteil an der Gesamtenergie (0..1)."""
    n = len(samples)
    total = sum(x * x for x in samples)
    if total <= 1e-9:
        return None, 0.0
    best_f, best_p = None, 0.0
    for f, c in _GOERTZEL:
        s1 = s2 = 0.0
        for x in samples:
            s0 = x + c * s1 - s2
            s2 = s1
            s1 = s0
        p = s1 * s1 + s2 * s2 - c * s1 * s2
        if p > best_p:
            best_f, best_p = f, p
    return best_f, min(1.0, best_p / (n / 2.0 * total))


TR_N = 80  # 10-ms-Hüllkurve für Schuss-Erkennung


def count_transients(pcm, st):
    """Zählt Schüsse in 0,1 s Ton: harter Anstieg (+10 dB in ≤30 ms) UND schnelles Abklingen (−8 dB in ≤80 ms).
    Sprache steigt auch an, bleibt aber länger laut – das zählt nicht. st = Zustand über Blöcke hinweg."""
    if not st:
        st.extend([[-90.0, -90.0, -90.0], 99, None])
    prev, since, pend = st
    hits = 0
    for i in range(0, len(pcm) - TR_N + 1, TR_N):
        seg = pcm[i:i + TR_N]
        e = 10 * math.log10(sum(x * x for x in seg) / TR_N + 1e-12)
        since += 1
        if pend is not None:
            peak, age = pend
            peak = max(peak, e) if age < 2 else peak
            age += 1
            if e <= peak - 8:
                hits += 1
                pend = None
            elif age > 8:
                pend = None
            else:
                pend = (peak, age)
        if pend is None and e > -45 and e - min(prev) >= 10 and since >= 5:
            pend = (e, 0)
            since = 0
        prev = prev[1:] + [e]
    st[0], st[1], st[2] = prev, since, pend
    return hits


def analyze_audio_frame(pcm, hp_state):
    """pcm: 0,1 s float-Samples. Gibt (spiel_db, stimme_db, (tonhöhe, tonanteil)) zurück."""
    n = len(pcm)
    full = sum(x * x for x in pcm) / n
    # Hochpass ~250 Hz (1. Ordnung) → grobe Stimm-/Sprach-Energie ohne Bass, Motoren, Explosions-Wummern
    a = 0.82
    y_prev, x_prev = hp_state
    acc = 0.0
    for x in pcm:
        y = a * (y_prev + x - x_prev)
        acc += y * y
        y_prev, x_prev = y, x
    hp_state[0], hp_state[1] = y_prev, x_prev
    db = lambda e: 10 * math.log10(e) if e > 1e-12 else -90.0
    mid = n // 2
    tone = tone_peak(pcm[mid - SIREN_N // 2: mid + SIREN_N // 2])
    return db(full), db(acc / n), tone


def sub_bass(pcm, st):
    """Wie viel dB der Tiefbass (< ~150 Hz) unter dem Gesamtpegel liegt. Explosion ≈ 0–6, Stimme/Baby/Schrei ≈ 15+."""
    if not st:
        st.extend([0.0, 0.0])
    a = 0.11  # Tiefpass 2. Ordnung (zwei 1-Pol-Stufen) ~150 Hz bei 8 kHz
    y1, y2 = st
    full = acc = 0.0
    for x in pcm:
        y1 += a * (x - y1)
        y2 += a * (y1 - y2)
        acc += y2 * y2
        full += x * x
    st[0], st[1] = y1, y2
    if full <= 1e-12:
        return 99.0
    return 10 * math.log10(full / max(acc, 1e-12))


def siren_score(tones):
    """tones: Liste (tonhöhe, anteil) der letzten ~3 s (10 pro Sekunde).
    Sirene = deutlicher Ton, der FLIESSEND hoch und runter gleitet (Musik springt in Stufen, Sprache ist kein Ton)."""
    if len(tones) < 20:
        return 0.0
    tonal = [(f, r) for f, r in tones if f and r >= 0.18]
    if len(tonal) < 0.5 * len(tones):
        return 0.0
    fs = [f for f, _ in tonal]
    rng = max(fs) - min(fs)
    if rng < 150:
        return 0.0
    glide = steady = jump = 0
    prev = None
    for f, r in tones:
        if not f or r < 0.18:
            prev = None
            continue
        if prev is not None:
            d = abs(f - prev)
            if d == 0:
                steady += 1
            elif d <= 250:
                glide += 1
            else:
                jump += 1
        prev = f
    moves = glide + steady + jump
    if not moves:
        return 0.0
    frac = len(tonal) / len(tones)
    # "Wail": langsames Auf und Ab → viele kleine Gleitschritte
    if glide / moves >= 0.45 and jump / moves <= 0.25:
        return min(3.0, (glide / moves) * frac * (rng / 300.0))
    # "Yelp": schnelles Auf und Ab → Tonhöhe ändert sich ständig, bleibt nie stehen (Musik hält Töne)
    if steady / moves <= 0.12 and frac >= 0.6 and rng >= 300:
        return min(3.0, frac * (rng / 400.0))
    return 0.0


def _stream_url(channel, fmt):
    kw = {"creationflags": 0x08000000} if IS_WIN else {}
    out = subprocess.run([core.tool("yt-dlp"), "-g", "-f", fmt, f"https://www.twitch.tv/{channel}"],
                         capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, **kw)
    url = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    if not url.startswith("http"):
        raise RuntimeError((out.stderr or "keine Live-Adresse").strip().splitlines()[-1][:120])
    return url


class AudioReader(threading.Thread):
    """Liest die Live-Tonspur als rohe Samples und liefert 10x pro Sekunde Spiel-dB, Stimm-dB und Tonhöhe."""

    def __init__(self, channel, on_db, stop, log, source=None):
        super().__init__(daemon=True)
        self.channel, self.on_db, self.stop, self.log = channel.lower(), on_db, stop, log
        self.source = source  # nur für Tests: Datei statt Twitch
        self.proc = None
        self.on_tone = lambda t, f, r: None

    def run(self):
        kw = {"creationflags": 0x08000000} if IS_WIN else {}
        first = True
        while not self.stop.is_set():
            err_tail = deque(maxlen=5)
            try:
                url = self.source or _stream_url(self.channel, "audio_only/Audio_Only/worst")
                self.proc = subprocess.Popen(
                    [core.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostats", "-i", url, "-vn",
                     "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, **kw)
                threading.Thread(target=lambda: [err_tail.append(l.decode("utf-8", "replace").strip())
                                                 for l in iter(self.proc.stderr.readline, b"")],
                                 daemon=True).start()
                hp = [0.0, 0.0]
                tr_state, sub_state = [], []
                voice_sec, sec_key = [], None
                while not self.stop.is_set():
                    raw = self.proc.stdout.read(FRAME * 2)
                    if not raw or len(raw) < FRAME * 2:
                        break
                    if first:
                        self.log("   🎧 Live-Ton verbunden – höre Spiel, Stimme und Sirenen mit")
                        first = False
                    pcm = array.array("h", raw)
                    if sys.byteorder == "big":
                        pcm.byteswap()
                    samples = [x / 32768.0 for x in pcm]
                    now = time.time()
                    game_db, voice_db, (tf, tr) = analyze_audio_frame(samples, hp)
                    self.on_db(now, game_db, "game")
                    self.on_db(now, game_db - voice_db, "low")
                    self.on_db(now, count_transients(samples, tr_state), "hits")
                    self.on_db(now, sub_bass(samples, sub_state), "sub")
                    self.on_tone(now, tf, tr)
                    k = int(now)
                    if sec_key is not None and k != sec_key and voice_sec:
                        self.on_db(now, max(voice_sec), "voice")
                        voice_sec = []
                    sec_key = k
                    voice_sec.append(voice_db)
                    if self.source:
                        time.sleep(0.0)  # Datei-Test: so schnell wie möglich
                self.kill()
                if self.source:
                    return
                if not self.stop.is_set():
                    self.log("   ⚠ Live-Ton abgebrochen" + (f" ({err_tail[-1][:100]})" if err_tail else "")
                             + " – verbinde neu …")
            except Exception as e:
                if not self.stop.is_set():
                    self.log(f"   ⚠ Live-Ton nicht verfügbar ({str(e)[:100]}) – versuche es gleich nochmal …")
            self.stop.wait(15)

    def kill(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
#  Live-Bild: Bewegung (Action) + Blaulicht (rot/blau blinkend)
# --------------------------------------------------------------------------- #
VW, VH = 128, 72


def analyze_video_frame(rgb, prev_gray):
    """rgb: bytes (VW*VH*3). Gibt (bewegung, rot_anteil, blau_anteil, gray) zurück."""
    n = VW * VH
    gray = bytearray(n)
    red = blue = 0
    mv = 0
    for i in range(n):
        r, g, b = rgb[3 * i], rgb[3 * i + 1], rgb[3 * i + 2]
        y = (r * 77 + g * 150 + b * 29) >> 8
        gray[i] = y
        if r > 170 and g < 90 and b < 110 and r - max(g, b) > 90:
            red += 1
        elif b > 170 and r < 110 and b - max(r, g) > 70:
            blue += 1
        if prev_gray is not None:
            mv += abs(y - prev_gray[i])
    motion = (mv / n) if prev_gray is not None else 0.0
    return motion, red / n, blue / n, gray


def scene_frame(rgb):
    """Minimap-Farben (Cops) + Bild-Look (Explosion) aus einem VWxVH-Bild."""
    x0, x1 = int(core.MM_BOX[0] * VW), int(core.MM_BOX[1] * VW)
    y0, y1 = int(core.MM_BOX[2] * VH), int(core.MM_BOX[3] * VH)
    mm = []
    for y in range(y0, y1):
        for x in range(x0, x1):
            i = 3 * (y * VW + x)
            mm.append((rgb[i], rgb[i + 1], rgb[i + 2]))
    small = []
    for y in range(2, VH, 8):
        for x in range(4, VW, 8):
            i = 3 * (y * VW + x)
            small.append((rgb[i], rgb[i + 1], rgb[i + 2]))
    red, blue = core.minimap_colors(mm)
    return red, blue, core.frame_look(small)


def bluelight_score(frames):
    """frames: Liste (rot, blau) der letzten ~3 s (2 pro Sekunde). Blaulicht = rot UND blau, die sich abwechseln."""
    if len(frames) < 4:
        return 0.0
    reds = [r for r, _ in frames]
    blues = [b for _, b in frames]
    if max(reds) < 0.002 or max(blues) < 0.002:
        return 0.0
    # Blinken: Anteile springen von Bild zu Bild deutlich
    flick_r = sum(1 for a, b in zip(reds, reds[1:]) if abs(a - b) > 0.5 * max(a, b, 1e-6))
    flick_b = sum(1 for a, b in zip(blues, blues[1:]) if abs(a - b) > 0.5 * max(a, b, 1e-6))
    both = sum(1 for r, b in frames if r > 0.001 or b > 0.001)
    if flick_r + flick_b < 2 or both < 0.5 * len(frames):
        return 0.0
    return min(3.0, (flick_r + flick_b) / len(frames) * 3)


class VideoReader(threading.Thread):
    """Liest das Live-Bild in Mini-Auflösung (4 Bilder/Sekunde) und meldet Bewegung + Rot/Blau-Anteile."""

    def __init__(self, channel, on_frame, stop, log, source=None):
        super().__init__(daemon=True)
        self.channel, self.on_frame, self.stop, self.log = channel.lower(), on_frame, stop, log
        self.on_scene = lambda *a: None
        self.source = source
        self.input_args = None  # z. B. Bildschirm-Aufnahme (PS Remote Play) statt Twitch
        self.proc = None

    def run(self):
        kw = {"creationflags": 0x08000000} if IS_WIN else {}
        first = True
        size = VW * VH * 3
        while not self.stop.is_set():
            try:
                if self.input_args:
                    inp = list(self.input_args() if callable(self.input_args) else self.input_args)
                else:
                    inp = ["-i", self.source or _stream_url(self.channel, "160p/160p30/360p/worst[vcodec!=none]/worst")]
                self.proc = subprocess.Popen(
                    [core.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostats", *inp, "-an",
                     "-vf", f"fps=4,scale={VW}:{VH}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **kw)
                prev = None
                while not self.stop.is_set():
                    raw = self.proc.stdout.read(size)
                    if not raw or len(raw) < size:
                        break
                    if first:
                        self.log("   🎥 Live-Bild verbunden – erkenne Action und Blaulicht")
                        first = False
                    motion, red, blue, prev = analyze_video_frame(raw, prev)
                    now_t = time.time()
                    self.on_frame(now_t, motion, red, blue)
                    try:
                        self.on_scene(now_t, *scene_frame(raw))
                    except Exception:
                        pass
                self.kill()
                if self.source:
                    return
            except Exception as e:
                if not self.stop.is_set():
                    self.log(f"   ⚠ Live-Bild nicht verfügbar ({str(e)[:100]}) – versuche es gleich nochmal …")
            self.stop.wait(20)

    def kill(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
#  🤖 Automatisches Profil: stellt sich selbst um, je nachdem was gerade im Spiel los ist
# --------------------------------------------------------------------------- #
AUTO_MODES = {
    # name: (auslöser, abstand s, max/stunde, schüsse ab, clips pro verfolgung)
    "action": (("cops", "explosion", "shots", "siren", "bluelight", "clipreq"), 40, 60, 3, 6),
    "normal": (("cops", "explosion", "shots", "siren", "bluelight", "clipreq"), 60, 30, 4, 3),
    "ruhig": (("cops", "explosion", "clipreq"), 120, 10, 8, 1),
}
AUTO_NAMES = {"action": "💰 Action-Modus (Cops/Explosionen gerade eben)",
              "normal": "🎮 Normal-Modus", "ruhig": "😎 Ruhig-Modus (lange nichts los)"}


def auto_mode(det, now):
    last = max(det.police_seen, det.boom_seen)
    if last and now - last < 180:
        return "action"
    if det.start and now - det.start > 15 * 60 and (not last or now - last > 15 * 60):
        return "ruhig"
    return "normal"


def apply_settings(det, st, now):
    """Einstellungen (oder das Auto-Profil) auf den Detektor anwenden. Gibt den Auto-Modus zurück (oder None)."""
    v = st.get("live_voice")
    if v is not None:
        det.t_voice = float(v) if float(v) > 0 else 999.0
    det.enabled = {k: True for k in TRIGGERS}
    det.enabled.update(st.get("live_trig") or {})
    det.t_action = float(st.get("live_game", det.t_action))
    det.t_vis = float(st.get("live_vis", det.t_vis))
    det.cooldown = max(20, int(st.get("live_gap", det.cooldown)))
    det.max_per_hour = max(1, int(st.get("live_hour", 999)))
    det.min_shots = max(2, int(st.get("live_shots", 4)))
    det.max_chase = max(1, int(st.get("live_chase", 3)))
    sens = SENSITIVITY.get(st.get("live_sens", "Mittel"), SENSITIVITY["Mittel"])
    det.t_chat, det.min_people = sens[0], sens[3]
    mode = None
    if st.get("live_autoprof"):
        mode = auto_mode(det, now)
        trigs, gap, hour, shots_n, chase = AUTO_MODES[mode]
        det.enabled = {k: (k in trigs) for k in TRIGGERS}
        det.cooldown, det.max_per_hour, det.min_shots, det.max_chase = gap, hour, shots_n, chase
    if not st.get("vis", True):
        det.enabled["vision"] = det.enabled["bluelight"] = False
    return mode


# --------------------------------------------------------------------------- #
#  🧪 Test: ein Video im Schnelldurchlauf so behandeln, als wärst du live
# --------------------------------------------------------------------------- #
def test_replay(media, st, log, check=lambda: None, progress=lambda pc: None):
    """Spielt ein Video im Schnelldurchlauf durch den Live-Clipper (gleiche Erkennung, gleiches Auto-Profil).
    Macht KEINE Clips und lädt nichts hoch. Gibt [(sekunde, art, grund)] und [(sekunde, modus)] zurück."""
    import numpy as np
    kw = {"creationflags": 0x08000000} if IS_WIN else {}
    ff = core.tool("ffmpeg")
    # Ton: 3 Kanäle aus ffmpeg – gesamt, Hochpass (Stimme), Tiefpass (Bass)
    ap = subprocess.Popen([ff, "-v", "error", "-i", media, "-vn", "-filter_complex",
                           "[0:a]aresample=8000,pan=mono|c0=0.5*c0+0.5*c1,asplit=3[f][h][l];"
                           "[h]highpass=f=250:p=1[hp];[l]lowpass=f=150,lowpass=f=150[lp];"
                           "[f][hp][lp]amerge=inputs=3[out]", "-map", "[out]", "-ac", "3",
                           "-f", "s16le", "-"], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, **kw)
    vp = subprocess.Popen([ff, "-v", "error", "-i", media, "-an", "-vf", f"fps=4,scale={VW}:{VH}",
                           "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], stdout=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    dur = 0.0
    try:
        out = []
        core.Runner(lambda *_: None).run([core.tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                                          "-of", "csv=p=0", media], on_line=out.append)
        dur = float(out[0])
    except Exception:
        pass
    det = Detector(st.get("channel", "x"), st.get("keywords", core.DEFAULT_KEYWORDS),
                   st.get("live_sens", "Mittel"), cooldown=60, warmup=120)
    T0 = 1_000_000.0
    det.start = T0
    freqs = np.array([f for f, _ in _GOERTZEL])
    E = np.exp(-2j * np.pi * np.outer(freqs, np.arange(SIREN_N)) / SR)
    x0, x1 = int(core.MM_BOX[0] * VW), int(core.MM_BOX[1] * VW)
    y0, y1 = int(core.MM_BOX[2] * VH), int(core.MM_BOX[3] * VH)
    tr = [[-90.0, -90.0, -90.0], 99, None]
    prev_gray = None
    fsz = VW * VH * 3
    vbuf = b""
    vi = 0
    k = 0
    trigs, modes = [], []
    last_mode = None
    db = lambda e: np.where(e > 1e-12, 10 * np.log10(np.maximum(e, 1e-30)), -90.0)
    CH = 600  # 60 s Ton pro Schritt
    last_pc = -10
    try:
        while True:
            check()
            raw = ap.stdout.read(CH * FRAME * 3 * 2)
            if not raw:
                break
            a = np.frombuffer(raw[:len(raw) // 6 * 6], dtype="<i2").astype(np.float64).reshape(-1, 3) / 32768.0
            nb = len(a) // FRAME
            if nb == 0:
                break
            a = a[:nb * FRAME]
            full, hp, lp = (a[:, c].reshape(nb, FRAME) for c in range(3))
            e_full = (full ** 2).mean(1)
            game = db(e_full)
            voice = db((hp ** 2).mean(1))
            sub = np.where(e_full > 1e-12, 10 * np.log10(np.maximum(e_full, 1e-30) /
                                                         np.maximum((lp ** 2).mean(1), 1e-12)), 99.0)
            mid = full[:, FRAME // 2 - SIREN_N // 2:FRAME // 2 + SIREN_N // 2]
            P = np.abs(mid @ E.T) ** 2
            tot = (mid ** 2).sum(1)
            ratio = np.where(tot > 1e-9, np.minimum(1.0, P.max(1) / (SIREN_N / 2.0 * np.maximum(tot, 1e-30))), 0.0)
            tf = np.where(tot > 1e-9, freqs[P.argmax(1)], 0)
            env = db((full.reshape(-1, TR_N) ** 2).mean(1)).tolist()
            hits = [0] * nb
            prev, since, pend = tr
            for i, e in enumerate(env):
                since += 1
                if pend is not None:
                    pk_, age = pend
                    pk_ = max(pk_, e) if age < 2 else pk_
                    age += 1
                    if e <= pk_ - 8:
                        hits[i // (FRAME // TR_N)] += 1
                        pend = None
                    elif age > 8:
                        pend = None
                    else:
                        pend = (pk_, age)
                if pend is None and e > -45 and e - min(prev) >= 10 and since >= 5:
                    pend = (e, 0)
                    since = 0
                prev = prev[1:] + [e]
            tr = [prev, since, pend]
            vs = []
            for j in range(nb):
                t = T0 + k * 0.1
                det.add_db(t, float(game[j]), "game")
                det.add_db(t, float(game[j] - voice[j]), "low")
                det.add_db(t, int(hits[j]), "hits")
                det.add_db(t, float(sub[j]), "sub")
                det.add_tone(t, int(tf[j]) or None, float(ratio[j]))
                vs.append(float(voice[j]))
                if k % 10 == 9:
                    det.add_db(t, max(vs), "voice")
                    vs = []
                # Bilder bis zu dieser Zeit
                while vi * 0.25 <= k * 0.1:
                    need = fsz - len(vbuf)
                    if need > 0:
                        chunk = vp.stdout.read(max(need, fsz * 40))
                        if not chunk:
                            break
                        vbuf += chunk
                        if len(vbuf) < fsz:
                            break
                    fr = np.frombuffer(vbuf[:fsz], dtype=np.uint8).reshape(VH, VW, 3).astype(np.int32)
                    vbuf = vbuf[fsz:]
                    r, g, b = fr[..., 0], fr[..., 1], fr[..., 2]
                    y = (r * 77 + g * 150 + b * 29) >> 8
                    red_m = (r > 170) & (g < 90) & (b < 110) & (r - np.maximum(g, b) > 90)
                    blue_m = ~red_m & (b > 170) & (r < 110) & (b - np.maximum(r, g) > 70)
                    motion = float(np.abs(y - prev_gray).mean()) if prev_gray is not None else 0.0
                    prev_gray = y
                    tv = T0 + vi * 0.25
                    det.add_frame(tv, motion, float(red_m.mean()), float(blue_m.mean()))
                    mm = fr[y0:y1, x0:x1].reshape(-1, 3)
                    mr, mg, mb = mm[:, 0], mm[:, 1], mm[:, 2]
                    mred = float(((mr > 90) & (mr - np.maximum(mg, mb) > 40)).mean())
                    mblue = float(((mb > 110) & (mb - np.maximum(mr, mg) > 40)).mean())
                    sm = fr[2::8, 4::8].reshape(-1, 3)
                    sr_, sg_, sb_ = sm[:, 0].mean(), sm[:, 1].mean(), sm[:, 2].mean()
                    look = ((sr_ * 77 + sg_ * 150 + sb_ * 29) / 256, sr_ - sb_,
                            float((sm.max(1) - sm.min(1)).mean()), float((sm.min(1) > 200).mean()))
                    det.add_scene(tv, mred, mblue, look)
                    vi += 1
                if k % 10 == 0:
                    mode = apply_settings(det, st, t)
                    if mode and mode != last_mode:
                        last_mode = mode
                        modes.append((k // 10, mode))
                        log(f"   {core.fmt_time(k // 10)}  🤖 → {AUTO_NAMES[mode]}")
                    x = det.tick(t)
                    if x:
                        trigs.append((k // 10, x["kind"], x["reason"]))
                        log(f"   {core.fmt_time(k // 10)}  ✂ würde clippen: {x['reason']}")
                k += 1
            if dur:
                pc = int(k / 10 / dur * 100)
                if pc >= last_pc + 10:
                    last_pc = pc
                    progress(min(pc, 100))
    finally:
        for p_ in (ap, vp):
            try:
                p_.kill()
            except Exception:
                pass
    return trigs, modes, k / 10.0


# --------------------------------------------------------------------------- #
#  Spannungs-Check: fertigen Clip anschauen + anhören und 0–100 Punkte geben
# --------------------------------------------------------------------------- #
def _med(vals):
    s = sorted(vals)
    return s[len(s) // 2] if s else 0.0


def rate_clip(path, base_game=None, base_motion=None, min_shots=4):
    """Bewertet, wie spannend ein Clip ist. Gibt (punkte 0–100, [gründe]) zurück.
    Zählt: Schüsse, Explosionen, Sirenen, Blaulicht, viel Bewegung, laute Action. Abzug: Standbild/Menü, nur Reden."""
    kw = {"creationflags": 0x08000000} if IS_WIN else {}
    a = subprocess.run([core.tool("ffmpeg"), "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR),
                        "-f", "s16le", "-"], capture_output=True, stdin=subprocess.DEVNULL, timeout=300, **kw).stdout
    v = subprocess.run([core.tool("ffmpeg"), "-v", "error", "-i", path, "-an", "-vf", f"fps=4,scale={VW}:{VH}",
                        "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True, stdin=subprocess.DEVNULL,
                       timeout=300, **kw).stdout
    pcm = array.array("h", a[:len(a) // 2 * 2])
    if sys.byteorder == "big":
        pcm.byteswap()
    hp, tr, sb = [0.0, 0.0], [], []
    blocks = []  # (spiel_db, bass_anteil, schüsse, (tonhöhe, anteil), tiefbass_abstand)
    for k in range(len(pcm) // FRAME):
        smp = [x / 32768.0 for x in pcm[k * FRAME:(k + 1) * FRAME]]
        g, vd, tone = analyze_audio_frame(smp, hp)
        blocks.append((g, g - vd, count_transients(smp, tr), tone, sub_bass(smp, sb)))
    fsz = VW * VH * 3
    frames, prev, scn = [], None, []
    for k in range(len(v) // fsz):
        raw = v[k * fsz:(k + 1) * fsz]
        m, rd, bl, prev = analyze_video_frame(raw, prev)
        scn.append(scene_frame(raw))
        if k:
            frames.append((m, rd, bl))
    # Cops (Minimap blinkt) + Explosion (Feuerball + Knall) – die wichtigsten GTA-Momente
    cops_secs = sum(1 for st in range(0, max(0, len(scn) - 15), 4)
                    if core.police_score([(r, b) for r, b, _ in scn[st:st + 16]]))
    lowdb = [b[0] - b[4] for b in blocks]
    boom_video = False
    for st in range(0, max(0, len(scn) - 7), 2):
        if core.flash_score([lk for _, _, lk in scn[st:st + 8]]):
            sec = (st + 6) / 4.0
            if core.boom_near(lowdb, sec):
                boom_video = True
                break
    score, why = 0.0, []
    if blocks:
        gs = [b[0] for b in blocks]
        norm = base_game if base_game is not None else _med(gs)
        # Schüsse: meiste Treffer in einem 4-s-Fenster (gleiche Regeln wie live)
        best_shots = 0
        for st in range(0, max(1, len(blocks) - 39), 5):
            win = blocks[st:st + 40]
            local = _med([b[0] for b in win])
            n = sum(b[2] for b in win if b[2] and b[0] - norm >= 12 and b[0] - local >= 8 and 2.0 <= b[1] <= 8.0
                    and b[4] <= 10.0 and not (b[3][0] and b[3][1] >= 0.18))
            best_shots = max(best_shots, n)
        boom = False
        for i, b in enumerate(blocks[:-5]):
            if (b[0] - norm >= 15 and b[1] >= 3.0 and b[4] <= 6.0 and not (b[3][0] and b[3][1] >= 0.18)
                    and (i < 4 or b[0] - min(x[0] for x in blocks[i - 4:i]) >= 10)):
                tail = blocks[i + 1:i + 6]
                if (min(x[0] for x in tail) - norm >= 8 and sum(x[2] for x in blocks[i:i + 6]) <= 2
                        and sum(x[4] for x in tail) / 5 <= 9.0):
                    boom = True
                    break
        siren = 0.0
        for st in range(0, max(1, len(blocks) - 29), 10):
            siren = max(siren, siren_score([b[3] for b in blocks[st:st + 30]]))
        loud = sum(1 for b in blocks if b[0] - norm >= 6 and b[1] >= 2.0) / len(blocks)  # laut UND Spiel, nicht Stimme
        talk = sum(1 for b in blocks if b[1] < 1.5 and b[0] > norm - 3) / len(blocks)
        if boom_video:
            score += 35
            why.append("💥 Explosion")
        if cops_secs:
            score += 25 + min(15, cops_secs * 2)
            why.append("🚔 Cops")
        if best_shots >= min_shots and not boom_video:
            score += min(35, 10 + best_shots * 2)
            why.append(f"🔫 {best_shots} Schüsse")
        if siren >= 1.2:
            score += 20
            why.append("🚨 Sirene")
        if loud >= 0.1:
            score += min(15, loud * 50)
            why.append(f"🔊 {loud * 100:.0f} % laute Action")
    else:
        talk = 0.0
    if frames:
        ms = [f[0] for f in frames]
        base = base_motion if base_motion else None
        mean_m = sum(ms) / len(ms)
        if base and base > 0.3:
            ratio = mean_m / base
            if ratio >= 1.2:
                score += min(12, (ratio - 1.0) * 12)
                why.append(f"🎬 Bewegung ×{ratio:.1f}")
        elif mean_m >= 4:
            score += min(10, mean_m * 0.8)
            why.append("🎬 viel Bewegung")
        blue = 0.0
        for st in range(0, max(1, len(frames) - 11), 4):
            blue = max(blue, bluelight_score([(r, b) for _, r, b in frames[st:st + 12]]))
        if blue >= 1.0:
            score += 15
            why.append("🚨 Blaulicht")
        static = sum(1 for m in ms if m < 0.3) / len(ms)
        if static > 0.5:
            score -= 30
            why.append(f"⏸ {static * 100:.0f} % Standbild/Menü")
    if talk > 0.6 and score < 30:
        score -= 10
        why.append("🗣 fast nur Reden")
    return int(max(0, min(100, round(score)))), why


# --------------------------------------------------------------------------- #
#  Hype-Erkennung in Echtzeit
# --------------------------------------------------------------------------- #
CLIP_REQ = re.compile(r"(^|\s)!?(clip|clippen|clip it|clip das|clipt)(\s|$|!)", re.I)


class Detector:
    """Bewertet jede Sekunde Chat + Stimme und sagt, ob jetzt geclippt werden soll."""

    def __init__(self, streamer, keywords, sensitivity="Mittel", cooldown=45, warmup=120):
        self.streamer = streamer.lower()
        self.kws = core.parse_keywords(keywords)
        self.t_chat, self.t_voice, self.t_action, self.min_people = SENSITIVITY.get(sensitivity,
                                                                                     SENSITIVITY["Mittel"])
        self.game = deque(maxlen=3000)    # (t, dB) ~10x pro Sekunde (5 Min.)
        self.tones = deque(maxlen=40)     # (t, tonhöhe, anteil) ~10x pro Sekunde
        self.low = deque(maxlen=60)       # (t, Bass-Anteil dB) – hoch = Knall/Explosion, niedrig = Sprache
        self.hits = deque(maxlen=60)      # (t, Einschläge im 0,1-s-Block) – Schüsse
        self.sub = deque(maxlen=60)       # (t, Tiefbass-Abstand dB) – klein = echtes Wummern (Explosion)
        self.lowdb = deque(maxlen=60)     # (t, Tiefbass-Pegel dB) – für Knall-Erkennung
        self.scenes = deque(maxlen=24)    # (t, minimap_rot, minimap_blau, look) ~4x pro Sekunde
        self.police_seen = 0.0            # wann zuletzt Cops (Minimap blinkt)
        self.chase_clips = 0
        self.max_chase = 3                # höchstens so viele Clips pro Verfolgung
        self.boom_seen = 0.0              # wann zuletzt eine echte Explosion
        self.min_shots = 4
        self.norm_game = None      # normales Spiel-dB (für die Spannungs-Bewertung der fertigen Clips)
        self.norm_motion = None
        self.enabled = {k: True for k in TRIGGERS}
        self.clip_times = deque()
        self.max_per_hour = 999
        self.frames = deque(maxlen=900)   # (t, bewegung, rot, blau) ~4x pro Sekunde
        self.cooldown, self.warmup = cooldown, warmup
        self.msgs = deque()       # (t, user, text, keyword-hits)
        self.clipreq = deque()    # (t, user)
        self.u_hist = deque(maxlen=600)   # Leute pro 15 s, 1x pro Sekunde
        self.db = deque(maxlen=400)       # (t, dB) pro Sekunde
        self.start = None
        self.last_clip = 0.0
        self.peak = {"u": 0, "kw": 0, "voice": None, "action": None, "siren": None, "blue": None, "vis": None}
        self.t_vis = 1.6 if sensitivity == MAXIMAL else 2.0
        self.lock = threading.Lock()

    def add_msg(self, t, user, text):
        low = text.lower().strip()
        is_bot = user in core.BOTS or user.endswith("bot")
        with self.lock:
            if not is_bot and CLIP_REQ.search(low):
                self.clipreq.append((t, user))
            if (is_bot or user == self.streamer or (low.startswith("!") and not low.startswith("!clip"))
                    or (core.GREETING.match(low) and len(low) < 40) or "raid" in low):
                return
            self.msgs.append((t, user, text, core.count_keywords(text, self.kws)))

    def add_tone(self, t, f, r):
        # Nur hörbare Töne zählen: in fast-Stille wirkt sonst jedes leise Piepen wie eine Sirene
        with self.lock:
            lvl = self.game[-1][1] if self.game else -90.0
            norm = self._median([v for _, v in list(self.game)[-600:]]) if len(self.game) > 50 else lvl
            if lvl < -50 or lvl < norm - 3:
                f, r = None, 0.0
            self.tones.append((t, f, r))

    def add_scene(self, t, red, blue, look):
        with self.lock:
            self.scenes.append((t, red, blue, look))

    def add_frame(self, t, motion, red, blue):
        with self.lock:
            self.frames.append((t, motion, red, blue))

    def add_db(self, t, v, kind="voice"):
        with self.lock:
            if kind == "low":
                self.low.append((t, v))
            elif kind == "hits":
                self.hits.append((t, v))
            elif kind == "sub":
                self.sub.append((t, v))
                if self.game:
                    self.lowdb.append((t, self.game[-1][1] - v))
            else:
                (self.game if kind == "game" else self.db).append((t, v))

    @staticmethod
    def _median(vals):
        s = sorted(vals)
        n = len(s)
        if not n:
            return 0.0
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    def tick(self, now):
        """Einmal pro Sekunde aufrufen. Gibt None oder ein Trigger-Dict zurück."""
        if self.start is None:
            self.start = now
        with self.lock:
            while self.msgs and self.msgs[0][0] < now - 600:
                self.msgs.popleft()
            while self.clipreq and self.clipreq[0][0] < now - 30:
                self.clipreq.popleft()
            recent = [m for m in self.msgs if m[0] >= now - 15]
            users = {m[1] for m in recent}
            kw = sum(m[3] for m in recent)
            req_users = {u for t, u in self.clipreq if t >= now - 12}
            dbs = list(self.db)
            game = list(self.game)
            tones = [(f, r) for t, f, r in self.tones if t >= now - 3]
            frames = list(self.frames)
        u15 = len(users)
        self.u_hist.append(u15)
        base = self._median(self.u_hist)
        mean = sum(self.u_hist) / len(self.u_hist)
        chat_score = 0.0
        if u15 >= max(self.min_people, 2.5 * mean) and u15 >= 2 * base + 1:
            chat_score = (u15 - base) / math.sqrt(base + 2)
        voice_diff = None
        if len(dbs) >= 60:
            srt = sorted(v for _, v in dbs)
            norm = srt[int(len(srt) * 0.75)]  # dein normales Reden – nicht die Pausen
            last2 = [v for t, v in dbs if t >= now - 3][-2:]
            if len(last2) == 2:
                voice_diff = min(last2) - norm
        action = None
        shots = 0
        explosion = None
        if len(game) >= 1200:  # ~2 Minuten Spielton als Vergleich (sonst wirkt am Anfang alles "laut")
            acc = {}
            for t, v in game:
                a = acc.setdefault(int(t), [0.0, 0])
                a[0] += v
                a[1] += 1
            per_sec = {k: a[0] / a[1] for k, a in acc.items()}  # Mittelwert pro Sekunde
            # Schüsse: mehrere harte Einschläge in 4 s (Dauerfeuer/Sturmgewehr oder mehrere Schüsse) – einzelner Schuss zählt nicht
            norm10 = self._median([v for _, v in game])
            self.norm_game = norm10
            with self.lock:
                lows = {round(t, 1): v for t, v in self.low}
                hitd = {round(t, 1): h for t, h in self.hits}
                subd = {round(t, 1): x for t, x in self.sub}
                toned = {round(t, 1): (r if f else 0.0) for t, f, r in self.tones}
            g_recent = [(t, v) for t, v in game if t >= now - 4]
            blocks = 0
            local = self._median([v for _, v in g_recent]) if g_recent else norm10
            for t, v in g_recent:
                h = hitd.get(round(t, 1), 0)
                # Waffe = harter Knall, deutlich lauter als Spiel UND letzte Sekunden, mit Wumms (Tiefbass)
                # UND Knall-Anteil in den Höhen (nur dumpfes Wummern = Schritte/Türen/Schläge) und ohne Tonhöhe. Tür, Schritte, Klicken, Reden, Hupe, Musik zählen nicht.
                kt = round(t, 1)
                if (h and v - norm10 >= 12 and v - local >= 8 and 2.0 <= lows.get(kt, 0.0) <= 8.0
                        and (not subd or subd.get(kt, 99.0) <= 10.0) and toned.get(kt, 0.0) < 0.18):
                    shots += h
                    blocks += 1
            if blocks < 2:
                shots = 0
            # (Explosion wird jetzt über Bild + Knall erkannt, siehe unten)
            norm_g = self._median(list(per_sec.values()))
            last2 = [per_sec[k] for k in range(int(now) - 2, int(now)) if k in per_sec]
            if len(last2) >= 2 and norm_g > -70:
                with self.lock:
                    lw = [v for t, v in self.low if t >= now - 2]
                    sw = [v for t, v in self.sub if t >= now - 2]
                    tw = [1 for t, f, r in self.tones if t >= now - 2 and f and r >= 0.18]
                # Spiel lauter = Motoren, Crash, Action mit Tiefbass – kein Schreien/Weinen/Musik (die haben einen klaren Ton)
                bassy = not sw or sum(sw) / len(sw) <= 12.0
                tonal = len(tw) >= 10
                if lw and sum(lw) / len(lw) >= 2.0 and bassy and not tonal:
                    action = min(last2) - norm_g
        # Sirene (Ton), Blaulicht + Action (Bild)
        siren = siren_score(tones) if len(tones) >= 20 else None
        recent_f = [x for x in frames if x[0] >= now - 3]
        blue = bluelight_score([(r, b) for _, _, r, b in recent_f]) if len(recent_f) >= 6 else None
        vis = None
        if len(frames) >= 480:  # ~2 Minuten Bild als Vergleich
            base_m = self._median([m for _, m, _, _ in frames])
            self.norm_motion = base_m
            last_m = [m for t, m, _, _ in frames if t >= now - 2]
            if last_m and base_m > 0.3:
                vis = (sum(last_m) / len(last_m)) / base_m
        # GTA-Szenen: Cops (Minimap blinkt rot/blau) + Explosion (Feuerball im Bild UND Bass-Knall)
        with self.lock:
            sc = [x for x in self.scenes if x[0] >= now - 4]
            lowdb = list(self.lowdb)
        police = core.police_score([(r, b) for _, r, b, _ in sc]) if len(sc) >= 8 else 0.0
        if police:
            if now - self.police_seen > 40:
                self.chase_clips = 0  # neue Verfolgung
            self.police_seen = now
        flash = core.flash_score([lk for t, _, _, lk in sc if t >= now - 2.5]) if sc else 0.0
        if flash:
            pre = sorted(v for t, v in lowdb if now - 5 <= t < now - 2)
            post = [v for t, v in lowdb if t >= now - 2]
            boom_ok = (not lowdb) or (pre and post and max(post) - pre[len(pre) // 2] >= 20 and max(post) >= -42)
            if boom_ok:
                explosion = flash
                self.boom_seen = now
        cops_ctx = now - self.police_seen < 10
        # Schüsse nur mit Action drumherum (Cops, Bewegung, Explosion) – Schüsse im Hintergrund zählen nicht
        if shots and not (cops_ctx or (vis or 0) >= 1.3 or flash):
            shots = 0
        # Sirene nur, wenn wirklich was los ist (Cops/Blaulicht/Bewegung) – nicht eine Sirene irgendwo in der Stadt
        if siren and not (cops_ctx or (blue or 0) >= 1.0 or (vis or 0) >= 1.6):
            siren = None
        # Messwerte fürs Log (Maximum pro Minute)
        pk = self.peak
        if police:
            pk["cops"] = (pk.get("cops") or 0) + 1
        pk["shots"] = max(pk.get("shots") or 0, shots)
        if explosion is not None:
            pk["boom"] = max(pk.get("boom") or 0.0, explosion)
        for key, val in (("siren", siren), ("blue", blue), ("vis", vis)):
            if val is not None:
                pk[key] = val if pk.get(key) is None else max(pk[key], val)
        pk["u"], pk["kw"] = max(pk["u"], u15), max(pk["kw"], kw)
        if voice_diff is not None:
            pk["voice"] = voice_diff if pk["voice"] is None else max(pk["voice"], voice_diff)
        if action is not None:
            pk["action"] = action if pk["action"] is None else max(pk["action"], action)
        feats = [min(chat_score / 4.0, 3.0), min((kw / max(1, u15)) / 0.5, 2.0),
                 min(max(0.0, (voice_diff or 0) - 4.0) / 8.0, 3.0), min(u15 / max(1.0, mean) / 4.0, 2.0)]
        if now - self.last_clip < self.cooldown:
            return None
        while self.clip_times and self.clip_times[0] < now - 3600:
            self.clip_times.popleft()
        if len(self.clip_times) >= self.max_per_hour:
            return None
        en = self.enabled
        if not en.get("chat", True):
            chat_score = 0.0
        if not en.get("clipreq", True):
            req_users = set()
        if not en.get("voice", True):
            voice_diff = None
        if not en.get("game", True):
            action = None
        if not en.get("shots", True):
            shots = 0
        if not en.get("explosion", True):
            explosion = None
        if not en.get("cops", True):
            police = 0.0
        if not en.get("siren", True):
            siren = None
        if not en.get("bluelight", True):
            blue = None
        if not en.get("vision", True):
            vis = None
        reasons, kind = [], None
        if req_users:
            kind = "clip"
            reasons.append(f"✂ {', '.join(sorted(req_users))[:40]} wollte das clippen")
        warm = now - self.start >= self.warmup
        if u15 < 5 and kw < 2:
            chat_score = 0.0  # bei kleinem Chat nur mit echten Reaktionen (KEKW, Cops, 💀 …)
        if warm and chat_score >= self.t_chat:
            kind = kind or "chat"
            reasons.append(f"{u15} Leute im Chat" + (f" · {kw}× Hype-Wörter" if kw else ""))
        if warm and voice_diff is not None and voice_diff >= self.t_voice:
            kind = kind or "voice"
            reasons.append(f"Stimme +{voice_diff:.0f} dB")
        if warm and action is not None and action >= self.t_action:
            kind = kind or "action"
            reasons.append(f"🎮 Action im Spiel (+{action:.0f} dB)")
        if warm and siren is not None and siren >= 1.2:
            kind = kind or "siren"
            reasons.insert(0, "🚨 Sirene / Alarm / Polizei")
        if explosion is not None:
            shots = 0  # Grollen einer Explosion nicht als Schüsse zählen
        if warm and shots >= self.min_shots:
            kind = kind or "shots"
            reasons.insert(0, f"🔫 Schüsse ({shots}× in 4 s)")
        if explosion is not None:
            kind = kind or "explosion"
            reasons.insert(0, "💥 Explosion")
        if police and self.chase_clips < self.max_chase:
            kind = kind or "cops"
            reasons.insert(0, "🚔 Cops hinter dir" + (" – Verfolgung läuft" if self.chase_clips else ""))
        if warm and blue is not None and blue >= 1.0 and (vis or 0) >= 0.8:  # kein Menü/Karte (die ist rot/blau)
            kind = kind or "bluelight"
            reasons.insert(0, "🚨 Blaulicht im Bild")
        if warm and vis is not None and vis >= self.t_vis:
            kind = kind or "vision"
            reasons.append(f"🎬 Action im Bild (×{vis:.1f} Bewegung)")
        if not kind:
            return None
        self.last_clip = now
        self.clip_times.append(now)
        if police:
            self.chase_clips += 1
        with self.lock:
            self.clipreq.clear()
        sample = core._top_messages([m[2] for m in recent])
        score = min(100, int(20 * chat_score + 6 * max(0.0, (voice_diff or 0)) + 4 * max(0.0, (action or 0))
                             + 25 * (siren or 0) + 25 * (blue or 0) + 15 * max(0.0, (vis or 0) - 1)
                             + (40 if req_users else 0)))
        return {"kind": kind, "reason": " · ".join(reasons), "chat": sample, "features": feats,
                "score": max(score, 30), "t": now}


# --------------------------------------------------------------------------- #
#  Steuerung: wartet auf deinen Stream, clippt live, liefert fertige Clips
# --------------------------------------------------------------------------- #
class LiveClipper:
    def __init__(self, twitch, settings, log, on_clip, on_state, notify):
        self.tw, self.st, self.log = twitch, settings, log
        self.on_clip, self.on_state, self.notify = on_clip, on_state, notify
        self.stop_all = threading.Event()
        self.stream_stop = None
        self.thread = None
        self.count = 0
        self.stream = None
        self.on_end = lambda stream: None
        self.state_prefix = "🔴 LIVE"

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.running():
            return
        self.stop_all.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_all.set()
        if self.stream_stop:
            self.stream_stop.set()

    def _loop(self):
        channel = self.st["channel"].lower()
        try:
            uid = self.tw.user_id(channel)
        except Exception as e:
            self.log(f"❌ Live-Clipper: {e}")
            self.on_state("Fehler – siehe Log")
            return
        self.log(f"🔴 Live-Clipper an – warte, bis {channel} live geht …")
        self.on_state("wartet auf deinen Stream")
        while not self.stop_all.is_set():
            try:
                s = self.tw.stream(uid)
            except Exception as e:
                self.log(f"   ⚠ Live-Status nicht abrufbar: {e}")
                s = None
            if s and not self.stream_stop:
                self._begin(channel, uid, s)
            elif not s and self.stream_stop:
                self._end()
            self.stop_all.wait(30)
        if self.stream_stop:
            self._end()
        self.on_state("aus")

    def _begin(self, channel, uid, s):
        self.stream, self.count = s, 0
        self.stream_stop = threading.Event()
        self.log(f"🔴 LIVE erkannt: „{s.get('title', '')}“ ({s.get('game_name', '')}) – clippe automatisch mit.")
        self.notify("🔴 Live-Clipper läuft", "Du bist live – ich clippe deine Hype-Momente automatisch.")
        self.on_state("🔴 LIVE – 0 Clips")
        sens = self.st.get("live_sens", "Mittel")
        length = int(self.st.get("live_len", 30))
        det = Detector(channel, self.st.get("keywords", core.DEFAULT_KEYWORDS), sens,
                       cooldown=max(45, length + 15),
                       warmup=60 if sens == MAXIMAL else 120)
        self.det = det
        ChatReader(channel, det.add_msg, self.stream_stop, self.log).start()
        # Live immer mithören + mitschauen – unabhängig vom Haken für die VOD-Analyse
        ar = AudioReader(channel, det.add_db, self.stream_stop, self.log)
        ar.on_tone = det.add_tone
        ar.start()
        vr = VideoReader(channel, det.add_frame, self.stream_stop, self.log)
        vr.on_scene = det.add_scene
        vr.start()
        self.log("   💬 lese Chat mit · 🎧 verbinde Live-Ton · 🎥 verbinde Live-Bild …")
        threading.Thread(target=self._detect, args=(det, uid), daemon=True).start()

    def _end(self):
        self.stream_stop.set()
        self.stream_stop = None
        self.log(f"⚫ Stream vorbei – Live-Clipper hat {self.count} Clip(s) erstellt.")
        self.notify("⚫ Stream vorbei", f"{self.count} Live-Clips erstellt – schau sie dir in der App an.")
        self.on_state(f"wartet auf deinen Stream (letzter: {self.count} Clips)")
        try:
            self.on_end(dict(self.stream or {}))
        except Exception:
            pass

    def _detect(self, det, uid):
        stop = self.stream_stop
        max_clips = int(self.st.get("live_max", 25))
        next_report = time.time() + 60
        while stop and not stop.is_set():
            stop.wait(1)
            mode = apply_settings(det, self.st, time.time())
            if mode and mode != getattr(self, "_auto_mode", None):
                self._auto_mode = mode
                self.log(f"🤖 Auto-Profil: {AUTO_NAMES[mode]}")
                self.on_state(f"{self.state_prefix} – {self.count} Clips · {AUTO_NAMES[mode].split(' (')[0]}")
            trig = det.tick(time.time())
            if time.time() >= next_report:
                next_report = time.time() + 60
                pk = det.peak
                fmt = lambda v, u: "—" if v is None else f"{v:+.0f} {u}"
                f2 = lambda v: "—" if v is None else f"{v:.1f}"
                self.log(f"   📊 letzte Minute: Chat {pk['u']} Leute · Stimme {fmt(pk['voice'], 'dB')} "
                         f"(ab {'aus' if det.t_voice >= 999 else '+' + format(det.t_voice, '.0f')}) · Spiel {fmt(pk['action'], 'dB')} (ab +{det.t_action:.0f}) · "
                         f"Sirene {f2(pk.get('siren'))} (ab 1) · Blaulicht {f2(pk.get('blue'))} (ab 1) · "
                         f"Bild ×{f2(pk.get('vis'))} (ab ×{det.t_vis}) · Schüsse {pk.get('shots') or 0} (ab {det.min_shots})"
                         + (" · 💥 Explosion" if pk.get("boom") else "")
                         + (f" · 🚔 Cops {pk['cops']} s" if pk.get("cops") else ""))
                det.peak = {"u": 0, "kw": 0, "voice": None, "action": None, "siren": None, "blue": None,
                            "vis": None}
            if not trig:
                continue
            if self.count >= max_clips:
                continue
            threading.Thread(target=self._make_clip, args=(trig, uid), daemon=True).start()

    def _make_clip(self, trig, uid):
        try:
            # SOFORT clippen – Twitch nimmt die letzten Sekunden VOR jetzt, der Moment ist also drauf
            self.notify("✂ Clip!", trig["reason"][:120])
            length = int(self.st.get("live_len", 30))
            self.count += 1
            nr = self.count
            started = self.stream.get("started_at", "") if self.stream else ""
            offset = 0.0
            try:
                t0 = calendar.timegm(time.strptime(started, "%Y-%m-%dT%H:%M:%SZ"))
                offset = max(0.0, trig["t"] - t0)
            except Exception:
                pass
            self.log(f"✂ LIVE-Clip #{nr}: {trig['reason']}")
            self.on_state(f"🔴 LIVE – {self.count} Clips")
            cid = self.tw.create_clip(uid, duration=length, title=f"Highlight #{nr}")
            info = self.tw.wait_clip(cid)
            if not info:
                raise LiveError("Twitch braucht zu lange für den Clip – er ist aber auf Twitch gespeichert.")
            date = time.strftime("%Y-%m-%d")
            title = core.safe_name((self.stream or {}).get("title", "Live"))
            folder = os.path.join(self.st["outdir"], f"{date}_LIVE_{title}")
            os.makedirs(folder, exist_ok=True)
            base = os.path.join(folder, f"live{nr:02d}_{core.fmt_time(offset, '-')}")
            runner = core.Runner(lambda *_: None)
            runner.run([core.tool("yt-dlp"), "--no-part", "-f", "best", "--ffmpeg-location",
                        os.path.dirname(core.tool("ffmpeg")), "--remux-video", "mp4", "-o", base + ".%(ext)s",
                        info.get("url") or f"https://clips.twitch.tv/{cid}"], quiet=True)
            wide = next((base + e for e in (".mp4", ".mkv", ".webm") if os.path.isfile(base + e)), None)
            if not wide:
                raise LiveError("Clip-Download fehlgeschlagen")
            tall = None
            if self.st.get("fmt", "") != "16:9 (YouTube/Twitch)":
                tall = core.make_vertical(wide, base + "_tiktok.mp4", self.st.get("vstyle_key", "blur"), runner)
            dur = float(info.get("duration") or length)
            ok, probs, qinfo = core.check_clip(tall or wide, dur, runner, tall=bool(tall))
            thrill = None
            if self.st.get("live_rate", True):
                try:
                    det = getattr(self, "det", None)
                    pts, why = rate_clip(wide, getattr(det, "norm_game", None), getattr(det, "norm_motion", None),
                                         max(2, int(self.st.get("live_shots", 4))))
                    keep = trig.get("kind") == "clip" or pts >= int(self.st.get("live_rate_min", 35))
                    thrill = {"score": pts, "why": why, "keep": keep}
                    self.log(f"   🎯 Spannung #{nr}: {pts}/100 " + (("· " + ", ".join(why)) if why else "")
                             + ("" if keep else " → 💤 zu langweilig, wird nicht hochgeladen"))
                except Exception as e:
                    self.log(f"   ⚠ Spannungs-Check ging nicht ({str(e)[:80]})")
            h = {"time": offset, "start": max(0.0, offset - dur), "end": offset, "score": trig["score"],
                 "reason": "🔴 LIVE · " + trig["reason"], "chat": trig["chat"], "features": trig["features"],
                 "clipped": True, "live": True, "clip_id": cid, "clip_url": info.get("url", ""),
                 "files": {"wide": wide, "tall": tall, "vstyle": self.st.get("vstyle_key", "blur")},
                 "qc": {"ok": ok, "problems": probs, "info": qinfo}, "thrill": thrill}
            if thrill and not thrill["keep"]:
                h["boring"] = True
                if self.st.get("live_drop") == "delete":
                    for fp in (wide, tall):
                        try:
                            if fp:
                                os.remove(fp)
                        except Exception:
                            pass
                    self.log(f"   🗑 Clip #{nr} gelöscht (bleibt auf Twitch unter deinen Clips)")
                    return
            self.log(f"   ✅ LIVE-Clip #{nr} fertig ({info.get('url', '')})" +
                     ("" if ok else " – ⚠ " + ", ".join(probs)))
            self.on_clip(dict(self.stream or {}), h)
        except Exception as e:
            self.log(f"   ❌ LIVE-Clip fehlgeschlagen: {e}")


# --------------------------------------------------------------------------- #
#  🎮 PS Remote Play ohne Stream: App schaut das Remote-Play-Fenster mit, OBS speichert den Clip
# --------------------------------------------------------------------------- #
RP_TITLE = "PS Remote Play"


def find_window(part=RP_TITLE):
    """Sichtbares, nicht minimiertes Fenster, dessen Titel `part` enthält → (titel, (x, y, w, h)) oder None."""
    if not IS_WIN:
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    try:
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # echte Pixel (wegen Windows-Skalierung)
    except Exception:
        pass
    found = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if part.lower() in buf.value.lower():
            rect = wintypes.RECT()
            try:
                ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))
            except Exception:
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w > 200 and h > 150:
                found.append((buf.value, (rect.left, rect.top, w, h)))
                return False
        return True
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb)
    user32.EnumWindows(proc, 0)
    return found[0] if found else None


class OBSError(Exception):
    pass


class OBS:
    """Mini-Client für OBS WebSocket 5 (in OBS 28+ eingebaut: Werkzeuge → WebSocket-Servereinstellungen)."""

    def __init__(self, port=4455, password="", host="127.0.0.1"):
        self.host, self.port, self.password = host, int(port), password or ""
        self.sock = None
        self.rid = 0
        self.pending = b""

    def _send(self, obj):
        data = json.dumps(obj).encode("utf-8")
        mask = os.urandom(4)
        head = bytearray([0x81])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += n.to_bytes(2, "big")
        else:
            head.append(0x80 | 127)
            head += n.to_bytes(8, "big")
        head += mask
        self.sock.sendall(bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv_exact(self, n):
        buf, self.pending = self.pending[:n], self.pending[n:]
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise OBSError("OBS hat die Verbindung geschlossen")
            buf += chunk
        return buf

    def _recv(self):
        while True:
            b1, b2 = self._recv_exact(2)
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = int.from_bytes(self._recv_exact(2), "big")
            elif n == 127:
                n = int.from_bytes(self._recv_exact(8), "big")
            mask = self._recv_exact(4) if b2 & 0x80 else None
            data = self._recv_exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 8:
                raise OBSError("OBS hat abgelehnt (Passwort falsch?)")
            if op == 1:
                return json.loads(data.decode("utf-8"))

    def connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=5)
        except OSError:
            raise OBSError("OBS ist nicht offen oder der WebSocket-Server ist aus "
                           "(OBS → Werkzeuge → WebSocket-Servereinstellungen → aktivieren)")
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET / HTTP/1.1\r\nHost: {self.host}:{self.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                           "Sec-WebSocket-Protocol: obswebsocket.json\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(1024)
            if not chunk:
                break
            resp += chunk
        head, _, self.pending = resp.partition(b"\r\n\r\n")  # Rest gehört schon zur ersten Nachricht
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise OBSError("OBS-WebSocket antwortet nicht richtig")
        hello = self._recv()
        ident = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = (hello.get("d") or {}).get("authentication")
        if auth:
            if not self.password:
                raise OBSError("OBS will ein Passwort – trag es in der App ein")
            secret = base64.b64encode(hashlib.sha256((self.password + auth["salt"]).encode()).digest()).decode()
            ident["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        self._send({"op": 1, "d": ident})
        msg = self._recv()
        if msg.get("op") != 2:
            raise OBSError("OBS hat die Anmeldung abgelehnt (Passwort falsch?)")
        return self

    def call(self, req, data=None):
        if not self.sock:
            self.connect()
        self.rid += 1
        rid = str(self.rid)
        self._send({"op": 6, "d": {"requestType": req, "requestId": rid, "requestData": data or {}}})
        while True:
            msg = self._recv()
            d = msg.get("d") or {}
            if msg.get("op") == 7 and d.get("requestId") == rid:
                st = d.get("requestStatus") or {}
                if not st.get("result"):
                    raise OBSError(f"{req}: {st.get('comment') or st.get('code')}")
                return d.get("responseData") or {}

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None


def obs_check(port, password):
    """Testet die OBS-Verbindung + Wiederholungspuffer. Gibt (ok, text) zurück."""
    o = OBS(port, password)
    try:
        o.connect()
        ver = o.call("GetVersion")
        st = o.call("GetReplayBufferStatus")
        if not st.get("outputActive"):
            o.call("StartReplayBuffer")
        return True, f"OBS {ver.get('obsVersion', '')} verbunden · Wiederholungspuffer läuft"
    except OBSError as e:
        msg = str(e)
        if "ReplayBuffer" in msg:
            msg = ("Wiederholungspuffer ist in OBS aus – OBS → Einstellungen → Ausgabe → "
                   "„Wiederholungspuffer aktivieren“ (max. Zeit 40 s)")
        return False, msg
    except Exception as e:
        return False, str(e)[:150]
    finally:
        o.close()


class AudioLoopback(threading.Thread):
    """Nimmt auf, was aus deinen Lautsprechern/dem Headset kommt (WASAPI-Loopback), immer die letzten ~2 Min."""
    RATE, CH = 48000, 2

    def __init__(self, stop, log):
        super().__init__(daemon=True)
        self.stop, self.log = stop, log
        self.chunks = deque(maxlen=1300)  # (endzeit, int16-bytes) à 0,1 s
        self.ok = False

    def run(self):
        try:
            import warnings
            warnings.filterwarnings("ignore")
            import soundcard as scard
            spk = scard.default_speaker()
            mic = scard.get_microphone(id=str(spk.name), include_loopback=True)
        except Exception as e:
            self.log(f"   ⚠ Ton-Aufnahme nicht möglich ({str(e)[:90]}) – Clips werden ohne Ton gespeichert.")
            return
        try:
            with mic.recorder(samplerate=self.RATE, channels=self.CH) as rec:
                self.ok = True
                self.log("   🔊 Ton wird mitgeschnitten (was aus deinen Lautsprechern/dem Headset kommt)")
                n = self.RATE // 10
                while not self.stop.is_set():
                    data = rec.record(numframes=n)
                    t = time.time()
                    pcm = (data.clip(-1, 1) * 32767).astype("<i2").tobytes()
                    self.chunks.append((t, pcm))
        except Exception as e:
            self.ok = False
            self.log(f"   ⚠ Ton-Aufnahme abgebrochen ({str(e)[:90]})")

    def write_wav(self, a, b, path):
        """Ton zwischen den Uhrzeiten a und b als WAV schreiben. Fehlendes wird mit Stille aufgefüllt."""
        import wave
        bps = self.CH * 2
        total = int((b - a) * self.RATE)
        buf = bytearray(total * bps)
        for t_end, pcm in list(self.chunks):
            t_start = t_end - len(pcm) / bps / self.RATE
            if t_end <= a or t_start >= b:
                continue
            src_off = max(0, int((a - t_start) * self.RATE)) * bps
            dst_off = max(0, int((t_start - a) * self.RATE)) * bps
            piece = pcm[src_off:]
            piece = piece[:max(0, len(buf) - dst_off)]
            buf[dst_off:dst_off + len(piece)] = piece
        with wave.open(path, "wb") as w:
            w.setnchannels(self.CH)
            w.setsampwidth(2)
            w.setframerate(self.RATE)
            w.writeframes(bytes(buf))
        return path


class SelfRecorder(threading.Thread):
    """Nimmt das Remote-Play-Fenster SELBST auf (ohne OBS): ffmpeg schreibt 4-Sekunden-Stücke in voller Qualität
    und liefert gleichzeitig Mini-Bilder für die Erkennung. Ton kommt aus AudioLoopback."""

    def __init__(self, rect, workdir, on_frame, on_scene, stop, log):
        super().__init__(daemon=True)
        self.rect, self.dir, self.stop, self.log = rect, workdir, stop, log
        self.on_frame, self.on_scene = on_frame, on_scene
        self.t0 = None
        self.proc = None
        self.audio = AudioLoopback(stop, log)

    def _cmd(self):
        x, y, w, h = self.rect
        w, h = w - w % 2, h - h % 2
        return [core.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostats",
                "-f", "gdigrab", "-framerate", "30", "-draw_mouse", "0", "-offset_x", str(x), "-offset_y", str(y),
                "-video_size", f"{w}x{h}", "-i", "desktop",
                "-filter_complex", f"[0:v]split=2[a][b];[a]scale=-2:'min(1080,ih)',format=yuv420p[hq];"
                                   f"[b]fps=4,scale={VW}:{VH}[lq]",
                "-map", "[hq]", "-c:v", "libx264", "-preset", "superfast", "-crf", "23", "-g", "30",
                "-f", "segment", "-segment_time", "4", "-reset_timestamps", "1",
                "-segment_list", "list.csv", "-segment_list_type", "csv", "seg_%06d.mp4",
                "-map", "[lq]", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]

    def run(self):
        os.makedirs(self.dir, exist_ok=True)
        for f in os.listdir(self.dir):
            try:
                os.remove(os.path.join(self.dir, f))
            except OSError:
                pass
        self.audio.start()
        kw = {"creationflags": 0x08000000} if IS_WIN else {}
        size = VW * VH * 3
        try:
            self.proc = subprocess.Popen(self._cmd(), cwd=self.dir, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, stdin=subprocess.PIPE, **kw)
            prev = None
            last_clean = time.time()
            while not self.stop.is_set():
                raw = self.proc.stdout.read(size)
                if not raw or len(raw) < size:
                    break
                now = time.time()
                if self.t0 is None:
                    self.t0 = now - 0.25
                    self.log("   🎥 Nehme das Remote-Play-Fenster selbst auf (ohne OBS) – erkenne Cops/Explosionen")
                motion, red, blue, prev = analyze_video_frame(raw, prev)
                self.on_frame(now, motion, red, blue)
                try:
                    self.on_scene(now, *scene_frame(raw))
                except Exception:
                    pass
                if now - last_clean > 15:
                    last_clean = now
                    self._cleanup(now)
        except Exception as e:
            if not self.stop.is_set():
                self.log(f"   ⚠ Eigene Aufnahme ging nicht ({str(e)[:100]})")
        finally:
            self.kill()

    def kill(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.stdin.write(b"q")
                p.stdin.flush()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def segments(self):
        """[(datei, start_uhrzeit, ende_uhrzeit)] der fertigen Stücke."""
        out = []
        if self.t0 is None:
            return out
        try:
            with open(os.path.join(self.dir, "list.csv"), encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.strip().split(",")
                    if len(parts) >= 3:
                        fp = os.path.join(self.dir, parts[0])
                        if os.path.isfile(fp):
                            out.append((fp, self.t0 + float(parts[1]), self.t0 + float(parts[2])))
        except (OSError, ValueError):
            pass
        return out

    def _cleanup(self, now):
        for fp, a, b in self.segments():
            if b < now - 150:
                try:
                    os.remove(fp)
                except OSError:
                    pass

    def save(self, length, out_path):
        """Speichert die letzten `length` Sekunden (bis jetzt) als MP4 mit Ton."""
        T = time.time()
        segs = []
        for _ in range(30):  # warten, bis das Stück mit dem Moment fertig geschrieben ist
            segs = self.segments()
            if segs and segs[-1][2] >= T - 0.2:
                break
            time.sleep(0.5)
        if not segs:
            raise LiveError("Noch keine Aufnahme vorhanden")
        A = max(segs[0][1], T - length)
        use = [s_ for s_ in segs if s_[2] > A and s_[1] < T]
        if not use:
            raise LiveError("Aufnahme-Stücke fehlen")
        cat = os.path.join(self.dir, f"cat_{int(T)}.txt")
        with open(cat, "w", encoding="utf-8") as f:
            for fp, _, _ in use:
                f.write("file '" + fp.replace("\\", "/").replace("'", "'\\''") + "'\n")
        args = [core.tool("ffmpeg"), "-y", "-v", "error", "-ss", f"{max(0.0, A - use[0][1]):.2f}",
                "-f", "concat", "-safe", "0", "-i", cat]
        wav = None
        if self.audio.ok and self.audio.chunks:
            wav = self.audio.write_wav(A, min(T, use[-1][2]), os.path.join(self.dir, f"a_{int(T)}.wav"))
            args += ["-i", wav, "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "160k"]
        args += ["-t", f"{min(T, use[-1][2]) - A:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]
        kw = {"creationflags": 0x08000000} if IS_WIN else {}
        r = subprocess.run(args, capture_output=True, stdin=subprocess.DEVNULL, timeout=300, **kw)
        for fp in (cat, wav):
            try:
                if fp:
                    os.remove(fp)
            except OSError:
                pass
        if r.returncode != 0 or not os.path.isfile(out_path):
            raise LiveError("Clip speichern fehlgeschlagen: " + r.stderr.decode("utf-8", "replace")[-200:])
        return out_path


class RemotePlayClipper(LiveClipper):
    """Clippt PS5-Gameplay, wenn du NICHT streamst: schaut das PS-Remote-Play-Fenster mit (Cops, Explosionen,
    Blaulicht) und lässt OBS den Wiederholungspuffer speichern (OBS nimmt das Fenster + Ton auf)."""

    def __init__(self, settings, log, on_clip, on_state, notify, is_live=lambda: False):
        super().__init__(None, settings, log, on_clip, on_state, notify)
        self.is_live = is_live
        self.rect = None
        self.obs = None
        self.obs_lock = threading.Lock()
        self.state_prefix = "🎮 Remote Play"

    def _loop(self):
        self.log("🎮 Remote-Play-Clipper an – warte auf das PS-Remote-Play-Fenster …")
        self.on_state("wartet auf PS Remote Play")
        while not self.stop_all.is_set():
            win = find_window() if not self.is_live() else None
            if win and not self.stream_stop:
                self._begin_rp(win)
            elif self.stream_stop and (not win or win[1] != self.rect):
                self._end()
                if win:
                    continue  # Fenster verschoben/größer → sofort neu starten
            self.stop_all.wait(5)
        if self.stream_stop:
            self._end()
        self.on_state("aus")

    def _grab_args(self):
        x, y, w, h = self.rect
        w, h = w - w % 2, h - h % 2
        return ["-f", "gdigrab", "-framerate", "8", "-draw_mouse", "0", "-offset_x", str(x), "-offset_y", str(y),
                "-video_size", f"{w}x{h}", "-i", "desktop"]

    def _begin_rp(self, win):
        self.rect = win[1]
        date = time.strftime("%Y-%m-%d")
        self.stream = {"id": f"rp-{date}", "title": f"PS5 Remote Play {date}",
                       "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.count = 0
        self.stream_stop = threading.Event()
        self.self_rec = None
        det = Detector(self.st.get("channel", "x"), self.st.get("keywords", core.DEFAULT_KEYWORDS), "Mittel",
                       cooldown=60, warmup=90)
        self.det = det
        if self.st.get("rp_mode", "self") == "self":
            self.log("🎮 PS Remote Play erkannt – ich nehme selbst auf und schaue mit (ohne OBS).")
            self.self_rec = SelfRecorder(self.rect, os.path.join(core.WORK_DIR, "_remoteplay"), det.add_frame,
                                         det.add_scene, self.stream_stop, self.log)
            self.self_rec.start()
        else:
            ok, txt = obs_check(self.st.get("obs_port", 4455), self.st.get("obs_pw", ""))
            self.log(f"🎮 PS Remote Play erkannt – ich schaue mit. OBS: {'✅ ' if ok else '⚠ '}{txt}")
            if not ok:
                self.notify("⚠ OBS fehlt", "Für Remote-Play-Clips mit OBS muss OBS mit Wiederholungspuffer laufen.")
            vr = VideoReader("rp", det.add_frame, self.stream_stop, self.log)
            vr.input_args = self._grab_args
            vr.on_scene = det.add_scene
            vr.start()
        self.on_state("🎮 Remote Play – 0 Clips")
        threading.Thread(target=self._detect, args=(det, None), daemon=True).start()

    def _end(self):
        self.stream_stop.set()
        self.stream_stop = None
        self.log(f"⚫ PS Remote Play zu – {self.count} Clip(s) erstellt.")
        self.on_state(f"wartet auf PS Remote Play (letztes Mal: {self.count} Clips)")
        try:
            self.on_end(dict(self.stream or {}))
        except Exception:
            pass

    def _make_clip(self, trig, uid):
        try:
            self.notify("✂ Clip!", trig["reason"][:120])
            self.count += 1
            nr = self.count
            self.log(f"✂ {self.state_prefix}-Clip #{nr}: {trig['reason']} – speichere gleich die letzten Sekunden …")
            self.on_state(f"{self.state_prefix} – {self.count} Clips")
            # kurz warten, damit auch das Danach (Explosion, Crash) mit drauf ist
            time.sleep(max(0, int(self.st.get("rp_post", 8))))
            rec = getattr(self, "self_rec", None)
            if rec is not None:
                length = int(self.st.get("live_len", 30)) + max(0, int(self.st.get("rp_post", 8)))
                tmp = os.path.join(core.WORK_DIR, "_remoteplay", f"clip_{nr:02d}_{int(time.time())}.mp4")
                with self.obs_lock:
                    path = rec.save(length, tmp)
                self._last_replay = path
            else:
                with self.obs_lock:
                    o = OBS(self.st.get("obs_port", 4455), self.st.get("obs_pw", ""))
                    try:
                        o.connect()
                        o.call("SaveReplayBuffer")
                        path = ""
                        for _ in range(20):
                            time.sleep(0.5)
                            path = (o.call("GetLastReplayBufferReplay") or {}).get("savedReplayPath", "")
                            if path and path != getattr(self, "_last_replay", "") and os.path.isfile(path):
                                break
                        self._last_replay = path
                    finally:
                        o.close()
            if not path or not os.path.isfile(path):
                raise LiveError("OBS hat keinen Clip gespeichert (Wiederholungspuffer an?)")
            time.sleep(1.5)  # OBS schreibt die Datei noch fertig
            date = time.strftime("%Y-%m-%d")
            folder = os.path.join(self.st["outdir"], f"{date}_PS5_" + ("Capture" if "Capture" in self.state_prefix
                                                                         else "RemotePlay"))
            os.makedirs(folder, exist_ok=True)
            base = os.path.join(folder, f"rp{nr:02d}_{time.strftime('%H-%M-%S')}")
            wide = base + os.path.splitext(path)[1]
            shutil.move(path, wide)
            runner = core.Runner(lambda *_: None)
            if not wide.lower().endswith(".mp4"):
                mp4 = base + ".mp4"
                runner.run([core.tool("ffmpeg"), "-y", "-v", "error", "-i", wide, "-c", "copy", mp4], quiet=True)
                if os.path.isfile(mp4):
                    os.remove(wide)
                    wide = mp4
            tall = None
            if self.st.get("fmt", "") != "16:9 (YouTube/Twitch)":
                tall = core.make_vertical(wide, base + "_tiktok.mp4", self.st.get("vstyle_key", "blur"), runner)
            ok, probs, qinfo = core.check_clip(tall or wide, 0, runner, tall=bool(tall))
            thrill = None
            if self.st.get("live_rate", True):
                try:
                    pts, why = rate_clip(wide, None, getattr(self.det, "norm_motion", None),
                                         max(2, int(self.st.get("live_shots", 4))))
                    keep = trig.get("kind") == "clip" or pts >= int(self.st.get("live_rate_min", 35))
                    thrill = {"score": pts, "why": why, "keep": keep}
                    self.log(f"   🎯 Spannung #{nr}: {pts}/100 " + (("· " + ", ".join(why)) if why else ""))
                except Exception as e:
                    self.log(f"   ⚠ Spannungs-Check ging nicht ({str(e)[:80]})")
            t0 = time.time() - (self.det.start or time.time())
            h = {"time": t0, "start": max(0.0, t0 - 30), "end": t0, "score": trig["score"],
                 "reason": ("🎥 CAPTURE · " if "Capture" in self.state_prefix else "🎮 REMOTE PLAY · ") + trig["reason"], "chat": trig["chat"], "features": trig["features"],
                 "clipped": True, "live": True, "files": {"wide": wide, "tall": tall,
                                                           "vstyle": self.st.get("vstyle_key", "blur")},
                 "qc": {"ok": ok, "problems": probs, "info": qinfo}, "thrill": thrill}
            if thrill and not thrill["keep"]:
                h["boring"] = True
            self.log(f"   ✅ {self.state_prefix}-Clip #{nr} fertig: {os.path.basename(wide)}")
            self.on_clip(dict(self.stream or {}), h)
        except Exception as e:
            self.log(f"   ❌ {self.state_prefix}-Clip fehlgeschlagen: {e}")


# --------------------------------------------------------------------------- #
#  🎥 Capture-Karte (oder jede andere Quelle in OBS): App schaut das OBS-Bild mit, OBS speichert den Clip
# --------------------------------------------------------------------------- #
class OBSFrameReader(threading.Thread):
    """Holt 4x pro Sekunde ein Mini-Bild der OBS-Szene über den WebSocket (kein Gerätekonflikt mit OBS)."""

    def __init__(self, st, on_frame, on_scene, stop, log):
        super().__init__(daemon=True)
        self.st, self.on_frame, self.on_scene, self.stop, self.log = st, on_frame, on_scene, stop, log

    def run(self):
        try:
            from PIL import Image
            import io
        except ImportError:
            self.log("   ❌ Pillow fehlt – App einmal neu starten (wird automatisch installiert).")
            return
        first = True
        while not self.stop.is_set():
            o = OBS(self.st.get("obs_port", 4455), self.st.get("obs_pw", ""))
            try:
                o.connect()
                prev = None
                while not self.stop.is_set():
                    t0 = time.time()
                    src = (self.st.get("cap_source") or "").strip() or \
                        o.call("GetCurrentProgramScene").get("currentProgramSceneName", "")
                    data = o.call("GetSourceScreenshot", {"sourceName": src, "imageFormat": "png",
                                                          "imageWidth": VW, "imageHeight": VH})
                    b64 = data.get("imageData", "").split(",", 1)[-1]
                    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
                    if img.size != (VW, VH):
                        img = img.resize((VW, VH))
                    raw = img.tobytes()
                    if first:
                        self.log(f"   🎥 OBS-Bild verbunden („{src}“) – erkenne Cops, Explosionen, Blaulicht")
                        first = False
                    motion, red, blue, prev = analyze_video_frame(raw, prev)
                    now = time.time()
                    self.on_frame(now, motion, red, blue)
                    self.on_scene(now, *scene_frame(raw))
                    self.stop.wait(max(0.0, 0.25 - (time.time() - t0)))
            except Exception as e:
                if not self.stop.is_set():
                    self.log(f"   ⚠ OBS-Bild nicht verfügbar ({str(e)[:100]}) – versuche es gleich nochmal …")
            finally:
                o.close()
            self.stop.wait(10)


class CaptureClipper(RemotePlayClipper):
    """Clippt, was in OBS zu sehen ist (z. B. PS5 über Capture-Karte), wenn du NICHT live bist."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.state_prefix = "🎥 Capture"

    def _loop(self):
        self.log("🎥 Capture-Clipper an – warte auf OBS …")
        self.on_state("wartet auf OBS")
        while not self.stop_all.is_set():
            live = self.is_live()
            ok = False
            if not live:
                o = OBS(self.st.get("obs_port", 4455), self.st.get("obs_pw", ""))
                try:
                    o.connect()
                    o.call("GetVersion")
                    ok = True
                except Exception:
                    ok = False
                finally:
                    o.close()
            if ok and not self.stream_stop:
                self._begin_cap()
            elif self.stream_stop and not ok:
                self._end()
            self.stop_all.wait(15)
        if self.stream_stop:
            self._end()
        self.on_state("aus")

    def _begin_cap(self):
        date = time.strftime("%Y-%m-%d")
        self.stream = {"id": f"cap-{date}", "title": f"PS5 Capture {date}",
                       "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.count = 0
        self.stream_stop = threading.Event()
        ok, txt = obs_check(self.st.get("obs_port", 4455), self.st.get("obs_pw", ""))
        self.log(f"🎥 OBS gefunden – ich schaue das Bild mit. {'✅ ' if ok else '⚠ '}{txt}")
        self.on_state("🎥 Capture – 0 Clips")
        det = Detector(self.st.get("channel", "x"), self.st.get("keywords", core.DEFAULT_KEYWORDS), "Mittel",
                       cooldown=60, warmup=90)
        self.det = det
        OBSFrameReader(self.st, det.add_frame, det.add_scene, self.stream_stop, self.log).start()
        threading.Thread(target=self._detect, args=(det, None), daemon=True).start()

    def _end(self):
        self.stream_stop.set()
        self.stream_stop = None
        self.log(f"⚫ Capture-Clipper pausiert – {self.count} Clip(s) erstellt.")
        self.on_state(f"wartet auf OBS (letztes Mal: {self.count} Clips)")
        try:
            self.on_end(dict(self.stream or {}))
        except Exception:
            pass
