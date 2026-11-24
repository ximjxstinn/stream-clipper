# -*- coding: utf-8 -*-
"""
Live-Clipper: Während du live bist (PC oder PS5 – egal), liest die App den Twitch-Chat mit,
hört deinen Stream-Ton ab und erstellt bei Hype-Momenten automatisch echte Twitch-Clips.
Die Clips werden heruntergeladen, als TikTok-Version gebaut, geprüft und an die App übergeben.

Twitch-Zugang: eigene Twitch-App (Client-Typ "Öffentlich") + Geräte-Code-Anmeldung, nur Scope clips:edit.
Daten bleiben lokal in twitch.json.
"""
import calendar
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
        self.proc = None

    def run(self):
        kw = {"creationflags": 0x08000000} if IS_WIN else {}
        first = True
        size = VW * VH * 3
        while not self.stop.is_set():
            try:
                url = self.source or _stream_url(self.channel, "160p/160p30/360p/worst[vcodec!=none]/worst")
                self.proc = subprocess.Popen(
                    [core.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostats", "-i", url, "-an",
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
            # Stimm-Schwelle live aus den Einstellungen (0 = Stimme zählt gar nicht)
            v = self.st.get("live_voice")
            if v is not None:
                det.t_voice = float(v) if float(v) > 0 else 999.0
            det.enabled = {k: True for k in TRIGGERS}
            det.enabled.update(self.st.get("live_trig") or {})
            det.t_action = float(self.st.get("live_game", det.t_action))
            det.t_vis = float(self.st.get("live_vis", det.t_vis))
            det.cooldown = max(20, int(self.st.get("live_gap", det.cooldown)))
            det.max_per_hour = max(1, int(self.st.get("live_hour", 999)))
            det.min_shots = max(2, int(self.st.get("live_shots", 4)))
            det.max_chase = max(1, int(self.st.get("live_chase", 3)))
            # „Modus“ + „Szenen-Erkennung im Bild“ aus dem Hauptfenster gelten auch live
            sens = SENSITIVITY.get(self.st.get("live_sens", "Mittel"), SENSITIVITY["Mittel"])
            det.t_chat, det.min_people = sens[0], sens[3]
            if not self.st.get("vis", True):
                det.enabled["vision"] = det.enabled["bluelight"] = False
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
