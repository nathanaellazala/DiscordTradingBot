// Local-only Discord browser bridge. No Discord API, bot, or user token.
const BRIDGE = 'http://127.0.0.1:8765';

async function setBadge(ok) {
  try {
    await chrome.action.setBadgeText({text: ok ? 'ON' : 'ERR'});
    await chrome.action.setBadgeBackgroundColor({color: ok ? '#167a3d' : '#bd2c31'});
  } catch (e) { console.warn('[Discord Webull reader] Badge error:', e.message); }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const url = sender.url || sender.tab?.url || '';
  if (!url.startsWith('https://discord.com/channels/')) return false;
  const isAlert = message?.type === 'alert';
  const isStatus = message?.type === 'status';
  if (!isAlert && !isStatus) return false;
  if (!/^\d{16,21}$/.test(message.channel_id || '')) return false;
  if (isAlert && (!/^\d{16,21}$/.test(message.message_id || '') ||
                  typeof message.text !== 'string' || message.text.length > 400)) return false;
  if (isStatus && (!['waiting-dom', 'armed', 'scanning'].includes(message.state) ||
      !Number.isInteger(message.visible) || message.visible < 0 || message.visible > 50000 ||
      !Number.isInteger(message.exactMatches) || message.exactMatches < 0 ||
      !Number.isInteger(message.newMessages) || message.newMessages < 0 ||
      !Number.isInteger(message.embeddedMatches) || message.embeddedMatches < 0)) return false;

  const destination = BRIDGE + (isAlert ? '/alert' : '/status');
  fetch(destination, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(message)
  }).then(async response => {
    await setBadge(response.ok);
    if (!response.ok) console.error('[Discord Webull reader] Local bridge HTTP error', response.status, 'endpoint:', destination);
    else if (isAlert) console.info('[Discord Webull reader] Fresh alert delivered to Python:', message.message_id);
    sendResponse({ok: response.ok, status: response.status});
  }).catch(error => {
    setBadge(false);
    console.error('[Discord Webull reader] Cannot reach Python at 127.0.0.1:8765:', error?.message || error);
    sendResponse({ok: false, error: String(error?.message || error)});
  });
  return true;
});
