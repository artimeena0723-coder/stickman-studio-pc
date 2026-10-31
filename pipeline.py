"""Project data, script splitting, prompt building and the ffmpeg render."""
import json, math, os, re, shutil, subprocess, sys, time, wave, zipfile
from pathlib import Path

from paths import BASE as ROOT
PROJECTS = ROOT / "projects"

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

ART = ("Hand-drawn 2D cartoon illustration for a YouTube storytelling channel. Stickman characters with big round white heads, "
       "thick clean black outlines, simple black dot eyes, very expressive eyebrows and mouths, thin black stick arms and legs, "
       "minimal clothing accessories. Richly detailed, colourful painted backgrounds with soft cinematic lighting and depth. "
       "Clean composition, crisp lines, high detail.")
NEG = ("No text, no letters, no numbers, no captions, no subtitles, no speech bubbles, no logos, no watermark, no signature, "
       "no sparkle or star icon, no border or frame. Fill the whole frame edge to edge.")
PRESETS = {
    "survival": {"min": 6, "max": 8, "voice": "calm, tense, intimate documentary narrator, measured pace, building suspense",
                 "look": "moody cinematic lighting, tension, survival atmosphere"},
    "history": {"min": 3, "max": 5, "voice": "warm, curious, confident storyteller, engaging and slightly fast, documentary feel",
                "look": "bright warm daylight, historical settings, clear visual metaphors and comparisons"},
    "custom": {"min": 5, "max": 7, "voice": "engaging cinematic storyteller", "look": ""},
}
WPS = {"hi": 2.4, "en": 2.6, "hinglish": 2.5}
VOICES = ["Charon", "Gacrux", "Algenib", "Sadaltager", "Orus", "Iapetus", "Schedar", "Rasalgethi", "Enceladus", "Alnilam",
          "Kore", "Puck", "Zephyr", "Fenrir", "Leda", "Aoede", "Callirrhoe", "Autonoe", "Umbriel", "Algieba", "Despina",
          "Erinome", "Laomedeia", "Achernar", "Pulcherrima", "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sulafat"]
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")


def safe(name):
    n = re.sub(r"[^\w\- ]+", "", name or "").strip().replace(" ", "_")
    return n[:60] or "video"


class Project:
    def __init__(self, name):
        self.name = safe(name)
        self.dir = PROJECTS / self.name
        for d in ["images", "voice", "clips", "inbox", "assets"]:
            (self.dir / d).mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "project.json"
        self.data = {"name": self.name, "settings": {}, "scenes": [], "voice_chunks": [], "sheet": {}}
        if self.file.exists():
            try:
                self.data.update(json.loads(self.file.read_text(encoding="utf-8")))
            except Exception:
                pass

    def save(self):
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.file)

    @property
    def scenes(self):
        return self.data["scenes"]

    def img_path(self, i):
        for ext in IMG_EXT:
            p = self.dir / "images" / f"scene_{i + 1:03d}{ext}"
            if p.exists():
                return p
        return None

    def ref_path(self):
        for ext in IMG_EXT:
            p = self.dir / "assets" / f"character{ext}"
            if p.exists():
                return p
        return None

    def asset(self, stem):
        for p in (self.dir / "assets").glob(stem + ".*"):
            return p
        return None

    def status(self):
        out = []
        for i, s in enumerate(self.scenes):
            out.append({"n": i + 1, "text": s.get("text", ""), "prompt": s.get("prompt", ""), "img": bool(self.img_path(i)),
                        "err": s.get("err", ""), "dur": s.get("dur")})
        return out


# ---------------------------------------------------------------- script -> scenes
def split_script(text, lang="hi", min_s=5, max_s=7):
    text = (text or "").replace("\r", "")
    # explicit SCENE format: "SCENE: prompt" lines followed by narration
    if re.search(r"(?im)^\s*(scene\s*\d*\s*[:\-])", text):
        scenes, cur = [], None
        for line in text.split("\n"):
            m = re.match(r"(?i)^\s*scene\s*\d*\s*[:\-]\s*(.*)$", line)
            if m:
                if cur and (cur["text"].strip() or cur["prompt"].strip()):
                    scenes.append(cur)
                cur = {"prompt": m.group(1).strip(), "text": ""}
            elif line.strip():
                if cur is None:
                    cur = {"prompt": "", "text": ""}
                m2 = re.match(r"(?i)^\s*(narration|voice|vo)\s*[:\-]\s*(.*)$", line)
                cur["text"] += (" " if cur["text"] else "") + (m2.group(2) if m2 else line.strip())
        if cur and (cur["text"].strip() or cur["prompt"].strip()):
            scenes.append(cur)
        for s in scenes:
            s["text"] = s["text"].strip()
        return [s for s in scenes if s["text"]]
    wps = WPS.get(lang, 2.5)
    target = max(4, round((min_s + max_s) / 2 * wps))
    max_w = max(target + 1, int(max_s * wps))
    raw = []
    for seg in re.split(r'("[^"\n]{1,400}")', text.replace("“", '"').replace("”", '"')):
        if not seg.strip():
            continue
        if seg.startswith('"') and seg.endswith('"'):
            raw.append(seg.strip())
        else:
            raw += [x.strip() for x in re.split(r"(?<=[।!?.…])\s+|\n+", seg) if x.strip()]
    pieces = []
    for s in raw:
        w = s.split()
        if len(w) <= max_w:
            pieces.append(s); continue
        cur = []
        for c in re.split(r"(?<=[,;:—–])\s+", s):
            cw = c.split()
            if len(cw) > max_w:
                if cur:
                    pieces.append(" ".join(cur)); cur = []
                for k in range(0, len(cw), target):
                    pieces.append(" ".join(cw[k:k + target]))
                continue
            if len(cur) + len(cw) > max_w:
                pieces.append(" ".join(cur)); cur = []
            cur += cw
        if cur:
            pieces.append(" ".join(cur))
    scenes, cur, cw = [], [], 0
    for p in pieces:
        w = len(p.split())
        if cw and cw + w > max_w:
            scenes.append(" ".join(cur)); cur, cw = [], 0
        cur.append(p); cw += w
        if cw >= target:
            scenes.append(" ".join(cur)); cur, cw = [], 0
    if cur:
        scenes.append(" ".join(cur))
    if len(scenes) > 1 and len(scenes[-1].split()) < target / 3:
        last = scenes.pop(); scenes[-1] += " " + last
    return [{"text": s, "prompt": ""} for s in scenes]


def prompt_request(project, idxs):
    P = project.data
    st = P["settings"]
    lines = "\n".join(f"{i + 1}. {P['scenes'][i]['text']}" for i in idxs)
    return f"""You are the art director of a faceless STICKMAN storytelling YouTube channel.
Story style notes: {st.get('notes', '')}
For EVERY numbered narration line below, write one vivid image prompt (English, 25-45 words) that shows EXACTLY that moment:
who is in frame, what the stickman is doing, his facial expression, the location/background and the camera shot.
Keep recurring places and characters described the same way every time. Never put text or words in the image.
Also give one fixed description of the main character and the world, the same for all scenes.
Reply with ONLY this, nothing else:
SCENEJSON_BEGIN
[{{"n": <line number>, "prompt": "..."}}, ...]
SCENEJSON_END
Before the list, on one line, write: CHARACTER: <main character look> | WORLD: <setting, era, palette>

NARRATION LINES:
{lines}"""


def full_prompt(project, i):
    P = project.data
    st, s, sh = P["settings"], P["scenes"][i], P.get("sheet", {})
    pre = PRESETS.get(st.get("preset", "custom"), PRESETS["custom"])
    scene = (s.get("prompt") or "").strip() or ("Illustrate this exact story moment: " + s.get("text", "").strip())
    parts = [
        "Use the attached stickman image as the main character (same face, same style)." if project.ref_path() else "",
        f"ART STYLE: {ART} {pre['look']} {st.get('style_extra', '')}".strip(),
        f"MAIN CHARACTER: {sh.get('character') or 'a classic stickman with a big round white head'}. Keep him identical in every frame.",
        f"WORLD: {sh['world']}" if sh.get("world") else "",
        f"SCENE: {scene}",
        f"Aspect ratio {st.get('aspect', '16:9')}. {NEG}",
    ]
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------- import images (folder / zip / files)
def scene_number(name):
    base = os.path.basename(name)
    m = re.search(r"scene[_\- ]?0*(\d+)", base, re.I) or re.match(r"\s*0*(\d+)", base)
    return int(m.group(1)) if m else None


def is_image(b):
    return b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n" or (b[:4] == b"RIFF" and b[8:12] == b"WEBP")


def import_images(project, paths, log):
    """paths: files, folders or zips. Maps by leading number in filename (Flow: '12_hash.jpg'); else by order."""
    items = []  # (name, bytes)

    def add_zip(zb, prefix=""):
        import io
        with zipfile.ZipFile(io.BytesIO(zb)) as z:
            for n in sorted(z.namelist()):
                if n.endswith("/"):
                    continue
                b = z.read(n)
                if n.lower().endswith(".zip"):
                    add_zip(b, prefix + n + "/")
                elif is_image(b):
                    items.append((n, b))

    for p in paths:
        p = Path(p)
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    b = f.read_bytes()
                    if f.suffix.lower() == ".zip":
                        add_zip(b)
                    elif is_image(b):
                        items.append((f.name, b))
        elif p.is_file():
            b = p.read_bytes()
            if p.suffix.lower() == ".zip" or b[:2] == b"PK":
                add_zip(b)
            elif is_image(b):
                items.append((p.name, b))
    n_sc = len(project.scenes)
    numbered = [(scene_number(n), n, b) for n, b in items]
    done = 0
    if numbered and sum(1 for k, _, _ in numbered if k) >= max(1, len(numbered) // 2):
        for k, n, b in numbered:
            if not k or k > n_sc:
                log(f"   skip: {os.path.basename(n)}")
                continue
            save_scene_image(project, k - 1, b); done += 1
    else:
        missing = [i for i in range(n_sc) if not project.img_path(i)]
        for (n, b), i in zip(sorted(items, key=lambda x: x[0]), missing):
            save_scene_image(project, i, b); done += 1
    log(f"📥 {done} images scenes me lag gayi")
    return done


def save_scene_image(project, i, data):
    for ext in IMG_EXT:
        old = project.dir / "images" / f"scene_{i + 1:03d}{ext}"
        if old.exists():
            old.unlink()
    ext = ".png" if data[:4] == b"\x89PNG" else ".webp" if data[:4] == b"RIFF" else ".jpg"
    (project.dir / "images" / f"scene_{i + 1:03d}{ext}").write_bytes(data)
    if i < len(project.scenes):
        project.scenes[i]["err"] = ""


# ---------------------------------------------------------------- audio helpers
def run(cmd, log=None):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if p.returncode != 0:
        raise RuntimeError("ffmpeg error:\n" + p.stderr[-1200:])
    return p.stderr


def duration(path):
    err = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
    if not m:
        raise RuntimeError(f"Audio duration nahi mili: {path}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def to_wav(src, dst, trim=True):
    af = ["-af", "silenceremove=start_periods=1:start_threshold=-45dB,areverse,silenceremove=start_periods=1:start_threshold=-45dB,areverse,apad=pad_dur=0.25"] if trim else []
    run([FFMPEG, "-y", "-hide_banner", "-i", str(src), *af, "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", str(dst)])
    return dst


def voice_chunks(scenes, max_chars=1100):
    chunks, cur, n = [], [], 0
    for i, s in enumerate(scenes):
        L = len(s["text"])
        if cur and n + L > max_chars:
            chunks.append(cur); cur, n = [], 0
        cur.append(i); n += L
    if cur:
        chunks.append(cur)
    return chunks


def assign_durations(project):
    """Fill scene['dur'] from voice chunk wavs (split by character count inside each chunk)."""
    P = project.data
    if P["settings"].get("voice_mode") == "upload":
        wav = project.dir / "voice" / "voice_full.wav"
        if not wav.exists():
            raise RuntimeError("Apni voice file upload karo (ya voice mode 'AI Studio' rakho)")
        total = duration(wav)
        weights = [max(8, len(s["text"])) for s in P["scenes"]]
        W = sum(weights)
        for s, w in zip(P["scenes"], weights):
            s["dur"] = round(total * w / W, 3)
        return [wav]
    files = []
    for ci, idxs in enumerate(P["voice_chunks"]):
        f = project.dir / "voice" / f"chunk_{ci + 1:03d}.wav"
        if not f.exists():
            raise RuntimeError(f"Voice part {ci + 1} abhi bana nahi")
        files.append(f)
        total = duration(f)
        weights = [max(8, len(P["scenes"][i]["text"])) for i in idxs]
        W = sum(weights)
        for i, w in zip(idxs, weights):
            P["scenes"][i]["dur"] = round(total * w / W, 3)
    return files


# ---------------------------------------------------------------- render
def render(project, log, stop=lambda: False, progress=lambda f: None):
    P = project.data
    st = P["settings"]
    aspect = st.get("aspect", "16:9")
    res = st.get("res", "1080")
    fps = int(st.get("fps", 30))
    if aspect == "9:16":
        W, H = (2160, 3840) if res == "4k" else (1080, 1920)
    else:
        W, H = (3840, 2160) if res == "4k" else (1920, 1080)
    missing = [i + 1 for i in range(len(P["scenes"])) if not project.img_path(i)]
    if missing:
        raise RuntimeError(f"In scenes ki image abhi nahi hai: {missing[:30]}")
    voice_files = assign_durations(project)
    project.save()
    out_dir = project.dir
    # 1) voice track
    voice = out_dir / "voice" / "voice_track.wav"
    if len(voice_files) == 1:
        shutil.copy(voice_files[0], voice)
    else:
        lst = out_dir / "voice" / "list.txt"
        lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in voice_files), encoding="utf-8")
        run([FFMPEG, "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", str(lst), "-c:a", "pcm_s16le", str(voice)])
    # 2) one clip per scene (slow pan, edges cropped so no corner logo shows)
    crop = float(st.get("edge_crop", 5)) / 100.0
    motion = st.get("motion", "pan")
    zoom = 1.12
    clips = []
    dirs = [("l2r"), ("r2l"), ("t2b"), ("b2t"), ("zin")]
    sig = f"{W}x{H}@{fps}-{crop}-{motion}"
    for i, s in enumerate(P["scenes"]):
        if stop():
            raise RuntimeError("Stop dabaya")
        d = max(0.5, float(s["dur"]))
        img = project.img_path(i)
        clip = out_dir / "clips" / f"clip_{i + 1:03d}.mp4"
        meta = out_dir / "clips" / f"clip_{i + 1:03d}.json"
        key = {"sig": sig, "dur": round(d, 3), "img": img.name, "mtime": int(img.stat().st_mtime)}
        if clip.exists() and meta.exists():
            try:
                if json.loads(meta.read_text()) == key:
                    clips.append(clip); progress((i + 1) / len(P["scenes"]) * 0.9); continue
            except Exception:
                pass
        BW, BH = int(W * zoom) // 2 * 2, int(H * zoom) // 2 * 2
        base = (f"crop=iw*{1 - 2 * crop:.4f}:ih*{1 - 2 * crop:.4f},scale={BW}:{BH}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={BW}:{BH},setsar=1")
        mv = dirs[i % len(dirs)] if motion == "pan" else "none"
        T = f"(t/{d:.3f})"
        if mv == "l2r":
            cr = f"crop={W}:{H}:x='(iw-{W})*{T}':y='(ih-{H})/2'"
        elif mv == "r2l":
            cr = f"crop={W}:{H}:x='(iw-{W})*(1-{T})':y='(ih-{H})/2'"
        elif mv == "t2b":
            cr = f"crop={W}:{H}:x='(iw-{W})/2':y='(ih-{H})*{T}'"
        elif mv == "b2t":
            cr = f"crop={W}:{H}:x='(iw-{W})/2':y='(ih-{H})*(1-{T})'"
        elif mv == "zin":
            frames = max(1, int(d * fps))
            cr = (f"zoompan=z='1+0.10*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={fps}")
        else:
            cr = f"crop={W}:{H}"
        vf = f"{base},{cr},fps={fps},format=yuv420p"
        fade = st.get("transition", "cut")
        if fade == "fade":
            vf += f",fade=t=in:st=0:d=0.25,fade=t=out:st={max(0, d - 0.25):.3f}:d=0.25"
        log(f"🎬 Clip {i + 1}/{len(P['scenes'])} ({d:.1f}s)")
        run([FFMPEG, "-y", "-hide_banner", "-loop", "1", "-framerate", str(fps), "-i", str(img), "-t", f"{d:.3f}",
             "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "19" if res == "4k" else "18",
             "-pix_fmt", "yuv420p", "-r", str(fps), "-an", str(clip)])
        meta.write_text(json.dumps(key))
        clips.append(clip)
        progress((i + 1) / len(P["scenes"]) * 0.9)
    # 3) join + audio (+ optional music)
    lst = out_dir / "clips" / "list.txt"
    lst.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips), encoding="utf-8")
    silent = out_dir / "clips" / "video_silent.mp4"
    run([FFMPEG, "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(silent)])
    final = out_dir / f"{project.name}_final.mp4"
    music = project.asset("music")
    log("🔊 Voice jod raha hoon...")
    if music:
        mv = float(st.get("music_vol", 12)) / 100.0
        run([FFMPEG, "-y", "-hide_banner", "-i", str(silent), "-i", str(voice), "-stream_loop", "-1", "-i", str(music),
             "-filter_complex", f"[2:a]volume={mv:.3f}[m];[1:a][m]amix=inputs=2:duration=first:dropout_transition=0[a]",
             "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(final)])
    else:
        run([FFMPEG, "-y", "-hide_banner", "-i", str(silent), "-i", str(voice), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
             "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(final)])
    progress(1.0)
    return final
