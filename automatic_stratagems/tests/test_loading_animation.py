import unittest
from threading import Event, Thread
from PIL import Image
from automatic_stratagems.loading_animation import LoadingAnimation, loading_frames


class Scheduler:
    def __init__(self):
        self.callbacks = {}
        self.next_id = 0

    def timeout_add(self, interval, callback):
        self.next_id += 1
        self.callbacks[self.next_id] = callback
        return self.next_id

    def source_remove(self, source):
        self.callbacks.pop(source, None)


class LoadingAnimationTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = Scheduler()
        self.visible = True
        self.images = []
        self.now = 0
        self.frames = (Image.new('RGBA', (2, 2), (255, 200, 0, 128)),
                       Image.new('RGBA', (2, 2), (255, 255, 0, 128)))
        self.animation = LoadingAnimation(
            self.scheduler, self.images.append, lambda: self.visible,
            frames=self.frames, clock=lambda: self.now)

    def test_frames_advance_loop_and_preserve_alpha_without_restarting(self):
        self.animation.start()
        self.animation.start()
        self.assertEqual(len(self.scheduler.callbacks), 1)
        tick = next(iter(self.scheduler.callbacks.values()))
        self.now = 1 / 30
        self.assertTrue(tick())
        self.assertEqual(self.images[-1].getpixel((0, 0)), (255, 255, 0, 128))
        self.now = 2 / 30
        tick()
        self.assertEqual(self.images[-1].getpixel((0, 0)), (255, 200, 0, 128))

    def test_stop_cancels_source_and_late_callback_cannot_replace_static_icon(self):
        self.animation.start()
        tick = next(iter(self.scheduler.callbacks.values()))
        self.animation.stop()
        count = len(self.images)
        self.assertFalse(tick())
        self.assertEqual(len(self.images), count)
        self.assertFalse(self.scheduler.callbacks)
        self.animation.start()
        self.assertFalse(tick())

    def test_hidden_or_finished_action_stops_without_another_frame(self):
        self.animation.start()
        tick = next(iter(self.scheduler.callbacks.values()))
        self.visible = False
        self.assertFalse(tick())
        self.assertEqual(len(self.images), 1)

    def test_asset_has_motion_and_transparent_background(self):
        frames = loading_frames()
        self.assertEqual(len(frames), 60)
        self.assertEqual(frames[0].size, (144, 144))
        self.assertNotEqual(frames[0].tobytes(), frames[15].tobytes())
        for frame in frames:
            self.assertEqual(frame.mode, 'RGBA')
            self.assertEqual(frame.getpixel((0, 0))[3], 0)
            self.assertEqual(frame.getpixel((143, 143))[3], 0)
            self.assertGreater(frame.getchannel('A').getextrema()[1], 200)

    def test_stop_waits_for_in_flight_frame_before_static_restore(self):
        entered = Event()
        release = Event()
        stopped = Event()
        output = []

        def display(image):
            if output:
                entered.set()
                self.assertTrue(release.wait(2))
            output.append('loading')

        animation = LoadingAnimation(self.scheduler, display, lambda: True,
                                     frames=self.frames)
        animation.start()
        tick = next(iter(self.scheduler.callbacks.values()))
        worker = Thread(target=tick)
        worker.start()
        self.assertTrue(entered.wait(2))

        def restore():
            animation.stop()
            output.append('static')
            stopped.set()

        restorer = Thread(target=restore)
        restorer.start()
        try:
            self.assertFalse(stopped.wait(.05))
        finally:
            release.set()
            worker.join(2)
            restorer.join(2)
        self.assertTrue(stopped.is_set())
        self.assertEqual(output, ['loading', 'loading', 'static'])
