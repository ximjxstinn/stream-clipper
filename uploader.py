# -*- coding: utf-8 -*-
"""
Upload zu YouTube (Data API v3) und TikTok (Content Posting API) – ohne Zusatz-Pakete.
Zugangsdaten + Tokens liegen nur lokal in uploads.json (wird nie hochgeladen).
"""
import base64
import hashlib
import http.client
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv and sys.argv[0] else __file__))
CONF_FILE = os.path.join(APP_DIR, "uploads.json")

DEFAULT_CONF = {
    "yt_client_file": "", "yt_client_id": "", "yt_client_secret": "", "yt_token": {},
    "yt_privacy": "public", "yt_auto": False, "yt_what": "shorts",
    "tt_client_key": "", "tt_client_secret": "", "tt_token": {}, "tt_mode": "draft", "tt_auto": False,
    "title": "{stream} – Highlight #{nr} 🔥",
    "description": "Live auf https://twitch.tv/ximjxstinn 🎮\nFolg mir für mehr Clips!",
    "hashtags": "#fortnite #twitch #gaming #ximjxstinn #clips",
    "auto_max": 3, "approve": True,
}


class UploadError(Exception):
    pass


def load_conf():
    c = dict(DEFAULT_CONF)
    try:
        with open(CONF_FILE, encoding="utf-8") as f:
            c.update(json.load(f))
    except Exception:
        pass
    return c


def save_conf(c):
    with open(CONF_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2, ensure_ascii=False)
    os.replace(CONF_FILE + ".tmp", CONF_FILE)


def build_text(conf, stream_title, nr, reason="", short=False):
    title = conf["title"].replace("{stream}", stream_title or "Stream").replace("{nr}", str(nr))
    tags = conf["hashtags"].strip()
    if short and "#shorts" not in tags.lower():
        tags = (tags + " #Shorts").strip()
    desc = conf["description"].replace("{stream}", stream_title or "").replace("{nr}", str(nr))
    return title.strip(), f"{desc}\n\n{tags}".strip(), tags


# --------------------------------------------------------------------------- #
#  HTTP-Helfer
# --------------------------------------------------------------------------- #
def _req(url, data=None, headers=None, method=None, form=False, timeout=60):
    headers = dict(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers.setdefault("Content-Type", "application/json; charset=UTF-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return (json.loads(raw) if raw else {}), r.headers
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        raise UploadError(f"HTTP {e.code}: {txt[:400]}")


def _put_range(url, path, start, end, total, headers, timeout=300):
    """PUT eines Datei-Bereichs [start, end] (inklusive) – gibt (status, header, body) zurück."""
    u = urllib.parse.urlsplit(url)
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(u.netloc, timeout=timeout)
    target = u.path + ("?" + u.query if u.query else "")
    length = end - start + 1
    h = dict(headers)
    h["Content-Length"] = str(length)
    h["Content-Range"] = f"bytes {start}-{end}/{total}"
    conn.putrequest("PUT", target)
    for k, v in h.items():
        conn.putheader(k, v)
    conn.endheaders()
    with open(path, "rb") as f:
        f.seek(start)
        left = length
        while left > 0:
            chunk = f.read(min(1 << 20, left))
            if not chunk:
                break
            conn.send(chunk)
            left -= len(chunk)
    r = conn.getresponse()
    body = r.read()
    status, hdrs = r.status, r.headers
    conn.close()
    return status, hdrs, body


# --------------------------------------------------------------------------- #
#  Lokaler OAuth-Rückruf (Browser leitet nach 127.0.0.1 zurück)
# --------------------------------------------------------------------------- #
def _oauth_browser(build_url, path="/", timeout=300):
    result = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if "code" in q or "error" in q:
                result.update({k: v[0] for k, v in q.items()})
                ok = "code" in q
                msg = ("✅ Verbunden! Du kannst dieses Fenster schließen und zur App zurück."
                       if ok else "❌ Abgebrochen: " + q.get("error_description", q.get("error", ["?"]))[0])
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"<html><body style='font-family:sans-serif;background:#0e0e10;color:#efeff1;"
                                 f"text-align:center;padding-top:80px'><h2>{msg}</h2></body></html>".encode())
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    redirect = f"http://127.0.0.1:{port}{path}"
    state = secrets.token_urlsafe(16)
    webbrowser.open(build_url(redirect, state))
    srv.timeout = 1
    t0 = time.time()
    while not result and time.time() - t0 < timeout:
        srv.handle_request()
    srv.server_close()
    if not result:
        raise UploadError("Anmeldung nicht abgeschlossen (Zeit abgelaufen).")
    if "error" in result:
        raise UploadError(f"Anmeldung abgelehnt: {result.get('error_description') or result['error']}")
    if result.get("state") != state:
        raise UploadError("Sicherheitsprüfung (state) fehlgeschlagen.")
    return result["code"], redirect


# --------------------------------------------------------------------------- #
#  YouTube
# --------------------------------------------------------------------------- #
YT_SCOPE = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


def yt_load_client_file(conf, path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    inner = data.get("installed") or data.get("web") or {}
    if not inner.get("client_id"):
        raise UploadError("Das ist keine gültige OAuth-Client-Datei (client_secret_….json).")
    if "web" in data:
        raise UploadError("Bitte einen OAuth-Client vom Typ 'Desktop-App' anlegen, nicht 'Webanwendung'.")
    conf["yt_client_file"] = path
    conf["yt_client_id"] = inner["client_id"]
    conf["yt_client_secret"] = inner.get("client_secret", "")
    save_conf(conf)


def yt_connect(conf, log=print):
    if not conf.get("yt_client_id"):
        raise UploadError("Zuerst die client_secret-Datei von Google auswählen.")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    log("🌐 Browser öffnet sich – melde dich mit deinem YouTube-Konto an …")

    def url(redirect, state):
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
            "client_id": conf["yt_client_id"], "redirect_uri": redirect, "response_type": "code",
            "scope": YT_SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})

    code, redirect = _oauth_browser(url)
    tok, _ = _req("https://oauth2.googleapis.com/token", {
        "code": code, "client_id": conf["yt_client_id"], "client_secret": conf["yt_client_secret"],
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier}, form=True)
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    conf["yt_token"] = tok
    save_conf(conf)
    name = yt_channel_name(conf)
    log(f"✅ YouTube verbunden: {name}")
    return name


def _yt_access(conf):
    tok = conf.get("yt_token") or {}
    if not tok.get("refresh_token") and not tok.get("access_token"):
        raise UploadError("YouTube ist nicht verbunden.")
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise UploadError("YouTube-Anmeldung abgelaufen – bitte neu verbinden.")
    try:
        new, _ = _req("https://oauth2.googleapis.com/token", {
            "client_id": conf["yt_client_id"], "client_secret": conf["yt_client_secret"],
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"}, form=True)
    except UploadError as e:
        if "invalid_grant" in str(e):
            raise UploadError("YouTube-Anmeldung abgelaufen – bitte in den Upload-Einstellungen neu verbinden. "
                              "(Tipp: In der Google Cloud die App auf 'In Produktion' stellen, sonst läuft "
                              "die Anmeldung alle 7 Tage ab.)")
        raise
    tok.update(new)
    tok["expires_at"] = time.time() + int(new.get("expires_in", 3600)) - 60
    conf["yt_token"] = tok
    save_conf(conf)
    return tok["access_token"]


def yt_channel_name(conf):
    data, _ = _req("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                   headers={"Authorization": "Bearer " + _yt_access(conf)})
    items = data.get("items") or []
    return items[0]["snippet"]["title"] if items else "(kein Kanal)"


def yt_upload(conf, path, title, description, tags, log=print, cancel=None):
    token = _yt_access(conf)
    size = os.path.getsize(path)
    meta = {
        "snippet": {"title": title[:100], "description": description[:4900], "categoryId": "20",
                    "tags": [t.lstrip("#") for t in tags.split() if t.startswith("#")][:15]},
        "status": {"privacyStatus": conf.get("yt_privacy", "public"), "selfDeclaredMadeForKids": False},
    }
    _, hdrs = _req("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
                   meta, headers={"Authorization": "Bearer " + token, "X-Upload-Content-Length": str(size),
                                  "X-Upload-Content-Type": "video/mp4"}, method="POST")
    session = hdrs.get("Location")
    if not session:
        raise UploadError("YouTube hat keine Upload-Adresse geliefert.")
    chunk = 8 * 1024 * 1024
    pos, last = 0, -25
    while pos < size:
        if cancel and cancel():
            raise UploadError("Abgebrochen")
        end = min(size, pos + chunk) - 1
        status, h, body = _put_range(session, path, pos, end, size, {"Content-Type": "video/mp4"})
        if status in (200, 201):
            vid = json.loads(body).get("id")
            locked = json.loads(body).get("status", {}).get("privacyStatus")
            return vid, locked
        if status == 308:
            rng = h.get("Range")
            pos = int(rng.split("-")[1]) + 1 if rng else 0
            pct = int(pos * 100 / size)
            if pct >= last + 25:
                last = pct
                log(f"      YouTube: {pct}%")
            continue
        raise UploadError(f"YouTube-Upload fehlgeschlagen (HTTP {status}): {body[:300].decode('utf-8', 'replace')}")
    raise UploadError("YouTube-Upload unvollständig")


# --------------------------------------------------------------------------- #
#  TikTok
# --------------------------------------------------------------------------- #
TT_SCOPE = "user.info.basic,video.upload,video.publish"


def tt_connect(conf, log=print):
    if not conf.get("tt_client_key") or not conf.get("tt_client_secret"):
        raise UploadError("Zuerst Client Key und Client Secret deiner TikTok-App eintragen.")
    verifier = secrets.token_urlsafe(48)
    challenge = hashlib.sha256(verifier.encode()).hexdigest()  # TikTok will HEX, nicht base64
    log("🌐 Browser öffnet sich – melde dich mit deinem TikTok-Konto an …")

    def url(redirect, state):
        return "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
            "client_key": conf["tt_client_key"], "scope": TT_SCOPE, "response_type": "code",
            "redirect_uri": redirect, "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256"})

    code, redirect = _oauth_browser(url, path="/callback/")
    tok, _ = _req("https://open.tiktokapis.com/v2/oauth/token/", {
        "client_key": conf["tt_client_key"], "client_secret": conf["tt_client_secret"], "code": code,
        "grant_type": "authorization_code", "redirect_uri": redirect, "code_verifier": verifier}, form=True)
    if "access_token" not in tok:
        raise UploadError(f"TikTok-Anmeldung fehlgeschlagen: {tok}")
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 86400)) - 120
    conf["tt_token"] = tok
    save_conf(conf)
    name = tt_creator(conf).get("creator_nickname") or "verbunden"
    log(f"✅ TikTok verbunden: {name}")
    return name


def _tt_access(conf):
    tok = conf.get("tt_token") or {}
    if not tok.get("access_token"):
        raise UploadError("TikTok ist nicht verbunden.")
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    new, _ = _req("https://open.tiktokapis.com/v2/oauth/token/", {
        "client_key": conf["tt_client_key"], "client_secret": conf["tt_client_secret"],
        "grant_type": "refresh_token", "refresh_token": tok.get("refresh_token", "")}, form=True)
    if "access_token" not in new:
        raise UploadError("TikTok-Anmeldung abgelaufen – bitte neu verbinden.")
    tok.update(new)
    tok["expires_at"] = time.time() + int(new.get("expires_in", 86400)) - 120
    conf["tt_token"] = tok
    save_conf(conf)
    return tok["access_token"]


def _tt_api(conf, endpoint, body):
    data, _ = _req("https://open.tiktokapis.com" + endpoint, body,
                   headers={"Authorization": "Bearer " + _tt_access(conf)})
    err = data.get("error") or {}
    if err.get("code") not in (None, "", "ok"):
        hint = {
            "unaudited_client_can_only_post_to_private_accounts":
                " → Deine TikTok-App ist noch nicht geprüft. Stell in den Upload-Einstellungen auf "
                "'Als Entwurf in die TikTok-App' um.",
            "spam_risk_too_many_posts": " → TikTok-Tageslimit erreicht, morgen geht's weiter.",
            "scope_not_authorized": " → Bitte TikTok in den Upload-Einstellungen neu verbinden.",
        }.get(err.get("code"), "")
        raise UploadError(f"TikTok: {err.get('code')} – {err.get('message', '')}{hint}")
    return data.get("data") or {}


def tt_creator(conf):
    try:
        return _tt_api(conf, "/v2/post/publish/creator_info/query/", {})
    except UploadError:
        return {}


def _tt_chunks(size):
    """TikTok-Regeln: Stücke 5–64 MB, letztes Stück nimmt den Rest auf; unter 5 MB = ein Stück."""
    if size <= 64 * 1024 * 1024:
        return size, 1
    chunk = 10 * 1024 * 1024
    return chunk, size // chunk


def tt_upload(conf, path, caption, log=print, cancel=None):
    size = os.path.getsize(path)
    chunk, count = _tt_chunks(size)
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
    if conf.get("tt_mode") == "direct":
        info = tt_creator(conf)
        opts = info.get("privacy_level_options") or ["SELF_ONLY"]
        privacy = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in opts else opts[0]
        data = _tt_api(conf, "/v2/post/publish/video/init/", {
            "post_info": {"title": caption[:2200], "privacy_level": privacy, "disable_duet": False,
                          "disable_comment": False, "disable_stitch": False, "video_cover_timestamp_ms": 1000},
            "source_info": source})
    else:
        data = _tt_api(conf, "/v2/post/publish/inbox/video/init/", {"source_info": source})
    url, pid = data.get("upload_url"), data.get("publish_id")
    if not url:
        raise UploadError("TikTok hat keine Upload-Adresse geliefert.")
    for k in range(count):
        if cancel and cancel():
            raise UploadError("Abgebrochen")
        start = k * chunk
        end = size - 1 if k == count - 1 else start + chunk - 1
        status, _, body = _put_range(url, path, start, end, size, {"Content-Type": "video/mp4"})
        if status not in (200, 201, 206):
            raise UploadError(f"TikTok-Upload fehlgeschlagen (HTTP {status}): {body[:300].decode('utf-8', 'replace')}")
        if count > 1:
            log(f"      TikTok: {int((end + 1) * 100 / size)}%")
    # Status abfragen, bis TikTok fertig verarbeitet hat
    for _ in range(40):
        time.sleep(3)
        st = _tt_api(conf, "/v2/post/publish/status/fetch/", {"publish_id": pid})
        s = st.get("status", "")
        if s in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            return pid, s
        if s == "FAILED":
            raise UploadError(f"TikTok hat das Video abgelehnt: {st.get('fail_reason', '?')}")
    return pid, "PROCESSING"
