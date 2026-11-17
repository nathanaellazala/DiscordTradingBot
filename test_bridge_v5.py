import json
from queue import Queue
from http.server import ThreadingHTTPServer
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from discord_webull_bot import LocalBridgeHandler

class BridgeDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events = Queue()
        class Handler(LocalBridgeHandler):
            queue = cls.events
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_health(self):
        with urlopen(self.url + '/health', timeout=3) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(json.loads(r.read())['version'], 6)

    def test_extension_status_reaches_queue(self):
        item = {'type': 'status', 'state': 'armed', 'channel_id': '123456789012345678',
                'visible': 15, 'exactMatches': 2, 'newMessages': 0}
        req = Request(self.url + '/status', json.dumps(item).encode(),
                      {'Content-Type': 'application/json', 'Origin': 'chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef'}, method='POST')
        with urlopen(req, timeout=3) as r:
            self.assertEqual(r.status, 202)
        self.assertEqual(self.events.get_nowait(), item)

    def test_website_origin_rejected(self):
        item = {'type':'status','state':'armed','channel_id':'123456789012345678',
                'visible':1,'exactMatches':0,'newMessages':0}
        req = Request(self.url + '/status', json.dumps(item).encode(),
                      {'Origin': 'https://discord.com', 'Content-Type': 'application/json'}, method='POST')
        with self.assertRaises(HTTPError) as err:
            urlopen(req, timeout=3)
        self.assertEqual(err.exception.code, 403)

    def test_status_cannot_send_trade_order(self):
        item = {'type':'status','state':'armed','channel_id':'123456789012345678',
                'visible':1,'exactMatches':0,'newMessages':0,
                'message_id':'123456789012345678', 'text':'SPY 750C @ .60'}
        req = Request(self.url + '/status', json.dumps(item).encode(),
                      {'Origin': 'chrome-extension://abcdefghijklmnopqrstuvwxyzabcdef', 'Content-Type': 'application/json'}, method='POST')
        with urlopen(req, timeout=3) as r: self.assertEqual(r.status, 202)
        self.assertEqual(self.events.get_nowait()['type'], 'status')

if __name__ == '__main__': unittest.main()
