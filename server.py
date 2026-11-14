"""
Jarvis — Real Time TTS · backend

A single-file FastAPI server implementing the realtime voice loop:
push-to-talk -> /voice/chat (NDJSON sentence stream) -> /tts (streaming audio).

The core trick: the LLM reply is streamed token-by-token, split into complete
sentences as they form, and each sentence is emitted as an NDJSON event the
moment it is complete. The browser speaks it immediately, so the first sound
lands ~1s after you stop talking while the rest of the reply is still
generating.

Endpoints:
  POST /voice/chat   {query}  -> NDJSON: {t:'s',text} per sentence,
                                 {t:'agent',cmd} action handoff,
                                 {t:'err',msg}, {t:'done'}
  GET  /tts?text=...          -> streaming audio (edge-tts; gemini optional)
  POST /voice/reset           -> clear rolling history
  GET  /healthz               -> liveness

Config (env vars or a .env file next to this script):
  OPENROUTER_API_KEY   required for /voice/chat
  GEMINI_API_KEY       optional — enables provider=gemini on /tts
  VOICE_MODEL          default z-ai/glm-5.3-flash (any OpenRouter model)
  JARVIS_PERSONA       who the assistant serves (default "the operator")
  TTS_VOICE            edge voice (default en-GB-RyanNeural)
  TTS_RATE             edge rate (default +4%)
  GEMINI_TTS_VOICE     gemini voice (default Charon)

Extracted from the jarvis-deck project (github.com/DeezGit21/jarvis-deck).
MIT licensed.
"""

import json
import os
import re
import time
import urllib.request
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
CACHE.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- config


def _load_env() -> None:
    """Read KEY=VALUE lines from a local .env into os.environ (no override)."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


_load_env()

VOICE_MODEL = os.environ.get("VOICE_MODEL", "z-ai/glm-5.3-flash")
PERSONA = os.environ.get("JARVIS_PERSONA", "the operator")
TTS_VOICE = os.environ.get("TTS_VOICE", "en-GB-RyanNeural")
TTS_RATE = os.environ.get("TTS_RATE", "+4%")
GEMINI_TTS_VOICE = os.environ.get("GEMINI_TTS_VOICE", "Charon")
GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"

# ---------------------------------------------------------------- voice chat

VOICE_SYSTEM = """You are JARVIS, the voice assistant of {persona}. You are answering through a VOICE interface — your reply is spoken aloud, sentence by sentence.

Rules:
- Answer conversationally in one to three SHORT sentences. Plain text only: no markdown, no lists, no emoji, no stage directions.
- If the request needs a REAL ACTION you cannot answer from knowledge alone (run a script, restart a service, send a message), reply with EXACTLY one line and nothing else:
[[agent]] <the command, phrased as an instruction to the host agent>
- If something important is ambiguous, ask one short question instead of guessing.""".format(persona=PERSONA)

# Rolling history survives across turns, capped at 24 messages.
_history: list = []
_SENTENCE_END = re.compile(r"[.!?…]+(?:\s|$)")


def _drain_sentences(buf: str):
    """Split complete sentences off the front of the streaming buffer."""
    out = []
    while True:
        m = _SENTENCE_END.search(buf)
        if not m:
            break
        end = m.end()
        while end < len(buf) and buf[end] in ")]'»":
            end += 1
        sent = buf[:end].strip()
        buf = buf[end:]
        if sent:
            out.append(sent)
    return out, buf.lstrip()


def _openrouter_stream(messages, timeout: int = 90):
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY missing — put it in .env or the environment")
    payload = json.dumps({"model": VOICE_MODEL, "messages": messages, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=payload,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                 "X-Title": "Jarvis Real Time TTS"})
    return urllib.request.urlopen(req, timeout=timeout)


app = FastAPI(title="Jarvis — Real Time TTS")


@app.get("/healthz")
def healthz():
    return {"ok": True, "model": VOICE_MODEL}


@app.post("/voice/chat")
def voice_chat(body: dict = Body(...)):
    """Streaming voice turn. NDJSON events: {t:'s',text} per sentence,
    {t:'agent',cmd} to hand an action to a host agent, {t:'err',msg}, {t:'done'}."""
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query required")
    if len(query) > 8000:
        raise HTTPException(status_code=400, detail="query too long")

    def ev(obj) -> bytes:
        return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

    def generate():
        started = time.time()
        full = ""
        buf = ""
        held: list[str] = []  # sentences withheld once "[[" appears — could be
        try:                  # an [[agent]] marker; never speak that aloud
            messages = ([{"role": "system", "content": VOICE_SYSTEM}]
                        + _history[-16:]
                        + [{"role": "user", "content": query}])
            resp = _openrouter_stream(messages)
            try:
                for raw in resp:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content")
                    if not delta:
                        continue
                    full += delta
                    buf += delta
                    sents, buf = _drain_sentences(buf)
                    if "[[" in full:
                        held.extend(sents)
                    else:
                        for s in sents:
                            yield ev({"t": "s", "text": s, "at": round(time.time() - started, 2)})
                if buf.strip() and "[[" not in full:
                    yield ev({"t": "s", "text": buf.strip()})
            finally:
                resp.close()
        except Exception as e:  # surface mid-stream as an event, not a broken pipe
            msg = str(e)[:300]
            if full:
                yield ev({"t": "done", "partial": True, "err": msg})
            else:
                yield ev({"t": "err", "msg": msg})
            return
        m = re.search(r"\[\[agent\]\]\s*(.+)", full)
        if m:
            _history.append({"role": "user", "content": query})
            _history.append({"role": "assistant", "content": "[handed to agent: " + m.group(1).strip() + "]"})
            del _history[:-24]
            yield ev({"t": "agent", "cmd": m.group(1).strip()})
            return
        # "[[" appeared but no complete marker — flush the withheld sentences
        for s in held:
            yield ev({"t": "s", "text": s})
        if buf.strip():
            yield ev({"t": "s", "text": buf.strip()})
        if not full.strip():
            yield ev({"t": "err", "msg": "empty reply"})
            return
        _history.append({"role": "user", "content": query[:4000]})
        _history.append({"role": "assistant", "content": full[:4000]})
        del _history[:-24]
        yield ev({"t": "done"})

    return StreamingResponse(generate(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/voice/reset")
def voice_reset():
    had = bool(_history)
    _history.clear()
    return {"ok": True, "cleared": had}


# ---------------------------------------------------------------- tts

_TTS_GEMINI_STYLE = (
    "You are JARVIS, the composed British AI butler from the Iron Man films. "
    "Speak with a refined, calm, articulate British RP accent: measured pace, "
    "warm confidence, dry understated wit, precise enunciation. Read naturally:\n\n"
)


def _tts_gemini_wav(text: str, voice: str) -> bytes:
    """Synthesize via Gemini TTS (style-prompted JARVIS) -> full WAV bytes."""
    import base64
    import io
    import wave

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("no GEMINI_API_KEY — add it to .env to use provider=gemini")
    body = json.dumps({
        "contents": [{"parts": [{"text": _TTS_GEMINI_STYLE + text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_TTS_MODEL}:generateContent?key={key}",
        data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        r = json.load(resp)
    b64 = r["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    pcm = base64.b64decode(b64)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)
    return buf.getvalue()


def _tts_stream(text: str, voice: str, rate: str):
    """Async generator: edge-tts chunks as they arrive, teed to a cache file."""
    import asyncio
    import hashlib
    import edge_tts

    async def _gen():
        comm = edge_tts.Communicate(text, voice, rate=rate)
        tmp = CACHE / f"tts-{hashlib.sha1((voice + '|' + rate + '|' + text).encode('utf-8')).hexdigest()[:16]}.mp3.part"
        first = True
        try:
            with open(tmp, "wb") as fh:
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        yield chunk["data"]  # header bytes go out immediately
                        first = False
                        fh.write(chunk["data"])
            tmp.rename(tmp.with_suffix(""))
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    return _gen


@app.get("/tts")
def tts(text: str = "", provider: str = "", voice: str = ""):
    """Streaming TTS. Defaults to edge-tts (fast, starts playing as chunks
    arrive). provider=gemini swaps to the style-prompted Gemini voice — but
    only when the caller explicitly asks, so a missing GEMINI_API_KEY can
    never silently degrade the realtime path."""
    import hashlib

    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    if len(text) > 600:  # spoken-sentence safety cap
        cut = text.rfind(". ", 0, 600)
        text = (text[:cut + 1] if cut > 200 else text[:600]).rstrip(" ,;:") + "."

    if provider == "gemini" and os.environ.get("GEMINI_API_KEY"):
        v = voice or GEMINI_TTS_VOICE
        gkey = hashlib.sha1(("gemini|" + v + "|" + text).encode("utf-8")).hexdigest()[:16]
        gout = CACHE / f"tts-gemini-{gkey}.wav"
        if gout.exists() and gout.stat().st_size > 0:
            return Response(content=gout.read_bytes(), media_type="audio/wav")
        wav = _tts_gemini_wav(text, v)
        gout.write_bytes(wav)
        return Response(content=wav, media_type="audio/wav")

    v, rate = (voice or TTS_VOICE), TTS_RATE
    key = hashlib.sha1(("edge|" + v + "|" + rate + "|" + text).encode("utf-8")).hexdigest()[:16]
    out = CACHE / f"tts-edge-{key}.mp3"
    if out.exists() and out.stat().st_size > 0:
        return Response(content=out.read_bytes(), media_type="audio/mpeg")
    try:
        import edge_tts  # noqa: F401 — availability check before streaming starts
    except ImportError:
        raise HTTPException(status_code=501, detail="edge-tts not installed (pip install edge-tts)") from None
    return StreamingResponse(_tts_stream(text, v, rate)(), media_type="audio/mpeg")


# ---------------------------------------------------------------- static ui

app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8899")))
