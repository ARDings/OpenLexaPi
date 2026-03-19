"""
ElevenLexa Launcher
===================
Wartet bis PipeWire + USB-Mic bereit sind, zeigt einen Countdown
auf dem Display, dann startet main.py.

Warum ein separater Launcher?
  Der systemd-Service startet früh im Boot-Prozess. PipeWire ist zu
  diesem Zeitpunkt noch nicht vollständig initialisiert, selbst wenn
  sound.target erreicht wurde. Würde main.py direkt starten, findet
  Porcupine das Mikrofon nicht zuverlässig und reagiert kaum auf das
  Wake-Word. Dieser Launcher wartet explizit bis das USB-Audiogerät
  als alsa_input in PipeWire auftaucht, zählt dann sichtbar herunter
  und übergibt mit os.execv direkt an main.py (gleicher Prozess).
"""

import os
import subprocess
import sys
import time

PIPEWIRE_ENV = {**os.environ, "XDG_RUNTIME_DIR": "/run/user/1000"}
COUNTDOWN = 10


def wait_for_mic():
    print("Warte auf USB-Mikrofon...", flush=True)
    while True:
        try:
            out = subprocess.check_output(
                ["pactl", "list", "sources", "short"],
                env=PIPEWIRE_ENV, stderr=subprocess.DEVNULL
            ).decode()
            if "alsa_input" in out:
                print("USB-Mic gefunden.", flush=True)
                return
        except Exception:
            pass
        time.sleep(1)


def show_countdown():
    try:
        import pygame
        os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")

        drivers = ["kmsdrm", "fbcon", "x11", "wayland"]
        if os.environ.get("DISPLAY"):
            drivers = ["x11", "wayland"] + drivers

        initialized = False
        for drv in drivers:
            os.environ["SDL_VIDEODRIVER"] = drv
            try:
                pygame.display.init()
                initialized = True
                break
            except Exception:
                continue

        if not initialized:
            _countdown_terminal()
            return

        pygame.font.init()
        screen = pygame.display.set_mode((800, 480), pygame.FULLSCREEN | pygame.NOFRAME)
        pygame.display.set_caption("ElevenLexa")
        pygame.mouse.set_visible(False)
        font_big   = pygame.font.SysFont(None, 160)
        font_small = pygame.font.SysFont(None, 48)
        BG    = (10, 28, 10)
        GREEN = (140, 220, 100)
        DIM   = (60, 120, 50)

        for i in range(COUNTDOWN, 0, -1):
            screen.fill(BG)
            lbl = font_small.render("ElevenLexa startet in...", True, DIM)
            screen.blit(lbl, lbl.get_rect(center=(400, 180)))
            num = font_big.render(str(i), True, GREEN)
            screen.blit(num, num.get_rect(center=(400, 300)))
            pygame.display.flip()
            time.sleep(1)
            for e in pygame.event.get():
                pass  # drain events

        screen.fill(BG)
        lbl = font_small.render("Los geht's!", True, GREEN)
        screen.blit(lbl, lbl.get_rect(center=(400, 300)))
        pygame.display.flip()
        time.sleep(0.5)
        pygame.quit()

    except Exception as e:
        print(f"Display nicht verfügbar ({e}), nutze Terminal-Countdown.", flush=True)
        _countdown_terminal()


def _countdown_terminal():
    for i in range(COUNTDOWN, 0, -1):
        print(f"Starte in {i}s...", flush=True)
        time.sleep(1)


if __name__ == "__main__":
    wait_for_mic()
    show_countdown()
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    os.execv(sys.executable, [sys.executable, script])
