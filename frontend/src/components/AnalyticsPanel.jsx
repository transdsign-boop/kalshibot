import { useState, useEffect } from 'react'
import { fetchAnalytics, applySuggestion } from '../api'

const SEGMENT_ORDER = [
  { key: 'side', title: 'By Side' },
  { key: 'entry_price', title: 'By Entry Price' },
  { key: 'exit_type', title: 'By Exit Type' },
  { key: 'position_size', title: 'By Position Size' },
  { key: 'hold_time', title: 'By Hold Time' },
  { key: 'vol_regime', title: 'By Volatility' },
  { key: 'edge', title: 'By Edge' },
  { key: 'confidence', title: 'By Confidence' },
  { key: 'time', title: 'By Time Left' },
  { key: 'trigger', title: 'By Trigger' },
]

function StatBox({ label, value, color }) {
  return (
    <div className="bg-black/20 rounded-lg px-2 py-1.5 text-center">
      <div className={`text-sm font-mono font-semibold ${color}`}>{value}</div>
      <div className="text-[9px] text-gray-600 mt-0.5">{label}</div>
    </div>
  )
}

function SummaryRow({ summary }) {
  const { total_trades, wins, losses, win_rate, avg_pnl_cents, profit_factor } = summary
  return (
    <div className="grid grid-cols-4 gap-2">
      <StatBox
        label="Win Rate"
        value={`${(win_rate * 100).toFixed(0)}%`}
        color={win_rate >= 0.5 ? 'text-green-400' : 'text-red-400'}
      />
      <StatBox
        label="Avg P&L"
        value={`${avg_pnl_cents >= 0 ? '+' : ''}${(avg_pnl_cents / 100).toFixed(2)}`}
        color={avg_pnl_cents >= 0 ? 'text-green-400' : 'text-red-400'}
      />
      <StatBox
        label="Profit Factor"
        value={profit_factor === null || profit_factor === undefined ? '\u221e' : profit_factor.toFixed(2)}
        color={profit_factor >= 1 ? 'text-green-400' : 'text-red-400'}
      />
      <StatBox
        label="Record"
        value={`${wins}W / ${losses}L`}
        color="text-gray-300"
      />
    </div>
  )
}

function SegmentSection({ title, data }) {
  if (!data || Object.keys(data).length === 0) return null

  return (
    <div>
      <h3 className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider mb-1">{title}</h3>
      <div className="space-y-0.5">
        {Object.entries(data)
          .sort((a, b) => b[1].trades - a[1].trades)
          .map(([label, stats]) => (
            <div key={label} className="flex items-center gap-2 text-[10px] font-mono py-0.5">
              <span className="text-gray-500 w-16 shrink-0 truncate">{label}</span>
              <span className="text-gray-600 w-8 shrink-0 text-right">{stats.trades}t</span>
              <span className={`w-10 shrink-0 text-right ${stats.win_rate >= 0.5 ? 'text-green-400/70' : 'text-red-400/70'}`}>
                {(stats.win_rate * 100).toFixed(0)}%
              </span>
              <span className={`w-16 shrink-0 text-right ${stats.avg_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {stats.avg_pnl >= 0 ? '+' : ''}{(stats.avg_pnl / 100).toFixed(2)}
              </span>
              <div className="flex-1 h-1 bg-white/[0.04] rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full ${stats.avg_pnl >= 0 ? 'bg-green-500' : 'bg-red-500'}`}
                  style={{ width: `${Math.min(100, Math.abs(stats.avg_pnl) / 2)}%` }}
                />
              </div>
            </div>
          ))}
      </div>
    </div>
  )
}

function SuggestionCard({ suggestion, onApply }) {
  const { param, current_value, suggested_value, reasoning, sample_size, confidence } = suggestion
  const [applying, setApplying] = useState(false)

  const confColors = {
    high: 'bg-green-500/15 text-green-400',
    medium: 'bg-amber-500/15 text-amber-400',
    low: 'bg-gray-700 text-gray-500',
  }

  return (
    <div className="bg-black/20 border border-white/[0.06] rounded-lg px-3 py-2">
      <div className="flex items-center justify-between mb-1">
        <span className="text-[11px] font-semibold text-gray-300">{param}</span>
        <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${confColors[confidence] || confColors.low}`}>
          {confidence} ({sample_size} trades)
        </span>
      </div>
      <p className="text-[10px] text-gray-500 leading-relaxed mb-2">{reasoning}</p>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-[10px] font-mono">
          <span className="text-gray-500">{String(current_value)}</span>
          <span className="text-gray-600">{'\u2192'}</span>
          <span className="text-blue-400 font-semibold">{String(suggested_value)}</span>
        </div>
        <button
          onClick={async () => {
            setApplying(true)
            await onApply()
            setApplying(false)
          }}
          disabled={applying}
          className="px-2 py-1 rounded bg-blue-500/20 text-blue-400 text-[10px] font-semibold hover:bg-blue-500/30 transition disabled:opacity-50"
        >
          {applying ? 'Applying...' : 'Apply'}
        </button>
      </div>
    </div>
  )
}

export default function AnalyticsPanel({ mode = '' }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [statusMsg, setStatusMsg] = useState(null)

  const refresh = async () => {
    setLoading(true)
    try {
      const result = await fetchAnalytics(mode)
      setData(result)
    } catch (err) {
      console.error('Analytics fetch failed:', err)
    }
    setLoading(false)
  }

  useEffect(() => { refresh() }, [mode])

  if (!data || !data.summary || !data.summary.total_trades) {
    return (
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <p className="text-xs text-gray-600">
            {loading ? 'Analyzing trades...' : 'No completed trades to analyze yet. Snapshots are recorded as trades complete.'}
          </p>
          <button onClick={refresh} disabled={loading}
            className="text-[10px] text-blue-400 hover:text-blue-300 shrink-0">
            {loading ? 'Loading...' : 'Refresh'}
          </button>
        </div>
      </div>
    )
  }

  const { summary, segments, suggestions } = data

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <span className="text-[10px] text-gray-600">
          Based on {summary.total_trades} completed trades
        </span>
        <button onClick={refresh} disabled={loading}
          className="text-[10px] text-blue-400 hover:text-blue-300 shrink-0">
          {loading ? 'Analyzing...' : 'Refresh'}
        </button>
      </div>

      {/* Summary */}
      <SummaryRow summary={summary} />

      {/* Suggestions */}
      {suggestions && suggestions.length > 0 && (
        <div>
          <h3 className="text-[11px] font-semibold text-amber-400/80 uppercase tracking-wider mb-2 border-b border-white/[0.06] pb-1">
            Suggested Tweaks
          </h3>
          <div className="space-y-2">
            {suggestions.map((s, i) => (
              <SuggestionCard key={i} suggestion={s} onApply={async () => {
                try {
                  await applySuggestion(s.param, s.suggested_value)
                  setStatusMsg({ text: `Applied: ${s.param}`, ok: true })
                  setTimeout(() => setStatusMsg(null), 3000)
                  window.dispatchEvent(new Event('config-updated'))
                  refresh()
                } catch {
                  setStatusMsg({ text: `Failed to apply ${s.param}`, ok: false })
                  setTimeout(() => setStatusMsg(null), 3000)
                }
              }} />
            ))}
          </div>
        </div>
      )}

      {/* Segments */}
      <div className="border-t border-white/[0.06] pt-3">
        <h3 className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider mb-2">
          Performance Breakdown
        </h3>
        <div className="space-y-3">
          {SEGMENT_ORDER.map(({ key, title }) => (
            <SegmentSection key={key} title={title} data={segments[key]} />
          ))}
        </div>
      </div>

      {statusMsg && (
        <p className={`text-[10px] ${statusMsg.ok ? 'text-green-400' : 'text-red-400'}`}>
          {statusMsg.text}
        </p>
      )}
    </div>
  )
}
