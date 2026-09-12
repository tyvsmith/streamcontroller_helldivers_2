"""Exercise action-to-page completion with a real offline scanner process."""
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from automatic_stratagems import scan_runner
from automatic_stratagems.scan_session import ScanSession
from automatic_stratagems.temporary_scan_page import TemporaryScanPages
from .action_test_support import ActionTestHarness
from .test_temporary_scan_page import Deck, PageManager

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / 'fixtures'


class ReplayLifecycleTests(ActionTestHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.plugin.PATH = str(ROOT)
        self.plugin.stratagems = json.loads((ROOT / 'assets/data/stratagems.json').read_text())
        self.source = self.root / 'source.json'
        self.source.write_text('{"keys": {}}')
        self.deck = Deck(self.source)
        self.page = self.deck.active_page
        self.coordinator = self.mod.ScanCoordinator(self.plugin, self.root / 'scan-state')
        self.pages = TemporaryScanPages(self.root / 'pages', PageManager(), self.coordinator.store)
        self.coordinator.temporary_pages = self.pages
        self.opener = self.rendering_action(self.mod.AutomaticStratagemPage)
        self.opener.input_ident = type('Input', (), {'input_type': 'keys', 'json_identifier': '0x0'})()
        self.opener.state = 0
        self.opener.get_own_action_index = lambda: 0
        self.opener.on_ready_called = True
        self.opener.render = Mock()
        self.coordinator.actions.add(self.opener)
        source_session = self.coordinator.session(self.opener)
        token = source_session.begin([99])
        source_session.finish(token, {'status': 'matched', 'rows': [{'id': 'MachineGun'}]},
                              self.plugin.stratagems)
        self.coordinator.persist(self.opener, source_session)
        self.source_state = self.coordinator.store.path(self.coordinator.identity(self.opener))
        self.source_state_bytes = self.source_state.read_bytes()
        self.queued = Queue()
        self.processes = []
        self.threads = []
        self.launched = threading.Event()
        self.launch_gate = threading.Event()
        self.launch_gate.set()
        self.addCleanup(self.cleanup_workers)
        manifest = json.loads((FIXTURES / 'manifest.json').read_text())
        dataset = next(item for item in manifest['datasets'] if item['id'] == 'mission-panels')
        self.expected = dataset['expected_ids']['18n2fofu.png']
        self.image_path = self.root / 'replay.png'
        with Image.open(FIXTURES / 'mission-panels/18n2fofu.png') as panel:
            image = Image.new('RGB', (5120, 2160))
            image.paste(panel, (0, 0))
            image.save(self.image_path)

    def cleanup_workers(self):
        self.coordinator.cancel_all()
        self.launch_gate.set()
        self.coordinator.shutdown(timeout=10)
        for thread in self.threads:
            thread.join(10)
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def start_replay(self):
        real_popen = subprocess.Popen
        real_thread = threading.Thread

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            self.processes.append(process)
            self.launched.set()
            if not self.launch_gate.wait(10):
                raise RuntimeError('Replay launch barrier timed out')
            return process

        def thread(**kwargs):
            worker = real_thread(**kwargs)
            self.threads.append(worker)
            return worker

        command = [sys.executable, '-m', 'automatic_stratagems.scanner',
                   '--image', str(self.image_path), '--no-cache', '--workers', '1', '--json']
        # Replace live acquisition and its prerequisites; keep recognition,
        # process supervision, report validation, sessions, and persistence real.
        for replacement in (
            patch.object(scan_runner, 'check_scan_setup'),
            patch.object(scan_runner, 'resolve_scanner_runtime', return_value=Mock(
                interpreter=Path(sys.executable), env=dict(os.environ),
                profile='native', runtime_root=None)),
            patch.object(scan_runner, 'scan_command', return_value=command),
            patch.object(scan_runner.subprocess, 'Popen', side_effect=launch),
            patch.object(self.mod.scan_lifecycle, 'Thread', side_effect=thread),
            patch.object(self.mod.scan_lifecycle, 'run_scan', side_effect=scan_runner.run_scan),
            patch.object(self.mod.scan_lifecycle.GLib, 'idle_add',
                         side_effect=lambda callback, *args: self.queued.put((callback, args)) or 1),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.coordinator.start(self.opener, replace=True)

    def join_worker(self):
        self.assertEqual(len(self.threads), 1)
        self.threads[0].join(60)
        self.assertFalse(self.threads[0].is_alive(), 'Scanner worker did not finish')
        self.assertFalse(self.plugin.input_lock.locked())
        self.assertEqual(len(self.processes), 1)
        self.assertIsNotNone(self.processes[0].poll(), 'Scanner was not reaped')

    def assert_source_preserved(self, before):
        after = self.coordinator.session(self.opener).checkpoint()
        expected = dict(before)
        self.assertGreater(after.pop('scan_number'), expected.pop('scan_number'))
        self.assertEqual(after, expected)
        self.assertEqual(self.source_state.read_bytes(), self.source_state_bytes)

    def test_real_report_creates_page_and_persists_exact_assignments(self):
        before = self.coordinator.session(self.opener).checkpoint()
        self.start_replay()
        self.join_worker()
        self.assertEqual(self.deck.active_page.json_path, str(self.source))
        self.assertFalse(list((self.root / 'pages').glob('*.json')))
        callback, args = self.queued.get_nowait()
        callback(*args)
        self.assertTrue(self.queued.empty())
        generated = self.deck.active_page.json_path
        self.assertNotEqual(generated, str(self.source))
        saved = self.coordinator.store.load(
            dict(deck=self.deck.serial_number(), page=generated, group='default'),
            self.plugin.stratagems)
        self.assertEqual([row['id'] for row in saved.latest_report()['rows']], self.expected)
        self.assertEqual([key for key in saved.snapshot().assignments.values() if key], self.expected)
        self.assert_source_preserved(before)
        self.assertEqual(json.loads(self.source.read_text()), {'keys': {}})

    def test_cancel_reaps_scanner_and_preserves_existing_cached_page(self):
        address = self.mod.scan_lifecycle.source_action_address(self.opener)
        cached = self.pages.create(self.deck, str(self.source), 'default', 'auto', address)
        identity = dict(deck=self.deck.serial_number(), page=cached, group='default')
        state = ScanSession()
        token = state.begin([1])
        state.finish(token, {'status': 'matched', 'rows': [{'id': 'Reinforce'}]}, self.plugin.stratagems)
        self.coordinator.write_state(identity, state)
        page_bytes = Path(cached).read_bytes()
        state_bytes = self.coordinator.store.path(identity).read_bytes()
        before = self.coordinator.session(self.opener).checkpoint()
        self.launch_gate.clear()
        self.start_replay()
        self.assertTrue(self.launched.wait(10))
        self.coordinator.cancel_context(self.coordinator.context(self.opener))
        self.assertTrue(self.plugin.input_lock.locked(), 'Ownership released before process cleanup')
        self.launch_gate.set()
        self.join_worker()
        self.assertTrue(self.queued.empty())
        self.assertEqual(Path(cached).read_bytes(), page_bytes)
        self.assertEqual(self.coordinator.store.path(identity).read_bytes(), state_bytes)
        self.assert_source_preserved(before)
        self.assertEqual(self.deck.active_page.json_path, str(self.source))

    def test_cancel_rejects_already_queued_real_report(self):
        before = self.coordinator.session(self.opener).checkpoint()
        self.start_replay()
        self.join_worker()
        self.assertEqual(self.queued.qsize(), 1)
        self.coordinator.cancel_context(self.coordinator.context(self.opener))
        callback, args = self.queued.get_nowait()
        callback(*args)
        self.assertFalse(list((self.root / 'pages').glob('*.json')))
        self.assertEqual(self.deck.active_page.json_path, str(self.source))
        self.assert_source_preserved(before)
