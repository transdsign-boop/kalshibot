export default function BotStatus({ status }) {
  const {
    running, last_action, decision, confidence, reasoning, alpha_override,
    balance, day_pnl, position_pnl, position, active_position, orderbook
  } = status
  const conf = Math.round((confidence || 0) * 100)
  const action = last_action || 'Idle'
  const pnl = typeof day_pnl === 'number' ? day_pnl : parseFloat(day_pnl) || 0
  const posPnl = typeof position_pnl === 'number' ? position_pnl : parseFloat(position_pnl) || 0
  const ob = orderbook || {}

  // Accent color from action
  let dotColor = 'bg-gray-600'
  if (running) {
    if (action.includes('Placed') || action.includes('Filled') || action.includes('Retry')) dotColor = 'bg-green-500'
    else if (action.includes('guard') || action.includes('Guard') || action.includes('too cheap') || action.includes('expensive')) dotColor = 'bg-yellow-500'
    else if (action.includes('Error') || action.includes('rejected')) dotColor = 'bg-red-500'
    else dotColor = 'bg-blue-500'
  }

  // Decision badge
  let badgeBg = 'bg-white/[0.06] text-gray-500'
  if (decision === 'BUY_YES') badgeBg = 'bg-green-500/15 text-green-400'
  else if (decision === 'BUY_NO') badgeBg = 'bg-red-500/15 text-red-400'

  // Position value computation
  const bal = typeof balance === 'number' ? balance : parseFloat(balance) || 0
  let posMarketValue = 0
  let posQty = 0
  let posSide = ''
  let costPerContract = 0
  let valuePerContract = 0
  if (active_position) {
    const rawQty = active_position.position || 0
    const exposureCents = active_position.market_exposure || 0
    posQty = Math.abs(rawQty)
    posSide = rawQty > 0 ? 'YES' : 'NO'
    costPerContract = posQty > 0 ? exposureCents / posQty : 0
    valuePerContract = rawQty > 0 ? (ob.best_bid || 0) : (100 - (ob.best_ask || 100))
    posMarketValue = valuePerContract * posQty / 100 // dollars
  }
  const totalAccount = bal + posMarketValue
  const startBal = totalAccount - pnl
  const pnlPct = startBal > 0 ? (pnl / startBal) * 100 : 0

  return (
    <div className="card p-4 mb-4">
      {/* Action line */}
      <div className="flex items-center gap-2 mb-2">
        {running ? (
          <div className="w-3.5 h-3.5 rounded-full border-2 border-blue-500 border-t-transparent animate-spin flex-shrink-0" />
        ) : (
          <div className={`w-2 h-2 rounded-full flex-shrink-0 ${dotColor}`} />
        )}
        <span className="text-sm font-medium text-gray-200 truncate flex-1">
          {running ? action : 'Bot stopped'}
        </span>
        {decision && decision !== '—' && (
          <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded-full ${badgeBg}`}>
            {decision}
          </span>
        )}
      </div>

      {/* Reasoning */}
      {reasoning && (
        <p className="text-xs text-gray-500 leading-relaxed mb-2">{reasoning}</p>
      )}

      {/* Alpha override */}
      {alpha_override && (
        <p className="text-[10px] text-purple-400 mb-2">Alpha: {alpha_override}</p>
      )}

      {/* Confidence bar (thin) */}
      {conf > 0 && (
        <div className="flex items-center gap-2 mb-3">
          <div className="flex-1 h-1 bg-white/[0.04] rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-700 ${
                conf >= 75 ? 'bg-green-500' : conf >= 50 ? 'bg-yellow-500' : 'bg-red-500'
              }`}
              style={{ width: `${conf}%` }}
            />
          </div>
          <span className="text-[10px] font-mono text-gray-600">{conf}%</span>
        </div>
      )}

      {/* Account overview */}
      <div className="pt-2 border-t border-white/[0.04]">
        {/* Row 1: Total account + P&L + % */}
        <div className="flex items-baseline justify-between">
          <div className="flex items-baseline gap-1.5">
            <span className="text-2xl font-bold font-mono text-gray-100">
              ${totalAccount.toFixed(2)}
            </span>
            <span className="text-[10px] text-gray-600">total</span>
          </div>
          <div className="flex items-baseline gap-2">
            <span className={`text-lg font-bold font-mono ${pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
              {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)}
            </span>
            <span className={`text-sm font-semibold font-mono ${pnlPct >= 0 ? 'text-green-400/70' : 'text-red-400/70'}`}>
              {pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(1)}%
            </span>
          </div>
        </div>

        {/* Row 2: Cash + position breakdown */}
        <div className="flex items-center gap-3 mt-1 text-[11px] font-mono text-gray-500">
          <span>${bal.toFixed(2)} cash</span>
          {posQty > 0 && (
            <>
              <span className={posPnl >= 0 ? 'text-green-400/70' : 'text-red-400/70'}>
                ${posMarketValue.toFixed(2)} position
              </span>
              <span className="text-gray-600">
                {posQty}x {posSide} @ {costPerContract.toFixed(0)}c → {valuePerContract}c
              </span>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
