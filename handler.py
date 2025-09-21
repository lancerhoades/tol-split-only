import os, re, uuid, tempfile, subprocess, json
from typing import List, Tuple, Optional
from urllib.parse import urlparse
import requests
import runpod

PHRASES = {
    "worship_start": [
        "God is more excited",
        "He's been anticipating your presence",
        "anticipating your presence here for a long time",
        "Welcome to Tree of Life",
        "I'm so excited for today"
    ],
    "worship_end": [
        "it's that time of the day where we're going to walk around and say hi to some of our friends here at Tree of Life",
        "walk around and say hi to some of our friends here at Tree of Life",
        "it's that time of the day where we're going to walk around"
    ],
    "announcements_start": [
        "All right, everyone, please have a seat now so we can listen to our important announcements"
    ],
    "sermon_split": [
        "you can always mail a check",
        "Pittsburgh PA 15226"
    ]
}

# Slack: set via env or input["slack_webhook"]
SLACK_WEBHOOK_ENV = os.environ.get("SLACK_WEBHOOK", "").strip()

def post_to_slack(message: str, webhook_override: Optional[str] = None):
    url = (webhook_override or SLACK_WEBHOOK_ENV or "").strip()
    if not url:
        return
    try:
        requests.post(url, json={"text": message}, timeout=5)
    except Exception:
        pass

def log_and_slack(msg: str, webhook_override: Optional[str] = None):
    print(msg, flush=True)
    post_to_slack(msg, webhook_override)

def normalize_text(s: str) -> str:
    import string
    s = s.lower().strip()
    s = s.translate(str.maketrans('', '', string.punctuation))
    s = re.sub(r"\s+", " ", s)
    return s

def partial_match(norm_phrase: str, norm_text: str, num_words: int = 8) -> bool:
    pw = norm_phrase.split()
    tw = norm_text.split()
    if len(pw) < num_words:
        num_words = len(pw)
    if num_words == 0:
        return False
    snippet = " ".join(pw[:num_words])
    return snippet in " ".join(tw)

def parse_timestamped_lines(text: str) -> List[Tuple[float, float, str]]:
    pattern = r'(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?): (.*)'
    parsed = []
    for line in text.splitlines():
        m = re.match(pattern, line.strip())
        if m:
            start = float(m.group(1))
            end = float(m.group(2))
            parsed.append((start, end, m.group(3)))
    return parsed

def find_phrase_time(parsed: List[Tuple[float, float, str]],
                     phrases: List[str],
                     which: str = "start",
                     return_offset: float = 0.0,
                     fallback: Optional[float] = None,
                     debug_lines: int = 3,
                     webhook_override: Optional[str] = None) -> Optional[float]:
    for start, end, text in parsed:
        norm_text = normalize_text(text)
        for phrase in phrases:
            norm_phrase = normalize_text(phrase)
            # debug trace (trim to keep logs reasonable)
            print(f"[MATCH DEBUG] '{norm_phrase[:36]}...' vs '{norm_text[:56]}...'")
            if partial_match(norm_phrase, norm_text):
                found = (start if which == "start" else end) + float(return_offset or 0)
                log_and_slack(f"[INFO] Matched '{phrase[:40]}...' at {found:.2f}s (offset={return_offset})",
                              webhook_override)
                return found

    print("[DEBUG] No partial match found. Sample lines:")
    for i, (s, e, t) in enumerate(parsed[:debug_lines]):
        print(f"  sample {i}: {s}-{e}: {t}")
    log_and_slack(f":warning: Phrase(s) not found: {phrases}", webhook_override)
    return fallback

def http_get_to(path: str, url: str):
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)

def ffprobe_duration(path: str) -> Optional[float]:
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            stderr=subprocess.STDOUT
        )
        return float(out.strip())
    except Exception as e:
        print(f"[WARN] ffprobe failed: {e}")
        return None

def ffmpeg_trim_copy(src: str, out: str, start: float, duration: float,
                     webhook_override: Optional[str] = None):
    log_and_slack(f"[TRIM] {out} start={start:.2f} dur={duration:.2f}", webhook_override)
    if duration <= 0:
        log_and_slack(f"[ERROR] Invalid duration: {duration}", webhook_override)
        return False
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", str(start), "-i", src, "-t", str(duration),
           "-c", "copy", out]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        log_and_slack(f"[ERROR] ffmpeg failed for {out}: {e}", webhook_override)
        return False
    ok = os.path.exists(out) and os.path.getsize(out) > 0
    if not ok:
        log_and_slack(f"[ERROR] Output missing/empty: {out}", webhook_override)
    else:
        log_and_slack(f"[OK] Wrote {out} ({os.path.getsize(out)} bytes)", webhook_override)
    return ok

def upload_put(put_url: str, file_path: str) -> str:
    with open(file_path, "rb") as f:
        r = requests.put(put_url, data=f, timeout=300)
        r.raise_for_status()
    return put_url  # caller can also supply a separate GET url

def upload_s3(bucket: str, key: str, file_path: str, region: Optional[str] = None) -> str:
    import boto3
    s3 = boto3.client("s3", region_name=region)
    s3.upload_file(file_path, bucket, key, ExtraArgs={"ContentType": "video/mp4"})
    return f"s3://{bucket}/{key}"

def handler(event):
    inp = (event or {}).get("input") or {}
    job_id = inp.get("job_id")
    transcript_url = inp.get("transcript_url")
    raw_video_url = inp.get("raw_video_url")
    slack_webhook = inp.get("slack_webhook") or None

    if not job_id:
        return {"error": "missing job_id"}
    if not transcript_url or not str(transcript_url).startswith("http"):
        return {"error": f"transcript_url missing/invalid: {transcript_url}"}
    if not raw_video_url or not str(raw_video_url).startswith("http"):
        return {"error": f"raw_video_url missing/invalid: {raw_video_url}"}

    # Upload targets (choose PUT or S3; supply any subset you want uploaded)
    # Presigned PUTs:
    put_pre = inp.get("pre_worship_put_url")
    put_worship = inp.get("worship_put_url")
    put_ann = inp.get("announcements_put_url")
    put_sermon = inp.get("sermon_put_url")
    # Optional separate GET urls (if PUT url is not public)
    get_pre = inp.get("pre_worship_get_url") or put_pre
    get_worship = inp.get("worship_get_url") or put_worship
    get_ann = inp.get("announcements_get_url") or put_ann
    get_sermon = inp.get("sermon_get_url") or put_sermon

    # OR S3 outputs (bucket/key per artifact):
    s3 = inp.get("s3") or {}  # { "bucket": "...", "region": "...", "keys": { "pre":"...", "worship":"...", "ann":"...", "sermon":"..." } }

    try:
        with tempfile.TemporaryDirectory() as td:
            # download inputs
            video_path = os.path.join(td, "raw.mp4")
            transcript_path = os.path.join(td, "transcript.txt")
            http_get_to(video_path, raw_video_url)
            http_get_to(transcript_path, transcript_url)

            # parse transcript
            with open(transcript_path, "r", encoding="utf-8", errors="ignore") as f:
                parsed = parse_timestamped_lines(f.read())

            # duration
            dur = ffprobe_duration(video_path)
            if dur is None:
                return {"error": "video_duration_unknown"}

            # === compute boundaries (mimic your previous logic) ===
            # 1) worship_start (+15s offset)
            worship_start = find_phrase_time(parsed, PHRASES["worship_start"],
                                             which="start", return_offset=15,
                                             fallback=0.0, webhook_override=slack_webhook)
            if worship_start is None:
                worship_start = 0.0

            # 2) worship_end (if missing, try sermon_split; else end of file)
            worship_end = find_phrase_time(parsed, PHRASES["worship_end"], which="start",
                                           fallback=None, webhook_override=slack_webhook)
            if worship_end is None:
                worship_end = find_phrase_time(parsed, PHRASES["sermon_split"], which="start",
                                               fallback=None, webhook_override=slack_webhook)
                if worship_end is None:
                    worship_end = dur

            # 3) announcements_end = sermon_split (else end)
            announcements_end = find_phrase_time(parsed, PHRASES["sermon_split"], which="start",
                                                 fallback=None, webhook_override=slack_webhook)
            if announcements_end is None:
                announcements_end = dur

            # safe clamp
            def clamp(x): return max(0.0, min(float(x), float(dur)))
            worship_start = clamp(worship_start)
            worship_end = clamp(worship_end)
            announcements_end = clamp(announcements_end)

            # === outputs ===
            pre_p = os.path.join(td, "pre_worship_trimmed.mp4")
            worship_p = os.path.join(td, "worship_trimmed.mp4")
            ann_p = os.path.join(td, "announcements_trimmed.mp4")
            sermon_p = os.path.join(td, "sermon_trimmed.mp4")

            # trims (copy: fast; if you see artifacts on non-keyframes, switch to re-encode)
            ok_pre = ffmpeg_trim_copy(video_path, pre_p, 0.0, worship_start, slack_webhook)
            ok_worship = ffmpeg_trim_copy(video_path, worship_p, worship_start, max(0.0, worship_end - worship_start), slack_webhook)
            ok_ann = ffmpeg_trim_copy(video_path, ann_p, worship_end, max(0.0, announcements_end - worship_end), slack_webhook)
            ok_sermon = ffmpeg_trim_copy(video_path, sermon_p, announcements_end, max(0.0, dur - announcements_end), slack_webhook)

            # uploads (upload only if a target is provided)
            urls = {}

            def up(file_path, put_url, get_url, s3key_name):
                if not (os.path.exists(file_path) and os.path.getsize(file_path) > 0):
                    return None
                if put_url:
                    upload_put(put_url, file_path)
                    return get_url
                elif s3 and s3.get("bucket") and s3.get("keys", {}).get(s3key_name):
                    return upload_s3(s3["bucket"], s3["keys"][s3key_name], file_path, s3.get("region"))
                else:
                    return None

            urls["pre_worship_url"] = up(pre_p, put_pre, get_pre, "pre")
            urls["worship_url"] = up(worship_p, put_worship, get_worship, "worship")
            urls["announcements_url"] = up(ann_p, put_ann, get_ann, "ann")
            urls["sermon_url"] = up(sermon_p, put_sermon, get_sermon, "sermon")

            return {
                "ok": True,
                "urls": urls,
                "bounds": {
                    "duration": dur,
                    "worship_start": worship_start,
                    "worship_end": worship_end,
                    "announcements_end": announcements_end
                },
                "notes": "ffmpeg -c copy trims based on transcript phrase matches"
            }

    except requests.RequestException as e:
        return {"error": "network_error", "details": str(e)}
    except subprocess.CalledProcessError as e:
        return {"error": "ffmpeg_error", "details": str(e)}
    except Exception as e:
        return {"error": "unexpected_error", "details": str(e)}

runpod.serverless.start({"handler": handler})
