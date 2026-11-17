(() => {
  'use strict';
  // Monitor rendered chat only. Never access Discord API, tokens, or private data.
  const EPOCH = 1420070400000n;
  const matcher = globalThis.DiscordWebullAlertMatcher;
  if (!matcher) { console.error('[Discord Webull reader] Missing matcher.js, reload the extension'); return; }
  const seenIds = new Set();
  let maxMessageId = 0n;
  let armed = false;
  let channelId = '';
  let lastPath = '';
  let epoch = 0;
  let scanPending = false;
  let warmupTimer = null;
  let lastStatusSent = 0;
  let newMessages = 0;

  function currentChannel() {
    const m = location.pathname.match(/^\/channels\/(?:@me|\d+)\/(\d{16,21})(?:\/|$)/);
    return m ? m[1] : '';
  }

  function messages() {
    const found = new Map();
    // Primary: Discord's ID on a rendered individual message body.
    for (const el of document.querySelectorAll('[id^="message-content-"]')) {
      const m = el.id.match(/^message-content-(\d{16,21})$/);
      if (m) found.set(m[1], (el.innerText || el.textContent || '').trim());
    }
    // Fallback for Discord layouts that put the Snowflake on the message wrapper.
    // Only read the wrapper's dedicated message-content child, never the whole
    // wrapper (which can contain unrelated quotes, buttons, reactions or embed text).
    for (const el of document.querySelectorAll('[id^="chat-messages-"], [data-list-item-id^="chat-messages___"]')) {
      const attr = el.id || el.getAttribute('data-list-item-id') || '';
      const m = attr.match(/(?:chat-messages-|chat-messages___)(\d{16,21})$/);
      if (!m || found.has(m[1])) continue;
      const body = el.querySelector('[id^="message-content-"], [class*="messageContent"]');
      if (body) found.set(m[1], (body.innerText || body.textContent || '').trim());
    }
    return Array.from(found, ([id, text]) => ({id, text}));
  }

  function status(state, current, force=false) {
    const now = Date.now();
    if (!current || (!force && now - lastStatusSent < 15000)) return;
    lastStatusSent = now;
    const list = messages();
    const data = {
      type: 'status', state, channel_id: current, visible: list.length,
      exactMatches: list.filter(item => matcher.category(item.text) === 'exact').length,
      embeddedMatches: list.filter(item => matcher.category(item.text) === 'embedded').length,
      newMessages
    };
    chrome.runtime.sendMessage(data, (reply) => {
      if (chrome.runtime.lastError) {
        console.warn('[Discord Webull reader] Extension messaging error:', chrome.runtime.lastError.message);
      } else if (reply && !reply.ok) {
        console.warn('[Discord Webull reader] Python bridge rejected status:', reply.status || reply.error);
      }
    });
  }

  function startBaseline() {
    armed = false;
    newMessages = 0;
    seenIds.clear();
    maxMessageId = 0n;
    lastStatusSent = 0;
    if (warmupTimer !== null) clearTimeout(warmupTimer);
    const currentEpoch = ++epoch;
    warmupTimer = setTimeout(() => {
      if (currentEpoch !== epoch || !currentChannel()) return;
      const existing = messages();
      if (!existing.length) {
        status('waiting-dom', currentChannel(), true);
        startBaseline(); // fail closed if the Discord DOM has no identifiable message IDs
        return;
      }
      for (const item of existing) {
        seenIds.add(item.id);
        const n = BigInt(item.id);
        if (n > maxMessageId) maxMessageId = n;
      }
      armed = true;
      status('armed', currentChannel(), true);
      console.info('[Discord Webull reader] Armed in channel', channelId,
                   'baseline:', existing.length, 'candidate alerts:', existing.filter(x => matcher.category(x.text) !== null).length);
    }, 3000);
  }

  function scan() {
    scanPending = false;
    const path = location.pathname;
    const current = currentChannel();
    if (path !== lastPath || current !== channelId) {
      lastPath = path;
      channelId = current;
      if (current) console.info('[Discord Webull reader] Channel detected:', current);
      else console.warn('[Discord Webull reader] This URL is not a normal Discord channel:', path);
      startBaseline();
    }
    if (!channelId) return;
    if (!armed) return;
    const now = BigInt(Date.now());
    for (const item of messages()) {
      if (seenIds.has(item.id)) continue;
      seenIds.add(item.id);
      const n = BigInt(item.id);
      if (n <= maxMessageId) continue;
      maxMessageId = n;
      newMessages++;
      const sentMs = (n >> 22n) + EPOCH;
      if (sentMs > now + 2000n || now - sentMs > 8000n) continue;
      const kind = matcher.category(item.text);
      if (!kind) {
        console.info('[Discord Webull reader] New message ignored (not a unique trade candidate):', item.id);
        continue;
      }
      chrome.runtime.sendMessage({type: 'alert', channel_id: channelId, message_id: item.id, text: item.text}, (reply) => {
        if (chrome.runtime.lastError) console.warn('[Discord Webull reader] Sending alert failed:', chrome.runtime.lastError.message);
        else if (reply && !reply.ok) console.warn('[Discord Webull reader] Python bridge rejected alert:', reply.status || reply.error);
      });
      console.info('[Discord Webull reader] Fresh trade candidate sent for Python validation:', item.id, kind);
    }
    status('scanning', channelId);
    if (seenIds.size > 6000) seenIds.clear();
  }

  function scheduleScan() {
    if (scanPending) return;
    scanPending = true;
    setTimeout(scan, 180);
  }
  const observer = new MutationObserver(scheduleScan);
  observer.observe(document.documentElement, {childList:true, subtree:true, characterData:true});
  setInterval(scheduleScan, 800);
  scheduleScan();
})();
