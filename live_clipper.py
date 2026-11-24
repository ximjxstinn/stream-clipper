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
    MAXIMAL: (1.5, 6.0, 6.0, 2),
    "Hoch (mehr Clips)": (2.5, 8.0, 9.0, 3),
    "Mittel": (4.0, 10.0, 12.0, 3),
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
#  Live-Ton (Stimme) über die Audiospur des Streams
# --------------------------------------------------------------------------- #
class AudioReader(threading.Thread):
    def __init__(self, channel, on_db, stop, log):
        super().__init__(daemon=True)
        self.channel, self.on_db, self.stop, self.log = channel.lower(), on_db, stop, log
        self.proc = None

    def run(self):
        kw = {"creationflags": 0x08000000} if IS_WIN else {}
        while not self.stop.is_set():
            try:
                out = subprocess.run([core.tool("yt-dlp"), "-g", "-f", "audio_only/Audio_Only/worst",
                                      f"https://www.twitch.tv/{self.channel}"], capture_output=True, text=True,
                                     timeout=60, stdin=subprocess.DEVNULL, **kw)
                url = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
                if not url.startswith("http"):
                    raise RuntimeError("keine Live-Audiospur")
                # Kette 1: deine Stimme (Sprach-Frequenzen) · Kette 2: Spiel-Lautstärke (Action, Sirenen, Schüsse)
                graph = ("[0:a]aformat=channel_layouts=mono,asplit=2[v][g];"
                         "[v]aresample=8000,highpass=f=250,lowpass=f=3400,asetnsamples=8000,"
                         "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-[vo];"
                         "[g]aresample=48000,ebur128=metadata=1,ametadata=print:key=lavfi.r128.M:file=-[go]")
                self.proc = subprocess.Popen([core.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostats",
                                              "-i", url, "-vn", "-filter_complex", graph,
                                              "-map", "[vo]", "-f", "null", "-", "-map", "[go]", "-f", "null", "-"],
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                             stdin=subprocess.DEVNULL, **kw)
                for raw in iter(self.proc.stdout.readline, b""):
                    if self.stop.is_set():
                        break
                    line = raw.decode("utf-8", "replace")
                    kind = "voice" if "RMS_level=" in line else ("game" if "r128.M=" in line else None)
                    if not kind:
                        continue
                    try:
                        v = float(line.split("=", 1)[1])
                    except ValueError:
                        v = -90.0
                    if math.isinf(v) or math.isnan(v):
                        v = -90.0
                    self.on_db(time.time(), max(-90.0, v), kind)
                self.kill()
            except Exception as e:
                if not self.stop.is_set():
                    self.log(f"   ⚠ Live-Ton nicht verfügbar ({str(e)[:80]}) – versuche es gleich nochmal …")
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
        self.game = deque(maxlen=4000)    # (t, LUFS) ~10x pro Sekunde
        self.cooldown, self.warmup = cooldown, warmup
        self.msgs = deque()       # (t, user, text, keyword-hits)
        self.clipreq = deque()    # (t, user)
        self.u_hist = deque(maxlen=600)   # Leute pro 15 s, 1x pro Sekunde
        self.db = deque(maxlen=400)       # (t, dB) pro Sekunde
        self.start = None
        self.last_clip = 0.0
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
            per_sec = {}
            for t, v in game:
                k = int(t)
                per_sec[k] = max(per_sec.get(k, -90.0), v)
            norm_g = self._median(list(per_sec.values()))
            last3 = [per_sec[k] for k in range(int(now) - 3, int(now)) if k in per_sec]
            if len(last3) >= 3 and norm_g > -70:
                action = min(last3) - norm_g
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
            reasons.append(f"🎮 Action im Spiel (+{action:.0f} LU)")
        if not kind:
            return None
        self.last_clip = now
        with self.lock:
            self.clipreq.clear()
        sample = core._top_messages([m[2] for m in recent])
        score = min(100, int(20 * chat_score + 6 * max(0.0, (voice_diff or 0)) + 4 * max(0.0, (action or 0))
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
        if self.st.get("audio", True):
            AudioReader(channel, det.add_db, self.stream_stop, self.log).start()
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
        while stop and not stop.is_set():
            stop.wait(1)
            trig = det.tick(time.time())
            if not trig:
                continue
            if self.count >= max_clips:
                continue
            threading.Thread(target=self._make_clip, args=(trig, uid), daemon=True).start()

    def _make_clip(self, trig, uid):
        try:
            if trig["kind"] in ("voice", "action"):
                time.sleep(6)  # deine Reaktion noch mit drauf
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
