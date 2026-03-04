"""
Cadre-AI Live Voice Server
Custom FastAPI server for bidirectional audio streaming with ADK.
Based on Google's official bidi-demo pattern.
"""

import asyncio
import base64
import io
import json
import os
import re
import time
import traceback
from pathlib import Path

import edge_tts
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from google.adk.runners import Runner, RunConfig
from google.adk.agents.live_request_queue import LiveRequestQueue, LiveRequest
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from google.genai.types import RealtimeInputConfig, ActivityHandling

EDGE_TTS_VOICE = os.environ.get("CADRE_TTS_VOICE", "en-US-AndrewNeural")

load_dotenv()

# Import the agent
from cadre.agent import root_agent, REVIT_ENABLED

app = FastAPI(title="Cadre-AI")

# Persistent session storage — survives restarts and reconnects
_db_path = Path(__file__).parent / "cadre_sessions.db"
session_service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{_db_path}")
runner = Runner(
    agent=root_agent,
    app_name="cadre",
    session_service=session_service,
)


_revit_pipe_cache = {"value": False, "checked_at": 0}

async def _check_revit_pipe():
    """Check if Revit named pipe is accessible (Windows only). Cached for 10s."""
    if not REVIT_ENABLED:
        return False
    now = time.monotonic()
    if now - _revit_pipe_cache["checked_at"] < 10:
        return _revit_pipe_cache["value"]
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-Command",
            "Test-Path \\\\.\\pipe\\RevitMCPBridge2026",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        result = stdout.decode().strip().lower() == "true"
        _revit_pipe_cache["value"] = result
        _revit_pipe_cache["checked_at"] = now
        return result
    except Exception:
        _revit_pipe_cache["checked_at"] = now
        return False


@app.get("/")
async def index():
    landing = Path(__file__).parent / "landing.html"
    if landing.exists():
        return FileResponse(landing)
    return FileResponse(Path(__file__).parent / "voice_client.html")


@app.get("/app")
async def app_page():
    return FileResponse(Path(__file__).parent / "voice_client.html")


@app.get("/status")
async def status():
    """Service health check — polled by UI for status badges."""
    return JSONResponse({
        "revit": {
            "enabled": REVIT_ENABLED,
            "connected": (await _check_revit_pipe()) if REVIT_ENABLED else False,
        },
        "financial": {"enabled": True, "connected": True},
        "web_search": {"enabled": True, "connected": True},
    })


@app.post("/tts")
async def tts(request: Request):
    """Convert text to speech using Edge TTS (Andrew voice). Returns MP3 audio."""
    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        return JSONResponse({"error": "No text provided"}, status_code=400)

    try:
        communicate = edge_tts.Communicate(text, EDGE_TTS_VOICE)
        audio_buffer = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_buffer.write(chunk["data"])

        audio_buffer.seek(0)
        return StreamingResponse(audio_buffer, media_type="audio/mpeg")
    except Exception as e:
        print(f"[tts] Error: {e}", flush=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/apps/{app_name}/users/{user_id}/sessions")
async def create_session(app_name: str, user_id: str):
    session = await session_service.create_session(
        app_name=app_name, user_id=user_id
    )
    return {"id": session.id}


@app.get("/apps/{app_name}/users/{user_id}/sessions/{session_id}")
async def get_session(app_name: str, user_id: str, session_id: str):
    """Check if a session exists and is resumable."""
    try:
        session = await session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
        if session:
            return {"id": session.id, "exists": True}
        return JSONResponse({"exists": False}, status_code=404)
    except Exception:
        return JSONResponse({"exists": False}, status_code=404)


@app.get("/apps/{app_name}/users/{user_id}/sessions")
async def list_sessions(app_name: str, user_id: str):
    """List recent sessions for this user."""
    try:
        result = await session_service.list_sessions(
            app_name=app_name, user_id=user_id
        )
        sessions = result.sessions if hasattr(result, 'sessions') else (result or [])
        return {"sessions": [{"id": s.id} for s in sessions]}
    except Exception:
        return {"sessions": []}


def _enrich_event(data: dict) -> dict:
    """Add _cadre metadata to tool call/response events for the UI."""
    if "content" not in data or not data["content"] or "parts" not in data["content"]:
        return data

    for part in data["content"]["parts"]:
        # Tool call start
        if "functionCall" in part:
            fn = part["functionCall"]
            tool_name = fn.get("name", "")
            domain = "web"
            if tool_name.startswith("revit_") or tool_name.startswith("revit"):
                domain = "revit"
            elif tool_name.startswith("get_stock") or tool_name.startswith("get_market") \
                    or tool_name.startswith("get_sector") or tool_name.startswith("get_fear") \
                    or tool_name.startswith("get_company") or tool_name.startswith("get_earnings") \
                    or tool_name.startswith("get_portfolio") or tool_name.startswith("get_sentiment") \
                    or tool_name.startswith("screen_") or tool_name.startswith("compare_"):
                domain = "financial"
            data["_cadre_event"] = "tool_call"
            data["_cadre_tool"] = {
                "name": tool_name,
                "domain": domain,
                "args": fn.get("args", {}),
            }
            break

        # Tool response
        if "functionResponse" in part:
            fn = part["functionResponse"]
            tool_name = fn.get("name", "")
            data["_cadre_event"] = "tool_response"
            data["_cadre_tool"] = {
                "name": tool_name,
                "response": fn.get("response", {}),
            }
            # Structured extraction for known multimedia tools
            try:
                resp_obj = fn.get("response", {})
                resp_str = json.dumps(resp_obj) if isinstance(resp_obj, dict) else str(resp_obj)
                if tool_name == "image_search":
                    urls = re.findall(r'"image_url"\s*:\s*"([^"]+)"', resp_str)
                    titles = re.findall(r'"title"\s*:\s*"([^"]*)"', resp_str)
                    if urls:
                        data["_cadre_images"] = [
                            {"url": urls[i], "title": titles[i] if i < len(titles) else ""}
                            for i in range(min(len(urls), 6))
                        ]
                elif tool_name == "video_search":
                    embeds = re.findall(r'"embed_url"\s*:\s*"([^"]+)"', resp_str)
                    titles = re.findall(r'"title"\s*:\s*"([^"]*)"', resp_str)
                    channels = re.findall(r'"channel"\s*:\s*"([^"]*)"', resp_str)
                    if embeds:
                        data["_cadre_videos"] = [
                            {
                                "embed_url": embeds[i],
                                "title": titles[i] if i < len(titles) else "",
                                "channel": channels[i] if i < len(channels) else "",
                            }
                            for i in range(min(len(embeds), 4))
                        ]
                elif tool_name == "generate_image":
                    # Extract base64 generated image
                    data_uri_match = re.search(r'"data_uri"\s*:\s*"(data:image/[^"]+)"', resp_str)
                    if data_uri_match:
                        data["_cadre_generated_image"] = {
                            "data_uri": data_uri_match.group(1),
                            "prompt": re.search(r'"prompt"\s*:\s*"([^"]*)"', resp_str).group(1) if re.search(r'"prompt"\s*:\s*"([^"]*)"', resp_str) else "",
                        }
                # Extract citation sources from research/search tools
                if tool_name in ("web_search", "deep_research", "search_papers", "wikipedia_lookup"):
                    print(f"[sources] Tool: {tool_name}, resp_str length: {len(resp_str)}", flush=True)
                    print(f"[sources] First 500 chars: {resp_str[:500]}", flush=True)
                    sources = []
                    # Extract all title/url pairs from the response
                    all_titles = re.findall(r'"title"\s*:\s*"([^"]*)"', resp_str)
                    all_urls = re.findall(r'"(?:url|link|pdf_url|AbstractURL|FirstURL|page)"\s*:\s*"(https?://[^"]+)"', resp_str)
                    # Determine source type
                    src_type = "web"
                    if tool_name == "search_papers":
                        src_type = "paper"
                    elif tool_name == "wikipedia_lookup":
                        src_type = "wiki"
                    elif tool_name == "deep_research":
                        src_type = "web"  # mixed, but default to web
                    # Pair titles with URLs (best effort)
                    seen_urls = set()
                    for url in all_urls:
                        if url in seen_urls:
                            continue
                        seen_urls.add(url)
                        # Try to find a title near this URL in the response
                        title = ""
                        for t in all_titles:
                            if t and len(t) > 3:
                                title = t
                                all_titles.remove(t)
                                break
                        # Infer type for deep_research
                        stype = src_type
                        if tool_name == "deep_research":
                            if "arxiv.org" in url or "semanticscholar" in url or "pdf" in url.lower():
                                stype = "paper"
                            elif "wikipedia.org" in url:
                                stype = "wiki"
                        sources.append({"num": len(sources) + 1, "title": title or url, "url": url, "type": stype})
                    if sources:
                        print(f"[sources] Extracted {len(sources)} sources", flush=True)
                        data["_cadre_sources"] = sources[:10]
                    else:
                        print(f"[sources] No sources extracted!", flush=True)

                if tool_name not in ("web_search", "deep_research", "search_papers", "wikipedia_lookup",
                                     "image_search", "video_search", "generate_image"):
                    # Generic: extract any image URLs from other tool responses
                    img_urls = re.findall(
                        r'https?://[^\s"\'\\,\]}>]+\.(?:jpg|jpeg|png|gif|webp|svg|JPG|JPEG|PNG)(?:\?[^\s"\'\\,\]}>]*)?',
                        resp_str
                    )
                    if img_urls:
                        data["_cadre_images"] = [
                            {"url": u, "title": ""} for u in list(dict.fromkeys(img_urls))[:6]
                        ]
            except Exception:
                pass
            break

    return data


@app.websocket("/run_live")
async def run_live(
    websocket: WebSocket,
    app_name: str,
    user_id: str,
    session_id: str,
):
    await websocket.accept()
    live_queue = LiveRequestQueue()

    async def upstream():
        """Client audio/text → LiveRequestQueue"""
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    # Check for activity_start (soft interrupt from client)
                    msg = json.loads(raw)
                    if "activity_start" in msg:
                        print("[upstream] Client sent activity_start (soft interrupt)", flush=True)
                        # Forward as a LiveRequest with activity_start
                        live_queue.send(LiveRequest.model_validate({"activity_start": {}}))
                        continue
                    live_queue.send(LiveRequest.model_validate_json(raw))
                except Exception as e:
                    print(f"[upstream] Parse error: {e}", flush=True)
        except WebSocketDisconnect:
            print("[upstream] Client disconnected")
            live_queue.close()

    def clean_for_tts(text: str) -> str:
        """Strip URLs, file paths, and markdown syntax — keep only speakable text."""
        # Remove suggestion tags
        text = re.sub(r'\[SUGGESTIONS:.*?\]', '', text)
        # Remove markdown image syntax: ![alt](url) → alt
        text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'\1', text)
        # Remove markdown links: [text](url) → text
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
        # Remove bare URLs (http/https)
        text = re.sub(r'https?://[^\s),]+', '', text)
        # Remove file paths (/mnt/..., C:\..., D:\...)
        text = re.sub(r'[A-Za-z]:\\[^\s,]+', '', text)
        text = re.sub(r'/[a-z][a-z0-9_/.\-]+', '', text)
        # Remove markdown bold/code markers
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        text = re.sub(r'`([^`]+)`', r'\1', text)
        # Clean up extra whitespace
        text = re.sub(r'\s{2,}', ' ', text).strip()
        return text

    async def generate_tts(text: str):
        """Generate Edge TTS audio and push it to the client via WebSocket."""
        try:
            text = clean_for_tts(text)
            if not text:
                return
            print(f"[tts] Generating speech for: {text[:80]}", flush=True)
            communicate = edge_tts.Communicate(text, EDGE_TTS_VOICE)
            audio_buffer = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_buffer.write(chunk["data"])
            audio_bytes = audio_buffer.getvalue()
            if audio_bytes:
                audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
                tts_msg = json.dumps({
                    "_cadre_tts": {
                        "audio": audio_b64,
                        "mime": "audio/mpeg",
                        "text": text[:100],
                    }
                })
                try:
                    await websocket.send_text(tts_msg)
                except (WebSocketDisconnect, RuntimeError):
                    print("[tts] Client disconnected, discarding audio", flush=True)
                    return
                print(f"[tts] Sent {len(audio_bytes)} bytes of audio", flush=True)
        except (WebSocketDisconnect, RuntimeError):
            print("[tts] Client disconnected during TTS generation", flush=True)
        except Exception as e:
            print(f"[tts] Error: {e}", flush=True)

    async def downstream():
        """Agent events → Client (enriched with _cadre metadata + Edge TTS)"""
        transcript_buffer = ""
        all_spoken_texts = []  # Track ALL spoken segments for overlap detection
        last_spoken_text = ""  # Most recent spoken text
        flush_task = None
        tts_lock = asyncio.Lock()
        has_native_audio = False  # Track if model is sending its own audio

        def normalize(s):
            """Normalize text for comparison — lowercase, collapse whitespace."""
            return " ".join(s.lower().split())

        def is_already_spoken(text):
            """Check if text (or its beginning) was already spoken."""
            nt = normalize(text)
            for spoken in all_spoken_texts:
                ns = normalize(spoken)
                if nt == ns or nt.startswith(ns) or ns.startswith(nt):
                    return True
                # Also check significant overlap (>80% of words match at start)
                tw = nt.split()
                sw = ns.split()
                if len(sw) >= 3 and len(tw) >= 3:
                    overlap = 0
                    for i in range(min(len(tw), len(sw))):
                        if tw[i] == sw[i]:
                            overlap += 1
                        else:
                            break
                    if overlap >= len(sw) * 0.8:
                        return True
            return False

        async def delayed_flush():
            """Wait for transcript to settle, then generate TTS."""
            nonlocal transcript_buffer, last_spoken_text
            try:
                await asyncio.sleep(2.5)
                async with tts_lock:
                    text = transcript_buffer.strip()
                    transcript_buffer = ""

                    # Parse and strip suggestion chips before TTS
                    suggestion_match = re.search(r'\[SUGGESTIONS:\s*(.+?)\]', text)
                    if suggestion_match:
                        suggestions = [s.strip() for s in suggestion_match.group(1).split('|')]
                        text = text[:suggestion_match.start()].strip()
                        try:
                            await websocket.send_text(json.dumps({"_cadre_suggestions": suggestions}))
                        except Exception:
                            pass

                    if text and not is_already_spoken(text):
                        last_spoken_text = text
                        all_spoken_texts.append(text)
                        await generate_tts(text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[tts] Flush error: {e}", flush=True)

        try:
            async for event in runner.run_live(
                user_id=user_id,
                session_id=session_id,
                live_request_queue=live_queue,
                run_config=RunConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name="Aoede"
                            )
                        )
                    ),
                    output_audio_transcription=types.AudioTranscriptionConfig(),
                    input_audio_transcription=types.AudioTranscriptionConfig(),
                    realtime_input_config=RealtimeInputConfig(
                        activityHandling=ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
                    ),
                ),
            ):
                try:
                    msg = event.model_dump_json(exclude_none=True, by_alias=True)
                    data = json.loads(msg)

                    # Log events
                    event_types = []
                    if "content" in data and data["content"] and "parts" in data["content"]:
                        for part in data["content"]["parts"]:
                            if "inlineData" in part:
                                event_types.append(f"AUDIO({len(part['inlineData'].get('data',''))})")
                                if not has_native_audio:
                                    has_native_audio = True
                                    # Cancel any pending Edge TTS — native audio takes over
                                    if flush_task and not flush_task.done():
                                        flush_task.cancel()
                                        flush_task = None
                                    transcript_buffer = ""
                            if "text" in part:
                                event_types.append(f"TEXT:{part['text'][:60]}")
                            if "functionCall" in part:
                                event_types.append(f"TOOL_CALL:{part['functionCall'].get('name','?')}")
                            if "functionResponse" in part:
                                event_types.append(f"TOOL_RESP:{part['functionResponse'].get('name','?')}")

                    # Accumulate outputTranscription for TTS (only if model isn't sending native audio)
                    if "outputTranscription" in data and not has_native_audio:
                        ot = data["outputTranscription"]
                        chunk_text = ot if isinstance(ot, str) else (ot.get("text", "") if isinstance(ot, dict) else str(ot))
                        if chunk_text.strip():
                            ct = chunk_text.strip()
                            nct = normalize(ct)
                            bt = transcript_buffer.strip()

                            # Skip if this exact text (or close match) was already spoken
                            if is_already_spoken(ct):
                                # Check if there's a NEW tail beyond what was spoken
                                delta = ""
                                for spoken in all_spoken_texts:
                                    ns = normalize(spoken)
                                    if nct.startswith(ns) and len(nct) > len(ns):
                                        # Use normalized length for slicing to avoid mismatch
                                        candidate = nct[len(ns):].strip()
                                        if len(candidate) > len(delta):
                                            delta = candidate
                                if delta and not is_already_spoken(delta):
                                    transcript_buffer = delta
                                    if flush_task and not flush_task.done():
                                        flush_task.cancel()
                                    flush_task = asyncio.create_task(delayed_flush())
                                else:
                                    print(f"[tts] Skipping already-spoken: {ct[:50]}", flush=True)
                            else:
                                # Cumulative: new chunk contains buffer → replace
                                if bt and ct.startswith(bt):
                                    transcript_buffer = chunk_text
                                # Incremental: append new tokens
                                else:
                                    transcript_buffer += chunk_text
                                # Reset the flush timer
                                if flush_task and not flush_task.done():
                                    flush_task.cancel()
                                flush_task = asyncio.create_task(delayed_flush())
                            event_types.append(f"OUT_T:{ct[:40]}")

                    # Detect server-side interruption
                    if data.get("serverContent", {}).get("interrupted", False):
                        print("[event] INTERRUPTED by client", flush=True)
                        # Cancel pending TTS flush
                        if flush_task and not flush_task.done():
                            flush_task.cancel()
                            flush_task = None
                        transcript_buffer = ""
                        has_native_audio = False
                        # Notify client
                        try:
                            await websocket.send_text(json.dumps({"_cadre_event": "interrupted"}))
                        except Exception:
                            pass

                    if "inputTranscription" in data:
                        it = data["inputTranscription"]
                        chunk_text = it if isinstance(it, str) else (it.get("text", "") if isinstance(it, dict) else str(it))
                        if chunk_text.strip():
                            event_types.append(f"IN_T:{chunk_text[:40]}")
                            # New user turn — reset spoken history so next response speaks fresh
                            last_spoken_text = ""
                            all_spoken_texts.clear()
                            has_native_audio = False
                            # User started speaking — flush any pending TTS immediately
                            if transcript_buffer.strip():
                                if flush_task and not flush_task.done():
                                    flush_task.cancel()
                                    flush_task = None
                                async with tts_lock:
                                    text = transcript_buffer.strip()
                                    transcript_buffer = ""
                                    if text and not is_already_spoken(text):
                                        last_spoken_text = text
                                        all_spoken_texts.append(text)
                                        await generate_tts(text)

                    if event_types:
                        print(f"[event] {' | '.join(event_types)}", flush=True)

                    # Strip suggestion tags from outputTranscription before sending to client
                    if "outputTranscription" in data:
                        ot = data["outputTranscription"]
                        if isinstance(ot, str):
                            data["outputTranscription"] = re.sub(r'\[SUGGESTIONS:.*?\]', '', ot).rstrip()
                        elif isinstance(ot, dict) and "text" in ot:
                            ot["text"] = re.sub(r'\[SUGGESTIONS:.*?\]', '', ot["text"]).rstrip()

                    # Enrich with Cadre metadata for UI
                    data = _enrich_event(data)
                    await websocket.send_text(json.dumps(data))

                except Exception as e:
                    print(f"[downstream] Send error: {e}", flush=True)
                    break
        except Exception as e:
            print(f"[downstream] Agent error: {e}", flush=True)
            traceback.print_exc()
        finally:
            if flush_task and not flush_task.done():
                flush_task.cancel()

    tasks = [
        asyncio.create_task(upstream()),
        asyncio.create_task(downstream()),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for t in done:
        try:
            t.result()
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception as e:
            print(f"[live] Error: {e}", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8443))
    cert = Path(__file__).parent / "cert.pem"
    key = Path(__file__).parent / "key.pem"

    # On Cloud Run, PORT is set and SSL is handled by the load balancer
    use_ssl = cert.exists() and key.exists() and port != 8080

    if use_ssl:
        print(f"HTTPS enabled on port {port} (self-signed cert)")
    else:
        print(f"HTTP on port {port} (SSL handled externally or no certs)")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        ssl_keyfile=str(key) if use_ssl else None,
        ssl_certfile=str(cert) if use_ssl else None,
    )
