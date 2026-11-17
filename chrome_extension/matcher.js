// Narrow candidate detector only; Python authoritatively validates entry intent.
(() => {
  'use strict';
  const PATTERN = /(?<![A-Z0-9])\$?[A-Z]{1,6}\s+\d{1,5}(?:\.\d{1,2})?\s*[CP]\s*@\s*\$?(?:\d+)?\.\d{1,3}(?!\d)/gi;
  const STANDALONE = /^\s*(?:[0-9]{1,2}[.)]\s*)?\$?[A-Z]{1,6}\s+\d{1,5}(?:\.\d{1,2})?\s*[CP]\s*@\s*\$?(?:\d+)?\.\d{1,3}(?:\s*(?:<@&?[0-9]{16,21}>|@[a-z0-9_-]+))*\s*[.!]?\s*$/i;
  function matches(text) {
    return typeof text === 'string' && text.length > 0 && text.length <= 400
      ? Array.from(text.matchAll(PATTERN)) : [];
  }
  function category(text) {
    const found = matches(text);
    if (found.length !== 1) return null;
    return STANDALONE.test(text) ? 'exact' : 'embedded';
  }
  const api = Object.freeze({matches, category});
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else globalThis.DiscordWebullAlertMatcher = api;
})();
