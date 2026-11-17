const assert = require('node:assert/strict');
const test = require('node:test');
const matcher = require('./chrome_extension/matcher.js');

test('detect real embedded Discord entry', () => {
  assert.equal(matcher.category('1. gonna enter light size, if you are green call it a day: SPY 750C @ .55 @unknown-role'), 'embedded');
});
test('standalone alert matches', () => {
  assert.equal(matcher.category('SPY 757P @ .34'), 'exact');
});
test('standalone alert with ping matches', () => {
  assert.equal(matcher.category('SPY 757P @ .34 @unknown-role'), 'exact');
});
test('exclude messages with multiple potential alerts', () => {
  assert.equal(matcher.category('SPY 750C @ .55 or SPY 751P @ .44'), null);
});
test('not a signal if strike and price syntax missing', () => {
  assert.equal(matcher.category('SPY 750C is up 30%'), null);
});
test('not a signal if text too long', () => {
  assert.equal(matcher.category('a'.repeat(401) + 'SPY 750C @ .55'), null);
});
