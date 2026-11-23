# -*- coding: utf-8 -*-
"""Update-Loader: holt neue Versionen automatisch aus github.com/ximjxstinn/stream-clipper."""
import json
import os
import shutil
import sys
import time
import urllib.request

REPO_RAW = "https://raw.githubusercontent.com/ximjxstinn/stream-clipper/main/"
APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv and sys.argv[0] else __file__))
SAFE_NAMES = {"StreamClipper.pyw", "clipper_core.py", "updater.py", "uploader.py", "live_clipper.py", "start.bat", "ANLEITUNG.txt",
              "UPLOAD-EINRICHTEN.txt", "icon.ico"}


def _get(url, timeout=20):
    url += ("&" if "?" in url else "?") + f"nocache={int(time.time())}"
    req = urllib.request.Request(url, headers={"User-Agent": "xImJxstinn-StreamClipper", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _ver(v):
    out = []
    for part in str(v).strip().lstrip("v").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out + [0] * (3 - len(out)))


API_SHA = "https://api.github.com/repos/ximjxstinn/stream-clipper/commits/main"


def _latest_base():
    """Raw-Adresse des neuesten Commits – nie veraltet (raw/main wird von GitHub ein paar Minuten gecacht)."""
    try:
        req = urllib.request.Request(API_SHA, headers={"User-Agent": "xImJxstinn-StreamClipper",
                                                       "Accept": "application/vnd.github.sha"})
        with urllib.request.urlopen(req, timeout=15) as r:
            sha = r.read().decode().strip()
        if len(sha) == 40:
            return REPO_RAW.replace("/main/", f"/{sha}/")
    except Exception:
        pass
    return REPO_RAW


def check(current):
    """Gibt das Update-Info-Dict zurück, wenn es eine neuere Version gibt, sonst None."""
    base = _latest_base()
    info = json.loads(_get(base + "version.json").decode("utf-8"))
    info["_base"] = base
    if _ver(info.get("version", "0")) > _ver(current):
        return info
    return None


def install(info, log=print):
    """Lädt alle Dateien, prüft sie und ersetzt dann erst die alten (mit Backup)."""
    files = [f for f in info.get("files", []) if f in SAFE_NAMES]
    if not files:
        raise RuntimeError("Update enthält keine Dateien")
    tmp = os.path.join(APP_DIR, "_update")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for f in files:
        log(f"   ⬇ {f}")
        data = _get(info.get("_base", REPO_RAW) + f, timeout=60)
        if not data:
            raise RuntimeError(f"{f} ist leer")
        if f.endswith((".py", ".pyw")):
            compile(data.decode("utf-8"), f, "exec")  # kaputte Datei? Dann gar nicht erst installieren
        with open(os.path.join(tmp, f), "wb") as out:
            out.write(data)
    backup = os.path.join(APP_DIR, "backup")
    os.makedirs(backup, exist_ok=True)
    for f in files:
        old = os.path.join(APP_DIR, f)
        if os.path.isfile(old):
            shutil.copy2(old, os.path.join(backup, f))
    for f in files:
        os.replace(os.path.join(tmp, f), os.path.join(APP_DIR, f))
    shutil.rmtree(tmp, ignore_errors=True)
    return info.get("version")


def restart():
    import subprocess
    script = os.path.join(APP_DIR, "StreamClipper.pyw")
    args = [sys.executable, script] + [a for a in sys.argv[1:] if a == "--minimized"]
    kw = {"creationflags": 0x00000008} if os.name == "nt" else {}  # DETACHED_PROCESS
    subprocess.Popen(args, cwd=APP_DIR, **kw)
