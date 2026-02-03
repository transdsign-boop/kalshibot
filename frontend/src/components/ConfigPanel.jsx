import { useState, useEffect } from 'react'
import { fetchConfig, postConfig } from '../api'

// Settings NOT on the Alpha Dashboard (dashboard has inline editing for guards, exits, alpha thresholds)
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
  PAPER_FILL_FRACTION: {
    label: 'Paper Fill Realism',
    unit: '',
    desc: 'Fraction of orderbook depth available per fill. 0.1 = pessimistic, 0.5 = realistic, 1.0 = optimistic.',
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
  MAX_DAILY_LOSS_PCT: {
    label: 'Max Loss Limit',
    unit: '%',
    desc: 'Halt all trading if total realized losses exceed this percentage of your starting balance.',
  },
  STOP_LOSS_CENTS: {
    label: 'Stop Loss',
    unit: 'c',
    desc: 'Exit position if down this many cents per contract. Higher = more room before cutting losses.',
  },
  PROFIT_TAKE_MIN_SECS: {
    label: 'PT Min Time Left',
    unit: 's',
    desc: 'Only take profit if more than this many seconds remain. Prevents selling right before expiry when settlement may pay more.',
  },
  EXTREME_DELTA_THRESHOLD: {
    label: 'Extreme Momentum',
    unit: '$',
    desc: 'Momentum threshold for aggressive execution. Crosses the spread (market order) instead of limit.',
  },
  RULE_MIN_CONFIDENCE: {
    label: 'Min Confidence',
    unit: '',
    desc: 'Minimum confidence score (0-1) from the rule engine to execute. Combines edge size, trend, and time remaining.',
  },
  FAIR_VALUE_K: {
    label: 'Fair Value Steepness',
    unit: '',
    desc: 'How aggressively fair value reacts to BTC distance from strike. Higher = more decisive when BTC is clearly above/below.',
  },
  VOL_HIGH_THRESHOLD: {
    label: 'High Vol Threshold',
    unit: '$/min',
    desc: 'BTC movement above this = high volatility mode. Adds trend-following bonus. BTC median ~$100-150/min.',
  },
  VOL_LOW_THRESHOLD: {
    label: 'Low Vol Threshold',
    unit: '$/min',
    desc: 'BTC movement below this = low volatility. Bot sits out (if enabled). BTC quiet periods ~$50-80/min.',
  },
  RULE_SIT_OUT_LOW_VOL: {
    label: 'Sit Out Low Vol',
    desc: 'Skip trading entirely when volatility is below the low threshold. Disable to trade in flat markets too.',
  },
  TREND_FOLLOW_VELOCITY: {
    label: 'Trend Velocity',
    unit: '$/s',
    desc: 'Minimum BTC price movement speed for trend-following bonus in high-vol mode. Lower = easier to trigger.',
  },
  EDGE_EXIT_ENABLED: {
    label: 'Edge Exit',
    desc: 'Exit positions when remaining edge evaporates. Allows re-entry after cooldown with higher edge requirement.',
  },
}

const GROUPS = [
  {
    title: 'General',
    keys: ['TRADING_ENABLED', 'POLL_INTERVAL_SECONDS', 'PAPER_STARTING_BALANCE', 'PAPER_FILL_FRACTION'],
  },
  {
    title: 'Sizing & Risk',
    keys: ['ORDER_SIZE_PCT', 'MAX_POSITION_PCT', 'MAX_TOTAL_EXPOSURE_PCT', 'MAX_DAILY_LOSS_PCT', 'STOP_LOSS_CENTS'],
  },
  {
    title: 'Strategy',
    keys: ['EDGE_EXIT_ENABLED', 'EXTREME_DELTA_THRESHOLD', 'PROFIT_TAKE_MIN_SECS', 'RULE_MIN_CONFIDENCE', 'FAIR_VALUE_K', 'VOL_HIGH_THRESHOLD', 'VOL_LOW_THRESHOLD', 'RULE_SIT_OUT_LOW_VOL', 'TREND_FOLLOW_VELOCITY'],
  },
]

export default function ConfigPanel() {
  const [cfgMeta, setCfgMeta] = useState(null)
  const [statusMsg, setStatusMsg] = useState({ text: '', ok: true })
  const [saving, setSaving] = useState(false)

  const refresh = () => fetchConfig().then(setCfgMeta).catch(console.error)

  useEffect(() => {
    refresh()
    // Re-fetch when config is changed elsewhere (e.g. analytics suggestion applied)
    const handler = () => refresh()
    window.addEventListener('config-updated', handler)
    return () => window.removeEventListener('config-updated', handler)
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
                          step={spec.type === 'float' ? (spec.min < 0.001 ? '0.00001' : '0.01') : '1'}
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
