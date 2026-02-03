import { useState } from 'react'
import { toPacific } from '../utils/time'

export default function TradeLog({ tradeData, mode }) {
  if (!tradeData) return null
  const { trades, summary } = tradeData
  if (!trades || trades.length === 0) {
    return (
      <div className="card p-4 mb-4">
        <p className="text-xs text-gray-600">No trades yet</p>
      </div>
    )
  }

  const { total_trades, wins, losses, pending, net_pnl, win_rate } = summary

  // Group trades by market_id, preserving newest-first order
  const groups = []
  const groupMap = {}
  for (const t of trades) {
    const mid = t.market_id
    if (!groupMap[mid]) {
      const group = { market_id: mid, entries: [], settled: null }
      groupMap[mid] = group
      groups.push(group)
    }
    groupMap[mid].entries.push(t)
    if (['SELL', 'SETTLED', 'SETTLE', 'SL', 'TP', 'EDGE'].includes(t.action)) {
      groupMap[mid].settled = t
    }
  }

  return (
    <div className="card p-4 mb-4">
      {/* Summary bar */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <p className="text-xs font-medium text-gray-400">Trade Log</p>
          {mode && (
            <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${mode === 'live' ? 'bg-green-500/15 text-green-400' : 'bg-amber-500/15 text-amber-400'}`}>
              {mode.toUpperCase()}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 text-[10px] font-mono">
          <span className="text-gray-500">
            {total_trades} trade{total_trades !== 1 ? 's' : ''}
          </span>
          {total_trades > 0 && (
            <>
              <span className="text-green-400">{wins}W</span>
              <span className="text-red-400">{losses}L</span>
              <span className={`font-semibold ${net_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {net_pnl >= 0 ? '+' : ''}{net_pnl.toFixed(2)}
              </span>
              <span className="text-gray-500">{(win_rate * 100).toFixed(0)}%</span>
            </>
          )}
          {pending > 0 && <span className="text-amber-400">{pending} open</span>}
        </div>
      </div>

      {/* Grouped trades */}
      <div className="space-y-0.5 max-h-64 overflow-y-auto">
        {groups.map((g) => (
          <TradeGroup key={g.market_id} group={g} />
        ))}
      </div>
    </div>
  )
}

function TradeGroup({ group }) {
  const [open, setOpen] = useState(false)
  const { market_id, entries, settled } = group

  const isPaper = market_id.startsWith('[PAPER]')
  const ticker = market_id
    .replace('[PAPER] ', '')
    .replace('KXBTC15M-', '')

  const buys = entries.filter((e) => e.action === 'BUY')
  const totalQty = settled ? settled.quantity : buys.reduce((s, e) => s + e.quantity, 0)
  const side = settled ? settled.side : buys[0]?.side || '?'
  const pnl = settled?.pnl
  const isOpen = !settled
  const ts = settled?.ts || entries[0]?.ts

  // Chronological order for expanded view
  const chronological = [...entries].reverse()

  return (
    <div>
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 text-[11px] font-mono py-1.5 px-1 hover:bg-white/[0.03] rounded transition cursor-pointer"
      >
        <span className="text-gray-600 text-[9px] w-3 shrink-0">{open ? '▼' : '▶'}</span>
        <span className="text-gray-500 w-14 shrink-0">{toPacific(ts)}</span>
        <span className={`w-7 shrink-0 font-semibold ${side === 'yes' ? 'text-green-400' : 'text-red-400'}`}>
          {side.toUpperCase()}
        </span>
        <span className="text-gray-400 shrink-0">{totalQty}x</span>
        <span className="text-gray-600 truncate flex-1 text-left">{ticker}</span>
        {isPaper && <span className="text-amber-400/60 text-[9px] shrink-0">PAPER</span>}
        {isOpen ? (
          <span className="text-amber-400 font-semibold shrink-0">OPEN</span>
        ) : pnl != null ? (
          <span className={`shrink-0 font-semibold ${pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
            {pnl >= 0 ? 'W' : 'L'} {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)}
          </span>
        ) : (
          <span className="text-gray-600 shrink-0">--</span>
        )}
        <span className="text-gray-700 text-[9px] shrink-0">
          {buys.length} order{buys.length !== 1 ? 's' : ''}
        </span>
      </button>

      {open && (
        <div className="ml-4 pl-2 border-l border-white/[0.06] space-y-0.5 pb-1">
          {chronological.map((t, i) => {
            const actionColor =
              t.action === 'BUY'
                ? 'text-green-400/70'
                : t.action === 'SELL'
                  ? 'text-red-400/70'
                  : 'text-amber-400/70'
            return (
              <div key={i} className="flex items-center gap-2 text-[10px] font-mono text-gray-500">
                <span className="w-14 shrink-0">{toPacific(t.ts)}</span>
                <span className={`w-14 shrink-0 ${actionColor}`}>{t.action}</span>
                <span>
                  {t.quantity}x {t.side.toUpperCase()} @ {(t.price * 100).toFixed(0)}c
                </span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
