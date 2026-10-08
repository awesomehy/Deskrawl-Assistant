"""Desktop lifecycle tests without any real game actions or GUI automation."""
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.request
import urllib.error

from deskrawl_assistant.native_window import WindowHooks, main
from deskrawl_assistant.web_server import AssistantServer, make_handler


class WindowTests(unittest.TestCase):
    def test_clean_window_close_stops_background_monitor(self):
        window, service = Mock(), Mock()
        hooks = WindowHooks(window,service)
        self.assertTrue(hooks.closing())
        window.create_confirmation_dialog.assert_not_called()
        hooks.closed()
        service.stop.assert_called_once()

    def test_unsaved_configuration_can_cancel_window_close(self):
        window, service = Mock(), Mock()
        window.create_confirmation_dialog.return_value = False
        hooks = WindowHooks(window,service)
        hooks.set_dirty(True)
        self.assertFalse(hooks.closing())
        service.stop.assert_not_called()
        hooks.exiting = True
        self.assertTrue(hooks.closing())

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
