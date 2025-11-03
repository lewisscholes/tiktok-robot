import os, uuid, json, re, subprocess, tempfile, shutil, requests
from fastapi import FastAPI, Request, HTTPException, Response
import traceback
from pydub import AudioSegment
import whisper

# Read secrets from environment
APP_AUTH = os.environ.get("AUTH_TOKEN", "changeme")
CALLBACK = os.environ.get("BASE44_CALLBACK_URL", "")
MODEL_NAME = os.environ.get("WHISPER_MODEL", "tiny")  # default to tiny

# Lazy model loader to avoid memory spikes at startup
_model = None
def get_model():
    global _model
    if _model is None:
        _model = whisper.load_model(MODEL_NAME)
    return _model

app = FastAPI()
from fastapi.middleware.cors import CORSMiddleware

# Allow Base44 (and browser) calls
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # tighten later if you want
    allow_credentials=True,
    allow_methods=["*"],      # includes OPTIONS preflight
    allow_headers=["*"],
)

# Explicit preflight handler
@app.options("/process")
async def options_process():
    return Response(status_code=204)

# Optional: make GET / return 200 instead of 404
@app.get("/")
async def home():
    return {"status": "ok", "service": "tiktok-robot"}

# Allow Base44 and browser-based requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # you can restrict later to ["https://base44.ai"]
    allow_credentials=True,
    allow_methods=["*"],        # includes OPTIONS (preflight)
    allow_headers=["*"],
)

@app.api_route("/process", methods=["POST", "OPTIONS"])
async def process(req: Request):
    # Handle browser preflight fast
    if req.method == "OPTIONS":
        return Response(status_code=204)

    # Accept JSON or form-data body
    try:
        try:
            body = await req.json()
        except Exception:
            form = await req.form()
            body = dict(form)
    except Exception as e:
        print("❌ Failed to parse body:", e)
        return Response(status_code=400, content="Bad payload")

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
        result = get_model().transcribe(wav, word_timestamps=True)
        transcript = result.get("text", "").strip()

        # Make title hook
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
        draw = (
            "drawtext=text='{}'"
            ":fontcolor=white:fontsize=64:borderw=4:bordercolor=black:"
            "x=(w-tw)/2:y=h*0.2:enable='between(t,{},{})'".format(
                title_hook.replace(":", "\\:").replace('"', '\\"'),
                hook_start,
                hook_start + hook_dur
            )
        )

        if ass_path:
            run([
                "ffmpeg", "-y", "-i", tight,
                "-vf", f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,subtitles='{ass_path}',{draw}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", staged
            ])
        else:
            run([
                "ffmpeg", "-y", "-i", tight,
                "-vf", f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,{draw}",
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
