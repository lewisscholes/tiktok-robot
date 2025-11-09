import os, json, re, subprocess, tempfile, shutil, requests, traceback
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
import whisper

# ============== Config via Environment ==============
APP_AUTH   = os.environ.get("AUTH_TOKEN", "changeme")
# For legacy direct Base44 failure notices (optional)
BASE44_CALLBACK = os.environ.get("BASE44_CALLBACK_URL", "").strip()
# Zapier Catch Hook that receives the FINAL result (Render -> Zapier)
ZAPIER_WEBHOOK_URL = os.environ.get("ZAPIER_WEBHOOK_URL", "").strip()
# Whisper model
MODEL_NAME = os.environ.get("WHISPER_MODEL", "tiny")

# ============== Lazy Whisper Loader ==============
_model = None
def get_model():
    global _model
    if _model is None:
        _model = whisper.load_model(MODEL_NAME)
    return _model

# ============== FastAPI App + CORS ==============
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # tighten later if desired
    allow_credentials=True,
    allow_methods=["*"],           # includes OPTIONS
    allow_headers=["*"],
)

@app.get("/")
async def home():
    return {"status": "ok", "service": "tiktok-robot"}

@app.options("/process")
async def options_process():
    return Response(status_code=204)

# ============== Utils ==============
def download_file(url, path):
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(1024 * 1024):
            if chunk:
                f.write(chunk)

def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        print("FFmpeg error:\n", p.stderr[:2000])
        raise RuntimeError(p.stderr)
    return p.stdout

def pick_title_hook(text: str) -> str:
    if not text:
        return "Watch this"
    candidates = re.split(r'(?<=[.!?])\s+', text.strip())
    strong = [c for c in candidates if re.search(r'\b(how|what|why|stop|secret|best|avoid|never)\b', c, re.I)]
    line = strong[0] if strong else (candidates[0] if candidates else "Watch this")
    words = line.split()
    return " ".join(words[:8]) if len(words) > 8 else line

def words_to_chunks(words, max_words=3):
    chunks, i = [], 0
    while i < len(words):
        group = words[i:i + max_words]
        if not group:
            break
        text = " ".join(w["word"].strip() for w in group).strip()
        start, end = group[0]["start"], group[-1]["end"]
        if text:
            chunks.append({"text": text, "start": start, "end": end})
        i += max_words
    return chunks

def make_ass_from_chunks(chunks, ass_path):
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: TikTokClassic,Arial,64,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,4,0,2,80,80,240,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def ts(t):
        t = max(0.0, float(t))
        cs = int(round((t - int(t)) * 100))
        s = int(t) % 60
        m = (int(t) // 60) % 60
        h = int(t) // 3600
        return f"{h:01d}:{m:02d}:{s:02d}.{cs:02d}"

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        for c in chunks:
            f.write(f"Dialogue: 0,{ts(c['start'])},{ts(c['end'])},TikTokClassic,,0,0,0,,{c['text']}\n")

# ============== Main Endpoint ==============
@app.api_route("/process", methods=["POST", "OPTIONS"])
async def process(req: Request):
    if req.method == "OPTIONS":
        return Response(status_code=204)

    # Parse JSON first, then fallback to form-data
    try:
        try:
            body = await req.json()
        except Exception:
            form = await req.form()
            body = dict(form)
    except Exception as e:
        print("❌ Failed to parse body:", e)
        return Response(status_code=400, content="Bad payload")

    # --- Accept auth via JSON or Authorization: Bearer ---
    auth_token = body.get("auth")
    if not auth_token:
        auth_hdr = req.headers.get("authorization", "")
        if auth_hdr.lower().startswith("bearer "):
            auth_token = auth_hdr.split(" ", 1)[1].strip()

    if auth_token != APP_AUTH:
        raise HTTPException(status_code=401, detail="Bad auth")

    # --- Allow Zapier to send video_url instead of raw_url ---
    if "video_url" in body and "raw_url" not in body:
        body["raw_url"] = body["video_url"]

    # Defaults
    body.setdefault("has_captions", True)
    body.setdefault("settings", {})

    # Inputs
    video_id     = body["video_id"]
    raw_url      = body["raw_url"]
    has_captions = str(body.get("has_captions", True)).lower() == "true"
    settings     = body.get("settings", {})
    pause_trim_ms = int(settings.get("pause_trim_ms", 350))
    lufs          = float(settings.get("audio", {}).get("lufs", -14))
    peak          = float(settings.get("audio", {}).get("peak_db", -1))
    hook_start    = float(settings.get("export", {}).get("hook_start_min_sec", 0.3))
    hook_dur      = float(settings.get("export", {}).get("hook_duration_sec", 2.5))

    work = tempfile.mkdtemp()
    try:
        # 1) Download
        src = os.path.join(work, "input.mp4")
        download_file(raw_url, src)

        # 2) Transcribe
        wav = os.path.join(work, "audio.wav")
        run(["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", wav])
        result = get_model().transcribe(wav, word_timestamps=True)
        transcript = result.get("text", "").strip()

        # 3) Hook
        title_hook = pick_title_hook(transcript)

        # 4) Tighten silences (audio)
        tight = os.path.join(work, "tight.mp4")
        run([
            "ffmpeg", "-y", "-i", src,
            "-af", f"silenceremove=start_periods=1:start_silence={pause_trim_ms/1000.0}:"
                   f"stop_periods=1:stop_silence={pause_trim_ms/1000.0}:detection=peak",
            "-c:v", "copy", tight
        ])

        # 5) Captions (3-word chunks)
        words = []
        for seg in result.get("segments", []):
            for w in seg.get("words", []):
                if "start" in w and "end" in w and "word" in w:
                    words.append({"start": w["start"], "end": w["end"], "word": w["word"]})
        ass_path = None
        if has_captions and words:
            ass_path = os.path.join(work, "captions.ass")
            make_ass_from_chunks(words_to_chunks(words), ass_path)

        # 6) Title overlay (escape + escaped commas in between())
        safe_title = (
            title_hook
            .replace(":", r"\:")
            .replace("'", r"\'")
            .replace('"', r'\"')
        )
        draw = (
            "drawtext=text='{txt}':"
            "fontcolor=white:fontsize=64:borderw=4:bordercolor=black:"
            "x=(w-tw)/2:y=h*0.2:"
            "enable='between(t\\,{start}\\,{end})'"
        ).format(
            txt=safe_title,
            start=hook_start,
            end=hook_start + hook_dur
        )

        # 7) Burn text (+optional captions), export staged
        staged = os.path.join(work, "staged.mp4")
        if ass_path:
            vf = (
                "scale=1080:1920:force_original_aspect_ratio=increase,"
                f"crop=1080:1920,subtitles='{ass_path}',{draw}"
            )
        else:
            vf = (
                "scale=1080:1920:force_original_aspect_ratio=increase,"
                f"crop=1080:1920,{draw}"
            )

        run([
            "ffmpeg", "-y", "-i", tight,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "160k",
            staged
        ])

        # 8) Loudness normalize to final (use -af instead of filter_complex)
        final_mp4 = os.path.join(work, "final.mp4")
        run([
            "ffmpeg", "-y", "-i", staged,
            "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "160k",
            final_mp4
        ])

        # 9) Callback to Zapier (Render -> Zapier). Zapier will forward to Base44.
        if not ZAPIER_WEBHOOK_URL:
            print("⚠️  No ZAPIER_WEBHOOK_URL set; skipping Zapier callback")
        else:
            print("📩  Posting final to Zapier:", ZAPIER_WEBHOOK_URL)
            files = {"edited_file_upload": ("final.mp4", open(final_mp4, "rb"), "video/mp4")}
            payload = {
                "video_id": video_id,
                "status": "READY",
                "title_hook": title_hook,
                "source": "render",
            }
            try:
                r = requests.post(ZAPIER_WEBHOOK_URL, data=payload, files=files, timeout=120)
                print("📨  Zapier response:", r.status_code, r.text[:400])
                r.raise_for_status()
            except Exception as e:
                print("❌ Zapier callback failed:", repr(e))
                # Do not fail the whole job on callback errors

        return {"ok": True}

    except Exception as e:
        print("❌ Processing error:\n", traceback.format_exc())

        # Try to notify Zapier of failure if configured
        if ZAPIER_WEBHOOK_URL:
            try:
                requests.post(ZAPIER_WEBHOOK_URL, json={
                    "video_id": body.get("video_id", ""),
                    "status": "FAILED",
                    "error_msg": str(e)[:800],
                    "source": "render"
                }, timeout=60)
            except Exception as _:
                pass
        # Fallback: optionally notify Base44 directly (legacy)
        elif BASE44_CALLBACK:
            try:
                requests.post(BASE44_CALLBACK, json={
                    "video_id": body.get("video_id", ""),
                    "status": "FAILED",
                    "error_msg": str(e)[:800]
                }, timeout=60)
            except Exception as _:
                pass

        raise HTTPException(status_code=500, detail=str(e))

    finally:
        shutil.rmtree(work, ignore_errors=True)
