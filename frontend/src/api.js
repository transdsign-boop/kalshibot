const BASE = '';

export async function fetchStatus() {
  const res = await fetch(`${BASE}/api/status`);
  return res.json();
}

export async function fetchLogs() {
  const res = await fetch(`${BASE}/api/logs`);
  return res.json();
}

export async function fetchTrades() {
  const res = await fetch(`${BASE}/api/trades`);
  return res.json();
}

export async function fetchConfig() {
  const res = await fetch(`${BASE}/api/config`);
  return res.json();
}

export async function postControl(action) {
  const res = await fetch(`${BASE}/api/${action}`, { method: 'POST' });
  return res.json();
}

export async function postEnv(env) {
  const res = await fetch(`${BASE}/api/env`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ env }),
  });
  return res.json();
}

export async function postChat(message) {
  const res = await fetch(`${BASE}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  });
  return res.json();
}

export async function postConfig(updates) {
  const res = await fetch(`${BASE}/api/config`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
  return res.json();
}

export async function postPaperReset() {
  const res = await fetch(`${BASE}/api/paper/reset`, { method: 'POST' });
  return res.json();
}
