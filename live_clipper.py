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
SENSITIVITY = {  # (Chat-Score, Stimme +dB, Spiel-Action +LU, min. Leute im Chat)
    MAXIMAL: (1.5, 5.0, 4.0, 2),
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
                    self.on_frame(time.time(), motion, red, blue)
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
        self.game = deque(maxlen=4000)    # (t, dB) ~10x pro Sekunde
        self.tones = deque(maxlen=40)     # (t, tonhöhe, anteil) ~10x pro Sekunde
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
        with self.lock:
            self.tones.append((t, f, r))

    def add_frame(self, t, motion, red, blue):
        with self.lock:
            self.frames.append((t, motion, red, blue))

    def add_db(self, t, v, kind="voice"):
        with self.lock:
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
            norm = self._median([v for _, v in dbs])
            last2 = [v for t, v in dbs if t >= now - 3][-2:]
            if len(last2) == 2:
                voice_diff = min(last2) - norm
        action = None
        if len(game) >= 600:  # ~1 Minute Spielton als Vergleich
            acc = {}
            for t, v in game:
                a = acc.setdefault(int(t), [0.0, 0])
                a[0] += v
                a[1] += 1
            per_sec = {k: a[0] / a[1] for k, a in acc.items()}  # Mittelwert pro Sekunde
            norm_g = self._median(list(per_sec.values()))
            last2 = [per_sec[k] for k in range(int(now) - 2, int(now)) if k in per_sec]
            if len(last2) >= 2 and norm_g > -70:
                action = min(last2) - norm_g
        # Sirene (Ton), Blaulicht + Action (Bild)
        siren = siren_score(tones) if len(tones) >= 20 else None
        recent_f = [x for x in frames if x[0] >= now - 3]
        blue = bluelight_score([(r, b) for _, _, r, b in recent_f]) if len(recent_f) >= 6 else None
        vis = None
        if len(frames) >= 240:  # ~1 Minute Bild als Vergleich
            base_m = self._median([m for _, m, _, _ in frames])
            last_m = [m for t, m, _, _ in frames if t >= now - 2]
            if last_m and base_m > 0.3:
                vis = (sum(last_m) / len(last_m)) / base_m
        # Messwerte fürs Log (Maximum pro Minute)
        pk = self.peak
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
        if siren is not None and siren >= 1.0:
            kind = kind or "siren"
            reasons.insert(0, "🚨 Sirene / Polizei")
        if blue is not None and blue >= 1.0:
            kind = kind or "bluelight"
            reasons.insert(0, "🚨 Blaulicht im Bild")
        if warm and vis is not None and vis >= self.t_vis:
            kind = kind or "vision"
            reasons.append(f"🎬 Action im Bild (×{vis:.1f} Bewegung)")
        if not kind:
            return None
        self.last_clip = now
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
                       cooldown=(length + 2) if sens == MAXIMAL else max(30, length + 10),
                       warmup=60 if sens == MAXIMAL else 120)
        ChatReader(channel, det.add_msg, self.stream_stop, self.log).start()
        # Live immer mithören + mitschauen – unabhängig vom Haken für die VOD-Analyse
        ar = AudioReader(channel, det.add_db, self.stream_stop, self.log)
        ar.on_tone = det.add_tone
        ar.start()
        VideoReader(channel, det.add_frame, self.stream_stop, self.log).start()
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
            trig = det.tick(time.time())
            if time.time() >= next_report:
                next_report = time.time() + 60
                pk = det.peak
                fmt = lambda v, u: "—" if v is None else f"{v:+.0f} {u}"
                f2 = lambda v: "—" if v is None else f"{v:.1f}"
                self.log(f"   📊 letzte Minute: Chat {pk['u']} Leute · Stimme {fmt(pk['voice'], 'dB')} "
                         f"(ab +{det.t_voice:.0f}) · Spiel {fmt(pk['action'], 'dB')} (ab +{det.t_action:.0f}) · "
                         f"Sirene {f2(pk.get('siren'))} (ab 1) · Blaulicht {f2(pk.get('blue'))} (ab 1) · "
                         f"Bild ×{f2(pk.get('vis'))} (ab ×{det.t_vis})")
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
            h = {"time": offset, "start": max(0.0, offset - dur), "end": offset, "score": trig["score"],
                 "reason": "🔴 LIVE · " + trig["reason"], "chat": trig["chat"], "features": trig["features"],
                 "clipped": True, "live": True, "clip_id": cid, "clip_url": info.get("url", ""),
                 "files": {"wide": wide, "tall": tall, "vstyle": self.st.get("vstyle_key", "blur")},
                 "qc": {"ok": ok, "problems": probs, "info": qinfo}}
            self.log(f"   ✅ LIVE-Clip #{nr} fertig ({info.get('url', '')})" +
                     ("" if ok else " – ⚠ " + ", ".join(probs)))
            self.on_clip(dict(self.stream or {}), h)
        except Exception as e:
            self.log(f"   ❌ LIVE-Clip fehlgeschlagen: {e}")
