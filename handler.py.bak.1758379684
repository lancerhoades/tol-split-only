import os, tempfile, shutil, logging
import runpod
import boto3
from botocore.client import Config
import requests

LOG_LEVEL = os.getenv("LOG_LEVEL","INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, 20), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("split-only")

AWS_REGION    = os.getenv("AWS_REGION", "us-east-1")
AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET")
S3_PREFIX_BASE= os.getenv("S3_PREFIX_BASE","jobs")

if not AWS_S3_BUCKET:
    raise RuntimeError("AWS_S3_BUCKET must be set for S3-only split step.")

_s3 = boto3.client("s3", region_name=AWS_REGION, config=Config(s3={"addressing_style":"virtual"}))

def _s3_key(job_id: str, *parts: str) -> str:
    safe = [p.strip("/").replace("\\","/") for p in parts if p]
    return "/".join([S3_PREFIX_BASE.strip("/"), job_id] + safe)

def s3_put(local_path: str, job_id: str, subdir: str, filename: str|None=None, expires:int=604800) -> dict:
    base = filename or os.path.basename(local_path)
    key = _s3_key(job_id, subdir, base)
    _s3.upload_file(local_path, AWS_S3_BUCKET, key)
    url = _s3.generate_presigned_url("get_object", Params={"Bucket": AWS_S3_BUCKET, "Key": key}, ExpiresIn=expires)
    return {"s3_uri": f"s3://{AWS_S3_BUCKET}/{key}", "key": key, "url": url}

def download_to(path: str, url: str):
    with requests.get(url, stream=True, timeout=None) as r:
        r.raise_for_status()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path,"wb") as f:
            for chunk in r.iter_content(chunk_size=1<<20):
                if chunk:
                    f.write(chunk)

def handler(event):
    # Inputs
    inp = (event or {}).get("input") or {}
    job_id = inp.get("job_id")
    tr_path = inp.get("transcript_path")  # legacy, optional
    tr_url  = inp.get("transcript_url")   # S3/HTTP URL, optional
    split_phrase = (inp.get("split_phrase") or "sermon").strip() or "sermon"
    raw_video_url = inp.get("raw_video_url")

    if not job_id:
        return {"error": "missing job_id"}
    if not raw_video_url or not isinstance(raw_video_url,str) or not raw_video_url.startswith(("http://","https://")):
        return {"error": f"raw_video_url missing/invalid: {raw_video_url}"}

    # Work in /tmp only
    work = os.path.join("/tmp", job_id)
    raw_local = os.path.join(work, "raw.mp4")
    out_dir = os.path.join(work, "splits")
    os.makedirs(out_dir, exist_ok=True)
    out_mp4 = os.path.join(out_dir, f"{split_phrase}.mp4")

    # Download raw video (S3 presigned or HTTP)
    log.info(f"[{job_id}] downloading raw video for split: {raw_video_url[:80]}...")
    download_to(raw_local, raw_video_url)

    # TEMP split: copy full -> sermon.mp4 (placeholder)
    shutil.copy2(raw_local, out_mp4)

    # Upload result to S3
    up = s3_put(out_mp4, job_id, "splits", filename=f"{split_phrase}.mp4")

    return {
        "ok": True,
        "notes": "placeholder split-only: copied raw -> splits/{name}.mp4",
        "sermon_url": up["url"],
        "keys": {"sermon_key": up["key"]},
        "s3": {"sermon": up["s3_uri"]},
        "paths": {  # local (ephemeral), for backward-compat
            "video": out_mp4,
            "transcript": tr_path
        },
        "transcript_url": tr_url
    }

runpod.serverless.start({"handler": handler})
