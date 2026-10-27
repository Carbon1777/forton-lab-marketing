// node --test cf-worker/forton-cron-trigger/resolve.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveDispatch } from './src/index.js';

const at = (h) => Date.UTC(2026, 9, 30, h, 0, 0);

test('daily cron maps by UTC hour', () => {
	const c = '0 9,15,17 * * *';
	assert.deepEqual(resolveDispatch(c, at(9)), { workflow: 'preview_bot.yml' });
	assert.deepEqual(resolveDispatch(c, at(15)), { workflow: 'preview_bot.yml', inputs: { mode: 'video' } });
	assert.deepEqual(resolveDispatch(c, at(17)), { workflow: 'publish_scheduled.yml' });
	assert.equal(resolveDispatch(c, at(3)), null);
});

test('other crons unchanged', () => {
	assert.deepEqual(resolveDispatch('37 6 * * mon', at(6)), { workflow: 'store_metrics.yml' });
	assert.deepEqual(resolveDispatch('0 14 5 * *', at(14)), { workflow: 'monthly_report.yml' });
	assert.equal(resolveDispatch('1 2 3 * *', at(2)), null);
});
