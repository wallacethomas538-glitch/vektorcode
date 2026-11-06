"""Model downloads for the local voice pipeline.

All models are open-licensed, run on local CPU, and need no account,
no API key, and no payment. Downloads go to ``voice_agent/models/``
(local, gitignored) — never into the production repo.

STT — streaming Zipformer (English), Apache-2.0:
  repo: csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26
  files: encoder/decoder/joiner *-chunk-16-left-128.int8.onnx,
         tokens.txt, bpe.model
  size: ~75 MB total (int8)

TTS — Kokoro-82M int8 (multi-lang, English voice), Apache-2.0
(LICENSE file in the repo):
  repo: csukuangfj/kokoro-int8-multi-lang-v1_1
  files: model.int8.onnx, voices.bin, tokens.txt,
         lexicon-us-en.txt, espeak-ng-data/ (whole directory),
         dict/ (whole directory)
  size: ~110 MB total (int8 model ~82 MB + data)

Usage:
  python -m voice_agent.models download-stt [dir]
  python -m voice_agent.models download-tts [dir]
  python -m voice_agent.models download-all [dir]
  python -m voice_agent.models check [dir]   # list what is present/missing

Default dir: voice_agent/models (next to this file).
"""
from __future__ import annotations

import os
import sys
import urllib.request

HF = "https://huggingface.co"

STT_REPO = "csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26"
STT_FILES = [
    "encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
    "decoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
    "joiner-epoch-99-avg-1-chunk-16-left-128.int8.onnx",
    "tokens.txt",
    "bpe.model",
]

TTS_REPO = "csukuangfj/kokoro-int8-multi-lang-v1_1"
TTS_FILES = [
    "model.int8.onnx",
    "voices.bin",
    "tokens.txt",
    "lexicon-us-en.txt",
]
# Whole directories (many small files); fetched file-by-file from the
# repo tree listing, so no git clone is needed.
TTS_DIRS = ["espeak-ng-data", "dict"]

DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")


def _download(url: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"  exists: {dest}")
        return
    print(f"  downloading: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "VektorCode-Voice/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as fh:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    print(f"  saved: {dest} ({os.path.getsize(dest) // 1024} KB)")


def _list_tree(repo: str, path: str) -> list[str]:
    """File paths under ``path`` in a Hugging Face repo (recursive)."""
    import json
    url = f"{HF}/api/models/{repo}/tree/main/{path}?recursive=true"
    req = urllib.request.Request(url, headers={"User-Agent": "VektorCode-Voice/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        entries = json.load(resp)
    return [e["path"] for e in entries if e.get("type") == "file"]


def download_stt(base: str = DEFAULT_DIR) -> str:
    dest_dir = os.path.join(base, "stt-zipformer-en")
    print(f"STT model -> {dest_dir}  (repo {STT_REPO}, Apache-2.0)")
    for name in STT_FILES:
        _download(f"{HF}/{STT_REPO}/resolve/main/{name}",
                  os.path.join(dest_dir, name))
    return dest_dir


def download_tts(base: str = DEFAULT_DIR) -> str:
    dest_dir = os.path.join(base, "tts-kokoro-en")
    print(f"TTS model -> {dest_dir}  (repo {TTS_REPO}, Apache-2.0)")
    for name in TTS_FILES:
        _download(f"{HF}/{TTS_REPO}/resolve/main/{name}",
                  os.path.join(dest_dir, name))
    for sub in TTS_DIRS:
        for path in _list_tree(TTS_REPO, sub):
            _download(f"{HF}/{TTS_REPO}/resolve/main/{path}",
                      os.path.join(dest_dir, path))
    return dest_dir


def check(base: str = DEFAULT_DIR) -> dict:
    """Present/missing model files (no downloads)."""
    stt_dir = os.path.join(base, "stt-zipformer-en")
    tts_dir = os.path.join(base, "tts-kokoro-en")
    status = {
        "stt_dir": stt_dir,
        "stt_missing": [f for f in STT_FILES
                        if not os.path.isfile(os.path.join(stt_dir, f))],
        "tts_dir": tts_dir,
        "tts_missing": [f for f in TTS_FILES
                        if not os.path.isfile(os.path.join(tts_dir, f))]
        + [d + "/" for d in TTS_DIRS
           if not os.path.isdir(os.path.join(tts_dir, d))],
    }
    return status


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("download-stt", "download-tts",
                                        "download-all", "check"):
        print(__doc__)
        return 2
    base = argv[2] if len(argv) > 2 else DEFAULT_DIR
    if argv[1] == "download-stt":
        download_stt(base)
    elif argv[1] == "download-tts":
        download_tts(base)
    elif argv[1] == "download-all":
        download_stt(base)
        download_tts(base)
    else:
        status = check(base)
        print(f"STT dir: {status['stt_dir']}")
        print("  missing:", status["stt_missing"] or "none")
        print(f"TTS dir: {status['tts_dir']}")
        print("  missing:", status["tts_missing"] or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
