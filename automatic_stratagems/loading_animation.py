"""Play the scanner's RGBA PNG frames without the RGB-only video decoder."""
from functools import lru_cache
from pathlib import Path
from time import monotonic
from threading import RLock

from PIL import Image, ImageSequence


FPS = 30


@lru_cache(maxsize=1)
def loading_frames():
    with Image.open(Path(__file__).parent / 'assets/icons/scanning.png') as image:
        return tuple(frame.convert('RGBA') for frame in ImageSequence.Iterator(image))


class LoadingAnimation:
    """Own one cancellable timer; callers decide whether playback is still valid."""

    def __init__(self, scheduler, display, active, *, frames=None, clock=monotonic):
        self.scheduler = scheduler
        self.display = display
        self.active = active
        self.frames = loading_frames() if frames is None else frames
        self.clock = clock
        self.source = None
        self.generation = 0
        self.lock = RLock()

    def start(self):
        with self.lock:
            if self.source is not None:
                return
            self.generation += 1
            generation = self.generation
            started = self.clock()
            self.display(self.frames[0].copy())

            def tick():
                with self.lock:
                    if generation != self.generation:
                        return False
                    try:
                        if not self.active():
                            self.source = None
                            return False
                        index = int((self.clock() - started) * FPS) % len(self.frames)
                        self.display(self.frames[index].copy())
                        return True
                    except Exception:
                        self.source = None
                        self.generation += 1
                        raise

            self.source = self.scheduler.timeout_add(round(1000 / FPS), tick)

    def stop(self):
        # Finish any in-flight frame before the caller restores its static icon.
        with self.lock:
            self.generation += 1
            if self.source is not None:
                self.scheduler.source_remove(self.source)
                self.source = None
