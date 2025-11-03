import os, uuid, json, re, subprocess, tempfile, shutil, requests
from fastapi import FastAPI, Request, HTTPException
from pydub import AudioSegment
import whisper

# Read secrets from environment
APP_AUTH = os.environ.get("AUTH_TOKEN", "changeme")
CALLBACK = os.environ.get("BASE44_CALLBACK_URL", "")
MODEL = whisper.load_model("base")  # you can change to "small" or "medium" later

app = FastAPI()

def run(cmd):
    """Run ffmpeg commands safely"""
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[:5000])
    return p.stdout

def dl(url, path):
    """Download video file"""
    r = requests.get(url, stream=True)
    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(1024 * 1024):
            f.write(chunk)

def words_to_chunks(words, max_words=3):
    """Split transcript into <=3-word caption chunks"""
    chunks = []
    i = 0
    while i < len(words):
        group = words[i:i+max_words]
        text = " ".join(w["word"].strip() for w in group).strip()
        start = group[0]["start"]
        end = group[-1]["end"]
        if text:
            chunks.append({"text": text, "start": start, "end": end})
        i += max_words
    return chunks

def make_ass_from_chunks(chunks, ass_path):
    """Generate caption file with TikTok style"""
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
        ms = int((t - int(t)) * 100)
        s = int(t) % 60
        m = (int(t) // 60) % 60
        h = int(t) // 3600
        return f"{h:01d}:{m:02d}:{s:02d}.{ms:02d}"

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        for c in chunks:
            f.write(f"Dialogue: 0,{ts(c['start'])},{ts(c['end'])},TikTokClassic,,0,0,0,,{c['text']}\n")

def pick_title_hook(text):
    """Auto-generate title hook"""
    candidates = re.split(r'(?<=[.!?])\s+', text.strip())
    strong = [c for c in candidates if re.search(r'\bhow\b|\bwhat\b|\bwhy\b|\bstop\b|\bsecret\b', c, re.I)]
    line = (strong[0] if strong else candidates[0]) if candidates else "Watch this"
    words = line.split()
    if len(words) > 8:
        line = " ".join(words[:8])
    return line

@app.post("/process")
async def process(req: Request):
    body = await req.json()
    if body.get("auth") != APP_AUTH:
        raise HTTPException(status_code=401, detail="Bad auth")

    video_id = body["video_id"]
    raw_url = body["raw_url"]
    has_captions = str(body.get("has_captions", "true")).lower() == "true"
    settings = body.get("settings", {})
    pause_trim_ms = int(settings.get("pause_trim_ms", 350))
    tighten_to_ms = int(settings.get("tighten_to_ms", 170))
    lufs = float(settings.get("audio", {}).get("lufs", -14))
    peak = float(settings.get("audio", {}).get("peak_db", -1))
    hook_start = float(settings.get("export", {}).get("hook_start_min_sec", 0.3))
    hook_dur = float(settings.get("export", {}).get("hook_duration_sec", 2.5))

    work = tempfile.mkdtemp()
    try:
        src = os.path.join(work, "input.mp4")
        dl(raw_url, src)

        # Transcribe with Whisper
        wav = os.path.join(work, "audio.wav")
        run(["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", wav])
        result = MODEL.transcribe(wav, word_timestamps=True)
        transcript = result.get("text", "").strip()

        # Make hook text
        title_hook = pick_title_hook(transcript)

        # Shorten long silences
        tight = os.path.join(work, "tight.mp4")
        run([
            "ffmpeg", "-y", "-i", src,
            "-af", f"silenceremove=start_periods=1:start_silence={pause_trim_ms/1000.0}:"
                    f"stop_periods=1:stop_silence={pause_trim_ms/1000.0}:detection=peak",
            "-c:v", "copy", tight
        ])

        # Captions
        words = []
        for seg in result.get("segments", []):
            for w in seg.get("words", []):
                if "start" in w and "end" in w:
                    words.append({"start": w["start"], "end": w["end"], "word": w["word"]})
        ass_path = None
        if has_captions and words:
            ass_path = os.path.join(work, "captions.ass")
            make_ass_from_chunks(words_to_chunks(words), ass_path)

        # Burn text and export
        staged = os.path.join(work, "staged.mp4")
        if ass_path:
            draw = (f"drawtext=text='{title_hook.replace(':','\\:').replace(\"'\",\"\\'\")}'"
                    f":fontcolor=white:fontsize=64:borderw=4:bordercolor=black:"
                    f"x=(w-tw)/2:y=h*0.2:enable='between(t,{hook_start},{hook_start+hook_dur})'")
            run([
                "ffmpeg", "-y", "-i", tight,
                "-vf", f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                       f"subtitles='{ass_path}',{draw}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", staged
            ])
        else:
            run([
                "ffmpeg", "-y", "-i", tight,
                "-vf", f"drawtext=text='{title_hook}':fontcolor=white:fontsize=64:"
                       f"borderw=4:bordercolor=black:x=(w-tw)/2:y=h*0.2:"
                       f"enable='between(t,{hook_start},{hook_start+hook_dur})'",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", staged
            ])

        # Loudness normalize
        final_mp4 = os.path.join(work, "final.mp4")
        run([
            "ffmpeg", "-y", "-i", staged,
            "-filter_complex", f"loudnorm=I={lufs}:TP={peak}:LRA=11",
            "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "160k", final_mp4
        ])

        # Send back to Base44
        files = {"edited_file_upload": ("final.mp4", open(final_mp4, "rb"), "video/mp4")}
        data = {"video_id": video_id, "status": "READY", "title_hook": title_hook}
        requests.post(CALLBACK, data=data, files=files, timeout=120)
        return {"ok": True}

    except Exception as e:
        # Send failure to Base44
        try:
            requests.post(CALLBACK, json={
                "video_id": video_id, "status": "FAILED", "error_msg": str(e)[:800]
            }, timeout=60)
        except:
            pass
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        shutil.rmtree(work, ignore_errors=True)

