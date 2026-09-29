import { registerTsx } from '/home/quinton/Projects/Codify/ui/tests/tsxLoader.ts';
registerTsx();
const { withDom } = await import('/home/quinton/Projects/Codify/ui/tests/dom.ts');
const React = (await import('react')).default;
const h = React.createElement;
const listeners = new Map();
const calls = [];
let n = 0;
(globalThis).__TAURI_INTERNALS__ = {
  transformCallback(cb) { if (cb && cb.handler && cb.event) { const l = listeners.get(cb.event) ?? []; l.push(cb.handler); listeners.set(cb.event, l); } return ++n; },
  async invoke(cmd, args = {}) { calls.push(cmd); if (cmd === 'codify_terminal_open') return 'term-1'; if (cmd === 'codify_get_engine_info') return { port: 51820, token: 't' }; return null; },
  metadata: { currentWebview: { windowLabel: 'main' }, currentWindow: { label: 'main' } },
};
(globalThis).__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener() {} };
const realFetch = globalThis.fetch;
globalThis.fetch = (async (input) => {
  const url = String(input);
  const json = (body) => ({ ok: true, status: 200, json: async () => body });
  if (url.includes('/health')) return json({ ok: true, authenticated: true });
  if (url.includes('/workspaces')) return json([{ id: 'ws-alpha', name: 'Alpha', root_path: '/tmp/a', design_contract_path: '', created_at: 1 }]);
  if (url.includes('/models')) return json({ models: [] });
  return json([]);
});
const { App } = await import('/home/quinton/Projects/Codify/ui/src/App.tsx');
await withDom(async (dom) => {
  await dom.render(h(App));
  await dom.settle();
  for (let i = 0; i < 8; i++) { await new Promise(r => setTimeout(r, 0)); await new Promise(r => requestAnimationFrame(() => r(null))); }
  const btn = dom.container.querySelector('button[title^="Terminal"]');
  console.log('button found:', !!btn, 'disabled:', btn?.disabled);
  await dom.click(btn);
  for (let i = 0; i < 8; i++) { await new Promise(r => setTimeout(r, 0)); await new Promise(r => requestAnimationFrame(() => r(null))); }
  console.log('terminal calls:', calls.filter(c => c.includes('terminal')));
  const tabs = dom.container.querySelectorAll('[role="tab"]');
  console.log('tabs:', tabs.length, tabs.length ? JSON.stringify(tabs[0].getAttribute('aria-label')) : '');
  const pane = dom.container.querySelector('[data-terminal-id="term-1"]');
  console.log('pane mounted:', !!pane, 'xterm-rows:', !!pane?.querySelector('.xterm-rows'));
  const emit = (event, payload) => { for (const hh of listeners.get(event) ?? []) hh(payload); };
  emit('terminal-output', { id: 'term-1', data: 'quinton@alpha:~$ ' });
  for (let i = 0; i < 8; i++) { await new Promise(r => setTimeout(r, 0)); await new Promise(r => requestAnimationFrame(() => r(null))); }
  const rows = pane?.querySelector('.xterm-rows');
  console.log('screen:', JSON.stringify(rows?.textContent?.slice(0, 40) ?? null));
  console.log('terminal-output listeners:', (listeners.get('terminal-output') ?? []).length);
});
globalThis.fetch = realFetch;
