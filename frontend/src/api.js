const BASE = '';

export async function fetchStatus(asset = 'btc', bot = 'paper') {
  const res = await fetch(`${BASE}/api/status?asset=${asset}&bot=${bot}`);
  return res.json();
}

export async function fetchStatusBoth(asset = 'btc') {
  const res = await fetch(`${BASE}/api/status/both?asset=${asset}`);
  return res.json();
}

export async function fetchStatusAll() {
  const res = await fetch(`${BASE}/api/status/all`);
  return res.json();
}

export async function fetchLogs(asset = '') {
  const url = asset ? `${BASE}/api/logs?asset=${asset}` : `${BASE}/api/logs`;
  const res = await fetch(url);
  return res.json();
}

export async function fetchTrades(mode = '', asset = '') {
  const params = new URLSearchParams();
  if (mode) params.set('mode', mode);
  if (asset) params.set('asset', asset);
  const qs = params.toString();
  const url = qs ? `${BASE}/api/trades?${qs}` : `${BASE}/api/trades`;
  const res = await fetch(url);
  return res.json();
}

export async function fetchConfig(asset = 'btc', bot = 'paper') {
  const res = await fetch(`${BASE}/api/config?asset=${asset}&bot=${bot}`);
  return res.json();
}

export async function postControl(action, asset = 'btc', bot = 'paper') {
  const res = await fetch(`${BASE}/api/${action}?asset=${asset}&bot=${bot}`, { method: 'POST' });
  return res.json();
}

export async function postChat(message, asset = 'btc', bot = 'paper') {
  const res = await fetch(`${BASE}/api/chat?asset=${asset}&bot=${bot}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  });
  return res.json();
}

export async function postConfig(updates, asset = 'btc', bot = 'paper') {
  const res = await fetch(`${BASE}/api/config?asset=${asset}&bot=${bot}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
  return res.json();
}

export async function postPaperReset(asset = 'btc') {
  const res = await fetch(`${BASE}/api/paper/reset?asset=${asset}`, { method: 'POST' });
  return res.json();
}

export async function fetchAnalytics(mode = '', asset = '') {
  const params = new URLSearchParams();
  if (mode) params.set('mode', mode);
  if (asset) params.set('asset', asset);
  const qs = params.toString();
  const url = qs ? `${BASE}/api/analytics?${qs}` : `${BASE}/api/analytics`;
  const res = await fetch(url);
  return res.json();
}

export async function applySuggestion(param, value, asset = 'btc', bot = 'paper') {
  const res = await fetch(`${BASE}/api/analytics/apply?asset=${asset}&bot=${bot}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ param, value }),
  });
  return res.json();
}
