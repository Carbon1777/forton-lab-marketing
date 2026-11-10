// node --test cf-worker/forton-cron-trigger/resolve.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveDispatch } from './src/index.js';

const at = (h) => Date.UTC(2026, 9, 30, h, 0, 0);
const RN = { workflow: 'review_notifier.yml' };

test('hourly daily cron: review_notifier every hour 06–20 UTC', () => {
	const c = '0 6-20 * * *';
	for (let h = 6; h <= 20; h++) {
		assert.ok(resolveDispatch(c, at(h)).some((t) => t.workflow === 'review_notifier.yml'), `hour ${h}`);
	}
	assert.deepEqual(resolveDispatch(c, at(6)), [RN]);
	assert.deepEqual(resolveDispatch(c, at(20)), [RN]);
	assert.deepEqual(resolveDispatch(c, at(3)), []);
	assert.deepEqual(resolveDispatch(c, at(21)), []);
});

test('hourly daily cron keeps preview_bot / publish_scheduled ticks', () => {
	const c = '0 6-20 * * *';
	assert.deepEqual(resolveDispatch(c, at(9)), [{ workflow: 'preview_bot.yml' }, RN]);
	assert.deepEqual(resolveDispatch(c, at(15)), [{ workflow: 'preview_bot.yml', inputs: { mode: 'video' } }, RN]);
	assert.deepEqual(resolveDispatch(c, at(17)), [{ workflow: 'publish_scheduled.yml' }, RN]);
	assert.deepEqual(resolveDispatch(c, at(12)), [RN]);
});

test('other crons unchanged', () => {
	assert.deepEqual(resolveDispatch('37 6 * * mon', at(6)), [{ workflow: 'store_metrics.yml' }]);
	assert.deepEqual(resolveDispatch('0 14 5 * *', at(14)), [{ workflow: 'monthly_report.yml' }]);
	assert.deepEqual(resolveDispatch('0 9,15,17 * * *', at(9)), []);
	assert.deepEqual(resolveDispatch('1 2 3 * *', at(2)), []);
});
