# OpenLexaPi

A real-time AI voice assistant running on a **Raspberry Pi Zero 1.1**, powered by the **OpenAI Realtime API**. Say a wake word to activate it, speak naturally, and it responds through a USB speaker. An animated robot face renders on an HDMI display.

```
┌─────────────────────────────────────┐
│  ◉ ◉  (eyes)                        │
│─────────────────────────────────────│
│   Hello! How can I help you today?  │
└─────────────────────────────────────┘
```

---

## Features

- **Wake-word activation** — says "Computer" to wake, sleeps automatically after inactivity
- **Fully offline wake-word detection** — Porcupine runs locally, ~1% CPU on Pi Zero
- **Real-time conversation** via OpenAI `gpt-4o-realtime-preview`
- **Multilingual** — responds in English or Korean depending on the speaker
- **Retro robot face** on HDMI display — eyes close when sleeping, open when active
- **Echo prevention** — mic is muted while the AI speaks, echo buffer is flushed after
- **Auto-reconnect** — transparently reconnects if the WebSocket drops
- **Graceful degradation** — runs headless (no display) without any code changes

---

## Hardware

| Component | Details |
|---|---|
| **Computer** | Raspberry Pi Zero 1.1 (single-core ARMv6 @ 1 GHz, 512 MB RAM) |
| **Speaker + Mic** | USB Speaker Bar (MZ-631M or similar USB audio device) |
| **Display** | Any 800×480 HDMI screen |
| **Audio server** | PipeWire |
| **OS** | Raspberry Pi OS (Bookworm) |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  SLEEPING                                                        │
│    Porcupine (offline) ◄── pacat --record 16kHz ◄── USB Mic    │
│         │ "Computer" detected                                    │
│         ▼                                                        │
│  ACTIVE                                                          │
│    USB Mic ──► pacat --record 24kHz ──► AudioRecorder.queue     │
│                                              │                   │
│                                         send_audio()             │
│                                              │                   │
│                                       OpenAI Realtime API        │
│                                              │                   │
│                                      receive_events()            │
│                                              │                   │
│                              AudioPlayer ◄───┘                   │
│                                  │                               │
│                    pacat --playback 24kHz ──► USB Speaker        │
│                                                                  │
│    [15s inactivity] ──► back to SLEEPING                        │
└─────────────────────────────────────────────────────────────────┘
```

**`launcher.py`** — boot-time launcher: waits for USB mic, shows countdown, then exec's into main.py
**`main.py`** — wake-word loop, WebSocket session, audio I/O, event handling
**`display.py`** — Pygame rendering loop (daemon thread)

---

## Design Decisions

### Wake-word: why Porcupine?

Running continuously connected to OpenAI is expensive and wasteful. A local wake-word detector lets the device sleep (no WebSocket, no API cost) until the user actually wants to speak.

Porcupine (by Picovoice) was chosen because:
- It ships a **pre-compiled ARM binary** that runs on ARMv6 (Pi Zero 1.1)
- CPU usage is ~1% — leaves the Pi's single core free for audio and WebSocket I/O
- It works **fully offline** — no network call for wake-word detection
- Free tier includes built-in keywords: `computer`, `jarvis`, `porcupine`, `bumblebee`, and more
- Custom keywords (.ppn files) can be trained for free at [console.picovoice.ai](https://console.picovoice.ai)

### Why OpenAI Realtime API?

The OpenAI Realtime API provides speech-to-text, language model inference, and text-to-speech in a single persistent WebSocket connection. This eliminates the need to chain three separate services (Whisper → GPT → TTS) and dramatically reduces latency. It also handles voice activity detection (VAD) server-side, so no local VAD library is needed.

### Why PipeWire instead of ALSA or PulseAudio?

Raspberry Pi OS Bookworm ships PipeWire as the default audio server. It handles resampling (USB devices run at 48 kHz natively; our pipeline uses 16/24 kHz) transparently. `pacat` (PulseAudio-compatible client) works directly against PipeWire via its PulseAudio compatibility layer.

**Important:** PipeWire's default source may be set to a `.monitor` (speaker loopback) rather than the real microphone input. The code explicitly queries `pactl list sources short` to find the first `alsa_input.*` device and passes it to pacat via `--device=`, bypassing this issue.

### Why pacat instead of a Python audio library?

Python audio libraries (PyAudio, sounddevice) require compiled native extensions and often have dependency conflicts on Raspberry Pi OS. `pacat` is a standard system tool, always available where PipeWire/PulseAudio is installed. It communicates via subprocess stdin/stdout, which is reliable, portable, and adds no Python dependencies.

### USB audio warm-up silence

USB audio devices suspend themselves when idle to save power. The first ~200 ms of audio after a period of silence gets "eaten" by the device waking up. All sound effects (startup chime, wake acknowledgement) prepend 300 ms of silence before the actual audio, ensuring the device is active before the tone begins.

### Inactivity timeout

After the AI finishes speaking (`response.done`), a 15-second inactivity timer starts. If the user doesn't speak within that window, the WebSocket is closed and the device returns to the sleeping (wake-word) state. The timer is cancelled while the AI is speaking (so long responses don't trigger a premature sleep) and reset whenever the user starts talking.

### Echo prevention strategy

The USB Speaker Bar's microphone is physically close to its speaker, making acoustic echo a problem. When the AI starts speaking, the microphone is muted in software (`recorder.muted = True`). After the AI finishes:

1. A 2.5-second silence allows the room echo to decay.
2. The microphone queue is flushed to discard any residual echo already captured.
3. The microphone is unmuted.

### Why 200×120 internal canvas for the display?

This project runs on a **Raspberry Pi Zero 1.1** — single-core ARMv6 @ 1 GHz, 512 MB RAM, no GPU. Rendering at full 800×480 every frame would saturate the CPU and starve the audio pipeline. By rendering animated elements on a 200×120 surface and scaling 4× with `pygame.transform.scale`, pixels touched per frame are reduced to 6.25% of full resolution. Combined with dirty-flag rendering, the display thread consumes a negligible fraction of CPU.

The 4× upscale creates a visible pixel grid that gives the robot face a retro LED-matrix aesthetic.

### Display states

| State | Eyes | When |
|---|---|---|
| `sleeping` | Closed (horizontal lines) | Waiting for wake word |
| `idle` | Open, pupils wandering | Session active, waiting for user |
| `listening` | Open | User is speaking |
| `speaking` | Open | AI is speaking |

---

## Setup

### 1. System packages

```bash
sudo apt update
sudo apt install -y pipewire pipewire-pulse fonts-nanum python3-pygame
```

### 2. Python dependencies

```bash
pip3 install --break-system-packages -r requirements.txt
```

### 3. Picovoice Access Key (free)

1. Create a free account at [console.picovoice.ai](https://console.picovoice.ai)
2. Copy your **Access Key** from the dashboard
3. Paste it into `main.py → PORCUPINE_ACCESS_KEY`

### 4. Configuration

Edit `main.py` and set:

```python
OPENAI_API_KEY       = "sk-..."       # Your OpenAI API key
PORCUPINE_ACCESS_KEY = "..."          # Your Picovoice access key (free)
WAKE_WORD            = "computer"     # Built-in keyword, or "custom"
WAKE_WORD_MODEL_PATH = ""             # Path to .ppn file if WAKE_WORD = "custom"
INACTIVITY_TIMEOUT   = 15            # Seconds of silence before going back to sleep
VOICE                = "verse"        # AI voice (alloy, echo, nova, shimmer, verse, ...)
INSTRUCTIONS         = "..."          # System prompt / personality
```

**Built-in free keywords** (no .ppn file needed):
`computer`, `jarvis`, `porcupine`, `bumblebee`, `alexa`, `grasshopper`, `blueberry`, `grapefruit`, `terminator`, `hey barista`, `americano`, `picovoice`

**Custom keyword** (e.g. "Hey Peter"):
Go to [console.picovoice.ai](https://console.picovoice.ai) → Wake Word → create your keyword → download the `.ppn` file for Raspberry Pi → set `WAKE_WORD = "custom"` and `WAKE_WORD_MODEL_PATH = "/path/to/file.ppn"`.

### 5. Autostart (systemd)

```bash
sudo nano /etc/systemd/system/openlexa.service
```

```ini
[Unit]
Description=OpenLexa AI Voice Assistant
After=network-online.target bluetooth.target sound.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/ElevenLexa
ExecStart=/usr/bin/python3 /home/pi/ElevenLexa/launcher.py
Restart=on-failure
RestartSec=5
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=DISPLAY=:0

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable openlexa.service
sudo systemctl start openlexa.service
```

> **Why `launcher.py` instead of `main.py` directly?**
> PipeWire is a user-level service and may not be fully initialised when the system service starts. `launcher.py` polls `pactl list sources` until the USB microphone (`alsa_input.*`) appears, then shows a countdown on the display before handing off to `main.py` via `os.execv`. This eliminates the race condition where Porcupine starts with a non-functional audio source and only reacts to very loud sounds.

### 6. Run manually

```bash
python3 main.py
```

---

## File Structure

```
OpenLexaPi/
├── launcher.py       # Boot launcher — waits for USB mic, countdown, then starts main.py
├── main.py           # Main application — wake-word loop, audio pipeline, OpenAI session
├── display.py        # Pygame HDMI display (optional, auto-detected)
├── requirements.txt  # Python dependencies
├── README.md         # This file
└── archive/          # Old debug scripts (not needed for running)
```

---

## Customisation

| What | Where | How |
|---|---|---|
| Wake word | `main.py → WAKE_WORD` | Built-in keyword name or `"custom"` |
| Custom wake word | `main.py → WAKE_WORD_MODEL_PATH` | Path to `.ppn` file from Picovoice Console |
| Inactivity timeout | `main.py → INACTIVITY_TIMEOUT` | Seconds before returning to sleep |
| AI personality | `main.py → INSTRUCTIONS` | Edit the system prompt string |
| Voice | `main.py → VOICE` | Any OpenAI Realtime voice name |
| Languages | `main.py → INSTRUCTIONS` | Change language instructions |
| VAD sensitivity | `main.py → turn_detection.threshold` | 0.0–1.0, lower = more sensitive |
| Eye colours | `display.py → colour constants` | RGB tuples at the top of the file |
| Display layout | `display.py → EYE_AREA_H, TEXT_AREA_Y` | Adjust the eye/text split point |

---

## License

MIT — free to use, modify, and distribute.
