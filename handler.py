import os, re, uuid, tempfile, subprocess, json, mimetypes, time, shutil, platform
from typing import List, Tuple, Optional
from urllib.parse import urlparse
import requests
import runpod
from download_from_s3 import download_url_parallel, default_progress_logger

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

DEFAULT_PHRASES = PHRASES

# Slack: set via env or input["slack_webhook"]
SLACK_WEBHOOK_ENV = os.environ.get("SLACK_WEBHOOK", "").strip()
SLACK_VERBOSE = os.environ.get("SLACK_VERBOSE", "false").lower() in ("1","true","yes","on")
PHRASES_URL = os.environ.get("PHRASES_URL", "").strip()
POD_NAME = os.environ.get("POD_NAME", "tol-split-only")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TEMPERATURE = float(os.environ.get("OPENAI_TEMPERATURE", "0.2"))
OPENAI_MAX_TOKENS = int(os.environ.get("OPENAI_MAX_TOKENS", "120"))
OUTRO_A_URL = os.environ.get("OUTRO_A_URL", "").strip()
OUTRO_B_URL = os.environ.get("OUTRO_B_URL", "").strip()
OUTRO_MATCH_SAMPLE_RATE = int(os.environ.get("OUTRO_MATCH_SAMPLE_RATE", "1000"))
OUTRO_MATCH_MIN_SCORE = float(os.environ.get("OUTRO_MATCH_MIN_SCORE", "0.35"))

def _slack_is_important(message: str) -> bool:
    msg = (message or "").lower()
    return any(tok in msg for tok in (":x:", ":warning:", "[error]", "error:", "failed"))

def post_to_slack(message: str, webhook_override: Optional[str] = None, force: bool = False):
    if not (force or SLACK_VERBOSE or _slack_is_important(message)):
        return
    url = (webhook_override or SLACK_WEBHOOK_ENV or "").strip()
    if not url:
        return
    try:
        msg = f"[{POD_NAME}] {message}"
        requests.post(url, json={"text": msg}, timeout=5)
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
    s = s.replace("—", "-").replace("–", "-").replace("’", "'").replace("“", '"').replace("”", '"').replace("|", " ")
    s = s.lower().strip()
    s = s.translate(str.maketrans('', '', string.punctuation))
    s = re.sub(r"\s+", " ", s)
    return s

def partial_match(norm_phrase: str, norm_text: str, num_words: int = 5) -> bool:
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
        ts = ts.replace(',', '.')
        h, m, s = ts.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)

    parsed: List[Tuple[float, float, str]] = []

    pat_num = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*:\s*(.+)$')
    pat_vtt_inline = re.compile(r'^\s*(\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s*-->\s*(\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)(?:\s*\|\s*|\s+)?(.*)$')
    pat_block_ts = re.compile(r'^\s*(\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s*-->\s*(\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)(?:.*)$')
    pat_bracket = re.compile(r'^\s*\[?(\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\]?\s+(.+)$')

    lines = text.splitlines()
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue
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

# --- VTT writer from FW JSON segments ---
def _fmt_ts(t: float) -> str:
    if t < 0: t = 0.0
    h = int(t // 3600); m = int((t % 3600) // 60); s = t - (h*3600 + m*60)
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")

def write_vtt_from_segments(segments, out_path, window_start: float, window_end: float, shift_to_zero: bool=True):
    """Clip FW segments to [window_start, window_end] and write WebVTT.
    The cues are shifted so window_start -> 00:00.
    """
    cues = []
    for s in segments or []:
        try:
            st = float(s.get("start")); en = float(s.get("end")); tx = (s.get("text") or "").strip()
        except Exception:
            continue
        if en <= window_start or st >= window_end:
            continue
        st = max(st, window_start); en = min(en, window_end)
        if shift_to_zero:
            st -= window_start; en -= window_start
        if en - st <= 0.05:
            continue
        cues.append((st, en, tx))
    if not cues:
        return False
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for idx, (st, en, tx) in enumerate(cues, 1):
            f.write(f"{idx}\n{_fmt_ts(st)} --> {_fmt_ts(en)}\n{tx}\n\n")
    return True

# -------------------------------------------------------------------
# Zero-based transcript helpers
# -------------------------------------------------------------------
def _fmt_ts_dot_ms(t: float) -> str:
    """Format seconds to HH:MM:SS.mmm with dot milliseconds (for timestamped.txt)."""
    if t < 0: t = 0.0
    h = int(t // 3600); m = int((t % 3600) // 60); s = t - (h*3600 + m*60)
    return f"{h:02d}:{m:02d}:{s:06.3f}"

def clip_segments(segments, start_s: float, end_s: float):
    """Return list of segments overlapping [start_s, end_s], trimmed to the window."""
    out = []
    for s in segments or []:
        try:
            st = float(s.get("start")); en = float(s.get("end"))
        except Exception:
            continue
        if en <= start_s or st >= end_s:
            continue
        st2 = max(st, start_s); en2 = min(en, end_s)
        if en2 - st2 <= 0.01:
            continue
        seg_copy = dict(s)
        seg_copy["start"] = st2
        seg_copy["end"] = en2
        seg_copy["text"] = (seg_copy.get("text") or "").strip()
        out.append(seg_copy)
    return out

def write_section_transcripts_zeroed(section_dir: str, window_start: float, window_end: float,
                                     full_segments_json, full_meta=None):
    """
    Writes three zero-based files into <section_dir>/transcripts/:
      - transcript.json  (segments shifted so window_start -> 0)
      - transcript.txt   (plain text)
      - timestamped.txt  (00:00:00.000 --> ... with dot millis, zero-based)
    Returns dict of local paths.
    """
    if not full_segments_json:
        return {}
    os.makedirs(os.path.join(section_dir, "transcripts"), exist_ok=True)
    tdir = os.path.join(section_dir, "transcripts")

    # Clip to window, then shift to zero
    clipped = clip_segments(full_segments_json, window_start, window_end)
    zeroed = []
    for seg in clipped:
        z = dict(seg)
        z["start"] = float(seg["start"]) - window_start
        z["end"] = float(seg["end"]) - window_start
        z["text"] = (z.get("text") or "").strip()
        zeroed.append(z)

    # transcript.json
    json_obj = {"segments": zeroed}
    if isinstance(full_meta, dict):
        for k in ("detected_language", "device", "model"):
            if k in full_meta:
                json_obj[k] = full_meta[k]
    json_path = os.path.join(tdir, "transcript.json")
    with open(json_path, "w", encoding="utf-8") as jf:
        json.dump(json_obj, jf, ensure_ascii=False, indent=2)

    # transcript.txt
    txt_concat = " ".join([seg.get("text","").strip() for seg in zeroed if seg.get("text")]).strip()
    txt_path = os.path.join(tdir, "transcript.txt")
    with open(txt_path, "w", encoding="utf-8") as tf:
        tf.write(txt_concat + ("\n" if txt_concat else ""))

    # timestamped.txt (zero-based, dot milliseconds)
    ts_path = os.path.join(tdir, "timestamped.txt")
    with open(ts_path, "w", encoding="utf-8") as sf:
        for seg in zeroed:
            st = float(seg["start"]); en = float(seg["end"])
            line = f"{_fmt_ts_dot_ms(st)} --> {_fmt_ts_dot_ms(en)} | {seg.get('text','').strip()}"
            sf.write(line + "\n")

    return {"json": json_path, "txt": txt_path, "timestamped": ts_path}

def upload_transcripts_for_section(s3_conf: dict, mp4_key: str, local_paths: dict):
    """Upload transcripts under <dir of mp4>/transcripts/ with proper content-types."""
    if not (s3_conf and s3_conf.get("bucket") and mp4_key and local_paths):
        return {}
    bucket = s3_conf["bucket"]
    region = s3_conf.get("region")
    base_dir = os.path.dirname(mp4_key)  # e.g., splits/announcements
    tdir_key = base_dir + "/transcripts"

    def _put(local_path, rel_name, ctype):
        if not (local_path and os.path.exists(local_path) and os.path.getsize(local_path) > 0):
            return None
        key = f"{tdir_key}/{rel_name}"
        upload_s3(bucket, key, local_path, region, content_type=ctype)
        return f"s3://{bucket}/{key}"

    return {
        "json": _put(local_paths.get("json"), "transcript.json", "application/json"),
        "txt": _put(local_paths.get("txt"), "transcript.txt", "text/plain"),
        "timestamped": _put(local_paths.get("timestamped"), "timestamped.txt", "text/plain"),
    }

# -------------------------------------------------------------------
# JSON transcript (Whisper/Faster-Whisper) parsing
# -------------------------------------------------------------------
def parse_whisper_json_str(s: str) -> List[Tuple[float, float, str]]:
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
# Verse extraction
# -------------------------------------------------------------------
_BIBLE_BOOKS = [
    "Genesis","Exodus","Leviticus","Numbers","Deuteronomy",
    "Joshua","Judges","Ruth","1 Samuel","2 Samuel","1 Kings","2 Kings",
    "1 Chronicles","2 Chronicles","Ezra","Nehemiah","Esther","Job",
    "Psalms","Proverbs","Ecclesiastes","Song of Solomon","Isaiah","Jeremiah",
    "Lamentations","Ezekiel","Daniel","Hosea","Joel","Amos","Obadiah","Jonah",
    "Micah","Nahum","Habakkuk","Zephaniah","Haggai","Zechariah","Malachi",
    "Matthew","Mark","Luke","John","Acts","Romans","1 Corinthians","2 Corinthians",
    "Galatians","Ephesians","Philippians","Colossians","1 Thessalonians","2 Thessalonians",
    "1 Timothy","2 Timothy","Titus","Philemon","Hebrews","James","1 Peter","2 Peter",
    "1 John","2 John","3 John","Jude","Revelation"
]

_BOOK_ALT = [
    "Gen","Ex","Lev","Num","Deut","Josh","Judg","Ruth","1 Sam","2 Sam","1 Kgs","2 Kgs",
    "1 Chr","2 Chr","Ezra","Neh","Esth","Job","Ps","Prov","Eccl","Song","Isa","Jer",
    "Lam","Ezek","Dan","Hos","Joel","Amos","Obad","Jonah","Mic","Nah","Hab","Zeph",
    "Hag","Zech","Mal","Matt","Mk","Lk","Jn","Rom","1 Cor","2 Cor","Gal","Eph","Phil",
    "Col","1 Thess","2 Thess","1 Tim","2 Tim","Tit","Phlm","Heb","Jas","1 Pet","2 Pet",
    "1 Jn","2 Jn","3 Jn","Jude","Rev"
]

_BOOK_PATTERN = "|".join([re.escape(b) for b in (_BIBLE_BOOKS + _BOOK_ALT)])
_VERSE_RE = re.compile(
    rf"\b(?:{_BOOK_PATTERN})\s+\d{{1,3}}(?:[:.]\d{{1,3}}(?:-\d{{1,3}})?)?\b",
    re.IGNORECASE
)

def extract_verse_refs(text: str):
    if not text:
        return []
    seen = set()
    out = []
    for m in _VERSE_RE.finditer(text):
        ref = re.sub(r"\s+", " ", m.group(0)).strip()
        key = ref.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
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
# def http_get_to(path: str, url: str):
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
    if duration <= 0.10:
        log_and_slack(f"[SKIP] {out} duration too small ({duration:.2f}s)", webhook_override)
        return False
    return ffmpeg_trim_copy(src, out, start, duration, webhook_override)

def upload_put(put_url: str, file_path: str) -> str:
    with open(file_path, "rb") as f:
        r = requests.put(put_url, data=f, timeout=300)
        r.raise_for_status()
    return put_url

def upload_s3(bucket: str, key: str, file_path: str, region: Optional[str] = None,
              content_type: Optional[str] = None) -> str:
    import boto3, mimetypes
    s3 = boto3.client("s3", region_name=region)
    if not content_type:
        guessed = mimetypes.guess_type(key)[0]
        content_type = guessed or "application/octet-stream"
    s3.upload_file(file_path, bucket, key, ExtraArgs={"ContentType": content_type})
    return f"s3://{bucket}/{key}"

# -------------------------------------------------------------------
# Outro matching
# -------------------------------------------------------------------
def _download_to_local(url: str, dest_path: str, tmp_root: str):
    if url.startswith("s3://"):
        _, _, rest = url.partition("s3://")
        bucket, _, key = rest.partition("/")
        import boto3
        boto3.client("s3").download_file(bucket, key, dest_path)
        return dest_path
    download_url_parallel(
        url=url,
        dest_path=dest_path,
        chunk_bytes=4 * 1024 * 1024,
        concurrency=4,
        progress_cb=None,
        workdir_preferred=tmp_root
    )
    return dest_path

def _extract_audio_raw(input_path: str, raw_path: str, sample_rate: int):
    cmd = [
        "ffmpeg","-hide_banner","-loglevel","error","-y",
        "-i", input_path,
        "-vn","-ac","1","-ar", str(sample_rate),
        "-f","s16le", raw_path
    ]
    subprocess.check_call(cmd)

def _match_outro_offset(full_raw: str, outro_raw: str, sample_rate: int, min_idx: int):
    import numpy as np
    x = np.fromfile(full_raw, dtype=np.int16).astype(np.float32)
    y = np.fromfile(outro_raw, dtype=np.int16).astype(np.float32)
    if len(y) == 0 or len(x) == 0 or len(y) > len(x):
        return None, None

    x = x - x.mean()
    y = y - y.mean()
    y_rev = y[::-1]

    n = 1 << int((len(x) + len(y) - 1).bit_length())
    X = np.fft.rfft(x, n)
    Y = np.fft.rfft(y_rev, n)
    corr = np.fft.irfft(X * Y, n)
    corr = corr[len(y)-1:len(x)]

    win = np.ones(len(y), dtype=np.float32)
    energy = np.convolve(x * x, win, mode="valid")
    denom = np.sqrt(energy * np.sum(y * y)) + 1e-8
    score = corr / denom

    if min_idx > 0 and min_idx < len(score):
        score[:min_idx] = -1.0

    idx = int(np.argmax(score))
    best = float(score[idx])
    return idx / float(sample_rate), best

# -------------------------------------------------------------------
# Phrase overrides
# -------------------------------------------------------------------
def load_phrases_override(phrases_url: Optional[str]) -> Optional[dict]:
    if not phrases_url:
        return None
    try:
        r = requests.get(phrases_url, timeout=30)
        r.raise_for_status()
        obj = r.json()
        if isinstance(obj, dict):
            return obj
    except Exception as e:
        print(f"[PHRASES] Failed to load phrases_url: {e}")
    return None

# -------------------------------------------------------------------
# LLM helpers
# -------------------------------------------------------------------
def _openai_chat(prompt: str) -> Optional[str]:
    if not OPENAI_API_KEY:
        return None
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": OPENAI_MODEL,
        "temperature": OPENAI_TEMPERATURE,
        "max_tokens": OPENAI_MAX_TOKENS,
        "messages": [
            {
                "role": "system",
                "content": "You write concise, factual summaries for sermon content."
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            return None
        return (choices[0].get("message") or {}).get("content")
    except Exception as e:
        print(f"[LLM] OpenAI request failed: {e}")
        return None

def generate_sermon_goals(text: str) -> Optional[str]:
    if not text:
        return None
    prompt = (
        "Read the sermon transcript and write exactly two sentences that explain the goals "
        "of the sermon. Be concise and use plain language.\n\n"
        f"Transcript:\n{text[:8000]}"
    )
    return _openai_chat(prompt)

# -------------------------------------------------------------------
# Main handler
# -------------------------------------------------------------------
def handler(event):
    # env & node basics
    try: log_env_basics()
    except Exception as _e: print(f"[ENV] log error: {_e}")
    inp = (event or {}).get("input") or {}
    job_id = inp.get("job_id")
    transcript_url = inp.get("transcript_url")
    raw_video_url = inp.get("raw_video_url")
    slack_webhook = inp.get("slack_webhook") or None
    phrases_url = (inp.get("phrases_url") or PHRASES_URL or "").strip()
    phrases = load_phrases_override(phrases_url) or DEFAULT_PHRASES
    outro_a_url = (inp.get("outro_a_url") or OUTRO_A_URL or "").strip()
    outro_b_url = (inp.get("outro_b_url") or OUTRO_B_URL or "").strip()
    outro_min_score = float(inp.get("outro_min_score") or OUTRO_MATCH_MIN_SCORE)
    outro_sample_rate = int(inp.get("outro_sample_rate") or OUTRO_MATCH_SAMPLE_RATE)

    debug_mode = bool(inp.get("debug", True) or os.environ.get("DEBUG_SPLITTER"))

    if not job_id:
        return {"error": "missing_job_id"}
    if not transcript_url or not str(transcript_url).startswith("http"):
        return {"error": f"transcript_url missing/invalid: {transcript_url}"}
    if not raw_video_url or not str(raw_video_url).startswith("http"):
        return {"error": f"raw_video_url missing/invalid: {raw_video_url}"}

    try:
        tmp_root = os.environ.get("TMPDIR") or "/tmp"
        os.makedirs(tmp_root, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as td:
            video_path = os.path.join(td, "raw.mp4")

            t_ext = os.path.splitext(urlparse(transcript_url).path)[1].lower() or ".txt"
            if t_ext not in {".json", ".txt", ".vtt", ".srt"}:
                t_ext = ".txt"
            transcript_path = os.path.join(td, f"transcript{t_ext}")

            print(f"[INPUT] job_id={job_id}")
            print(f"[INPUT] transcript_url={transcript_url}")
            print(f"[INPUT] raw_video_url={raw_video_url}")

            progress = default_progress_logger(slack_fn=(lambda m: post_to_slack(m, slack_webhook)) if SLACK_VERBOSE else None)
            download_url_parallel(
                url=raw_video_url,
                dest_path=video_path,
                chunk_bytes=16777216,
                concurrency=16,
                progress_cb=progress,
                workdir_preferred=tmp_root
            )
            download_url_parallel(
                url=transcript_url,
                dest_path=transcript_path,
                chunk_bytes=4194304,
                concurrency=8,
                progress_cb=None,
                workdir_preferred=tmp_root
            )

            print(f"[FILES] video_path={video_path}")
            print(f"[FILES] transcript_path={transcript_path}")

            if debug_mode:
                debug_file_head(transcript_path, max_lines=int(inp.get("debug_head_lines") or 12))

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

            dur = ffprobe_duration(video_path)
            if dur is None:
                return {"error": "video_duration_unknown"}
            print(f"[VIDEO] duration={dur:.3f}s")

            # === compute boundaries (phrases) ===
            worship_start = find_phrase_time(
                parsed, phrases["worship_start"], which="start",
                return_offset=15, fallback=0.0, webhook_override=slack_webhook, debug=debug_mode
            )
            if worship_start is None:
                worship_start = 0.0

            worship_end = find_phrase_time(
                parsed, phrases["worship_end"], which="start",
                fallback=None, webhook_override=slack_webhook, debug=debug_mode
            )
            if worship_end is None:
                worship_end = find_phrase_time(
                    parsed, phrases["sermon_split"], which="start",
                    fallback=None, webhook_override=slack_webhook, debug=debug_mode
                )
                if worship_end is None:
                    worship_end = dur

            announcements_end = find_phrase_time(
                parsed, phrases["sermon_split"], which="start",
                fallback=None, webhook_override=slack_webhook, debug=debug_mode
            )
            if announcements_end is None:
                announcements_end = dur

            # === optional outro match to set announcements_end ===
            outro_used = None
            outro_score = None
            outro_start = None
            outro_dur = None
            if (outro_a_url or outro_b_url) and dur is not None:
                try:
                    full_raw = os.path.join(td, "full_audio.s16le")
                    _extract_audio_raw(video_path, full_raw, outro_sample_rate)
                    min_idx = int(max(0.0, worship_end) * outro_sample_rate)

                    best = {"score": -1.0, "url": None, "start": None, "dur": None}
                    for label, url in (("A", outro_a_url), ("B", outro_b_url)):
                        if not url:
                            post_to_slack(f"⏭️ [OUTRO] skip {label} (url missing)", slack_webhook)
                            continue
                        local_outro = os.path.join(td, f"outro_{label}.mp4")
                        _download_to_local(url, local_outro, tmp_root)
                        outro_raw = os.path.join(td, f"outro_{label}.s16le")
                        _extract_audio_raw(local_outro, outro_raw, outro_sample_rate)
                        offset, score = _match_outro_offset(full_raw, outro_raw, outro_sample_rate, min_idx)
                        odur = ffprobe_duration(local_outro)
                        post_to_slack(
                            f"🎧 [OUTRO] tried {label} score={score if score is not None else 'n/a'} "
                            f"start={offset if offset is not None else 'n/a'} dur={odur if odur is not None else 'n/a'}",
                            slack_webhook
                        )
                        if offset is not None and score is not None:
                            if score > best["score"]:
                                best = {"score": score, "url": url, "start": offset, "dur": odur}
                    if best["url"] and best["score"] >= outro_min_score and best["dur"]:
                        outro_used = best["url"]
                        outro_score = best["score"]
                        outro_start = best["start"]
                        outro_dur = best["dur"]
                        announcements_end = min(dur, outro_start + outro_dur)
                        print(f"[OUTRO] matched {outro_used} score={outro_score:.3f} start={outro_start:.2f} dur={outro_dur:.2f}")
                except Exception as e:
                    print(f"[OUTRO] match failed: {e}")
                    post_to_slack(f"⚠️ [OUTRO] match failed: {e}", slack_webhook)

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
            method = "DEFAULT_WORDS"
            if outro_used:
                if outro_a_url and outro_used == outro_a_url:
                    method = "OUTRO_A"
                elif outro_b_url and outro_used == outro_b_url:
                    method = "OUTRO_B"
                else:
                    method = "OUTRO"
            elif phrases_url:
                method = "JSON_WORDS"
            method_emoji = {
                "OUTRO_A": "🎬🅰️",
                "OUTRO_B": "🎬🅱️",
                "OUTRO": "🎬",
                "JSON_WORDS": "🧾",
                "DEFAULT_WORDS": "🧩"
            }.get(method, "🧩")
            post_to_slack(
                f"{method_emoji} [SPLIT_METHOD] method={method} worship_start={worship_start:.2f}s "
                f"worship_end={worship_end:.2f}s announcements_end={announcements_end:.2f}s "
                f"outro_score={outro_score if outro_score is not None else 'n/a'}",
                slack_webhook,
                force=True
            )
            print(f"[SEGS] pre={seg_pre:.2f}s, worship={seg_worship:.2f}s, ann={seg_ann:.2f}s, sermon={seg_sermon:.2f}s")

            # === outputs ===
            pre_p = os.path.join(td, "pre_worship_trimmed.mp4")
            worship_p = os.path.join(td, "worship_trimmed.mp4")
            ann_p = os.path.join(td, "announcements_trimmed.mp4")
            sermon_p = os.path.join(td, "sermon_trimmed.mp4")

            ok_pre = safe_trim(video_path, pre_p, 0.0, seg_pre, slack_webhook)
            ok_worship = safe_trim(video_path, worship_p, worship_start, seg_worship, slack_webhook)
            ok_ann = safe_trim(video_path, ann_p, worship_end, seg_ann, slack_webhook)
            ok_sermon = safe_trim(video_path, sermon_p, announcements_end, seg_sermon, slack_webhook)

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
                    uri = upload_s3(
                        s3_inp["bucket"],
                        s3_inp["keys"][s3key_name],
                        file_path,
                        s3_inp.get("region"),
                        content_type="video/mp4"
                    )
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

            # --- Load segments JSON + optional meta for per-section transcripts ---
            segments_json = None
            top_meta = None
            try:
                with open(transcript_path, "r", encoding="utf-8", errors="ignore") as _tf:
                    _maybe = json.load(_tf)
                    if isinstance(_maybe, dict) and isinstance(_maybe.get("segments"), list):
                        segments_json = _maybe["segments"]
                        top_meta = {k: _maybe.get(k) for k in ("detected_language","device","model") if k in _maybe}
            except Exception:
                segments_json = None
                top_meta = None

            # --- Per-section transcripts & VTTs (ALL zero-based and placed in transcripts/) ---
            transcript_urls = {}
            vtt_urls = {}
            bounds_urls = {}
            verses_urls = {}
            goals_urls = {}

            def _section_dir_from_mp4(path_mp4: str) -> Optional[str]:
                return os.path.dirname(path_mp4) if path_mp4 else None

            def _write_bounds_json_local(path_mp4: str, section: str, start_s: float, end_s: float):
                sec_dir = _section_dir_from_mp4(path_mp4)
                if not sec_dir:
                    return None
                os.makedirs(sec_dir, exist_ok=True)
                out_path = os.path.join(sec_dir, "timestamps.json")
                payload = {
                    "section": section,
                    "start": float(start_s),
                    "end": float(end_s),
                    "duration": max(0.0, float(end_s) - float(start_s))
                }
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                return out_path

            def _upload_bounds_json(s3_conf: dict, mp4_key: str, local_json: Optional[str]):
                if not (s3_conf and s3_conf.get("bucket") and mp4_key and local_json and os.path.exists(local_json)):
                    return None
                bucket = s3_conf["bucket"]
                region = s3_conf.get("region")
                base_dir = os.path.dirname(mp4_key)
                json_key = f"{base_dir}/timestamps.json"
                upload_s3(bucket, json_key, local_json, region, content_type="application/json")
                print(f"[UPLOAD] S3 timestamps: s3://{bucket}/{json_key}")
                return f"s3://{bucket}/{json_key}"

            def _mk_vtt_local(path_mp4, start_s, end_s):
                """Write a zero-based VTT under <mp4 dir>/transcripts/captions.vtt"""
                if not segments_json:
                    return None
                sec_dir = _section_dir_from_mp4(path_mp4)
                if not sec_dir:
                    return None
                tdir = os.path.join(sec_dir, "transcripts")
                os.makedirs(tdir, exist_ok=True)
                vtt_path = os.path.join(tdir, "captions.vtt")
                ok_vtt = write_vtt_from_segments(segments_json, vtt_path, start_s, end_s, shift_to_zero=True)
                return vtt_path if ok_vtt else None

            def _upload_vtt_for_section(s3_conf: dict, mp4_key: str, local_vtt: Optional[str]):
                """Upload transcripts/captions.vtt with text/vtt content type."""
                if not (s3_conf and s3_conf.get("bucket") and mp4_key and local_vtt and os.path.exists(local_vtt)):
                    return None
                bucket = s3_conf["bucket"]
                region = s3_conf.get("region")
                base_dir = os.path.dirname(mp4_key)
                vtt_key = f"{base_dir}/transcripts/captions.vtt"
                upload_s3(bucket, vtt_key, local_vtt, region, content_type="text/vtt")
                print(f"[UPLOAD] S3 VTT: s3://{bucket}/{vtt_key}")
                return f"s3://{bucket}/{vtt_key}"

            def _write_verses_local(txt_path: Optional[str]):
                if not (txt_path and os.path.exists(txt_path)):
                    return None
                with open(txt_path, "r", encoding="utf-8", errors="ignore") as tf:
                    text = tf.read()
                refs = extract_verse_refs(text)
                payload = {"count": len(refs), "references": refs}
                vdir = os.path.dirname(txt_path)
                out_path = os.path.join(vdir, "verses.json")
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                return out_path

            def _upload_verses(s3_conf: dict, mp4_key: str, local_json: Optional[str]):
                if not (s3_conf and s3_conf.get("bucket") and mp4_key and local_json and os.path.exists(local_json)):
                    return None
                bucket = s3_conf["bucket"]
                region = s3_conf.get("region")
                base_dir = os.path.dirname(mp4_key)
                json_key = f"{base_dir}/transcripts/verses.json"
                upload_s3(bucket, json_key, local_json, region, content_type="application/json")
                print(f"[UPLOAD] S3 verses: s3://{bucket}/{json_key}")
                return f"s3://{bucket}/{json_key}"

            def _write_goals_local(txt_path: Optional[str]):
                if not (txt_path and os.path.exists(txt_path)):
                    return None
                with open(txt_path, "r", encoding="utf-8", errors="ignore") as tf:
                    text = tf.read()
                goals = generate_sermon_goals(text)
                if not goals:
                    return None
                payload = {
                    "goals": goals.strip()
                }
                vdir = os.path.dirname(txt_path)
                out_path = os.path.join(vdir, "sermon_goals.json")
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                return out_path

            def _upload_goals(s3_conf: dict, mp4_key: str, local_json: Optional[str]):
                if not (s3_conf and s3_conf.get("bucket") and mp4_key and local_json and os.path.exists(local_json)):
                    return None
                bucket = s3_conf["bucket"]
                region = s3_conf.get("region")
                base_dir = os.path.dirname(mp4_key)
                json_key = f"{base_dir}/transcripts/sermon_goals.json"
                upload_s3(bucket, json_key, local_json, region, content_type="application/json")
                print(f"[UPLOAD] S3 sermon goals: s3://{bucket}/{json_key}")
                return f"s3://{bucket}/{json_key}"

            def _section_all(section_name: str, path_mp4: str, start_s: float, end_s: float, s3key_name: str):
                # Write transcripts (zero-based) locally
                if segments_json:
                    sec_dir = _section_dir_from_mp4(path_mp4)
                    if sec_dir:
                        local_paths = write_section_transcripts_zeroed(sec_dir, start_s, end_s, segments_json, full_meta=top_meta)
                        if inp.get("s3"):
                            s3_inp = inp["s3"]
                            mp4_key = s3_inp.get("keys", {}).get(s3key_name)
                            if mp4_key:
                                up_uris = upload_transcripts_for_section(s3_inp, mp4_key, local_paths)
                                transcript_urls[section_name] = up_uris
                                if section_name == "sermon":
                                    verses_local = _write_verses_local(local_paths.get("txt"))
                                    verses_uri = _upload_verses(s3_inp, mp4_key, verses_local)
                                    if verses_uri:
                                        verses_urls["sermon_verses"] = verses_uri
                                    goals_local = _write_goals_local(local_paths.get("txt"))
                                    goals_uri = _upload_goals(s3_inp, mp4_key, goals_local)
                                    if goals_uri:
                                        goals_urls["sermon_goals"] = goals_uri
                # Write & upload VTT into transcripts/
                local_vtt = _mk_vtt_local(path_mp4, start_s, end_s)
                if local_vtt and inp.get("s3"):
                    s3_inp = inp["s3"]
                    mp4_key = s3_inp.get("keys", {}).get(s3key_name)
                    if mp4_key:
                        vtt_uri = _upload_vtt_for_section(s3_inp, mp4_key, local_vtt)
                        vtt_urls[f"{section_name}_vtt"] = vtt_uri
                # Write & upload timestamps.json alongside mp4
                local_bounds = _write_bounds_json_local(path_mp4, section_name, start_s, end_s)
                if local_bounds and inp.get("s3"):
                    s3_inp = inp["s3"]
                    mp4_key = s3_inp.get("keys", {}).get(s3key_name)
                    if mp4_key:
                        bounds_uri = _upload_bounds_json(s3_inp, mp4_key, local_bounds)
                        bounds_urls[f"{section_name}_timestamps"] = bounds_uri

            if seg_pre > 0.10 and ok_pre:
                _section_all("pre", pre_p, 0.0, seg_pre, "pre")
            if ok_worship:
                _section_all("worship", worship_p, worship_start, worship_end, "worship")
            if ok_ann:
                _section_all("announcements", ann_p, worship_end, announcements_end, "ann")
            if ok_sermon:
                _section_all("sermon", sermon_p, announcements_end, dur, "sermon")

            return {
                "ok": True,
                "urls": urls,
                "vtts": vtt_urls,
                "timestamps": bounds_urls,
                "verses": verses_urls,
                "sermon_goals": goals_urls,
                "transcripts": transcript_urls,
                "bounds": {
                    "duration": dur,
                    "worship_start": worship_start,
                    "worship_end": worship_end,
                    "announcements_end": announcements_end
                },
                "notes": "ffmpeg -c copy trims; per-section zero-based transcripts & VTT"
            }

    except requests.RequestException as e:
        return {"error": "network_error", "details": str(e)}
    except subprocess.CalledProcessError as e:
        return {"error": "ffmpeg_error", "details": str(e)}
    except Exception as e:
        return {"error": "unexpected_error", "details": str(e)}

# ==== inserted diagnostics & overrides ====
def human_bytes(n: int) -> str:
    if n is None:
        return "?"
    if n < 1024: return f"{n} B"
    for unit in ["KB","MB","GB","TB","PB"]:
        n /= 1024.0
        if n < 1024.0:
            return f"{n:.2f} {unit}"
    return f"{n:.2f} EB"

def log_env_basics():
    try:
        total_root, used_root, free_root = shutil.disk_usage("/")
        total_tmp, used_tmp, free_tmp   = shutil.disk_usage("/tmp")
        print(f"[ENV] cores={os.cpu_count()} py={platform.python_version()} "
              f"disk(/) free={free_root/1e9:.2f}GB disk(/tmp) free={free_tmp/1e9:.2f}GB")
    except Exception as e:
        print(f"[ENV] info error: {e}")

# Verbose downloader with Slack progress every ~60s.
# def http_get_to(path: str, url: str, webhook: Optional[str] = None):
    import requests
    print(f"[FETCH] GET {url}")
    t0 = time.time()
    last_console = t0
    last_slack = t0
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        size_hdr = int(r.headers.get("Content-Length") or 0)
        ctype = r.headers.get("Content-Type")
        print(f"[FETCH] -> {path} (CL={human_bytes(size_hdr)}; {ctype or 'unknown MIME'})")
        total = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                total += len(chunk)
                now = time.time()
                if now - last_console >= 5:
                    mbps = (total/1048576.0) / max(1e-6, now - t0)
                    print(f"[FETCH] progress {human_bytes(total)} in {now - t0:.1f}s ({mbps:.2f} MB/s)")
                    last_console = now
                if webhook and now - last_slack >= 60:
                    try:
                        mbps = (total/1048576.0) / max(1e-6, now - t0)
                        post_to_slack(f":arrow_down: Downloading… {human_bytes(total)} so far @ {mbps:.2f} MB/s", webhook)
                    except Exception:
                        pass
                    last_slack = now
    st = os.stat(path)
    elapsed = time.time() - t0
    mbps = (st.st_size/1048576.0) / max(1e-6, elapsed)
    print(f"[FETCH] Saved {path} ({human_bytes(st.st_size)} in {elapsed:.1f}s @ {mbps:.2f} MB/s)")
    if size_hdr and st.st_size != size_hdr:
        print(f"[WARN] Size mismatch: wrote {st.st_size} vs header {size_hdr}")

# ffprobe with deep JSON dump when duration fails.
def ffprobe_duration(path: str):
    try:
        out = subprocess.check_output(
            ["ffprobe","-v","error","-show_entries","format=duration","-of","default=nw=1:nk=1", path],
            stderr=subprocess.STDOUT
        )
        return float(out.strip())
    except Exception as e:
        print(f"[WARN] ffprobe failed (duration): {e}")
        try:
            probe = subprocess.check_output(
                ["ffprobe","-v","warning","-show_streams","-show_format","-of","json", path],
                stderr=subprocess.STDOUT
            )
            s = probe.decode("utf-8", errors="ignore")
            print("[DEBUG] ffprobe json (trunc):", s[:4000])
        except Exception as e2:
            print(f"[WARN] ffprobe deep failed: {e2}")
        return None

# ffmpeg trim that captures stderr/stdout for root-cause.
def ffmpeg_trim_copy(src: str, out: str, start: float, duration: float,
                     webhook_override: Optional[str] = None):
    log_and_slack(f"[TRIM] {out} start={start:.2f} dur={duration:.2f}", webhook_override)
    if duration <= 0:
        log_and_slack(f"[ERROR] Invalid duration: {duration}", webhook_override)
        return False
    cmd = ["ffmpeg","-y","-hide_banner","-loglevel","warning","-nostdin",
           "-ss", str(start), "-i", src, "-t", str(duration),
           "-c","copy", out]
    try:
        res = subprocess.run(cmd, check=True, capture_output=True, text=True)
        if res.stdout: print("[FFMPEG:OUT]", res.stdout[:2000])
        if res.stderr: print("[FFMPEG:ERR]", res.stderr[:4000])
    except subprocess.CalledProcessError as e:
        print("[FFMPEG:CMD]", " ".join(cmd))
        print("[FFMPEG:RC]", e.returncode)
        if e.stdout: print("[FFMPEG:OUT]", e.stdout[:2000])
        if e.stderr: print("[FFMPEG:ERR]", e.stderr[:4000])
        log_and_slack(f"[ERROR] ffmpeg failed for {out}: rc={e.returncode}", webhook_override)
        return False
    ok = os.path.exists(out) and os.path.getsize(out) > 0
    if not ok:
        log_and_slack(f"[ERROR] Output missing/empty: {out}", webhook_override)
    else:
        log_and_slack(f"[OK] Wrote {out} ({os.path.getsize(out)} bytes)", webhook_override)
    return ok
# ==== end inserted ====
runpod.serverless.start({"handler": handler})
