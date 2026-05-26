// Forton Lab — external cron trigger for GH Actions workflows.
// Reliable replacement for GH Actions cron throttling (incidents 2026-05-11..20
// for preview_bot, 2026-05-26 for funnel_metrics/centry_funnel).
//
// Cron → workflow map (single source of truth, кодом, а не Dashboard):
//   "0 9 * * *"   — daily 12:00 МСК → preview_bot.yml
//   "7 12 * * 2"  — Tue 15:07 МСК   → funnel_metrics.yml (Diktum funnel)
//   "12 12 * * 2" — Tue 15:12 МСК   → centry_funnel.yml  (Centry funnel)
//
// Triggers in wrangler.jsonc MUST match keys in CRON_TO_WORKFLOW. If they
// drift, the `scheduled` handler logs "UNKNOWN cron" and the workflow is
// silently skipped — keep them in sync.

const OWNER = 'Carbon1777';
const REPO = 'forton-lab-marketing';

const CRON_TO_WORKFLOW = {
	'0 9 * * *': 'preview_bot.yml',
	'7 12 * * 2': 'funnel_metrics.yml',
	'12 12 * * 2': 'centry_funnel.yml',
};

async function triggerWorkflow(env, workflow) {
	const url = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${workflow}/dispatches`;
	const res = await fetch(url, {
		method: 'POST',
		headers: {
			'Authorization': `Bearer ${env.GH_PAT}`,
			'Accept': 'application/vnd.github+json',
			'X-GitHub-Api-Version': '2022-11-28',
			'User-Agent': 'forton-cron-trigger',
		},
		body: JSON.stringify({ ref: 'main' }),
	});
	return { status: res.status, body: await res.text() };
}

export default {
	async scheduled(event, env, ctx) {
		const workflow = CRON_TO_WORKFLOW[event.cron];
		if (!workflow) {
			console.log('UNKNOWN cron — no workflow mapped:', event.cron);
			return;
		}
		const r = await triggerWorkflow(env, workflow);
		console.log('scheduled dispatch', event.cron, '→', workflow, r.status, r.body.slice(0, 200));
	},
	async fetch(request, env, ctx) {
		const u = new URL(request.url);
		// /trigger          → default preview_bot.yml (back-compat)
		// /trigger?wf=X.yml → triggers X.yml (must be in CRON_TO_WORKFLOW values)
		if (u.pathname !== '/trigger') return new Response('OK', { status: 200 });
		const workflow = u.searchParams.get('wf') || 'preview_bot.yml';
		if (!Object.values(CRON_TO_WORKFLOW).includes(workflow)) {
			return new Response(JSON.stringify({ error: 'workflow not in allowlist', workflow }), {
				status: 400,
				headers: { 'content-type': 'application/json' },
			});
		}
		const r = await triggerWorkflow(env, workflow);
		return new Response(JSON.stringify({ workflow, ...r }), {
			status: r.status === 204 ? 200 : r.status,
			headers: { 'content-type': 'application/json' },
		});
	},
};
