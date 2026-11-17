import unittest
from discord_webull_bot import extract_browser_entry, validate_browser_signal

DISCORD_EPOCH = 1420070400000
NOW = 1794830400000
CHANNEL = '1571934169435742332'
CFG = {'allowed_channel_ids':[CHANNEL], 'allowed_tickers':['SPY'], 'max_message_age_seconds':8}

def payload(text, sent_ms=NOW-1000):
    return {'type':'alert', 'channel_id':CHANNEL,
            'message_id':str((sent_ms-DISCORD_EPOCH)<<22), 'text':text}

class EmbeddedEntryTests(unittest.TestCase):
    def accepts(self, msg):
        return extract_browser_entry(msg, ['SPY'])
    def test_real_user_example(self):
        msg='1. gonna enter light size, if you are green call it a day: SPY 750C @ .55 @unknown-role'
        alert,reason=self.accepts(msg)
        self.assertIsNotNone(alert,reason)
        self.assertEqual(alert.description(),'SPY 750C @ $0.55')
        ok,why,alert=validate_browser_signal(payload(msg), CFG, NOW-5000, NOW, set(), True)
        self.assertTrue(ok,why)
        self.assertEqual(alert.description(),'SPY 750C @ $0.55')
    def test_standalone_ping(self):
        alert,_=self.accepts('SPY 750C @ .55 @unknown-role')
        self.assertIsNotNone(alert)
    def test_standalone_plain(self):
        alert,_=self.accepts('SPY 757P @ .34')
        self.assertIsNotNone(alert)
    def test_multiple_signals_rejected(self):
        a, why=self.accepts('buy SPY 750C @ .55 and SPY 751P @ .44')
        self.assertIsNone(a)
        self.assertIn('exactly one', why)
    def test_recap_rejected(self):
        a,_=self.accepts('Earlier I was gonna enter SPY 750C @ .55 @unknown-role')
        self.assertIsNone(a)
    def test_negative_instruction_rejected(self):
        a,_=self.accepts('Do not buy SPY 750C @ .55')
        self.assertIsNone(a)
    def test_post_trade_rejected(self):
        a,_=self.accepts('Already sold SPY 750C @ .55')
        self.assertIsNone(a)
    def test_non_entry_mention_rejected(self):
        a,_=self.accepts('Look at SPY 750C @ .55')
        self.assertIsNone(a)
    def test_ambiguous_suffix_rejected(self):
        a,_=self.accepts('Gonna enter SPY 750C @ .55 but only if it breaks 751')
        self.assertIsNone(a)
    def test_old_message_rejected_even_with_entry_cue(self):
        ok,why,_=validate_browser_signal(payload('gonna enter SPY 750C @ .55', NOW-30000), CFG, NOW-60000, NOW, set(), True)
        self.assertFalse(ok)
        self.assertIn('too old',why)
    def test_previous_to_monitor_start_rejected(self):
        ok,why,_=validate_browser_signal(payload('gonna enter SPY 750C @ .55', NOW-3000), CFG, NOW-2000, NOW, set(), True)
        self.assertFalse(ok)
        self.assertIn('predates',why)
    def test_other_channel_rejected(self):
        msg=payload('gonna enter SPY 750C @ .55')
        msg['channel_id']='123456789012345678'
        ok,_,_=validate_browser_signal(msg, CFG, NOW-5000, NOW, set(), True)
        self.assertFalse(ok)

if __name__=='__main__': unittest.main()
