import unittest
from datetime import datetime
from discord_webull_bot import discord_timestamp_ms, validate_browser_signal

DISCORD_EPOCH_MS = 1420070400000
NOW = 1794830400000
CHANNEL = '123456789012345678'
CFG = {'allowed_channel_ids': [CHANNEL], 'allowed_tickers': ['SPY'], 'max_message_age_seconds': 8}

def snowflake(unix_ms):
    return str((unix_ms - DISCORD_EPOCH_MS) << 22)

def sample(unix_ms=NOW-1000, channel=CHANNEL, text='SPY 750C @ .55'):
    return {'type':'alert','channel_id':channel,'message_id':snowflake(unix_ms),'text':text}

class FreshnessTests(unittest.TestCase):
    def validate(self, payload, started=NOW-5000, at=NOW, seen=None, live=True):
        return validate_browser_signal(payload, CFG, started, at, seen or set(), live)

    def test_recent_message_valid(self):
        ok, why, alert = self.validate(sample())
        self.assertTrue(ok, why)
        self.assertEqual(alert.description(), 'SPY 750C @ $0.55')

    def test_snowflake_exact_ms(self):
        self.assertEqual(discord_timestamp_ms(snowflake(NOW)), NOW)

    def test_old_message_from_scrolling_denied(self):
        ok, why, _ = self.validate(sample(NOW - 120000), started=NOW-600000)
        self.assertFalse(ok)
        self.assertIn('too old', why)

    def test_prior_to_start_denied_even_if_few_seconds_ago(self):
        ok, why, _ = self.validate(sample(NOW-3000), started=NOW-2000)
        self.assertFalse(ok)
        self.assertIn('predates', why)

    def test_wrong_channel_denied(self):
        ok, why, _ = self.validate(sample(channel='999999999999999999'))
        self.assertFalse(ok)
        self.assertIn('not allowlisted', why)

    def test_duplicate_denied(self):
        alert = sample()
        ok, why, _ = self.validate(alert, seen={alert['message_id']})
        self.assertFalse(ok)
        self.assertIn('Duplicate', why)

    def test_future_clock_denied(self):
        ok, why, _ = self.validate(sample(NOW + 5000))
        self.assertFalse(ok)
        self.assertIn('future', why)

    def test_no_timestamp_cannot_trade(self):
        alert = sample()
        alert['message_id'] = 'ocr-detected'
        ok, why, _ = self.validate(alert)
        self.assertFalse(ok)
        self.assertIn('verifiable', why)

    def test_recap_or_quote_denied(self):
        ok, why, _ = self.validate(sample(text='Earlier SPY 750C @ .55 sold +45%'))
        self.assertFalse(ok)
        self.assertTrue(any(token in why.lower() for token in ('entry', 'uncertain', 'multiline', 'multi-line', 'manual')))

    def test_second_line_denied(self):
        ok, why, _ = self.validate(sample(text='SPY 750C @ .55\nLast trade was awesome'))
        self.assertFalse(ok)
        self.assertTrue(any(token in why.lower() for token in ('entry', 'uncertain', 'multiline', 'multi-line', 'manual')))

    def test_unlisted_ticker_denied(self):
        ok, why, _ = self.validate(sample(text='QQQ 512C @ .55'))
        self.assertFalse(ok)
        self.assertIn('permitted', why)

    def test_no_channel_allowed_for_live(self):
        cfg = {**CFG, 'allowed_channel_ids': []}
        ok, why, _ = validate_browser_signal(sample(), cfg, NOW-5000, NOW, set(), True)
        self.assertFalse(ok)
        self.assertIn('not allowlisted', why)

if __name__ == '__main__':
    unittest.main()
