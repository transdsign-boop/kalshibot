function fmt(value) {
  const num = typeof value === 'number' ? value : parseFloat(value) || 0
  return num
}

export default function KeyMetrics({ status }) {
  const { balance, day_pnl, day_pnl_pct, position_pnl, position_pnl_pct, position, orderbook, active_position } = status
  const pnl = fmt(day_pnl)
  const pnlPct = fmt(day_pnl_pct)
  const posPnl = fmt(position_pnl)
  const posPnlPct = fmt(position_pnl_pct)
  const ob = orderbook || {}

  // Compute per-contract cost and current value for position context
  let posDetail = null
  if (active_position) {
    const posQty = active_position.position || 0
    const exposureCents = active_position.market_exposure || 0
    const qty = Math.abs(posQty)
    if (qty > 0) {
      const costPer = (exposureCents / qty).toFixed(0)
      const valuePer = posQty > 0
        ? (ob.best_bid || 0)
        : (100 - (ob.best_ask || 100))
      posDetail = `${valuePer}c vs ${costPer}c cost`
    }
  }

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-4">
      <Metric label="Balance" value={balance} />
      <Metric
        label="P&L"
        value={`${pnl >= 0 ? '+' : ''}$${Math.abs(pnl).toFixed(2)}`}
        color={pnl > 0 ? 'text-green-400' : pnl < 0 ? 'text-red-400' : 'text-gray-400'}
        detail={`${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(1)}%`}
      />
      <Metric
        label="Pos P&L"
        value={`${posPnl >= 0 ? '+' : ''}$${Math.abs(posPnl).toFixed(2)}`}
        color={posPnl > 0 ? 'text-green-400' : posPnl < 0 ? 'text-red-400' : 'text-gray-500'}
        detail={posPnlPct !== 0 ? `${posPnlPct >= 0 ? '+' : ''}${posPnlPct.toFixed(1)}% | ${posDetail}` : posDetail}
      />
      <Metric label="Position" value={position} small />
    </div>
  )
}

function Metric({ label, value, color = 'text-gray-100', small = false, detail = null }) {
  return (
    <div className="card px-3 py-3">
      <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-0.5">{label}</p>
      <p className={`${small ? 'text-sm' : 'text-lg'} font-semibold ${color} truncate`}>{value}</p>
      {detail && <p className="text-[10px] text-gray-600 font-mono mt-0.5">{detail}</p>}
    </div>
  )
}
