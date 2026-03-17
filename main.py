"""
ElevenLexa — AI Voice Assistant
================================
Raspberry Pi voice assistant powered by the OpenAI Realtime API.

Architecture overview:
  AudioPlayer    streams PCM audio from OpenAI → speaker (pacat)
  AudioRecorder  captures mic → queues PCM chunks for OpenAI (pacat)
  EyeDisplay     optional pygame display on HDMI screen (display.py)
  realtime_session  manages the WebSocket connection to OpenAI

Audio pipeline:
  Mic ──► pacat --record ──► AudioRecorder.audio_queue ──► OpenAI WebSocket
  OpenAI WebSocket ──► AudioPlayer ──► pacat --playback ──► Speaker

Echo prevention:
  recorder.muted = True while the AI speaks (prevents the mic from
  picking up the speaker output). After the AI finishes, a 2.5 s
  pause lets the room echo settle, then the audio queue is flushed.

Display (optional):
  EyeDisplay renders a retro robot face on the HDMI display.
  Falls back gracefully when no display is available (SSH / headless).

Hardware:
  Raspberry Pi 4/5, Soundcore Mini Bluetooth Speaker,
  PipeWire audio server (with module-echo-cancel loaded at startup)

Start:
  python3 main.py
"""

import asyncio
import base64
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

try:
    import websockets
except ImportError:
    print("Missing dependency: sudo pip3 install --break-system-packages websockets")
    sys.exit(1)

try:
    from display import EyeDisplay, STATE_SLEEPING, STATE_IDLE, STATE_LISTENING, STATE_SPEAKING
    DISPLAY_AVAILABLE = True
except Exception as _de:
    print(f"Display not available: {_de}")
    DISPLAY_AVAILABLE = False
    EyeDisplay = None
    STATE_SLEEPING = STATE_IDLE = STATE_LISTENING = STATE_SPEAKING = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OPENAI_API_KEY = "sk-proj"

WS_URL      = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
VOICE       = "verse"   # alloy, ash, ballad, coral, echo, fable,
                         # onyx, nova, sage, shimmer, verse
SAMPLE_RATE = 24000      # Hz — OpenAI Realtime uses 24 kHz PCM16 mono
CHUNK_MS    = 100        # ms per audio chunk sent to OpenAI
CHUNK_SIZE  = SAMPLE_RATE * 2 * CHUNK_MS // 1000  # bytes (16-bit = 2 bytes/sample)

# ---------------------------------------------------------------------------
# Wake-Word Configuration (Porcupine — offline, ~1 % CPU on Pi Zero)
# ---------------------------------------------------------------------------
# 1. Gratis-Account: https://console.picovoice.ai  → "Access Key" kopieren
# 2. Eingebaute freie Keywords: alexa, americano, blueberry, bumblebee,
#    grapefruit, grasshopper, hey barista, hey google, hey siri,
#    jarvis, ok google, picovoice, porcupine, terminator
# 3. Eigenes Keyword (.ppn-Datei) mit dem Picovoice Console erstellen
#    und den Pfad in WAKE_WORD_MODEL_PATH eintragen.
# Leer lassen → Wake-Word deaktiviert (immer aktiv wie bisher)
PORCUPINE_ACCESS_KEY  = "4dmycJDoeZar4JbxsgMCPmeoS4YSHIuIva8/gTwITeov+BFkgeOIvQ=="
WAKE_WORD             = "computer"  # Eingebautes Keyword-Name ODER "custom"
WAKE_WORD_MODEL_PATH  = ""          # Pfad zur .ppn-Datei (nur bei WAKE_WORD="custom")
WAKE_WORD_SAMPLE_RATE = 16000       # Porcupine erwartet immer 16 kHz
INACTIVITY_TIMEOUT    = 15          # Sekunden Stille → Session schließen, schlafen gehen

# PipeWire needs XDG_RUNTIME_DIR to locate the user session socket
PIPEWIRE_ENV = {**os.environ, "XDG_RUNTIME_DIR": "/run/user/1000"}


def _get_default_source() -> str | None:
    """
    Gibt das echte Mikrofon-Aufnahmegerät zurück.
    Filtert .monitor-Quellen heraus (das sind Loopbacks vom Lautsprecher,
    nicht das echte Mikrofon).
    """
    try:
        # Alle verfügbaren Sources holen
        out = subprocess.check_output(
            ["pactl", "list", "sources", "short"], env=PIPEWIRE_ENV,
            stderr=subprocess.DEVNULL
        ).decode()
        # Erste alsa_input.* Quelle nehmen (kein .monitor)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and "alsa_input" in parts[1]:
                return parts[1]
        # Fallback: Default Source aus pactl info (auch wenn .monitor)
        out2 = subprocess.check_output(
            ["pactl", "info"], env=PIPEWIRE_ENV, stderr=subprocess.DEVNULL
        ).decode()
        for line in out2.splitlines():
            if line.startswith("Default Source:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


def _get_default_sink() -> str | None:
    """Fragt PipeWire nach dem aktuellen Standard-Wiedergabegerät."""
    try:
        out = subprocess.check_output(
            ["pactl", "info"], env=PIPEWIRE_ENV, stderr=subprocess.DEVNULL
        ).decode()
        for line in out.splitlines():
            if line.startswith("Default Sink:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None

INSTRUCTIONS = (
    "You are Peter, a warm and intelligent personal assistant running on a Raspberry Pi. "
    "IMPORTANT: Always respond in English or Korean only — never German or any other language. "
    "Match the language the user speaks: if they speak Korean, reply in Korean; otherwise use English. "
    "The speech recognition may sometimes be inaccurate due to microphone quality. "
    "Use context to interpret what the user likely meant, even if the transcript seems odd. "
    "If something is genuinely unclear, ask one short clarifying question. "
    "Never repeat back what the user said word-for-word. "
    "Be conversational, natural, and concise — like a smart friend, not a robot. "
    "You can help with anything: questions, tasks, conversation, reminders, ideas."
)


# ---------------------------------------------------------------------------
# Startup sound
# ---------------------------------------------------------------------------

def _make_tone(freq: float, duration_ms: int, sample_rate: int = 24000,
               amplitude: float = 0.35, fade_ms: int = 20) -> bytes:
    """Generate a PCM16 sine-wave tone with short fade-in/out to avoid clicks."""
    import math, struct
    n_samples = int(sample_rate * duration_ms / 1000)
    fade_n    = int(sample_rate * fade_ms / 1000)
    out       = bytearray()
    for i in range(n_samples):
        env = 1.0
        if i < fade_n:
            env = i / fade_n
        elif i >= n_samples - fade_n:
            env = (n_samples - i) / fade_n
        val = math.sin(2 * math.pi * freq * i / sample_rate) * amplitude * env
        out += struct.pack("<h", int(val * 32767))
    return bytes(out)


def _make_silence(duration_ms: int, sample_rate: int = 24000) -> bytes:
    n_samples = int(sample_rate * duration_ms / 1000)
    return b"\x00\x00" * n_samples


def play_startup_sound():
    """Plays a short C-major arpeggio melody via pacat (no WAV file needed)."""
    print("DEBUG: Starte Startup-Chime...")
    try:
        # C5 – E5 – G5 – C6  (ascending C-major arpeggio)
        notes = [
            (523.25, 160),   # C5
            (659.25, 160),   # E5
            (783.99, 160),   # G5
            (1046.5, 320),   # C6  (held longer for finish)
        ]
        # 300 ms Stille vorab: weckt USB-Audiogerät aus dem Suspend auf,
        # damit der erste Ton nicht verschluckt wird
        pcm = _make_silence(300) + b"".join(
            _make_tone(freq, dur) + _make_silence(40)
            for freq, dur in notes
        )
        cmd = ["pacat", "--playback", "--format=s16le", "--rate=24000", "--channels=1",
               "--latency-msec=200"]
        sink = _get_default_sink()
        if sink:
            cmd.append(f"--device={sink}")
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, env=PIPEWIRE_ENV, stderr=subprocess.PIPE
        )
        proc.stdin.write(pcm)
        proc.stdin.flush()
        proc.stdin.close()
        proc.wait(timeout=5)
        print("DEBUG: Startup-Chime abgespielt.")
    except Exception as e:
        print(f"DEBUG: Startup-Sound Fehler: {e}")


# ---------------------------------------------------------------------------
# Wake acknowledgement sound
# ---------------------------------------------------------------------------

def play_wake_sound():
    """Zwei kurze Töne als Bestätigung nach Wake-Word-Erkennung."""
    try:
        notes = [(880.0, 80), (1174.7, 120)]   # A5 – D6
        pcm = _make_silence(300) + b"".join(_make_tone(f, d) + _make_silence(20) for f, d in notes)
        cmd = ["pacat", "--playback", "--format=s16le", "--rate=24000", "--channels=1",
               "--latency-msec=200"]
        sink = _get_default_sink()
        if sink:
            cmd.append(f"--device={sink}")
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, env=PIPEWIRE_ENV, stderr=subprocess.DEVNULL)
        proc.stdin.write(pcm)
        proc.stdin.flush()
        proc.stdin.close()
        proc.wait(timeout=3)
    except Exception as e:
        print(f"DEBUG: Wake-Sound Fehler: {e}")


# ---------------------------------------------------------------------------
# Wake-Word detection (Porcupine, offline, ~1 % CPU on Pi Zero 1)
# ---------------------------------------------------------------------------

def listen_for_wake_word(shutdown_flag) -> bool:
    """
    Blockiert, bis das Wake-Word erkannt wird oder shutdown_flag gesetzt ist.
    Gibt True zurück bei Erkennung, False bei Fehler oder Shutdown.

    Verwendet pacat bei 16 kHz (Porcupine-Anforderung) — kein pvrecorder nötig,
    bleibt damit konsistent mit dem restlichen PipeWire-Ansatz.
    """
    import struct

    try:
        import pvporcupine
    except ImportError:
        print("WARNUNG: pvporcupine nicht installiert — Wake-Word übersprungen.")
        print("         pip3 install --break-system-packages pvporcupine")
        return True

    if not PORCUPINE_ACCESS_KEY:
        return True

    try:
        if WAKE_WORD == "custom" and WAKE_WORD_MODEL_PATH:
            porcupine = pvporcupine.create(
                access_key=PORCUPINE_ACCESS_KEY,
                keyword_paths=[WAKE_WORD_MODEL_PATH],
            )
        else:
            porcupine = pvporcupine.create(
                access_key=PORCUPINE_ACCESS_KEY,
                keywords=[WAKE_WORD],
            )
    except Exception as e:
        print(f"FEHLER: Porcupine konnte nicht geladen werden: {e}")
        return True  # Graceful fallback: kein Wake-Word-Block

    frame_bytes = porcupine.frame_length * 2  # 16-bit = 2 Bytes/Sample
    cmd = ["pacat", "--record", "--format=s16le",
           f"--rate={WAKE_WORD_SAMPLE_RATE}", "--channels=1"]
    source = _get_default_source()
    if source:
        cmd.append(f"--device={source}")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, env=PIPEWIRE_ENV, stderr=subprocess.DEVNULL
    )

    print(f'Schlafe — warte auf Wake-Word "{WAKE_WORD}"...', flush=True)
    detected = False

    try:
        while not shutdown_flag.is_set():
            data = proc.stdout.read(frame_bytes)
            if len(data) < frame_bytes:
                break
            pcm = list(struct.unpack_from(f"{porcupine.frame_length}h", data))
            if porcupine.process(pcm) >= 0:
                print(f'Wake-Word "{WAKE_WORD}" erkannt!', flush=True)
                detected = True
                break
    except Exception as e:
        print(f"DEBUG: Wake-Word Fehler: {e}", flush=True)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass
        porcupine.delete()

    return detected


# ---------------------------------------------------------------------------
# Audio output
# ---------------------------------------------------------------------------

class AudioPlayer:
    """
    Wraps `pacat --playback`.
    Receives raw PCM16 bytes and writes them to pacat stdin →
    PipeWire routes them to the default sink (Bluetooth speaker).
    """

    def __init__(self):
        self.process = None

    def start(self):
        """Start the pacat playback process."""
        cmd = ["pacat", "--playback", "--format=s16le",
               f"--rate={SAMPLE_RATE}", "--channels=1",
               "--latency-msec=200"]   # Größerer Buffer → weniger Knacksen auf Pi Zero
        sink = _get_default_sink()
        if sink:
            cmd.append(f"--device={sink}")
        self.process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, env=PIPEWIRE_ENV, stderr=subprocess.PIPE
        )
        threading.Thread(target=self._log_stderr, daemon=True).start()
        time.sleep(0.3)
        if self.process.poll() is not None:
            print(f"DEBUG: KRITISCH - pacat-play sofort beendet! Exit Code: {self.process.poll()}")
        else:
            print(f"DEBUG: pacat-play gestartet (PID: {self.process.pid})")

    def _log_stderr(self):
        for line in self.process.stderr:
            txt = line.decode(errors="replace").strip()
            if txt:
                print(f"DEBUG: pacat-play: {txt}", flush=True)

    def write(self, audio_bytes: bytes):
        """Write a PCM16 chunk to the playback stream."""
        if self.process and self.process.stdin and self.process.poll() is None:
            try:
                self.process.stdin.write(audio_bytes)
                self.process.stdin.flush()
            except Exception as e:
                print(f"DEBUG: Player write error: {e}", flush=True)

    def stop(self):
        if self.process:
            try:
                self.process.stdin.close()
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Audio input
# ---------------------------------------------------------------------------

class AudioRecorder:
    """
    Wraps `pacat --record`.
    Continuously reads PCM16 chunks from the mic into audio_queue.
    Set muted=True to discard incoming audio (used during AI speech
    to prevent echo feedback into the API).
    """

    def __init__(self):
        self.process     = None
        self.audio_queue = queue.Queue()
        self._running    = False
        self.muted       = False

    def start(self):
        """Start the pacat recording process and reader thread."""
        self._running = True
        cmd = ["pacat", "--record", "--format=s16le",
               f"--rate={SAMPLE_RATE}", "--channels=1"]
        source = _get_default_source()
        if source:
            cmd.append(f"--device={source}")
        self.process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, env=PIPEWIRE_ENV, stderr=subprocess.PIPE
        )
        threading.Thread(target=self._log_stderr, daemon=True).start()
        threading.Thread(target=self._read_loop, daemon=True).start()
        print(f"DEBUG: pacat-record gestartet (PID: {self.process.pid})")

    def _log_stderr(self):
        for line in self.process.stderr:
            txt = line.decode(errors="replace").strip()
            if txt:
                print(f"DEBUG: pacat-record: {txt}", flush=True)

    def _read_loop(self):
        """Background thread: reads mic chunks and enqueues them."""
        while self._running and self.process and self.process.poll() is None:
            try:
                data = self.process.stdout.read(CHUNK_SIZE)
                if data and not self.muted:
                    self.audio_queue.put(data)
            except Exception as e:
                print(f"DEBUG: Mic-Read Fehler: {e}", flush=True)
                break
        print("DEBUG: Mic-Thread beendet.", flush=True)

    def stop(self):
        self._running = False
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# OpenAI Realtime session
# ---------------------------------------------------------------------------

async def realtime_session(
    player: AudioPlayer,
    recorder: AudioRecorder,
    stop_event: asyncio.Event,
    session_stop: asyncio.Event,
    display=None
):
    """
    Manages one WebSocket session with the OpenAI Realtime API.

    Sends mic audio to OpenAI and plays back the AI's audio response.
    Handles all API events and updates the display state accordingly.

    Args:
        player:       AudioPlayer — speaker output
        recorder:     AudioRecorder — mic input
        stop_event:   asyncio.Event — set by SIGINT for global shutdown
        session_stop: asyncio.Event — set after INACTIVITY_TIMEOUT to end this session
        display:      Optional EyeDisplay for HDMI output
    """
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    print("DEBUG: Verbinde mit OpenAI Realtime API...")
    async with websockets.connect(WS_URL, additional_headers=headers, max_size=None) as ws:
        print("DEBUG: Verbunden!")

        # Configure the session parameters
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "modalities": ["audio", "text"],
                "voice": VOICE,
                "instructions": INSTRUCTIONS,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {"model": "whisper-1"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 500,   # ms of audio kept before speech onset
                    "silence_duration_ms": 900,  # ms of silence before turn ends
                },
            }
        }))

        # Trigger Peter's opening greeting immediately
        await ws.send(json.dumps({"type": "response.create"}))

        ai_speaking = False
        _inactivity_task = None
        loop = asyncio.get_event_loop()

        def _is_stopping():
            return stop_event.is_set() or session_stop.is_set()

        async def _arm_inactivity_timer():
            """Start/reset the inactivity countdown. Fires after INACTIVITY_TIMEOUT s."""
            nonlocal _inactivity_task
            if _inactivity_task:
                _inactivity_task.cancel()

            async def _timeout():
                await asyncio.sleep(INACTIVITY_TIMEOUT)
                if not _is_stopping():
                    print(
                        f"\nDEBUG: Keine Aktivität seit {INACTIVITY_TIMEOUT}s — "
                        "Verbindung getrennt, gehe schlafen.", flush=True
                    )
                    session_stop.set()

            _inactivity_task = asyncio.create_task(_timeout())

        # Start inactivity timer immediately when session opens
        await _arm_inactivity_timer()

        async def send_audio():
            """Reads mic chunks from queue and streams them to OpenAI."""
            while not _is_stopping():
                try:
                    audio_data = await loop.run_in_executor(
                        None, lambda: recorder.audio_queue.get(timeout=0.05)
                    )
                    await ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(audio_data).decode(),
                    }))
                except queue.Empty:
                    pass
                except Exception as e:
                    print(f"DEBUG: Send-Audio Fehler: {e}", flush=True)
                    break

        async def receive_events():
            """Handles all incoming events from the OpenAI Realtime API."""
            nonlocal ai_speaking, _inactivity_task
            async for message in ws:
                if _is_stopping():
                    break
                event = json.loads(message)
                etype = event.get("type", "")

                if etype == "response.audio.delta":
                    # AI is sending audio — play it and mute mic to prevent echo
                    if not ai_speaking:
                        ai_speaking = True
                        recorder.muted = True
                        # Timer stoppen während KI spricht — nicht einschlafen mid-sentence
                        if _inactivity_task:
                            _inactivity_task.cancel()
                            _inactivity_task = None
                        print("\nDEBUG: KI spricht — Mikrofon stumm.", flush=True)
                        if display:
                            display.set_state(STATE_SPEAKING)
                            display.clear_text()
                    audio_bytes = base64.b64decode(event["delta"])
                    await loop.run_in_executor(None, player.write, audio_bytes)

                elif etype == "response.audio_transcript.delta":
                    # Streaming text transcript of AI speech → update display live
                    if display:
                        display.append_text(event.get("delta", ""))

                elif etype == "response.done":
                    # AI finished — wait for echo to settle, then unmute mic
                    ai_speaking = False
                    if display:
                        display.set_state(STATE_IDLE)
                    await asyncio.sleep(2.5)
                    flushed = 0
                    while not recorder.audio_queue.empty():
                        try:
                            recorder.audio_queue.get_nowait()
                            flushed += 1
                        except queue.Empty:
                            break
                    if flushed:
                        print(f"DEBUG: {flushed} Echo-Chunks verworfen.", flush=True)
                    recorder.muted = False
                    print("DEBUG: KI fertig — Mikrofon aktiv.", flush=True)
                    # Restart inactivity countdown after each AI response
                    await _arm_inactivity_timer()

                elif etype == "response.audio_transcript.done":
                    # Full transcript available — show final text on display
                    transcript = event.get("transcript", "")
                    print(f"\nAgent: {transcript}", flush=True)
                    if display:
                        display.set_text(transcript)

                elif etype == "conversation.item.input_audio_transcription.completed":
                    # Whisper transcript of the user's speech
                    print(f"\nDu: {event.get('transcript', '')}", flush=True)

                elif etype == "input_audio_buffer.speech_started":
                    # VAD detected user starting to speak — reset inactivity timer
                    print("\nDEBUG: Sprache erkannt...", flush=True)
                    await _arm_inactivity_timer()
                    if display:
                        display.set_state(STATE_LISTENING)
                        display.clear_text()

                elif etype == "error":
                    print(f"\nFEHLER von OpenAI: {json.dumps(event, ensure_ascii=False)}", flush=True)

                elif etype not in {
                    # Known informational events — no action needed
                    "session.created", "session.updated", "response.created",
                    "response.output_item.added", "response.content_part.added",
                    "response.content_part.done", "response.output_item.done",
                    "input_audio_buffer.speech_stopped", "input_audio_buffer.committed",
                    "conversation.item.created", "rate_limits.updated",
                    "response.audio.done", "response.audio_transcript.delta",
                    "conversation.item.input_audio_transcription.delta",
                }:
                    print(f"DEBUG: Unbekanntes Event: {etype}", flush=True)

        # Run send and receive concurrently; end on global shutdown OR inactivity
        t1 = asyncio.create_task(send_audio())
        t2 = asyncio.create_task(receive_events())
        t_stop    = asyncio.create_task(stop_event.wait())
        t_session = asyncio.create_task(session_stop.wait())
        await asyncio.wait([t_stop, t_session], return_when=asyncio.FIRST_COMPLETED)
        t_stop.cancel()
        t_session.cancel()
        if _inactivity_task:
            _inactivity_task.cancel()
        t1.cancel()
        t2.cancel()
        await ws.close()
        await asyncio.gather(t1, t2, return_exceptions=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _wait_for_audio_device(timeout: int = 30):
    """
    Blockiert bis PipeWire ein echtes Audio-Gerät (kein auto_null) meldet.
    Wichtig beim Autostart: USB-Geräte brauchen nach dem Boot etwas Zeit.
    """
    print("Warte auf Audio-Gerät...", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        sink = _get_default_sink()
        source = _get_default_source()
        if sink and "auto_null" not in sink and source and "auto_null" not in source:
            print(f"Audio-Gerät bereit: {sink.split('.')[-1]}", flush=True)
            return
        time.sleep(2)
    print("WARNUNG: Kein echtes Audio-Gerät gefunden, fahre trotzdem fort.", flush=True)


def main():
    _wait_for_audio_device()
    play_startup_sound()
    wake_word_mode = bool(PORCUPINE_ACCESS_KEY)
    mode_str = f'Wake-Word "{WAKE_WORD}"' if wake_word_mode else "Always-On"
    print(f"Initialisiere ElevenLexa — Modus: {mode_str}")

    display = EyeDisplay() if DISPLAY_AVAILABLE else None
    if display:
        display.start()

    player   = AudioPlayer()
    recorder = AudioRecorder()
    player.start()
    # recorder wird nur im aktiven Modus gestartet (pacat-Konflikt mit Wake-Word-pacat)

    loop       = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_event = asyncio.Event()

    # shutdown_flag für den synchronen Wake-Word-Thread (threading.Event)
    _shutdown_flag = threading.Event()

    def shutdown(sig, frame):
        print("\nBeende...")
        _shutdown_flag.set()
        loop.call_soon_threadsafe(stop_event.set)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    async def run():
        """
        Haupt-Schleife:
          SCHLAFEN  → Wake-Word abwarten (blockierend in Thread-Executor)
          AKTIV     → OpenAI-Session, reconnect bei Verbindungsabbruch
          → Inaktivität → zurück zu SCHLAFEN
        """
        while not stop_event.is_set():

            # ── SCHLAFEN: Wake-Word abwarten ─────────────────────────────
            if wake_word_mode:
                if display:
                    display.set_state(STATE_SLEEPING)
                    display.set_text("")

                detected = await loop.run_in_executor(
                    None, listen_for_wake_word, _shutdown_flag
                )
                if not detected or stop_event.is_set():
                    break

                play_wake_sound()

            # ── AKTIV: Mikrofon + OpenAI-Session ─────────────────────────
            if display:
                display.set_state(STATE_IDLE)

            recorder.start()
            print("--- Voice-Modus aktiv. Sprich mit Peter! ---")

            # Reconnect-Schleife innerhalb einer aktiven Session
            # (trennt z.B. bei Netzwerkabbruch, aber nicht bei Inaktivitäts-Timeout)
            while not stop_event.is_set():
                session_stop = asyncio.Event()
                try:
                    await realtime_session(
                        player, recorder, stop_event, session_stop, display
                    )
                except Exception as e:
                    if stop_event.is_set():
                        break
                    print(f"DEBUG: Verbindung unterbrochen ({e}), reconnect in 3s...", flush=True)
                    await asyncio.sleep(3)
                    continue  # Reconnect

                # Sauber beendet: prüfen ob Inaktivität oder globaler Shutdown
                if session_stop.is_set() and not stop_event.is_set():
                    # Inaktivität → Session beenden, zurück zu Wake-Word
                    break
                break  # stop_event oder normales Ende

            recorder.stop()

            if stop_event.is_set():
                break

            # Wenn kein Wake-Word-Modus: sofort reconnecten (Always-On-Fallback)
            if not wake_word_mode:
                await asyncio.sleep(3)

    try:
        loop.run_until_complete(run())
    except Exception as e:
        import traceback
        print(f"Fehler: {e}")
        traceback.print_exc()
    finally:
        player.stop()
        recorder.stop()
        if display:
            display.stop()
        loop.close()


if __name__ == "__main__":
    main()
