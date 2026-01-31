import { useState, useEffect } from 'react'
import { fetchConfig, postConfig } from '../api'

const SETTINGS = {
  TRADING_ENABLED: {
    label: 'Trading Enabled',
    desc: 'Master switch. When off, the bot analyzes markets but never places real orders.',
  },
  POLL_INTERVAL_SECONDS: {
    label: 'Poll Interval',
    unit: 's',
    desc: 'Seconds between each bot cycle. Lower = more responsive but more API calls.',
  },
  PAPER_STARTING_BALANCE: {
    label: 'Paper Balance',
    unit: '$',
    desc: 'Starting balance for paper trading. Use the Reset button in the header to apply a new value.',
  },
  ORDER_SIZE_PCT: {
    label: 'Order Size',
    unit: '%',
    desc: 'Percentage of your balance to spend on each individual order.',
  },
  MAX_POSITION_PCT: {
    label: 'Max Position',
    unit: '%',
    desc: 'Maximum percentage of balance allowed in a single contract. Prevents over-concentration.',
  },
  MAX_TOTAL_EXPOSURE_PCT: {
    label: 'Max Exposure',
    unit: '%',
    desc: 'Maximum percentage of balance at risk across all open positions combined.',
  },
  MIN_SECONDS_TO_CLOSE: {
    label: 'Min Time to Enter',
    unit: 's',
    desc: 'Don\'t open new positions if fewer than this many seconds remain on the contract.',
  },
  MAX_SPREAD_CENTS: {
    label: 'Max Spread',
    unit: 'c',
    desc: 'Skip trading if the bid-ask spread is wider than this. Avoids poor fills in illiquid markets.',
  },
  MIN_AGENT_CONFIDENCE: {
    label: 'Min AI Confidence',
    unit: '',
    desc: 'Minimum confidence (0-1) from the AI agent to execute a trade. Higher = more selective.',
  },
  MIN_CONTRACT_PRICE: {
    label: 'Min Entry Price',
    unit: 'c',
    desc: 'Don\'t buy contracts cheaper than this. Low-price contracts are lottery tickets with low win rates.',
  },
  MAX_CONTRACT_PRICE: {
    label: 'Max Entry Price',
    unit: 'c',
    desc: 'Don\'t buy contracts above this price. Expensive contracts have poor risk/reward ratio.',
  },
  STOP_LOSS_CENTS: {
    label: 'Stop Loss',
    unit: 'c',
    desc: 'Exit the position if it drops this many cents per contract from your average entry price. Set to 0 to disable.',
  },
  MAX_DAILY_LOSS_PCT: {
    label: 'Max Loss Limit',
    unit: '%',
    desc: 'Halt all trading if total realized losses exceed this percentage of your starting balance.',
  },
  PROFIT_TAKE_PCT: {
    label: 'Profit Take',
    unit: '%',
    desc: 'Sell entire position when profit exceeds this % gain from entry. E.g., 50% on a 30c entry triggers at 45c.',
  },
  PROFIT_TAKE_MIN_SECS: {
    label: 'PT Min Time Left',
    unit: 's',
    desc: 'Only take profit if more than this many seconds remain. Prevents selling right before expiry when settlement may pay more.',
  },
  FREE_ROLL_PRICE: {
    label: 'Free Roll Price',
    unit: 'c',
    desc: 'Sell half the position at this contract price to lock in capital. The remaining half rides for free.',
  },
  HOLD_EXPIRY_SECS: {
    label: 'Hold to Expiry',
    unit: 's',
    desc: 'Don\'t sell in the last N seconds before expiry. Ride the position to settlement instead.',
  },
  DELTA_THRESHOLD: {
    label: 'Momentum Trigger',
    unit: '$',
    desc: 'Binance-Coinbase price momentum (in USD) required to force a trade, overriding the AI agent.',
  },
  EXTREME_DELTA_THRESHOLD: {
    label: 'Extreme Momentum',
    unit: '$',
    desc: 'Momentum threshold for aggressive execution. Crosses the spread (market order) instead of limit.',
  },
  ANCHOR_SECONDS_THRESHOLD: {
    label: 'Anchor Defense',
    unit: 's',
    desc: 'Seconds before expiry when anchor defense activates. Projects settlement and can force exit if losing.',
  },
  LEAD_LAG_ENABLED: {
    label: 'Lead-Lag Signal',
    desc: 'Enable multi-exchange lead-lag signal. Uses all 6 exchanges to detect when BTC moves but Kalshi contracts lag behind.',
  },
  LEAD_LAG_THRESHOLD: {
    label: 'Lead-Lag Threshold',
    unit: '$',
    desc: 'How much the weighted global BTC price must differ from strike to trigger. Higher = less sensitive, fewer trades.',
  },
}

const GROUPS = [
  {
    title: 'General',
    keys: ['TRADING_ENABLED', 'POLL_INTERVAL_SECONDS', 'PAPER_STARTING_BALANCE'],
  },
  {
    title: 'Position Sizing',
    keys: ['ORDER_SIZE_PCT', 'MAX_POSITION_PCT', 'MAX_TOTAL_EXPOSURE_PCT'],
  },
  {
    title: 'Entry Guards',
    keys: ['MIN_SECONDS_TO_CLOSE', 'MAX_SPREAD_CENTS', 'MIN_AGENT_CONFIDENCE', 'MIN_CONTRACT_PRICE', 'MAX_CONTRACT_PRICE'],
  },
  {
    title: 'Risk Management',
    keys: ['STOP_LOSS_CENTS', 'MAX_DAILY_LOSS_PCT'],
  },
  {
    title: 'Profit & Exit',
    keys: ['PROFIT_TAKE_PCT', 'PROFIT_TAKE_MIN_SECS', 'FREE_ROLL_PRICE', 'HOLD_EXPIRY_SECS'],
  },
  {
    title: 'Alpha Engine',
    keys: ['LEAD_LAG_ENABLED', 'LEAD_LAG_THRESHOLD', 'DELTA_THRESHOLD', 'EXTREME_DELTA_THRESHOLD', 'ANCHOR_SECONDS_THRESHOLD'],
  },
]

export default function ConfigPanel() {
  const [cfgMeta, setCfgMeta] = useState(null)
  const [statusMsg, setStatusMsg] = useState({ text: '', ok: true })
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    fetchConfig().then(setCfgMeta).catch(console.error)
  }, [])

  function showStatus(text, ok) {
    setStatusMsg({ text, ok })
    setTimeout(() => setStatusMsg({ text: '', ok: true }), 3000)
  }

  async function handleFieldChange(key, value) {
    const info = SETTINGS[key] || {}
    try {
      await postConfig({ [key]: value })
      showStatus(`Saved: ${info.label || key}`, true)
    } catch {
      showStatus(`Error saving ${info.label || key}`, false)
    }
  }

  async function handleSaveAll() {
    if (!cfgMeta) return
    setSaving(true)
    try {
      const updates = {}
      for (const [key, spec] of Object.entries(cfgMeta)) {
        updates[key] = spec.value
      }
      await postConfig(updates)
      showStatus('All settings saved', true)
    } catch {
      showStatus('Error saving', false)
    }
    setSaving(false)
  }

  function updateLocalValue(key, value) {
    setCfgMeta(prev => ({
      ...prev,
      [key]: { ...prev[key], value },
    }))
  }

  if (!cfgMeta) return <p className="text-xs text-gray-600">Loading config...</p>

  return (
    <div className="space-y-5">
      {GROUPS.map(group => {
        const visibleKeys = group.keys.filter(k => cfgMeta[k])
        if (visibleKeys.length === 0) return null
        return (
          <div key={group.title}>
            <h3 className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider mb-2.5 border-b border-white/[0.06] pb-1.5">
              {group.title}
            </h3>
            <div className="space-y-3">
              {visibleKeys.map(key => {
                const spec = cfgMeta[key]
                const info = SETTINGS[key] || {}
                return (
                  <div key={key} className="flex items-start gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5">
                        <span className="text-[11px] text-gray-300 font-medium">{info.label || key}</span>
                        {info.unit && <span className="text-[9px] text-gray-600">({info.unit})</span>}
                      </div>
                      <p className="text-[10px] text-gray-600 leading-relaxed mt-0.5">{info.desc}</p>
                    </div>
                    <div className="w-20 shrink-0">
                      {spec.type === 'bool' ? (
                        <label className="relative inline-flex items-center cursor-pointer">
                          <input
                            type="checkbox"
                            checked={spec.value}
                            onChange={e => {
                              const val = e.target.checked
                              updateLocalValue(key, val)
                              handleFieldChange(key, val)
                            }}
                            className="sr-only peer"
                          />
                          <div className="w-8 h-4 bg-gray-700 peer-checked:bg-green-500 rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-3 after:w-3 after:transition-all" />
                        </label>
                      ) : (
                        <input
                          type="number"
                          value={spec.value}
                          min={spec.min}
                          max={spec.max}
                          step={spec.type === 'float' ? '0.01' : '1'}
                          onChange={e => {
                            const val = spec.type === 'float' ? parseFloat(e.target.value) : parseInt(e.target.value, 10)
                            updateLocalValue(key, val)
                          }}
                          onBlur={e => {
                            const val = spec.type === 'float' ? parseFloat(e.target.value) : parseInt(e.target.value, 10)
                            if (!isNaN(val)) handleFieldChange(key, val)
                          }}
                          className="w-full bg-black/20 border border-white/[0.06] rounded px-2 py-1 text-xs text-gray-200 text-right focus:outline-none focus:border-blue-500/50"
                        />
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )
      })}

      <div className="flex items-center justify-between pt-2 border-t border-white/[0.06]">
        <button
          onClick={handleSaveAll}
          disabled={saving}
          className="px-3 py-1.5 rounded-lg bg-purple-500/20 text-purple-400 text-[11px] font-semibold hover:bg-purple-500/30 transition disabled:opacity-50"
        >
          {saving ? 'Saving...' : 'Save All'}
        </button>
        {statusMsg.text && (
          <span className={`text-[11px] ${statusMsg.ok ? 'text-green-400' : 'text-red-400'}`}>
            {statusMsg.text}
          </span>
        )}
      </div>
    </div>
  )
}
