// Forton Lab — external cron trigger for GH Actions workflows.
// Reliable replacement for GH Actions cron throttling (incidents 2026-05-11..20
// for preview_bot, 2026-05-26 for funnel_metrics/centry_funnel).
//
// Cron → workflow map (single source of truth, кодом, а не Dashboard):
//   "0 6-20 * * *" — ежечасно 09–23 МСК → review_notifier.yml (+ preview_bot / publish_scheduled в 9/15/17 UTC, см. DAILY)
//   "37 6 * * mon" — Mon 09:37 МСК  → store_metrics.yml   (per-app reports)
//   "7 7 * * mon"  — Mon 10:07 МСК  → channel_metrics.yml (channels report)
//   "7 12 * * tue" — Tue 15:07 МСК  → funnel_metrics.yml (Diktum funnel)
//   "0 14 5 * *"   — 5-е 17:00 МСК  → monthly_report.yml (monthly report)
//
// ★ День недели — ТОЛЬКО именем (mon/tue/…). У Cloudflare цифры дней
// недели 1–7 начинаются с ВОСКРЕСЕНЬЯ (1=Sun), а не с понедельника как в
// GH Actions/crontab. Incident 2026-10-04/11: «* * 1» стрелял в воскресенье →
// недельный отчёт уходил в Вс за позапрошлую неделю, в Пн не приходил.
//   (centry_funnel.yml снят 2026-10-03: Centry закрыт, workflow отключён)
//
// Triggers in wrangler.jsonc MUST match keys in CRON_TO_WORKFLOW. If they
// drift, the `scheduled` handler logs "UNKNOWN cron" and the workflow is
// silently skipped — keep them in sync.

const OWNER = 'Carbon1777';
const REPO = 'forton-lab-marketing';

const CRON_TO_WORKFLOW = {
	'37 6 * * mon': 'store_metrics.yml',
	'7 7 * * mon': 'channel_metrics.yml',
	'7 12 * * tue': 'funnel_metrics.yml',
	'0 14 5 * *': 'monthly_report.yml',
};

// Один ежечасный cron-триггер на все ежедневные тики (лимит Workers Free: 5 триггеров на аккаунт):
//   каждый час 06–20 UTC = 09–23 МСК → review_notifier.yml (отзывы/оценки из сторов в «Планировщик»)
//   09:00 UTC = 12:00 МСК → + preview_bot.yml (обычные посты)
//   15:00 UTC = 18:00 МСК → + preview_bot.yml mode=video (превью видео, за 2 ч до публикации)
//   17:00 UTC = 20:00 МСК → + publish_scheduled.yml (отложенные видео-публикации)
// review_notifier раньше висел только на GH-cron «каждый час», но GitHub реально
// запускал его 2–3 раза в сутки и ни разу утром (quick 261110-p2u).
const DAILY = '0 6-20 * * *';

// Возвращает СПИСОК запусков (может быть пустым) — на один тик бывает несколько workflow.
export function resolveDispatch(cron, scheduledTime) {
	if (cron === DAILY) {
		const hour = new Date(scheduledTime).getUTCHours();
		const out = [];
		if (hour === 9) out.push({ workflow: 'preview_bot.yml' });
		if (hour === 15) out.push({ workflow: 'preview_bot.yml', inputs: { mode: 'video' } });
		if (hour === 17) out.push({ workflow: 'publish_scheduled.yml' });
		if (hour >= 6 && hour <= 20) out.push({ workflow: 'review_notifier.yml' });
		return out;
	}
	const workflow = CRON_TO_WORKFLOW[cron];
	return workflow ? [{ workflow }] : [];
}

const ALLOWED_WORKFLOWS = new Set([
	...Object.values(CRON_TO_WORKFLOW),
	'preview_bot.yml',
	'publish_scheduled.yml',
	'review_notifier.yml',
]);

async function triggerWorkflow(env, workflow, inputs) {
	const url = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${workflow}/dispatches`;
	const res = await fetch(url, {
		method: 'POST',
		headers: {
			'Authorization': `Bearer ${env.GH_PAT}`,
			'Accept': 'application/vnd.github+json',
			'X-GitHub-Api-Version': '2022-11-28',
			'User-Agent': 'forton-cron-trigger',
		},
		body: JSON.stringify(inputs ? { ref: 'main', inputs } : { ref: 'main' }),
	});
	return { status: res.status, body: await res.text() };
}

export default {
	async scheduled(event, env, ctx) {
		// Регистр/пробелы нормализуем: ключи в CRON_TO_WORKFLOW — lowercase.
		const cron = String(event.cron).trim().toLowerCase().replace(/\s+/g, ' ');
		const targets = resolveDispatch(cron, event.scheduledTime);
		if (!targets.length) {
			console.log('UNKNOWN cron — no workflow mapped:', event.cron);
			return;
		}
		// Каждый запуск независим: сбой одного не должен отменять остальные.
		for (const target of targets) {
			try {
				const r = await triggerWorkflow(env, target.workflow, target.inputs);
				console.log('scheduled dispatch', event.cron, '→', target.workflow, JSON.stringify(target.inputs || {}), r.status, r.body.slice(0, 200));
			} catch (e) {
				console.log('scheduled dispatch FAILED', event.cron, '→', target.workflow, String(e));
			}
		}
	},
	async fetch(request, env, ctx) {
		const u = new URL(request.url);
		// /trigger          → default preview_bot.yml (back-compat)
		// /trigger?wf=X.yml → triggers X.yml (must be in CRON_TO_WORKFLOW values)
		if (u.pathname !== '/trigger') return new Response('OK', { status: 200 });
		const workflow = u.searchParams.get('wf') || 'preview_bot.yml';
		if (!ALLOWED_WORKFLOWS.has(workflow)) {
			return new Response(JSON.stringify({ error: 'workflow not in allowlist', workflow }), {
				status: 400,
				headers: { 'content-type': 'application/json' },
			});
		}
		const mode = u.searchParams.get('mode');   // preview_bot.yml?mode=video
		const r = await triggerWorkflow(env, workflow, mode ? { mode } : undefined);
		return new Response(JSON.stringify({ workflow, ...r }), {
			status: r.status === 204 ? 200 : r.status,
			headers: { 'content-type': 'application/json' },
		});
	},
};
