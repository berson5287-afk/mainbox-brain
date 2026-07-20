"""
call_ingest.py — v1.0
Watch a folder for new call recordings, transcribe them LOCALLY (no cloud),
and forward the transcript into MaINbox Voice — which saves it, extracts
calendar events, and pushes an alert to your phone.

The seamless pipeline:
  Samsung call recording  ──Syncthing──▶  PC folder  ──this script──▶
  local Whisper transcription  ──▶  MaINbox Voice alert + events

SETUP (one time):
  1. pip install faster-whisper          (local speech-to-text; ~200MB model
                                          downloads on first run, then cached)
  2. Install Syncthing on the phone + PC and sync the phone folder
     Internal storage/Recordings/Call  →  a PC folder (e.g. C:\\CallRecordings)
     (Samsung saves call recordings there; transcripts stay inside Samsung's
      app, but we transcribe the audio ourselves so nothing manual is needed.)
  3. Run this script (or CALL_INGEST.bat) pointed at that folder.

Also accepts .txt files dropped in the folder (e.g. a transcript you exported
by hand) — those forward as-is without transcription.

Usage:
  python call_ingest.py --dir C:\\CallRecordings
  python call_ingest.py --dir C:\\CallRecordings --once
  python call_ingest.py --dir C:\\CallRecordings --model small --language en
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac", ".amr"}
STATE_NAME = ".ingested.json"


def _default_token() -> str:
    tok = os.environ.get("MBB_TOKEN", "")
    if tok:
        return tok
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, ".voice_token"),
              os.path.join(here, "mainbox_voice", ".voice_token")):
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            continue
    return ""


def load_state(d: str) -> dict:
    try:
        with open(os.path.join(d, STATE_NAME), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_state(d: str, st: dict) -> None:
    try:
        with open(os.path.join(d, STATE_NAME), "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1)
    except OSError:
        pass


_MODEL = None


def transcribe(path: str, model_size: str, language: str) -> str:
    """Local Whisper via faster-whisper. Model loads once, stays warm."""
    global _MODEL
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415
    except ImportError:
        print("\nfaster-whisper is not installed. Run:\n"
              "    pip install faster-whisper\n"
              "then start this script again.")
        sys.exit(1)
    if _MODEL is None:
        print(f"  loading Whisper model '{model_size}' "
              "(first run downloads it)...")
        _MODEL = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _info = _MODEL.transcribe(path, language=language or None,
                                        vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


def forward(server: str, token: str, filename: str, text: str) -> dict:
    body = json.dumps({"filename": filename, "text": text}).encode()
    url = f"{server.rstrip('/')}/api/call_transcript?token={token}"
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def stable_size(path: str) -> bool:
    """True once the file stops growing (sync finished)."""
    try:
        s1 = os.path.getsize(path)
        time.sleep(1.5)
        return os.path.getsize(path) == s1
    except OSError:
        return False


def process_once(d: str, server: str, token: str,
                 model: str, language: str) -> int:
    st = load_state(d)
    done = st.setdefault("done", {})
    n = 0
    try:
        names = sorted(os.listdir(d))
    except OSError as e:
        print(f"cannot read {d}: {e}")
        return 0
    for name in names:
        ext = os.path.splitext(name)[1].lower()
        if name.startswith(".") or name.endswith(".transcript.txt"):
            continue
        if ext not in AUDIO_EXTS and ext != ".txt":
            continue
        full = os.path.join(d, name)
        key = f"{name}:{os.path.getsize(full)}"
        if done.get(name) == key:
            continue
        if not stable_size(full):
            print(f"  {name}: still syncing, will retry")
            continue
        print(f"  {name}: ", end="", flush=True)
        try:
            if ext == ".txt":
                with open(full, encoding="utf-8", errors="replace") as f:
                    text = f.read().strip()
                print("text file — forwarding... ", end="", flush=True)
            else:
                print("transcribing... ", end="", flush=True)
                text = transcribe(full, model, language)
                # keep a human-readable copy next to the audio
                try:
                    with open(full + ".transcript.txt", "w",
                              encoding="utf-8") as f:
                        f.write(text)
                except OSError:
                    pass
            if not text:
                print("(empty transcript — skipped)")
                done[name] = key
                continue
            r = forward(server, token, name, text)
            evs = r.get("events", [])
            print(f"sent ({len(text)} chars, "
                  f"{len(evs)} event(s) found).")
            done[name] = key
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAILED: {e}")
    save_state(d, st)
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="Call-recording ingest for "
                                             "MaINbox Voice")
    ap.add_argument("--dir", required=True,
                    help="folder where call recordings arrive (Syncthing "
                         "target)")
    ap.add_argument("--server", default="http://127.0.0.1:8770",
                    help="MaINbox Voice server URL")
    ap.add_argument("--token", default="",
                    help="voice server token (default: .voice_token / "
                         "MBB_TOKEN)")
    ap.add_argument("--model", default="base",
                    help="Whisper model: tiny/base/small (default base)")
    ap.add_argument("--language", default="en")
    ap.add_argument("--once", action="store_true",
                    help="process what's there and exit")
    ap.add_argument("--watch", type=int, default=20,
                    help="poll interval seconds (default 20)")
    args = ap.parse_args()

    token = args.token or _default_token()
    if not token:
        print("No token — pass --token or set MBB_TOKEN "
              "(or keep .voice_token next to this script).")
        sys.exit(1)

    print(f"call_ingest v1.0 — watching {args.dir}")
    print(f"  forwarding to {args.server} (model={args.model})")
    if args.once:
        n = process_once(args.dir, args.server, token,
                         args.model, args.language)
        print(f"done — {n} forwarded.")
        return
    while True:
        try:
            process_once(args.dir, args.server, token,
                         args.model, args.language)
        except KeyboardInterrupt:
            print("\nstopped.")
            return
        except Exception as e:  # noqa: BLE001
            print(f"watch error: {e}")
        time.sleep(max(5, args.watch))


if __name__ == "__main__":
    main()
