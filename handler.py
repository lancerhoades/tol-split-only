import os, re, uuid, tempfile, subprocess, json, mimetypes
from typing import List, Tuple, Optional
from urllib.parse import urlparse
import requests
import runpod

# -------------------------------------------------------------------
# Phrase variants (expanded for robustness against real transcript)
# -------------------------------------------------------------------
PHRASES = {
    "worship_start": [
        "God is more excited",
        "believe that God is more excited",
        "He's been anticipating your presence",
        "anticipating your presence here for a long time",
        "Welcome to Tree of Life",
        "anticipating your presence",
        "I'm so excited for today"
    ],
    "worship_end": [
        "it's that time of the day where we're going to walk around and say hi to some of our friends here at Tree of Life",
        "walk around and say hi to some of our friends here at Tree of Life",
        "it's that time of the day where we're going to walk around",
        "So why don't you go ahead and take a moment",
        "take a moment right now",
        "look around the people who are next to you",
        "walk on up and give a good friendly hello",
        "give a good, friendly hello"
    ],
    # not used for boundaries directly (worship_end acts as announcements start),
    # but kept here for reference / future use.
    "announcements_start": [
        "All right, everyone, please have a seat now so we can listen to our important announcements",
        "So why don't you go ahead and take a moment"
    ],
    "sermon_split": [
        "you can always mail a check",
        "If you'd like to mail a check",
        "mailing address is",
        "Brookline Boulevard Pittsburgh PA 15226",
        "Pittsburgh PA 15226",
        "you can give online at",
        "church center app"
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

# -------------------------------------------------------------------
# Small debug helpers
# -------------------------------------------------------------------
def human_bytes(n: int) -> str:
    if n is None:
        return "?"
    if n < 1024: return f"{n} B"
    for unit in ["KB","MB","GB","TB"]:
        n /= 1024.0
        if n < 1024.0:
            return f"{n:.2f} {unit}"
    return f"{n:.2f} PB"

def debug_file_head(path: str, max_lines: int = 12):
    try:
        print(f"[DEBUG] Head of {path} (first {max_lines} lines):")
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for i, ln in enumerate(f):
                if i >= max_lines: break
                print("  ", ln.rstrip("\n"))
    except Exception as e:
        print(f"[DEBUG] Unable to read head of {path}: {e}")

def debug_parsed_sample(parsed, max_rows: int = 6, max_text: int = 120):
    print(f"[DEBUG] Parsed cues: {len(parsed)} total")
    for i, (s, e, t) in enumerate(parsed[:max_rows]):
        txt = (t[:max_text] + "…") if len(t) > max_text else t
        print(f"  [{i}] {s:.3f} -> {e:.3f} | {txt}")

def truncate(s: str, n: int = 80) -> str:
    s = s or ""
    return s if len(s) <= n else (s[:n] + "…")

# -------------------------------------------------------------------
# Text normalization / matching
# -------------------------------------------------------------------
def normalize_text(s: str) -> str:
    import string
    # handle a few common non-ascii punctuation marks and separators
    s = s.replace("—", "-").replace("–", "-").replace("’", "'").replace("“", '"').replace("”", '"').replace("|", " ")
    s = s.lower().strip()
    s = s.translate(str.maketrans('', '', string.punctuation))
    s = re.sub(r"\s+", " ", s)
    return s

def partial_match(norm_phrase: str, norm_text: str, num_words: int = 5) -> bool:
    """
    Simple containment check using the first N words of the phrase.
    Try 5→4→3-word prefixes for tolerance.
    """
    pw = norm_phrase.split()
    if not pw:
        return False
    for n in (num_words, 4, 3):
        n = min(n, len(pw))
        if n <= 0:
            continue
        snippet = " ".join(pw[:n])
        if snippet in norm_text:
            return True
    return False

# -------------------------------------------------------------------
# Transcript parsing (supports numeric, VTT inline, VTT/SRT block)
# -------------------------------------------------------------------
def parse_timestamped_lines(text: str) -> List[Tuple[float, float, str]]:
    def to_seconds(ts: str) -> float:
        # accept comma or dot milliseconds
        ts = ts.replace(',', '.')
        h, m, s = ts.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)

    parsed: List[Tuple[float, float, str]] = []

    # 1) numeric "start-end: text"
    pat_num = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*:\s*(.+)$')

    # 2) VTT inline (timestamp + text on same line)
    pat_vtt_inline = re.compile(
        r'^\s*(\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s*-->\s*(\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)(?:\s*\|\s*|\s+)?(.*)$'
    )

    # 3) VTT/SRT block: timestamp line only; text lines follow until blank
    pat_block_ts = re.compile(
        r'^\s*(\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s*-->\s*(\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)(?:.*)$'
    )

    # 4) [HH:MM:SS(.ms)] text
    pat_bracket = re.compile(r'^\s*\[?(\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\]?\s+(.+)$')

    lines = text.splitlines()
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # Skip SRT numeric cue indices
        if line.isdigit():
            i += 1
            continue

        m = pat_num.match(line)
        if m:
            start = float(m.group(1)); end = float(m.group(2)); txt = m.group(3)
            parsed.append((start, end, txt))
            i += 1
            continue

        m = pat_vtt_inline.match(line)
        if m:
            start = to_seconds(m.group(1)); end = to_seconds(m.group(2)); txt = m.group(3)
            parsed.append((start, end, txt))
            i += 1
            continue

        m = pat_block_ts.match(line)
        if m:
            start = to_seconds(m.group(1)); end = to_seconds(m.group(2))
            i += 1
            text_buf = []
            while i < n and lines[i].strip():
                text_buf.append(lines[i].strip())
                i += 1
            txt = " ".join(text_buf).strip()
            parsed.append((start, end, txt))
            continue

        m = pat_bracket.match(line)
        if m:
            start = to_seconds(m.group(1)); end = start + 2.0
            parsed.append((start, end, m.group(2).strip()))
            i += 1
            continue

        i += 1

    return parsed

# -------------------------------------------------------------------
# JSON transcript (Whisper/Faster-Whisper) parsing
# -------------------------------------------------------------------
def parse_whisper_json_str(s: str) -> List[Tuple[float, float, str]]:
    """
    Accept a Whisper/Faster-Whisper style JSON string with a top-level dict
    containing 'segments': [{start, end, text, ...}, ...] or a list.
    Returns [(start, end, text), ...]
    """
    try:
        obj = json.loads(s)
    except Exception:
        return []

    segs = []
    if isinstance(obj, dict):
        if isinstance(obj.get("segments"), list):
            segs = obj["segments"]
        else:
            for v in obj.values():
                if isinstance(v, list):
                    segs = v
                    break
    elif isinstance(obj, list):
        segs = obj

    out: List[Tuple[float, float, str]] = []
    for seg in segs:
        try:
            st = float(seg.get("start"))
            en = float(seg.get("end"))
            tx = (seg.get("text") or "").strip()
            if tx and en > st >= 0:
                out.append((st, en, tx))
        except Exception:
            continue
    return out

# -------------------------------------------------------------------
# Phrase locator
# -------------------------------------------------------------------
def find_phrase_time(parsed: List[Tuple[float, float, str]],
                     phrases: List[str],
                     which: str = "start",
                     return_offset: float = 0.0,
                     fallback: Optional[float] = None,
                     debug_lines: int = 3,
                     webhook_override: Optional[str] = None,
                     debug: bool = True) -> Optional[float]:
    checks = 0
    for start, end, text in parsed:
        norm_text = normalize_text(text)
        for phrase in phrases:
            norm_phrase = normalize_text(phrase)
            checks += 1
            # only print a tiny rolling sample
            if debug and checks <= 12:
                print(f"[MATCH] try: '{truncate(norm_phrase, 36)}' vs '{truncate(norm_text, 56)}'")
            if partial_match(norm_phrase, norm_text, num_words=5):
                found = (start if which == "start" else end) + float(return_offset or 0)
                log_and_slack(
                    f"[INFO] Matched “{truncate(phrase, 60)}” at {found:.2f}s (offset={return_offset})",
                    webhook_override
                )
                return found

    if debug:
        print("[DEBUG] No match; first few cues:")
        for i, (s, e, t) in enumerate(parsed[:debug_lines]):
            print(f"  sample {i}: {s}-{e}: {truncate(t, 90)}")
    log_and_slack(f":warning: Phrase(s) not found: {phrases}", webhook_override)
    return fallback

# -------------------------------------------------------------------
# IO helpers
# -------------------------------------------------------------------
def http_get_to(path: str, url: str):
    print(f"[FETCH] GET {url}")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        size = int(r.headers.get("Content-Length") or 0)
        ctype = r.headers.get("Content-Type")
        print(f"[FETCH] -> {path} ({human_bytes(size)}; {ctype or 'unknown MIME'})")
        with open(path, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)
    st = os.stat(path)
    guessed = mimetypes.guess_type(path)[0]
    print(f"[FETCH] Saved {path} ({human_bytes(st.st_size)}; guessed {guessed or 'n/a'})")

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

def safe_trim(src: str, out: str, start: float, duration: float,
              webhook_override: Optional[str] = None) -> bool:
    """
    Guard against zero/near-zero durations to avoid noisy errors.
    """
    if duration <= 0.10:
        log_and_slack(f"[SKIP] {out} duration too small ({duration:.2f}s)", webhook_override)
        return False
    return ffmpeg_trim_copy(src, out, start, duration, webhook_override)

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

# -------------------------------------------------------------------
# Main handler
# -------------------------------------------------------------------
def handler(event):
    inp = (event or {}).get("input") or {}
    job_id = inp.get("job_id")
    transcript_url = inp.get("transcript_url")
    raw_video_url = inp.get("raw_video_url")
    slack_webhook = inp.get("slack_webhook") or None

    # debug toggle
    debug_mode = bool(inp.get("debug", True) or os.environ.get("DEBUG_SPLITTER"))

    if not job_id:
        return {"error": "missing_job_id"}
    if not transcript_url or not str(transcript_url).startswith("http"):
        return {"error": f"transcript_url missing/invalid: {transcript_url}"}
    if not raw_video_url or not str(raw_video_url).startswith("http"):
        return {"error": f"raw_video_url missing/invalid: {raw_video_url}"}

    try:
        with tempfile.TemporaryDirectory() as td:
            # download inputs
            video_path = os.path.join(td, "raw.mp4")

            # choose transcript filename extension by URL & parse JSON if needed
            t_ext = os.path.splitext(urlparse(transcript_url).path)[1].lower() or ".txt"
            if t_ext not in {".json", ".txt", ".vtt", ".srt"}:
                t_ext = ".txt"
            transcript_path = os.path.join(td, f"transcript{t_ext}")

            print(f"[INPUT] job_id={job_id}")
            print(f"[INPUT] transcript_url={transcript_url}")
            print(f"[INPUT] raw_video_url={raw_video_url}")

            http_get_to(video_path, raw_video_url)
            http_get_to(transcript_path, transcript_url)

            print(f"[FILES] video_path={video_path}")
            print(f"[FILES] transcript_path={transcript_path}")

            # peek at transcript head
            if debug_mode:
                debug_file_head(transcript_path, max_lines=int(inp.get("debug_head_lines") or 12))

            # parse transcript (JSON first, else VTT/SRT/TXT)
            with open(transcript_path, "r", encoding="utf-8", errors="ignore") as f:
                raw_tx = f.read()

            parsed: List[Tuple[float, float, str]] = []
            if t_ext == ".json" or raw_tx.lstrip().startswith("{"):
                parsed = parse_whisper_json_str(raw_tx)
                if debug_mode:
                    print(f"[DEBUG] Detected JSON transcript; segments={len(parsed)}")
            if not parsed:
                parsed = parse_timestamped_lines(raw_tx)
                if debug_mode:
                    print(f"[DEBUG] Text/VTT/SRT parse; segments={len(parsed)}")

            if debug_mode:
                debug_parsed_sample(parsed, max_rows=int(inp.get("debug_parsed_rows") or 6))

            if not parsed:
                log_and_slack(":warning: Transcript parsed as empty; check format/regex or JSON shape.", slack_webhook)

            # duration
            dur = ffprobe_duration(video_path)
            if dur is None:
                return {"error": "video_duration_unknown"}
            print(f"[VIDEO] duration={dur:.3f}s")

            # === compute boundaries ===
            # 1) worship_start (+15s offset to get past intro/transition)
            worship_start = find_phrase_time(
                parsed, PHRASES["worship_start"], which="start",
                return_offset=15, fallback=0.0, webhook_override=slack_webhook, debug=debug_mode
            )
            if worship_start is None:
                worship_start = 0.0

            # 2) worship_end (if missing, try sermon_split; else end of file)
            worship_end = find_phrase_time(
                parsed, PHRASES["worship_end"], which="start",
                fallback=None, webhook_override=slack_webhook, debug=debug_mode
            )
            if worship_end is None:
                worship_end = find_phrase_time(
                    parsed, PHRASES["sermon_split"], which="start",
                    fallback=None, webhook_override=slack_webhook, debug=debug_mode
                )
                if worship_end is None:
                    worship_end = dur

            # 3) announcements_end = sermon_split (else end)
            announcements_end = find_phrase_time(
                parsed, PHRASES["sermon_split"], which="start",
                fallback=None, webhook_override=slack_webhook, debug=debug_mode
            )
            if announcements_end is None:
                announcements_end = dur

            # safe clamp + monotonic order
            clamp = lambda x: max(0.0, min(float(x), float(dur)))
            worship_start = clamp(worship_start)
            worship_end = clamp(worship_end)
            announcements_end = clamp(announcements_end)
            if worship_end < worship_start:
                worship_end = worship_start
            if announcements_end < worship_end:
                announcements_end = worship_end

            seg_pre = max(0.0, worship_start - 0.0)
            seg_worship = max(0.0, worship_end - worship_start)
            seg_ann = max(0.0, announcements_end - worship_end)
            seg_sermon = max(0.0, dur - announcements_end)

            log_and_slack(
                f"[BOUNDS] duration={dur:.2f}s; worship_start={worship_start:.2f}s; "
                f"worship_end={worship_end:.2f}s; announcements_end={announcements_end:.2f}s",
                slack_webhook
            )
            print(f"[SEGS] pre={seg_pre:.2f}s, worship={seg_worship:.2f}s, ann={seg_ann:.2f}s, sermon={seg_sermon:.2f}s")

            # === outputs ===
            pre_p = os.path.join(td, "pre_worship_trimmed.mp4")
            worship_p = os.path.join(td, "worship_trimmed.mp4")
            ann_p = os.path.join(td, "announcements_trimmed.mp4")
            sermon_p = os.path.join(td, "sermon_trimmed.mp4")

            # trims (copy: fast; if you see artifacts on non-keyframes, switch to re-encode)
            ok_pre = safe_trim(video_path, pre_p, 0.0, seg_pre, slack_webhook)
            ok_worship = safe_trim(video_path, worship_p, worship_start, seg_worship, slack_webhook)
            ok_ann = safe_trim(video_path, ann_p, worship_end, seg_ann, slack_webhook)
            ok_sermon = safe_trim(video_path, sermon_p, announcements_end, seg_sermon, slack_webhook)

            # uploads (upload only if a target is provided)
            urls = {}

            def up(file_path, put_url, get_url, s3key_name):
                if not (os.path.exists(file_path) and os.path.getsize(file_path) > 0):
                    return None
                if put_url:
                    log_and_slack(f"[UPLOAD] PUT -> {s3key_name}", slack_webhook)
                    upload_put(put_url, file_path)
                    return get_url
                elif inp.get("s3") and inp["s3"].get("bucket") and inp["s3"].get("keys", {}).get(s3key_name):
                    s3_inp = inp["s3"]
                    uri = upload_s3(s3_inp["bucket"], s3_inp["keys"][s3key_name], file_path, s3_inp.get("region"))
                    log_and_slack(f"[UPLOAD] S3 -> {s3key_name}: {uri}", slack_webhook)
                    return uri
                else:
                    log_and_slack(f"[UPLOAD] No target for {s3key_name}; skipping.", slack_webhook)
                    return None

            put_pre = inp.get("pre_worship_put_url")
            put_worship = inp.get("worship_put_url")
            put_ann = inp.get("announcements_put_url")
            put_sermon = inp.get("sermon_put_url")

            get_pre = inp.get("pre_worship_get_url") or put_pre
            get_worship = inp.get("worship_get_url") or put_worship
            get_ann = inp.get("announcements_get_url") or put_ann
            get_sermon = inp.get("sermon_get_url") or put_sermon

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
                "notes": "ffmpeg -c copy trims based on transcript phrase matches (JSON/VTT/SRT parser, safe trims, debug feed)"
            }

    except requests.RequestException as e:
        return {"error": "network_error", "details": str(e)}
    except subprocess.CalledProcessError as e:
        return {"error": "ffmpeg_error", "details": str(e)}
    except Exception as e:
        return {"error": "unexpected_error", "details": str(e)}

runpod.serverless.start({"handler": handler})
