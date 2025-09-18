import os, shutil
import runpod

VOLUME = os.getenv("RUNPOD_VOLUME_PATH", "/runpod-volume")

def handler(event):
    inp = (event or {}).get("input") or {}
    job_id = inp.get("job_id")
    tr_path = inp.get("transcript_path")

    if not job_id:
        return {"error": "missing job_id"}
    if not tr_path or not os.path.exists(tr_path):
        return {"error": f"transcript_path missing/not found: {tr_path}"}

    raw_mp4 = os.path.join(VOLUME, job_id, "raw", "full.mp4")
    if not os.path.exists(raw_mp4):
        return {"error": f"raw video not found: {raw_mp4}"}

    out_dir = os.path.join(VOLUME, job_id, "splits")
    os.makedirs(out_dir, exist_ok=True)
    out_mp4 = os.path.join(out_dir, f"{(inp.get('split_phrase') or 'sermon').strip() or 'sermon'}.mp4")

    # TEMP: no real splitting yet; just copy to keep pipeline moving
    shutil.copy2(raw_mp4, out_mp4)

    return {
        "ok": True,
        "paths": {"video": out_mp4, "transcript": tr_path},
        "notes": "placeholder split-only: copied full.mp4 -> splits/sermon.mp4"
    }

# start serverless handler
runpod.serverless.start({"handler": handler})
