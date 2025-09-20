import os
import runpod

def handler(event):
    inp = (event or {}).get("input") or {}
    job_id = inp.get("job_id")
    tjson_url = inp.get("transcript_url")
    raw_video_url = inp.get("raw_video_url")

    if not job_id:
        return {"error": "missing job_id"}
    if not tjson_url or not str(tjson_url).startswith("http"):
        return {"error": f"transcript_url missing/invalid: {tjson_url}"}
    if not raw_video_url or not str(raw_video_url).startswith("http"):
        return {"error": f"raw_video_url missing/invalid: {raw_video_url}"}

    # No local NV. For now, we "split" by passing the same URL through.
    sermon_url = raw_video_url

    return {
        "ok": True,
        "urls": {
            "transcript_url": tjson_url,
            "sermon_url": sermon_url
        },
        "notes": "split-only placeholder: passing through raw_video_url as sermon_url (S3-only)"
    }

runpod.serverless.start({"handler": handler})
