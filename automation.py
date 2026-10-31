"""Browser automation for Stickman Studio PC.

Drives the user's OWN Chrome (a separate profile per Google account) over the
Chrome DevTools Protocol.  The user signs in to Google himself in that window;
no password is ever stored by this app.
"""
import base64, json, os, re, shutil, socket, subprocess, sys, time, urllib.request

os.environ["NO_PROXY"] = ",".join(x for x in [os.environ.get("NO_PROXY", ""), "127.0.0.1,localhost"] if x)
os.environ["no_proxy"] = os.environ["NO_PROXY"]
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never send localhost through a proxy
from pathlib import Path

from paths import BASE as ROOT
PROFILES = ROOT / "data" / "profiles"
DEBUG = ROOT / "data" / "debug"
SEL_FILE = ROOT / "selectors.json"

FLOW_URL = os.environ.get("STICKMAN_FLOW_URL", "https://labs.google/fx/tools/flow")
TTS_URL = os.environ.get("STICKMAN_TTS_URL", "https://aistudio.google.com/generate-speech")
CHAT_URL = os.environ.get("STICKMAN_CHAT_URL", "https://aistudio.google.com/prompts/new_chat")

DEFAULT_SEL = {
    "flow_new_project": ["button:has-text('New project')", "text=New project", "button:has-text('Naya')"],
    "flow_prompt": ["textarea", "[contenteditable='true']", "[role='textbox']"],
    "flow_submit": ["button[aria-label*='Create' i]", "button[aria-label*='Generate' i]", "button[aria-label*='Send' i]",
                    "button:has(i:text-is('arrow_forward'))", "button:has-text('arrow_forward')"],
    "flow_mode_toggle": ["button:has-text('Text to Video')", "[role='combobox']:has-text('Video')", "button:has-text('Video')"],
    "flow_mode_image": ["[role='option']:has-text('Image')", "li:has-text('Create Image')", "text=Create Image", "text=Image"],
    "flow_add_image": ["button[aria-label*='Add' i]", "button:has(i:text-is('add'))", "button:has-text('add')"],
    "tts_text": ["textarea[placeholder*='speech' i]", "textarea[placeholder*='text' i]", "textarea"],
    "tts_style": ["textarea[placeholder*='style' i]", "textarea[placeholder*='instruction' i]", "input[placeholder*='style' i]"],
    "tts_run": ["button:has-text('Run')", "button[aria-label*='Run' i]"],
    "chat_input": ["textarea[aria-label*='prompt' i]", "textarea[placeholder*='prompt' i]", "textarea"],
    "chat_run": ["button:has-text('Run')", "button[aria-label*='Run' i]"],
    "error_words": ["limit", "quota", "try again later", "too many", "unusual activity", "credits", "something went wrong"],
    "policy_words": ["policy", "violat", "couldn't generate", "could not generate", "unable to generate", "safety"],
}


def selectors():
    s = dict(DEFAULT_SEL)
    try:
        if SEL_FILE.exists():
            s.update(json.loads(SEL_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return s


class StopJob(Exception):
    pass


class AccountLimit(Exception):
    """This Google account hit its daily limit -> try the next account."""


# ---------------------------------------------------------------- Chrome
def find_chrome():
    env = os.environ
    cands = []
    if sys.platform.startswith("win"):
        for base in [env.get("PROGRAMFILES", r"C:\Program Files"), env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), env.get("LOCALAPPDATA", "")]:
            if base:
                cands.append(os.path.join(base, r"Google\Chrome\Application\chrome.exe"))
        for base in [env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), env.get("PROGRAMFILES", r"C:\Program Files")]:
            cands.append(os.path.join(base, r"Microsoft\Edge\Application\msedge.exe"))
    elif sys.platform == "darwin":
        cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        for n in ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"]:
            p = shutil.which(n)
            if p:
                cands.append(p)
    extra = env.get("STICKMAN_CHROME")
    if extra:
        cands.insert(0, extra)
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def _port_open(port):
    try:
        with _LOCAL.open(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _free_port(start):
    p = start
    while p < start + 200:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
        p += 1
    return start


class Browser:
    """One Chrome window per Google account, reused across steps."""

    def __init__(self, log, stop_flag):
        self.log = log
        self.stop_flag = stop_flag
        self.pw = None
        self.conns = {}   # account -> (browser, context, port)
        self.ports = {}

    # -- lifecycle
    def _pw(self):
        if self.pw is None:
            from playwright.sync_api import sync_playwright
            self.pw = sync_playwright().start()
        return self.pw

    def launch(self, account, url="about:blank", headless=False):
        chrome = find_chrome()
        if not chrome:
            raise RuntimeError("Google Chrome nahi mila. https://www.google.com/chrome se install karo (ya Edge).")
        prof = PROFILES / account
        prof.mkdir(parents=True, exist_ok=True)
        port = self.ports.get(account)
        if not port or not _port_open(port):
            port = None
            for f in (prof / "stickman_port.txt", prof / "DevToolsActivePort"):  # Chrome also writes its debug port here
                if f.exists() and not port:
                    try:
                        pp = int(f.read_text().split()[0])
                        if _port_open(pp):
                            port = pp
                    except Exception:
                        pass
        if port:
            if url and url != "about:blank":
                try:
                    _LOCAL.open(urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{url}", method="PUT"), timeout=5)
                except Exception:
                    pass
        else:
            import hashlib
            port = 9301 + int(hashlib.md5(account.encode()).hexdigest(), 16) % 600
            if _port_open(port):
                port = _free_port(9950)
            args = [chrome, f"--user-data-dir={prof}", f"--remote-debugging-port={port}", "--no-first-run",
                    "--no-default-browser-check", "--disable-features=Translate", "--window-size=1300,900"]
            if headless or os.environ.get("STICKMAN_HEADLESS"):
                args += ["--headless=new"]
            if os.environ.get("STICKMAN_NO_SANDBOX"):
                args += ["--no-sandbox"]
            args.append(url)
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (prof / "stickman_port.txt").write_text(str(port))
            for _ in range(60):
                if _port_open(port):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("Chrome window start nahi hui. Is account ki purani Chrome window band karke dobara try karo.")
        self.ports[account] = port
        return port

    def context(self, account):
        c = self.conns.get(account)
        if c:
            try:
                c[1].pages  # still alive?
                if c[0].is_connected():
                    return c[1]
            except Exception:
                pass
        port = self.launch(account)
        try:
            with _LOCAL.open(f"http://127.0.0.1:{port}/json/version", timeout=5) as r:
                ws = json.loads(r.read())["webSocketDebuggerUrl"]
        except Exception:
            ws = f"http://127.0.0.1:{port}"
        b = self._pw().chromium.connect_over_cdp(ws)
        ctx = b.contexts[0] if b.contexts else b.new_context()
        self.conns[account] = (b, ctx, port)
        return ctx

    def page(self, account, url, reuse_match=None):
        ctx = self.context(account)
        pg = None
        if reuse_match:
            for p in ctx.pages:
                if reuse_match in (p.url or ""):
                    pg = p
                    break
        if pg is None:
            blank = [p for p in ctx.pages if p.url in ("about:blank", "chrome://newtab/", "")]
            pg = blank[0] if blank else ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=90000)
        try:
            pg.bring_to_front()
        except Exception:
            pass
        return pg

    def set_downloads(self, account, folder):
        try:
            ctx = self.context(account)
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            s = ctx.new_cdp_session(pg)
            s.send("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(folder), "eventsEnabled": False})
        except Exception as e:
            self.log(f"(download folder set nahi hua: {e})")

    def close(self):
        for acc, (b, ctx, port) in list(self.conns.items()):
            try:
                b.close()  # disconnects only, the Chrome window stays open
            except Exception:
                pass
        self.conns = {}
        if self.pw:
            try:
                self.pw.stop()
            except Exception:
                pass
            self.pw = None

    # -- helpers
    def check_stop(self):
        if self.stop_flag():
            raise StopJob()

    def sleep(self, s):
        end = time.time() + s
        while time.time() < end:
            self.check_stop()
            time.sleep(min(0.5, end - time.time()) if end > time.time() else 0)


def first_visible(page, sels, timeout=8000, largest=False):
    """Return the first visible element matching any selector (or the largest one)."""
    end = time.time() + timeout / 1000
    while time.time() < end:
        best, best_area = None, 0
        for sel in sels:
            try:
                loc = page.locator(sel)
                n = min(loc.count(), 25)
            except Exception:
                continue
            for i in range(n):
                el = loc.nth(i)
                try:
                    if not el.is_visible():
                        continue
                    if not largest:
                        return el
                    bb = el.bounding_box() or {}
                    a = bb.get("width", 0) * bb.get("height", 0)
                    if a > best_area:
                        best, best_area = el, a
                except Exception:
                    continue
            if largest and best is not None:
                return best
        time.sleep(0.4)
    return None


def dump_debug(page, name):
    try:
        DEBUG.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H%M%S")
        page.screenshot(path=str(DEBUG / f"{stamp}_{name}.png"), full_page=False)
        (DEBUG / f"{stamp}_{name}.html").write_text(page.content(), encoding="utf-8")
        return str(DEBUG / f"{stamp}_{name}.png")
    except Exception:
        return ""


def fill_box(page, el, text):
    el.click()
    try:
        tag = el.evaluate("e => e.tagName.toLowerCase()")
    except Exception:
        tag = ""
    if tag in ("textarea", "input"):
        el.fill(text)
    else:
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.keyboard.insert_text(text)
    time.sleep(0.4)


def fetch_bytes(page, url):
    """Download a URL using the page's own session (works for blob:, data:, signed urls)."""
    if url.startswith("data:"):
        return base64.b64decode(url.split(",", 1)[1])
    try:
        b64 = page.evaluate(
            """async (u) => { const r = await fetch(u, {credentials: 'include'}); const b = await r.arrayBuffer();
            let s = ''; const a = new Uint8Array(b); for (let i = 0; i < a.length; i += 32768) s += String.fromCharCode.apply(null, a.subarray(i, i + 32768));
            return btoa(s); }""", url)
        data = base64.b64decode(b64)
        if len(data) > 1000:
            return data
    except Exception:
        pass
    r = page.context.request.get(url, timeout=120000)
    return r.body()


def body_text(page):
    try:
        return (page.inner_text("body", timeout=3000) or "").lower()
    except Exception:
        return ""


def page_has_words(page, words, baseline=""):
    """A word counts only if it appears MORE often than in the baseline text (new message on screen)."""
    t = body_text(page)
    if not t:
        return None
    for w in words:
        w = w.lower()
        if t.count(w) > baseline.count(w):
            return w
    return None


# ---------------------------------------------------------------- Google sign-in check
def is_signed_in(br, account):
    pg = br.page(account, "https://myaccount.google.com/")
    pg.goto("https://myaccount.google.com/", wait_until="domcontentloaded", timeout=60000)
    time.sleep(2)
    url = pg.url
    return "accounts.google.com" not in url and "signin" not in url, url


# ---------------------------------------------------------------- AI Studio chat (scene prompts)
def aistudio_ask(br, account, prompt, log, timeout=420):
    S = selectors()
    pg = br.page(account, CHAT_URL)
    pg.goto(CHAT_URL, wait_until="domcontentloaded", timeout=90000)
    if "accounts.google.com" in pg.url:
        raise RuntimeError(f"Account '{account}' me Google sign-in nahi hai. Accounts me 'Sign in' dabao.")
    box = first_visible(pg, S["chat_input"], timeout=30000, largest=True)
    if not box:
        dump_debug(pg, "chat_no_box")
        raise RuntimeError("AI Studio chat box nahi mila")
    before = pg.inner_text("body").count("SCENEJSON_END")
    fill_box(pg, box, prompt)
    pg.keyboard.press("Control+Enter")
    time.sleep(2)
    run = first_visible(pg, S["chat_run"], timeout=1500)
    end = time.time() + timeout
    last_len, stable = -1, 0
    while time.time() < end:
        br.check_stop()
        time.sleep(3)
        try:
            txt = pg.inner_text("body")
        except Exception:
            continue
        arr = extract_json_block(txt) if txt.count("SCENEJSON_END") > before else None
        if arr:
            if len(txt) == last_len:
                stable += 1
                if stable >= 2:
                    return txt
            else:
                stable = 0
            last_len = len(txt)
        if run is not None and time.time() > end - timeout + 25 and not arr:
            # maybe Ctrl+Enter did not submit
            try:
                if run.is_visible() and run.is_enabled() and "Stop" not in (run.inner_text() or ""):
                    run.click()
                    run = None
            except Exception:
                run = None
    dump_debug(pg, "chat_timeout")
    raise RuntimeError("AI Studio se jawab nahi aaya (timeout)")


def extract_json_block(txt):
    blocks = re.findall(r"SCENEJSON_BEGIN(.*?)SCENEJSON_END", txt, re.S)
    for b in reversed(blocks):
        b = b.strip().strip("`").strip()
        if b.lower().startswith("json"):
            b = b[4:].strip()
        i, j = b.find("["), b.rfind("]")
        if i >= 0 and j > i:
            try:
                return json.loads(b[i:j + 1])
            except Exception:
                continue
    return None


# ---------------------------------------------------------------- AI Studio TTS
def _pick_voice(pg, voice, log):
    if not voice:
        return
    try:
        # the voice dropdown shows the current voice name; open it and choose ours
        names = ["Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus",
                 "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
                 "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia",
                 "Sadaltager", "Sulafat"]
        dd = None
        for sel in ["mat-select", "[role='combobox']", "button[aria-haspopup='listbox']"]:
            loc = pg.locator(sel)
            for i in range(min(loc.count(), 20)):
                el = loc.nth(i)
                try:
                    t = el.inner_text(timeout=800)
                except Exception:
                    continue
                if any(n in t for n in names):
                    dd = el
                    break
            if dd:
                break
        if not dd:
            log("   (voice dropdown nahi mila, default voice use hogi)")
            return
        if voice in (dd.inner_text() or ""):
            return
        dd.click()
        time.sleep(0.8)
        opt = first_visible(pg, [f"[role='option']:has-text('{voice}')", f"mat-option:has-text('{voice}')"], timeout=4000)
        if opt:
            opt.click()
            time.sleep(0.5)
        else:
            pg.keyboard.press("Escape")
            log(f"   (voice '{voice}' list me nahi mili)")
    except Exception as e:
        log(f"   (voice select nahi hua: {str(e)[:80]})")


def aistudio_tts(br, account, text, voice, style, out_path, log, timeout=420):
    S = selectors()
    pg = br.page(account, TTS_URL, reuse_match=TTS_URL.split("//")[-1])
    pg.goto(TTS_URL, wait_until="domcontentloaded", timeout=90000)  # fresh page for every part
    if "accounts.google.com" in pg.url:
        raise RuntimeError(f"Account '{account}' me Google sign-in nahi hai.")
    time.sleep(2)
    _pick_voice(pg, voice, log)
    boxes = []
    for sel in S["tts_text"]:
        loc = pg.locator(sel)
        for i in range(min(loc.count(), 10)):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    boxes.append(el)
            except Exception:
                pass
        if boxes:
            break
    style_box = first_visible(pg, S["tts_style"], timeout=1500)
    text_box = None
    best = 0
    for el in boxes:
        try:
            ph = (el.get_attribute("placeholder") or "").lower()
            if style_box is not None and el.evaluate("(a,b)=>a===b", style_box.element_handle()):
                continue
            if "style" in ph or "instruction" in ph:
                continue
            bb = el.bounding_box() or {}
            a = bb.get("width", 0) * bb.get("height", 0)
            if a > best:
                text_box, best = el, a
        except Exception:
            continue
    if text_box is None:
        dump_debug(pg, "tts_no_box")
        raise RuntimeError("AI Studio speech page par text box nahi mila")
    if style_box is not None and style:
        try:
            fill_box(pg, style_box, style)
        except Exception:
            pass
    old = set(pg.eval_on_selector_all("audio", "els => els.map(e => e.currentSrc || e.src || '')") or [])
    fill_box(pg, text_box, text)
    base = body_text(pg)
    run = first_visible(pg, S["tts_run"], timeout=3000)
    if run:
        run.click()
    else:
        pg.keyboard.press("Control+Enter")
    end = time.time() + timeout
    clicked_again = False
    while time.time() < end:
        br.check_stop()
        time.sleep(2)
        srcs = pg.eval_on_selector_all("audio", "els => els.map(e => e.currentSrc || e.src || '')") or []
        new = [s for s in srcs if s and s not in old]
        if new:
            time.sleep(1.5)
            data = fetch_bytes(pg, new[-1])
            if len(data) > 2000:
                Path(out_path).write_bytes(data)
                return out_path
        w = page_has_words(pg, ["quota", "rate limit", "resource exhausted", "exceeded"], base)
        if w and time.time() > end - timeout + 8:
            raise AccountLimit(f"AI Studio voice limit ({w})")
        if not clicked_again and time.time() > end - timeout + 20 and run is None:
            pg.keyboard.press("Control+Enter")
            clicked_again = True
    dump_debug(pg, "tts_timeout")
    raise RuntimeError("AI Studio se voice nahi aayi (timeout)")


# ---------------------------------------------------------------- Flow images
class FlowSession:
    def __init__(self, br, account, log, aspect="16:9", ref_image=None, attach_ref=True, inbox=None):
        self.br, self.account, self.log = br, account, log
        self.inbox = inbox
        self.aspect, self.ref_image, self.attach_ref = aspect, ref_image, attach_ref
        self.pg = None
        self.seen = set()
        self.ref_ok = False

    def _imgs(self):
        try:
            return self.pg.eval_on_selector_all(
                "img",
                "els => els.filter(e => e.naturalWidth >= 400 && e.naturalHeight >= 220).map(e => e.currentSrc || e.src)") or []
        except Exception:
            return []

    def open(self):
        S = selectors()
        self.pg = self.br.page(self.account, FLOW_URL, reuse_match=FLOW_URL.split("//")[-1][:30])
        pg = self.pg
        if FLOW_URL.split("//")[-1][:30] not in pg.url:
            pg.goto(FLOW_URL, wait_until="domcontentloaded", timeout=90000)
        time.sleep(4)
        if "accounts.google.com" in pg.url:
            raise RuntimeError(f"Account '{self.account}' me Google sign-in nahi hai.")
        # landing page -> sign in with Google button
        btn = first_visible(pg, ["button:has-text('Sign in')", "text=Sign in with Google"], timeout=1500)
        if btn and "/project/" not in pg.url:
            try:
                btn.click(); time.sleep(5)
            except Exception:
                pass
        if "/project/" not in pg.url:
            np = first_visible(pg, S["flow_new_project"], timeout=15000)
            if np:
                np.click()
                time.sleep(5)
        box = first_visible(pg, S["flow_prompt"], timeout=30000, largest=True)
        if not box:
            dump_debug(pg, "flow_no_box")
            raise RuntimeError("Flow me prompt box nahi mila")
        self._image_mode()
        if self.inbox:
            self.br.set_downloads(self.account, self.inbox)  # manual downloads from Flow land in the project inbox
        self.seen = set(self._imgs())
        return self

    def _image_mode(self):
        S = selectors()
        pg = self.pg
        try:
            t = first_visible(pg, S["flow_mode_toggle"], timeout=3000)
            if t:
                t.click(); time.sleep(1)
                o = first_visible(pg, S["flow_mode_image"], timeout=3000)
                if o:
                    o.click(); time.sleep(1)
                    self.log("   Flow: image mode chuna")
                else:
                    pg.keyboard.press("Escape")
        except Exception:
            pass

    def _attach(self):
        if not (self.attach_ref and self.ref_image and os.path.exists(self.ref_image)):
            return
        S = selectors()
        pg = self.pg
        try:
            inp = pg.locator("input[type=file]")
            if inp.count() == 0:
                b = first_visible(pg, S["flow_add_image"], timeout=1500)
                if b:
                    try:
                        with pg.expect_file_chooser(timeout=4000) as fc:
                            b.click()
                        fc.value.set_files(self.ref_image)
                        time.sleep(3)
                        self._confirm_upload()
                        self.ref_ok = True
                        return
                    except Exception:
                        pg.keyboard.press("Escape")
                        inp = pg.locator("input[type=file]")
            if inp.count() > 0:
                inp.first.set_input_files(self.ref_image)
                time.sleep(3)
                self._confirm_upload()
                self.ref_ok = True
        except Exception as e:
            if not getattr(self, "_ref_warned", False):
                self.log(f"   (reference image attach nahi hui, sirf text se banega: {str(e)[:80]})")
                self._ref_warned = True

    def _confirm_upload(self):
        b = first_visible(self.pg, ["button:has-text('Crop and save')", "button:has-text('Save')", "button:has-text('Done')", "button:has-text('Upload')"], timeout=2500)
        if b:
            try:
                b.click(); time.sleep(2)
            except Exception:
                pass

    def generate(self, prompt, out_path, timeout=300, assist_wait=600):
        S = selectors()
        pg = self.pg
        self.seen |= set(self._imgs())
        self._attach()
        box = first_visible(pg, S["flow_prompt"], timeout=20000, largest=True)
        if not box:
            dump_debug(pg, "flow_no_box2")
            raise RuntimeError("Flow prompt box gayab")
        fill_box(pg, box, prompt)
        base = body_text(pg)
        sub = first_visible(pg, S["flow_submit"], timeout=2500)
        try:
            if sub and sub.is_enabled():
                sub.click()
            else:
                pg.keyboard.press("Enter")
        except Exception:
            pg.keyboard.press("Enter")
        start = time.time()
        warned = False
        while time.time() - start < timeout + assist_wait:
            self.br.check_stop()
            time.sleep(2.5)
            new = [s for s in self._imgs() if s and s not in self.seen]
            if new:
                time.sleep(2)
                new = [s for s in self._imgs() if s and s not in self.seen] or new
                data = fetch_bytes(pg, new[0])
                self.seen |= set(new)
                if len(data) > 5000:
                    Path(out_path).write_bytes(data)
                    return out_path
            el = time.time() - start
            if el > 6:
                w = page_has_words(pg, S["policy_words"], base)
                if w:
                    raise ValueError(f"policy ({w})")
                w = page_has_words(pg, S["error_words"], base)
                if w and el > 8:
                    dump_debug(pg, "flow_limit")
                    raise AccountLimit(f"Flow: '{w}'")
            if el > timeout and not warned:
                warned = True
                self.log("   ⏳ Flow se image nahi aayi. Chrome window me dekho: chaho to khud Generate daba do, app nayi image apne aap utha lega...")
                dump_debug(pg, "flow_slow")
        raise RuntimeError("Flow se image nahi aayi (timeout)")
