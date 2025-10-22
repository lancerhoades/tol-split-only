# download_from_s3.py
import os, time, tempfile, threading, shutil, math, hashlib
from typing import Optional, Callable, Dict, Tuple
import requests

ProgressCB = Optional[Callable[[int, int, float], None]]  # (bytes_done, total_bytes, elapsed_s)

# -------------------------
# small helpers
# -------------------------
def human_bytes(n: int) -> str:
    if n < 1024: return f"{n} B"
    for u in ["KB","MB","GB","TB","PB","EB"]:
        n /= 1024.0
        if n < 1024: return f"{n:.2f} {u}"
    return f"{n:.2f} ZB"

def pick_workdir(preferred: Optional[str] = None) -> str:
    base = preferred or os.environ.get("TMPDIR") or "/tmp"
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix="dl_", dir=base)

def default_progress_logger(throttle_seconds: float = 5.0,
                            slack_fn: Optional[Callable[[str], None]] = None) -> ProgressCB:
    last = {"t": 0.0}
    def _cb(done: int, total: int, elapsed: float):
        now = time.time()
        if now - last["t"] < throttle_seconds: return
        last["t"] = now
        mbps = (done/1048576.0) / max(1e-6, elapsed)
        msg = f"[DL] {human_bytes(done)} / {human_bytes(total)} in {elapsed:.1f}s @ {mbps:.2f} MB/s"
        print(msg, flush=True)
        if slack_fn:
            try: slack_fn(msg)
            except: pass
    return _cb

def _sha256_file(path: str, block: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(block)
            if not b: break
            h.update(b)
    return h.hexdigest()

# -------------------------
# robust single-stream (fallback) with retries
# -------------------------
def _download_single(url: str, dest: str, progress_cb: ProgressCB, timeout: int):
    backoff = 1.0
    start = time.time()
    for attempt in range(6):
        try:
            with requests.get(url, stream=True, timeout=timeout) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1024 * 1024):
                        if not chunk: continue
                        f.write(chunk); done += len(chunk)
                        if progress_cb: progress_cb(done, total, time.time() - start)
            return
        except Exception as e:
            if attempt == 5: raise
            time.sleep(backoff); backoff = min(backoff * 2, 10)

# -------------------------
# public: fast parallel ranged download
# -------------------------
def download_url_parallel(
    url: str,
    dest_path: str,
    *,
    chunk_bytes: int = 16 * 1024 * 1024,  # 16MB
    concurrency: int = 16,
    progress_cb: ProgressCB = None,
    workdir_preferred: Optional[str] = None,
    timeout: int = 120,
    verify_sha256: Optional[str] = None,
) -> Dict[str, str]:
    """
    Download URL to dest_path using parallel HTTP Range requests when the server supports it.
    Falls back to robust single-stream with retries otherwise.

    Returns: {"path": dest_path, "bytes": "...", "sha256": "...", "ranged": "yes|no"}
    """
    t0 = time.time()
    # HEAD: get size + check ranges
    try:
        h = requests.head(url, timeout=timeout)
        h.raise_for_status()
    except Exception:
        # some CDNs don’t allow HEAD; try GET with range=0-0 to probe
        r = requests.get(url, headers={"Range": "bytes=0-0"}, timeout=timeout)
        r.raise_for_status()
        total = int(r.headers.get("Content-Range", "bytes 0-0/0").split("/")[-1])
        accept_ranges = True
    else:
        total = int(h.headers.get("Content-Length") or 0)
        accept_ranges = "bytes" in (h.headers.get("Accept-Ranges","").lower())

    if not total or not accept_ranges:
        print("[DL] Ranges not supported or size unknown; using single stream.")
        _download_single(url, dest_path, progress_cb, timeout)
        sha = _sha256_file(dest_path) if verify_sha256 or True else ""
        if verify_sha256 and sha != verify_sha256:
            raise ValueError(f"sha256 mismatch: got {sha}, expected {verify_sha256}")
        return {"path": dest_path, "bytes": str(os.path.getsize(dest_path)), "sha256": sha, "ranged": "no"}

    # plan ranges
    ranges = [(start, min(start + chunk_bytes, total) - 1) for start in range(0, total, chunk_bytes)]
    tmp_dir = pick_workdir(workdir_preferred)
    part_paths = [os.path.join(tmp_dir, f"part.{i:05d}") for i in range(len(ranges))]
    done = 0
    lock = threading.Lock()

    def fetch_part(i: int, start: int, end: int):
        nonlocal done
        backoff = 1.0
        for attempt in range(6):
            try:
                with requests.get(url, headers={"Range": f"bytes={start}-{end}"}, stream=True, timeout=timeout) as r:
                    if r.status_code not in (200, 206):
                        r.raise_for_status()
                    with open(part_paths[i], "wb") as f:
                        for chunk in r.iter_content(1024 * 1024):
                            if not chunk: continue
                            f.write(chunk)
                            if progress_cb:
                                with lock:
                                    done += len(chunk)
                                    progress_cb(done, total, time.time() - t0)
                return
            except Exception:
                if attempt == 5: raise
                time.sleep(backoff); backoff = min(backoff * 2, 10)

    # simple concurrency control
    threads = []
    for idx, (s, e) in enumerate(ranges):
        t = threading.Thread(target=fetch_part, args=(idx, s, e), daemon=True)
        threads.append(t); t.start()
        # throttle to 'concurrency' threads at a time
        if len(threads) >= concurrency:
            for tt in threads: tt.join()
            threads = []
    for tt in threads: tt.join()

    # stitch in order
    with open(dest_path, "wb") as out:
        for p in part_paths:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out)

    # cleanup
    for p in part_paths:
        try: os.remove(p)
        except: pass
    try: os.rmdir(tmp_dir)
    except: pass

    sha = _sha256_file(dest_path) if verify_sha256 or True else ""
    if verify_sha256 and sha != verify_sha256:
        raise ValueError(f"sha256 mismatch: got {sha}, expected {verify_sha256}")

    return {"path": dest_path, "bytes": str(os.path.getsize(dest_path)), "sha256": sha, "ranged": "yes"}

# -------------------------
# optional: native S3 via boto3
# -------------------------
def download_s3_boto3(
    bucket: str, key: str, dest_path: str, *,
    region: Optional[str] = None,
    multipart_threshold: int = 8 * 1024 * 1024,
    multipart_chunksize: int = 16 * 1024 * 1024,
    max_concurrency: int = 32,
):
    import boto3
    from boto3.s3.transfer import TransferConfig
    cfg = TransferConfig(
        multipart_threshold=multipart_threshold,
        multipart_chunksize=multipart_chunksize,
        max_concurrency=max_concurrency,
        use_threads=True,
    )
    boto3.client("s3", region_name=region).download_file(bucket, key, dest_path, Config=cfg)

def upload_presigned_put(put_url: str, file_path: str, *, timeout: int = 300) -> None:
    with open(file_path, "rb") as f:
        r = requests.put(put_url, data=f, timeout=timeout)
        r.raise_for_status()
