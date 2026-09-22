from concurrent.futures import CancelledError
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from automatic_stratagems.scanner.errors import ScanError
from automatic_stratagems.scanner import image_source


class ImageSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def image(self, name, color="red", *, format=None, size=(12, 8)):
        path = self.root / name
        Image.new("RGB", size, color).save(path, format=format)
        return path

    def document_portal_source(self, link_target="../../flatpak/doc"):
        mount = self.root / "run/user/1000/doc"
        mount.parent.mkdir(parents=True)
        exported = self.root / "run/flatpak/doc/document-id"
        exported.mkdir(parents=True)
        image = exported / "frame.png"
        Image.new("RGB", (12, 8), "red").save(image)
        mount.symlink_to(link_target, target_is_directory=True)
        flatpak_info = self.root / ".flatpak-info"
        flatpak_info.touch()
        lexical = mount / "document-id/frame.png"
        return mount, flatpak_info, lexical

    def test_path_source_detects_file_or_folder(self):
        file = self.image('frame.png')
        for path, selection in ((file, 'file'), (self.root, 'folder')):
            with self.subTest(selection=selection):
                image, metadata = image_source.read_image_source('path', path)
                self.assertEqual(image.size, (12, 8))
                self.assertEqual(metadata['selection'], selection)
                self.assertEqual(metadata['path'], str(file))

    def test_path_source_rejects_missing_and_symlink_paths(self):
        with self.assertRaises(ScanError):
            image_source.read_image_source('path', self.root / 'missing')
        target = self.image('frame.png')
        link = self.root / 'link.png'
        link.symlink_to(target)
        with self.assertRaisesRegex(ScanError, 'symlink'):
            image_source.read_image_source('path', link)

    def test_file_returns_rgb_image_and_read_only_metadata(self):
        path = self.image("frame.PNG", "red")
        before = (path.read_bytes(), path.stat().st_mtime_ns)

        loaded, metadata = image_source.read_image_source("file", path)

        self.assertEqual(loaded.mode, "RGB")
        self.assertEqual(loaded.getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(metadata, {
            "kind": "file",
            "selection": "file",
            "path": str(path.absolute()),
            "mtime_ns": path.stat().st_mtime_ns,
            "size_bytes": path.stat().st_size,
            "fingerprint": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_file_accepts_jpeg_case_insensitively(self):
        path = self.image("frame.JpEg", format="JPEG")

        loaded, metadata = image_source.read_image_source("file", path)

        self.assertEqual(loaded.size, (12, 8))
        self.assertEqual(metadata["path"], str(path.absolute()))

    def test_file_expands_user_before_lexical_absolute_normalization(self):
        path = self.image("frame.png")

        with patch.dict(os.environ, {"HOME": str(self.root)}):
            _loaded, metadata = image_source.read_image_source(
                "file", "~/frame.png")

        self.assertEqual(metadata["path"], str(path.absolute()))

    def test_folder_selects_newest_supported_regular_file_nonrecursively(self):
        old = self.image("old.png", "red")
        newest = self.image("newest.jpg", "blue", format="JPEG")
        ignored = self.root / "nested"
        ignored.mkdir()
        Image.new("RGB", (12, 8), "green").save(ignored / "later.png")
        stamp = time.time_ns()
        os.utime(old, ns=(stamp - 2_000_000, stamp - 2_000_000))
        os.utime(newest, ns=(stamp - 1_000_000, stamp - 1_000_000))
        os.utime(ignored / "later.png", ns=(stamp, stamp))

        loaded, metadata = image_source.read_image_source("folder", self.root)

        self.assertEqual(metadata["selection"], "folder")
        self.assertEqual(metadata["kind"], "file")
        self.assertEqual(metadata["path"], str(newest.absolute()))
        self.assertGreater(loaded.getpixel((0, 0))[2], 200)

    def test_folder_rejects_tied_newest_images(self):
        first = self.image("first.png")
        second = self.image("second.jpg", format="JPEG")
        stamp = time.time_ns()
        os.utime(first, ns=(stamp, stamp))
        os.utime(second, ns=(stamp, stamp))

        with self.assertRaisesRegex(ScanError, "multiple newest"):
            image_source.read_image_source("folder", self.root)

    def test_folder_does_not_fall_back_from_corrupt_newest_image(self):
        old = self.image("old.png", "green")
        newest = self.root / "new.jpg"
        newest.write_bytes(b"partial jpeg")
        stamp = time.time_ns()
        os.utime(old, ns=(stamp - 1_000_000, stamp - 1_000_000))
        os.utime(newest, ns=(stamp, stamp))

        with self.assertRaisesRegex(ScanError, "unreadable image"):
            image_source.read_image_source("folder", self.root)

    def test_folder_rejects_newest_supported_symlink_without_fallback(self):
        old = self.image("old.png")
        link = self.root / "new.jpg"
        link.symlink_to(old)
        stamp = time.time_ns()
        os.utime(old, ns=(stamp - 1_000_000, stamp - 1_000_000))
        os.utime(link, ns=(stamp, stamp), follow_symlinks=False)

        with self.assertRaisesRegex(ScanError, "regular file"):
            image_source.read_image_source("folder", self.root)

    def test_file_and_folder_symlink_inputs_are_rejected(self):
        target = self.image("target.png")
        file_link = self.root / "file-link.png"
        file_link.symlink_to(target)
        folder = self.root / "folder"
        folder.mkdir()
        folder_link = self.root / "folder-link"
        folder_link.symlink_to(folder, target_is_directory=True)

        with self.assertRaisesRegex(ScanError, "symlink"):
            image_source.read_image_source("file", file_link)
        with self.assertRaisesRegex(ScanError, "symlink"):
            image_source.read_image_source("folder", folder_link)

    def test_symlinked_path_components_are_rejected(self):
        target = self.root / "target"
        target.mkdir()
        Image.new("RGB", (12, 8), "red").save(target / "frame.png")
        alias = self.root / "alias"
        alias.symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(ScanError, "symlink"):
            image_source.read_image_source("file", alias / "frame.png")
        with self.assertRaisesRegex(ScanError, "symlink"):
            image_source.read_image_source("folder", alias)

    def test_flatpak_document_portal_alias_is_allowed_and_kept_lexical(self):
        mount, flatpak_info, path = self.document_portal_source()

        with patch.object(image_source, "DOCUMENT_PORTAL_ALIAS", mount,
                          create=True), \
             patch.object(image_source, "FLATPAK_INFO", flatpak_info,
                          create=True):
            loaded, metadata = image_source.read_image_source("file", path)

        self.assertEqual(loaded.size, (12, 8))
        self.assertEqual(metadata["path"], str(path))

    def test_flatpak_document_portal_alias_supports_folder_source(self):
        mount, flatpak_info, path = self.document_portal_source()
        folder = path.parent

        with patch.object(image_source, "DOCUMENT_PORTAL_ALIAS", mount,
                          create=True), \
             patch.object(image_source, "FLATPAK_INFO", flatpak_info,
                          create=True):
            loaded, metadata = image_source.read_image_source("folder", folder)

        self.assertEqual(loaded.size, (12, 8))
        self.assertEqual(metadata["selection"], "folder")
        self.assertEqual(metadata["path"], str(path))

    def test_document_portal_alias_requires_flatpak_and_exact_target(self):
        mount, flatpak_info, path = self.document_portal_source()
        flatpak_info.unlink()
        with patch.object(image_source, "DOCUMENT_PORTAL_ALIAS", mount,
                          create=True), \
             patch.object(image_source, "FLATPAK_INFO", flatpak_info,
                          create=True), \
             self.assertRaisesRegex(ScanError, "symlink"):
            image_source.read_image_source("file", path)

        mount.unlink()
        mount.symlink_to("../../flatpak/not-doc", target_is_directory=True)
        flatpak_info.touch()
        with patch.object(image_source, "DOCUMENT_PORTAL_ALIAS", mount,
                          create=True), \
             patch.object(image_source, "FLATPAK_INFO", flatpak_info,
                          create=True), \
             self.assertRaisesRegex(ScanError, "symlink"):
            image_source.read_image_source("file", path)

    def test_document_portal_does_not_allow_other_symlinks(self):
        mount, flatpak_info, path = self.document_portal_source()
        other = path.with_name("other.png")
        other.symlink_to(path.name)

        with patch.object(image_source, "DOCUMENT_PORTAL_ALIAS", mount,
                          create=True), \
             patch.object(image_source, "FLATPAK_INFO", flatpak_info,
                          create=True), \
             self.assertRaisesRegex(ScanError, "symlink"):
            image_source.read_image_source("file", other)

    def test_document_portal_alias_identity_is_revalidated_after_decode(self):
        mount, flatpak_info, path = self.document_portal_source()
        decode = image_source._decode_image
        old_mount = mount.with_name("old-doc")

        def replace_alias(encoded):
            loaded = decode(encoded)
            mount.rename(old_mount)
            mount.symlink_to("../../flatpak/doc", target_is_directory=True)
            return loaded

        with patch.object(image_source, "DOCUMENT_PORTAL_ALIAS", mount,
                          create=True), \
             patch.object(image_source, "FLATPAK_INFO", flatpak_info,
                          create=True), \
             patch.object(image_source, "_decode_image",
                          side_effect=replace_alias), \
             self.assertRaisesRegex(ScanError, "path changed"):
            image_source.read_image_source("file", path)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires POSIX")
    def test_supported_fifo_is_rejected_without_blocking(self):
        fifo = self.root / "incoming.png"
        os.mkfifo(fifo)
        started = time.monotonic()

        with self.assertRaisesRegex(ScanError, "regular file"):
            image_source.read_image_source("file", fifo)

        self.assertLess(time.monotonic() - started, 1)

    def test_rejects_unsupported_extension_and_encoded_format(self):
        unsupported = self.image("frame.bmp")
        disguised = self.image("frame.png", format="GIF")

        with self.assertRaisesRegex(ScanError, "PNG or JPEG"):
            image_source.read_image_source("file", unsupported)
        with self.assertRaisesRegex(ScanError, "PNG or JPEG"):
            image_source.read_image_source("file", disguised)

    def test_rejects_empty_folder_and_excessive_enumeration(self):
        with self.assertRaisesRegex(ScanError, "no PNG or JPEG"):
            image_source.read_image_source("folder", self.root)
        for index in range(3):
            (self.root / f"ignored-{index}.txt").write_text("x")

        with patch.object(image_source, "MAX_DIRECTORY_ENTRIES", 2), \
             self.assertRaisesRegex(ScanError, "too many entries"):
            image_source.read_image_source("folder", self.root)

    def test_rejects_oversized_bytes_and_dimensions(self):
        path = self.image("frame.png", size=(12, 8))
        with patch.object(image_source, "MAX_ENCODED_IMAGE_BYTES", 8), \
             patch.object(image_source.os, "read",
                          side_effect=AssertionError("must reject before read")), \
             self.assertRaisesRegex(ScanError, "too large"):
            image_source.read_image_source("file", path)
        with patch.object(image_source, "MAX_IMAGE_PIXELS", 50), \
             self.assertRaisesRegex(ScanError, "dimensions"):
            image_source.read_image_source("file", path)

    def test_waits_for_repeated_stable_identity_before_reading(self):
        path = self.image("frame.png", "red")
        initial_mtime = path.stat().st_mtime_ns
        original_sleep = image_source.time.sleep
        changed = False

        def replace_once(seconds):
            nonlocal changed
            if not changed:
                Image.new("RGB", (12, 8), "blue").save(path)
                os.utime(path, ns=(initial_mtime + 1, initial_mtime + 1))
                changed = True
            original_sleep(0)

        with patch.object(image_source.time, "sleep", side_effect=replace_once):
            loaded, metadata = image_source.read_image_source("file", path)

        self.assertTrue(changed)
        self.assertEqual(loaded.getpixel((0, 0)), (0, 0, 255))
        self.assertEqual(metadata["fingerprint"],
                         hashlib.sha256(path.read_bytes()).hexdigest())

    def test_post_decode_file_change_is_rejected(self):
        path = self.image("frame.png", "red")
        decode = image_source._decode_image

        def mutate(encoded):
            loaded = decode(encoded)
            Image.new("RGB", (12, 8), "blue").save(path)
            return loaded

        with patch.object(image_source, "_decode_image", side_effect=mutate), \
             self.assertRaisesRegex(ScanError, "changed while"):
            image_source.read_image_source("file", path)

    def test_post_decode_file_replacement_is_rejected(self):
        path = self.image("frame.png", "red")
        replacement = self.image("replacement.png", "blue")
        decode = image_source._decode_image

        def replace(encoded):
            loaded = decode(encoded)
            os.replace(replacement, path)
            return loaded

        with patch.object(image_source, "_decode_image", side_effect=replace), \
             self.assertRaisesRegex(ScanError, "changed while"):
            image_source.read_image_source("file", path)

    def test_post_decode_symlinked_ancestor_substitution_is_rejected(self):
        source = self.root / "source"
        source.mkdir()
        path = source / "frame.png"
        Image.new("RGB", (12, 8), "red").save(path)
        moved = self.root / "moved"
        decode = image_source._decode_image

        def substitute(encoded):
            loaded = decode(encoded)
            source.rename(moved)
            source.symlink_to(moved, target_is_directory=True)
            return loaded

        with patch.object(image_source, "_decode_image", side_effect=substitute), \
             self.assertRaisesRegex(ScanError, "symlink"):
            image_source.read_image_source("file", path)

    def test_post_decode_newer_folder_selection_is_rejected(self):
        old = self.image("old.png", "red")
        decode = image_source._decode_image

        def add_newer(encoded):
            loaded = decode(encoded)
            newer = self.image("newer.png", "blue")
            stamp = old.stat().st_mtime_ns + 1_000_000
            os.utime(newer, ns=(stamp, stamp))
            return loaded

        with patch.object(image_source, "_decode_image", side_effect=add_newer), \
             self.assertRaisesRegex(ScanError, "selection changed"):
            image_source.read_image_source("folder", self.root)

    def test_post_decode_newest_folder_tie_is_rejected(self):
        selected = self.image("selected.png", "red")
        decode = image_source._decode_image

        def add_tie(encoded):
            loaded = decode(encoded)
            tied = self.image("tied.jpg", "blue", format="JPEG")
            stamp = selected.stat().st_mtime_ns
            os.utime(tied, ns=(stamp, stamp))
            return loaded

        with patch.object(image_source, "_decode_image", side_effect=add_tie), \
             self.assertRaisesRegex(ScanError, "multiple newest"):
            image_source.read_image_source("folder", self.root)

    def test_folder_identity_change_during_post_decode_enumeration_is_rejected(self):
        folder = self.root / "source"
        folder.mkdir()
        selected = folder / "selected.png"
        Image.new("RGB", (12, 8), "red").save(selected)
        moved = self.root / "moved"
        scandir = image_source.os.scandir
        calls = 0

        def replace_folder_on_final_scan(path):
            nonlocal calls
            calls += 1
            if calls == 3:
                folder.rename(moved)
                folder.mkdir()
                (moved / selected.name).rename(folder / selected.name)
            return scandir(path)

        with patch.object(image_source.os, "scandir",
                          side_effect=replace_folder_on_final_scan), \
             self.assertRaisesRegex(ScanError, "folder changed"):
            image_source.read_image_source("folder", folder)
        self.assertEqual(calls, 3)

    def test_cancelled_before_and_during_stability_wait(self):
        path = self.image("frame.png")
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(CancelledError):
            image_source.read_image_source("file", path, cancel_event=cancelled)

        cancelled.clear()
        original_sleep = image_source.time.sleep

        def cancel(seconds):
            cancelled.set()
            original_sleep(0)

        with patch.object(image_source.time, "sleep", side_effect=cancel), \
             self.assertRaises(CancelledError):
            image_source.read_image_source("file", path, cancel_event=cancelled)

    def test_folder_enumeration_preserves_cancellation(self):
        for index in range(4):
            (self.root / f"ignored-{index}.txt").write_text("x")

        class CancelDuringEnumeration:
            calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls >= 4

        with self.assertRaises(CancelledError):
            image_source.read_image_source(
                "folder", self.root,
                cancel_event=CancelDuringEnumeration())

    def test_expired_deadline_and_unstable_source_are_bounded(self):
        path = self.image("frame.png")
        with self.assertRaisesRegex(ScanError, "deadline exhausted"):
            image_source.read_image_source(
                "file", path, deadline=time.monotonic() - 1)

        original_sleep = image_source.time.sleep
        counter = 0

        def keep_changing(seconds):
            nonlocal counter
            counter += 1
            path.write_bytes(path.read_bytes() + bytes([counter % 255]))
            original_sleep(.002)

        with patch.object(image_source, "MAX_STABILITY_WAIT_SECONDS", .02), \
             patch.object(image_source.time, "sleep", side_effect=keep_changing), \
             self.assertRaisesRegex(ScanError, "did not become stable"):
            image_source.read_image_source("file", path)

    def test_unchanged_fingerprint_requires_explicit_rescan(self):
        path = self.image("frame.png")
        fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()

        with self.assertRaisesRegex(ScanError, "--allow-image-rescan"):
            image_source.read_image_source(
                "file", path, previous_fingerprint=fingerprint)

        loaded, metadata = image_source.read_image_source(
            "file", path, previous_fingerprint=fingerprint, allow_rescan=True)
        self.assertEqual(loaded.size, (12, 8))
        self.assertEqual(metadata["fingerprint"], fingerprint)

    def test_invalid_arguments_fail_closed(self):
        path = self.image("frame.png")
        for kind in ("live", "", None):
            with self.subTest(kind=kind), self.assertRaisesRegex(
                    ScanError, "kind"):
                image_source.read_image_source(kind, path)
        with self.assertRaisesRegex(ScanError, "fingerprint"):
            image_source.read_image_source(
                "file", path, previous_fingerprint="not-a-hash")
        with self.assertRaisesRegex(ScanError, "fingerprint"):
            image_source.read_image_source(
                "file", path, previous_fingerprint="")
        with self.assertRaisesRegex(ScanError, "allow_rescan"):
            image_source.read_image_source("file", path, allow_rescan="yes")


if __name__ == "__main__":
    unittest.main()
