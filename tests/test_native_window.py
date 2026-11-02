"""Desktop lifecycle tests without any real game actions or GUI automation."""
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.request
import urllib.error

from deskrawl_assistant.native_window import WindowHooks, main
from deskrawl_assistant.web_server import AssistantServer, make_handler


class WindowTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows global shortcut integration')
    def test_saved_global_hotkey_message_hides_and_restores_without_capture_lease(self):
        import ctypes
        from deskrawl_assistant.assistant_settings import AssistantSettings
        with tempfile.TemporaryDirectory() as directory:
            window, service = Mock(), Mock()
            service.assistant_preferences = AssistantSettings(Path(directory) / 'settings.json')
            hooks = WindowHooks(window, service);hooks.tray = Mock()
            hidden, shown = threading.Event(), threading.Event()
            window.hide.side_effect = hidden.set;window.show.side_effect = shown.set
            try:
                hooks.capture_hotkey(True)
                try:
                    service.assistant_preferences.save(
                        {'steam_acceleration_enabled': False, 'boss_hotkey': 'Ctrl+Alt+Shift+F10'},
                        apply_hotkey=hooks.apply_hotkey)
                except ValueError as exc:
                    if '其他程序' in str(exc): self.skipTest('Test shortcut already in use')
                    raise
                self.assertEqual(hooks.hotkey_capture_until, 0)
                self.assertTrue(hooks.hotkey_state()['active'])
                self.assertTrue(ctypes.windll.user32.PostThreadMessageW(hooks.hotkeys.thread.native_id, 0x312, 0x3210, 0))
                self.assertTrue(hidden.wait(2))
                with hooks.visibility_guard: self.assertTrue(hooks.hidden)
                self.assertTrue(ctypes.windll.user32.PostThreadMessageW(hooks.hotkeys.thread.native_id, 0x312, 0x3210, 0))
                self.assertTrue(shown.wait(2))
                with hooks.visibility_guard: self.assertFalse(hooks.hidden)
                service.stop.assert_not_called()
                window.minimize.assert_not_called()
            finally: hooks.release_desktop()

    def test_close_hides_to_tray_and_does_not_minimize_or_stop(self):
        window, service = Mock(), Mock()
        hidden = threading.Event();window.hide.side_effect = hidden.set
        hooks = WindowHooks(window, service);hooks.tray = Mock()
        self.assertFalse(hooks.closing());self.assertTrue(hidden.wait(1))
        self.assertTrue(hooks.hidden);window.minimize.assert_not_called()
        service.stop.assert_not_called()

    def test_boss_key_hides_and_restores_from_tray(self):
        window, service = Mock(), Mock()
        changed = threading.Event();window.hide.side_effect = changed.set;window.show.side_effect = changed.set
        hooks = WindowHooks(window, service);hooks.tray = Mock()
        hooks.toggle_window();self.assertTrue(changed.wait(1));changed.clear()
        self.assertTrue(hooks.hidden)
        hooks.toggle_window();self.assertTrue(changed.wait(1))
        self.assertFalse(hooks.hidden);window.minimize.assert_not_called()
        service.stop.assert_not_called()

    def test_saving_shortcut_clears_recording_suppression(self):
        hooks = WindowHooks(Mock(), Mock());hooks.hotkeys = Mock()
        hooks.capture_hotkey(True);hooks.apply_hotkey('Ctrl+Alt+H', Mock())
        self.assertEqual(hooks.hotkey_capture_until, 0)

    def test_boss_key_hides_normally_minimized_window_to_tray(self):
        window, service = Mock(), Mock()
        hidden = threading.Event();window.hide.side_effect = hidden.set
        hooks = WindowHooks(window, service);hooks.tray = Mock();hooks.minimized = True
        hooks.toggle_window();self.assertTrue(hidden.wait(1))
        self.assertTrue(hooks.hidden);window.show.assert_not_called();window.restore.assert_not_called()
        service.stop.assert_not_called()

    def test_tray_exit_preserves_unsaved_draft_if_cancelled(self):
        window, service = Mock(), Mock();window.create_confirmation_dialog.return_value = False
        asked=threading.Event();window.create_confirmation_dialog.side_effect=lambda *_:(asked.set() or False)
        hooks=WindowHooks(window,service);hooks.dirty=True
        hooks.request_exit();self.assertTrue(asked.wait(1))
        self.assertFalse(hooks.exiting);window.destroy.assert_not_called();service.stop.assert_not_called()

    def test_close_minimizes_without_stopping_background_work(self):
        window, service = Mock(), Mock()
        minimized = threading.Event()
        window.minimize.side_effect = minimized.set
        hooks = WindowHooks(window,service)
        self.assertFalse(hooks.closing())
        self.assertTrue(minimized.wait(1))
        self.assertTrue(hooks.minimized)
        window.create_confirmation_dialog.assert_not_called()
        service.stop.assert_not_called()
        service.close.assert_not_called()

    def test_explicit_exit_releases_hotkey_and_stops_service(self):
        window, service = Mock(), Mock()
        hooks = WindowHooks(window,service)
        hooks.hotkeys = Mock()
        hooks.exiting = True
        self.assertTrue(hooks.closing())
        hooks.closed()
        service.stop.assert_called_once()
        hooks.hotkeys.close.assert_called_once()

    def test_minimizing_retains_unsaved_configuration(self):
        window, service = Mock(), Mock()
        window.create_confirmation_dialog.return_value = False
        hooks = WindowHooks(window,service)
        hooks.set_dirty(True)
        self.assertFalse(hooks.closing())
        self.assertTrue(hooks.dirty)
        window.create_confirmation_dialog.assert_not_called()
        service.stop.assert_not_called()
        hooks.exiting = True
        self.assertTrue(hooks.closing())

    def test_hotkey_toggles_window_without_stopping_service(self):
        window, service = Mock(), Mock()
        changed = threading.Event()
        window.minimize.side_effect = changed.set
        window.show.side_effect = changed.set
        hooks = WindowHooks(window,service)
        hooks.toggle_window()
        self.assertTrue(changed.wait(1));changed.clear()
        hooks.toggle_window()
        self.assertTrue(changed.wait(1))
        self.assertFalse(hooks.minimized)
        window.restore.assert_called_once()
        service.stop.assert_not_called()

    def test_recording_shortcut_temporarily_suppresses_boss_key(self):
        window, service = Mock(), Mock()
        hooks = WindowHooks(window,service)
        hooks.capture_hotkey(True)
        hooks.toggle_window()
        window.minimize.assert_not_called()
        hooks.capture_hotkey(False)
        self.assertEqual(hooks.hotkey_capture_until, 0)
        with self.assertRaises(ValueError): hooks.capture_hotkey('true')

    def test_draft_state_rejects_non_boolean_values(self):
        hooks = WindowHooks(Mock(),Mock())
        for value in ('false', 0, None):
            with self.assertRaises(ValueError): hooks.set_dirty(value)

    def test_second_launch_reuses_native_window(self):
        with patch('deskrawl_assistant.native_window.existing_service', return_value={'desktop':True}), patch('deskrawl_assistant.native_window.activate_existing') as activate:
            main(18742)
            activate.assert_called_once_with(18742)

    def test_old_browser_service_is_not_replaced_silently(self):
        with patch('deskrawl_assistant.native_window.existing_service',return_value={'desktop':False}):
            with self.assertRaisesRegex(RuntimeError,'旧版网页助手'): main(18742)

    def test_window_owned_server_closes_and_releases_port(self):
        service = Mock()
        host = AssistantServer(0,service)
        port = host.server.server_port
        host.start()
        host.close()
        self.assertTrue(host.finished.is_set())
        self.assertFalse(host.thread.is_alive())
        service.close.assert_called_once()
        replacement = ThreadingHTTPServer(('127.0.0.1',port),make_handler(Mock(),'new-token'))
        replacement.server_close()

    def test_desktop_routes_require_session_and_validate_draft(self):
        desktop = WindowHooks(Mock(),Mock())
        host = ThreadingHTTPServer(('127.0.0.1',0),make_handler(Mock(),'token'))
        host.desktop = desktop
        thread = threading.Thread(target=host.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{host.server_port}'
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def send(payload,token='token'):
                req=urllib.request.Request(base+'/api/window/draft',data=json.dumps(payload).encode(),headers={'Origin':base,'X-Assistant-Token':token})
                try: response=opener.open(req)
                except urllib.error.HTTPError as error: response=error
                with response: return response.status
            self.assertEqual(send({'dirty':True},'wrong'),403)
            self.assertFalse(desktop.dirty)
            self.assertEqual(send({'dirty':True}),200)
            self.assertTrue(desktop.dirty)
            self.assertEqual(send({'dirty':'false'}),400)
            self.assertTrue(desktop.dirty)
        finally:
            host.shutdown();host.server_close();thread.join(1)


if __name__=='__main__': unittest.main()
