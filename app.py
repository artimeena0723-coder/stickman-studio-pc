"""Stickman Studio PC  -  sab kuch automatic: AI Studio (prompts + voice) -> Flow (images) -> ffmpeg (video).
Run:  python app.py   then open http://127.0.0.1:7861
"""
import hashlib, json, mimetypes, os, queue, re, subprocess, sys, threading, time, traceback, urllib.parse, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pipeline as pl
import automation as au

VERSION = "PC v1.0"
from paths import BASE as ROOT, RES
CFG = ROOT / "data" / "config.json"
PORT = int(os.environ.get("STICKMAN_PORT", "7861"))
(ROOT / "data").mkdir(exist_ok=True)

LOG = []
STATE = {"job": None, "progress": 0, "stop": False, "project": None}
JOBS = queue.Queue()
LOCK = threading.Lock()


def log(msg):
    line = time.strftime("%H:%M:%S ") + str(msg)
    try:
        print(line, flush=True)
    except Exception:
        try:
            print(line.encode("ascii", "replace").decode(), flush=True)
        except Exception:
            pass
    with LOCK:
        LOG.append(line)
        del LOG[:-1500]
    p = STATE.get("project")
    if p:
        try:
            with open(pl.PROJECTS / p / "log.txt", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def cfg():
    try:
        return json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:
        return {"accounts": [], "last_project": "My_video"}


def save_cfg(c):
    CFG.write_text(json.dumps(c, indent=1, ensure_ascii=False), encoding="utf-8")


DEFAULTS = {"lang": "hi", "preset": "survival", "min_s": 6, "max_s": 8, "aspect": "16:9", "res": "1080", "fps": 30,
            "voice": "Charon", "style": "", "voice_mode": "aistudio", "motion": "pan", "transition": "cut",
            "edge_crop": 5, "music_vol": 12, "attach_ref": True, "notes": "", "style_extra": ""}


def get_project(name=None):
    name = name or cfg().get("last_project") or "My_video"
    p = pl.Project(name)
    for k, v in DEFAULTS.items():
        p.data["settings"].setdefault(k, v)
    return p


# ---------------------------------------------------------------- worker (all browser work runs on this one thread)
class Worker(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__()
        self.br = None

    def browser(self):
        if self.br is None:
            self.br = au.Browser(log, lambda: STATE["stop"])
        return self.br

    def run(self):
        while True:
            name, args = JOBS.get()
            STATE["job"], STATE["stop"], STATE["progress"] = name, False, 0
            try:
                getattr(self, "job_" + name)(*args)
            except au.StopJob:
                log("⏸ Ruk gaya (Stop). 'Sab automatic' dobara dabane par jahan ruka wahin se chalega.")
            except Exception as e:
                log(f"❌ {e}")
                (ROOT / "data" / "last_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            finally:
                STATE["job"] = None
                # do not keep stale browser connections between jobs
                if self.br:
                    try:
                        self.br.close()
                    except Exception:
                        pass
                    self.br = None

    # -- accounts
    def accounts(self):
        acc = cfg().get("accounts", [])
        if not acc:
            raise RuntimeError("Pehle '➕ Google account add karo' dabao aur us Chrome window me sign in karo.")
        return acc

    def job_check_accounts(self):
        br = self.browser()
        c = cfg()
        for a in c.get("accounts", []):
            ok, url = au.is_signed_in(br, a["id"])
            a["ok"] = ok
            log(f"{'✅' if ok else '❌'} {a['name']}: {'signed in' if ok else 'sign in nahi hai — us window me Google sign in karo'}")
        save_cfg(c)

    def with_accounts(self, what, fn):
        """Run fn(account_id) starting from the current account; rotate on daily limit."""
        acc = self.accounts()
        c = cfg()
        start = c.get(f"cur_{what}", 0) % len(acc)
        last = None
        for k in range(len(acc)):
            idx = (start + k) % len(acc)
            a = acc[idx]
            try:
                r = fn(a["id"])
                if idx != start:
                    c = cfg(); c[f"cur_{what}"] = idx; save_cfg(c)
                return r
            except au.AccountLimit as e:
                last = e
                log(f"⚠️ {a['name']}: {e} → agla Google account")
                continue
        raise RuntimeError(f"Saare Google accounts ki limit khatam ({last}). Kal dobara chalao ya naya account add karo.")

    # -- steps
    def job_all(self, name):
        p = get_project(name)
        STATE["project"] = p.name
        log(f"🚀 {p.name}: sab automatic shuru ({len(p.scenes)} scenes)")
        if not p.scenes:
            raise RuntimeError("Pehle script daal kar '✂️ Scenes banao' dabao")
        self.job_prompts(name)
        self.job_voice(name)
        self.job_images(name)
        self.job_render(name)

    def job_prompts(self, name, force=False):
        p = get_project(name)
        STATE["project"] = p.name
        todo = [i for i, s in enumerate(p.scenes) if force or not (s.get("prompt") or "").strip()]
        if not todo:
            log("✍️ Saare scene prompts pehle se hain")
            return
        log(f"✍️ AI Studio se {len(todo)} scene prompts bana raha hoon...")
        br = self.browser()
        for b in range(0, len(todo), 35):
            batch = todo[b:b + 35]
            req = pl.prompt_request(p, batch)
            try:
                txt = self.with_accounts("chat", lambda acc: au.aistudio_ask(br, acc, req, log))
            except au.StopJob:
                raise
            except Exception as e:
                log(f"⚠️ Prompts nahi bane ({str(e)[:150]}). Narration se image banegi.")
                return
            arr = au.extract_json_block(txt) or []
            got = 0
            for it in arr:
                try:
                    n = int(it.get("n")) - 1
                    if n in batch and it.get("prompt"):
                        p.scenes[n]["prompt"] = it["prompt"].strip(); got += 1
                except Exception:
                    pass
            m = re.findall(r"CHARACTER:\s*(.+?)\s*\|\s*WORLD:\s*(.+)", txt)
            if m:
                ch, wo = m[-1]
                if "<" not in ch:
                    p.data["sheet"] = {"character": ch.strip()[:600], "world": wo.split("\n")[0].strip()[:600]}
            p.save()
            log(f"   ✅ {got}/{len(batch)} prompts mile")
            STATE["progress"] = (b + len(batch)) / len(todo)

    def _voice_sig(self, p):
        st = p.data["settings"]
        h = hashlib.md5(("|".join(s["text"] for s in p.scenes) + st.get("voice", "") + st.get("style", "")).encode()).hexdigest()
        return h

    def job_voice(self, name):
        p = get_project(name)
        STATE["project"] = p.name
        st = p.data["settings"]
        if st.get("voice_mode") == "upload":
            if not (p.dir / "voice" / "voice_full.wav").exists():
                raise RuntimeError("Apni voice file upload karo")
            log("🎤 Aapki upload ki hui voice use hogi")
            return
        sig = self._voice_sig(p)
        if p.data.get("voice_sig") != sig or not p.data.get("voice_chunks"):
            for f in (p.dir / "voice").glob("chunk_*"):
                f.unlink()
            p.data["voice_chunks"] = pl.voice_chunks(p.scenes)
            p.data["voice_sig"] = sig
            p.save()
        chunks = p.data["voice_chunks"]
        style = st.get("style") or pl.PRESETS.get(st.get("preset"), pl.PRESETS["custom"])["voice"]
        br = self.browser()
        for ci, idxs in enumerate(chunks):
            wav = p.dir / "voice" / f"chunk_{ci + 1:03d}.wav"
            if wav.exists():
                continue
            text = " ".join(p.scenes[i]["text"] for i in idxs)
            log(f"🎤 Voice part {ci + 1}/{len(chunks)} (AI Studio)...")
            raw = p.dir / "voice" / f"chunk_{ci + 1:03d}.raw"
            for attempt in range(3):
                try:
                    self.with_accounts("tts", lambda acc: au.aistudio_tts(br, acc, text, st.get("voice"), style, raw, log))
                    break
                except (au.StopJob, RuntimeError) as e:
                    if isinstance(e, au.StopJob) or "Saare Google" in str(e) or attempt == 2:
                        raise
                    log(f"   retry ({str(e)[:120]})")
                    br.sleep(5)
            pl.to_wav(raw, wav)
            raw.unlink(missing_ok=True)
            log(f"   ✅ {pl.duration(wav):.1f}s")
            STATE["progress"] = (ci + 1) / len(chunks)
        log("🎤 Poori voice ready")

    def job_images(self, name, only=None):
        p = get_project(name)
        STATE["project"] = p.name
        st = p.data["settings"]
        self.sync_inbox(p)
        todo = [i for i in range(len(p.scenes)) if (only is None and not p.img_path(i)) or (only is not None and i in only)]
        if not todo:
            log("🖼️ Saari images pehle se hain")
            return
        log(f"🖼️ Flow se {len(todo)} images bana raha hoon...")
        br = self.browser()
        ref = str(p.ref_path()) if p.ref_path() else None
        sess = {}

        def one(acc, i, prompt):
            s = sess.get(acc)
            if s is None:
                s = au.FlowSession(br, acc, log, st.get("aspect"), ref, st.get("attach_ref", True), str(p.dir / "inbox")).open()
                sess[acc] = s
            tmp = p.dir / "images" / f"tmp_{i + 1:03d}"
            s.generate(prompt, tmp)
            return tmp

        done = 0
        for k, i in enumerate(todo):
            prompt = pl.full_prompt(p, i)
            log(f"🖼️ Scene {i + 1} ({k + 1}/{len(todo)})")
            ok = False
            for attempt in range(3):
                try:
                    tmp = self.with_accounts("flow", lambda acc: one(acc, i, prompt))
                    pl.save_scene_image(p, i, Path(tmp).read_bytes())
                    Path(tmp).unlink(missing_ok=True)
                    ok = True
                    break
                except au.StopJob:
                    raise
                except ValueError as e:  # policy -> soften prompt once
                    log(f"   ⚠️ Flow ne mana kiya ({e}) → prompt narm karke dobara")
                    prompt = prompt.replace("SCENE:", "SCENE (family-friendly, non-graphic, cartoon):")
                except RuntimeError as e:
                    if "Saare Google" in str(e):
                        raise
                    log(f"   retry {attempt + 1}: {str(e)[:140]}")
                    sess.clear()
                    br.sleep(4)
            if not ok:
                p.scenes[i]["err"] = "Flow fail"
                log(f"   ❌ Scene {i + 1} skip (baad me '🔁' se dobara bana sakte ho)")
            else:
                done += 1
            p.save()
            STATE["progress"] = (k + 1) / len(todo)
        log(f"🖼️ {done}/{len(todo)} images bani")
        left = [i + 1 for i in range(len(p.scenes)) if not p.img_path(i)]
        if left:
            raise RuntimeError(f"Ye scenes abhi bache hain: {left[:40]} — 'Sab automatic' dobara dabao ya images import karo")

    def job_image_one(self, name, n):
        self.job_images(name, only=[n - 1])

    def job_render(self, name):
        p = get_project(name)
        STATE["project"] = p.name
        self.sync_inbox(p)
        log("🎞️ Video render shuru (PC par seedha file, koi storage limit nahi)...")
        out = pl.render(p, log, stop=lambda: STATE["stop"], progress=lambda f: STATE.__setitem__("progress", f))
        log(f"✅ VIDEO READY: {out}")
        log(f"   Length {pl.duration(out):.0f}s. '📂 Folder kholo' dabao.")

    def job_import(self, name, paths):
        p = get_project(name)
        STATE["project"] = p.name
        pl.import_images(p, paths, log)
        p.save()

    def sync_inbox(self, p):
        inbox = p.dir / "inbox"
        files = [f for f in inbox.iterdir() if f.is_file() and not f.name.endswith(".crdownload")]
        if files:
            pl.import_images(p, files, log)
            done = inbox / "_imported"
            done.mkdir(exist_ok=True)
            for f in files:
                try:
                    os.replace(f, done / f.name)
                except Exception:
                    pass
            p.save()


WORKER = Worker()


def enqueue(name, *args):
    if STATE["job"]:
        raise RuntimeError(f"Abhi '{STATE['job']}' chal raha hai. Pehle khatam hone do ya ⏹ Stop dabao.")
    STATE["job"] = name
    JOBS.put((name, args))


# ---------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def q(self):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        q = self.q()
        try:
            if path == "/":
                return self.send(200, (RES / "ui.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/state":
                p = get_project(q.get("project"))
                since = int(q.get("since", 0))
                with LOCK:
                    lg = LOG[since:] if since <= len(LOG) else LOG[:]
                    n = len(LOG)
                final = p.dir / f"{p.name}_final.mp4"
                return self.send(200, {
                    "version": VERSION, "project": p.name, "settings": p.data["settings"], "scenes": p.status(),
                    "script": p.data.get("script", ""), "accounts": cfg().get("accounts", []),
                    "projects": sorted(d.name for d in pl.PROJECTS.iterdir() if d.is_dir()) if pl.PROJECTS.exists() else [],
                    "job": STATE["job"], "progress": STATE["progress"], "log": lg, "log_n": n,
                    "has_ref": bool(p.ref_path()), "has_music": bool(p.asset("music")),
                    "has_voice_upload": (p.dir / "voice" / "voice_full.wav").exists(),
                    "final": final.exists(), "final_path": str(final), "chrome": au.find_chrome() or "",
                    "voices": pl.VOICES, "sheet": p.data.get("sheet", {}),
                })
            if path == "/api/img":
                p = get_project(q.get("project"))
                n = int(q.get("n", 1))
                f = p.ref_path() if n == 0 else p.img_path(n - 1)
                if not f:
                    return self.send(404, {"error": "none"})
                return self.send(200, f.read_bytes(), mimetypes.guess_type(str(f))[0] or "image/jpeg")
            if path == "/api/video":
                p = get_project(q.get("project"))
                f = p.dir / f"{p.name}_final.mp4"
                size = f.stat().st_size
                rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "") or "")
                a_, b_ = 0, size - 1
                if rng:
                    a_ = int(rng.group(1) or 0); b_ = min(size - 1, int(rng.group(2) or size - 1))
                self.send_response(206 if rng else 200)
                self.send_header("Content-Type", "video/mp4"); self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(b_ - a_ + 1))
                if rng:
                    self.send_header("Content-Range", f"bytes {a_}-{b_}/{size}")
                self.end_headers()
                with open(f, "rb") as fh:
                    fh.seek(a_); left = b_ - a_ + 1
                    while left > 0:
                        chunk = fh.read(min(1 << 20, left))
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                        except Exception:
                            break
                        left -= len(chunk)
                return
            if path == "/api/log":
                with LOCK:
                    return self.send(200, "\n".join(LOG), "text/plain; charset=utf-8")
            return self.send(404, {"error": "not found"})
        except Exception as e:
            return self.send(500, {"error": str(e)})

    def body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        q = self.q()
        try:
            if path == "/api/upload":
                p = get_project(q.get("project"))
                kind = q.get("kind")
                fname = urllib.parse.unquote(self.headers.get("X-Filename", "file"))
                data = self.body()
                if kind == "character":
                    for old in p.dir.joinpath("assets").glob("character.*"):
                        old.unlink()
                    ext = os.path.splitext(fname)[1].lower() or ".png"
                    (p.dir / "assets" / f"character{ext}").write_bytes(data)
                    log("🧍 Character reference image save hui")
                elif kind == "music":
                    for old in p.dir.joinpath("assets").glob("music.*"):
                        old.unlink()
                    (p.dir / "assets" / ("music" + (os.path.splitext(fname)[1] or ".mp3"))).write_bytes(data)
                    log("🎵 Background music save hua")
                elif kind == "voice":
                    raw = p.dir / "voice" / ("upload" + (os.path.splitext(fname)[1] or ".wav"))
                    raw.write_bytes(data)
                    pl.to_wav(raw, p.dir / "voice" / "voice_full.wav", trim=False)
                    p.data["settings"]["voice_mode"] = "upload"; p.save()
                    log(f"🎤 Aapki voice save hui ({pl.duration(p.dir / 'voice' / 'voice_full.wav'):.0f}s)")
                elif kind == "images":
                    up = p.dir / "inbox" / os.path.basename(fname)
                    up.write_bytes(data)
                    if q.get("last") == "1":
                        WORKER.sync_inbox(p)
                return self.send(200, {"ok": True})
            d = json.loads(self.body() or b"{}")
            name = d.get("project")
            if path == "/api/project":
                c = cfg(); c["last_project"] = pl.safe(name); save_cfg(c)
                get_project(name).save()
                return self.send(200, {"ok": True, "project": pl.safe(name)})
            if path == "/api/settings":
                p = get_project(name)
                p.data["settings"].update(d.get("settings", {}))
                if "script" in d:
                    p.data["script"] = d["script"]
                p.save()
                return self.send(200, {"ok": True})
            if path == "/api/split":
                p = get_project(name)
                st = p.data["settings"]
                p.data["script"] = d.get("script", "")
                sc = pl.split_script(p.data["script"], st.get("lang", "hi"), float(st.get("min_s", 6)), float(st.get("max_s", 8)))
                if not sc:
                    return self.send(400, {"error": "Script khali hai"})
                if p.scenes and not d.get("confirm"):
                    pass
                for f in (p.dir / "images").glob("scene_*"):
                    f.unlink()
                for f in (p.dir / "voice").glob("chunk_*"):
                    f.unlink()
                p.data["scenes"] = sc
                p.data["voice_chunks"] = []
                p.data["voice_sig"] = ""
                p.save()
                log(f"✂️ {len(sc)} scenes bane")
                return self.send(200, {"ok": True, "n": len(sc)})
            if path == "/api/scene":
                p = get_project(name)
                i = int(d["n"]) - 1
                for k in ("text", "prompt"):
                    if k in d:
                        p.scenes[i][k] = d[k]
                p.save()
                return self.send(200, {"ok": True})
            if path == "/api/job":
                step = d.get("step")
                c = cfg(); c["last_project"] = pl.safe(name); save_cfg(c)
                if step == "image_one":
                    enqueue("image_one", name, int(d["n"]))
                elif step == "prompts_force":
                    enqueue("prompts", name, True)
                elif step == "import_folder":
                    enqueue("import", name, [d["path"]])
                elif step == "check_accounts":
                    enqueue("check_accounts")
                else:
                    enqueue(step, name)
                return self.send(200, {"ok": True})
            if path == "/api/stop":
                STATE["stop"] = True
                log("⏹ Stop request...")
                return self.send(200, {"ok": True})
            if path == "/api/account/add":
                c = cfg()
                acc = c.setdefault("accounts", [])
                nm = (d.get("name") or f"Account {len(acc) + 1}").strip()[:40]
                aid = "acc_" + hashlib.md5((nm + str(time.time())).encode()).hexdigest()[:8]
                acc.append({"id": aid, "name": nm, "ok": None})
                save_cfg(c)
                threading.Thread(target=lambda: au.Browser(log, lambda: False).launch(aid, "https://accounts.google.com/"), daemon=True).start()
                log(f"➕ '{nm}' ke liye Chrome window khul rahi hai — usme Google sign in karo, phir window khuli rehne do ya band kar do.")
                return self.send(200, {"ok": True})
            if path == "/api/account/open":
                aid = d.get("id")
                url = d.get("url") or "https://accounts.google.com/"
                threading.Thread(target=lambda: au.Browser(log, lambda: False).launch(aid, url), daemon=True).start()
                return self.send(200, {"ok": True})
            if path == "/api/account/remove":
                c = cfg()
                c["accounts"] = [a for a in c.get("accounts", []) if a["id"] != d.get("id")]
                save_cfg(c)
                return self.send(200, {"ok": True})
            if path == "/api/open":
                p = get_project(name)
                target = p.dir if not d.get("debug") else au.DEBUG
                target.mkdir(parents=True, exist_ok=True)
                if sys.platform.startswith("win"):
                    os.startfile(str(target))
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(target)])
                else:
                    subprocess.Popen(["xdg-open", str(target)])
                return self.send(200, {"ok": True})
            return self.send(404, {"error": "not found"})
        except Exception as e:
            return self.send(400, {"error": str(e)})


def open_window(url):
    """Open the UI as its own app window (Chrome/Edge --app mode). When the window closes, the app quits."""
    chrome = au.find_chrome()
    if not chrome or os.environ.get("STICKMAN_NO_BROWSER"):
        if not os.environ.get("STICKMAN_NO_BROWSER"):
            webbrowser.open(url)
        return None
    prof = ROOT / "data" / "ui_window"
    prof.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([chrome, f"--app={url}", f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check",
                             "--window-size=1450,950"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def already_running(url):
    try:
        with au._LOCAL.open(url + "/api/state", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    url = f"http://127.0.0.1:{PORT}"
    if already_running(url):          # second double-click: just show the window again
        open_window(url)
        return
    try:
        sys.stdout = open(ROOT / "data" / "app.log", "a", encoding="utf-8", buffering=1) if sys.stdout is None else sys.stdout
    except Exception:
        pass
    WORKER.start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    log(f"Stickman Studio {VERSION} start: {url} · Chrome: {au.find_chrome() or 'NAHI MILA'}")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.5)
    win = open_window(url)
    if win is None:
        while True:
            time.sleep(3600)
    # keep running while the app window is open (and while a job is still running)
    while True:
        time.sleep(2)
        if win.poll() is not None:
            if STATE["job"]:
                continue
            time.sleep(3)
            if win.poll() is not None and not STATE["job"]:
                os._exit(0)


if __name__ == "__main__":
    main()
