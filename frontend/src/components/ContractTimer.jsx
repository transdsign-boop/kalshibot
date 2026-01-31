import { useState, useEffect, useRef } from 'react'

const CONTRACT_DURATION = 15 * 60 // 15-minute contracts

export default function ContractTimer({ status }) {
  const { close_time, seconds_to_close, strike_price, alpha, market_title } = status
  const a = alpha || {}
  // Use projected settlement (rolling 60s average, BRTI proxy) for accuracy
  // Fall back to raw exchange price if projection not available
  const btcPrice = a.projected_settlement || a.coinbase_price || a.binance_price || null

  // ── Smooth client-side countdown ──────────────────────────
  const [displaySeconds, setDisplaySeconds] = useState(null)
  const closeTimeRef = useRef(null)

  // Sync from server on each poll
  useEffect(() => {
    if (close_time) {
      closeTimeRef.current = close_time
      const closeMs = new Date(close_time).getTime()
      const secs = Math.max(0, (closeMs - Date.now()) / 1000)
      setDisplaySeconds(secs)
    } else {
      closeTimeRef.current = null
      setDisplaySeconds(null)
    }
  }, [close_time, seconds_to_close])

  // Tick every second for smooth countdown
  useEffect(() => {
    if (!closeTimeRef.current) return
    const interval = setInterval(() => {
      const closeMs = new Date(closeTimeRef.current).getTime()
      const secs = Math.max(0, (closeMs - Date.now()) / 1000)
      setDisplaySeconds(secs)
    }, 1000)
    return () => clearInterval(interval)
  }, [close_time])

  // ── No active contract ────────────────────────────────────
  if (displaySeconds == null || !close_time) {
    return (
      <div className="card p-4 mb-4">
        <p className="text-xs text-gray-600 text-center">No active contract</p>
      </div>
    )
  }

  // ── Timer formatting ──────────────────────────────────────
  const mins = Math.floor(displaySeconds / 60)
  const secs = Math.floor(displaySeconds % 60)
  const timeStr = `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`
  const elapsed = CONTRACT_DURATION - displaySeconds
  const progressPct = Math.min(100, Math.max(0, (elapsed / CONTRACT_DURATION) * 100))

  const timerColor = displaySeconds <= 60
    ? 'text-red-400'
    : displaySeconds <= 180
      ? 'text-amber-400'
      : 'text-gray-100'

  const barColor = displaySeconds <= 60
    ? 'bg-red-500'
    : displaySeconds <= 180
      ? 'bg-amber-500'
      : 'bg-blue-500'

  // ── Price vs Strike ───────────────────────────────────────
  const hasStrike = strike_price != null && strike_price > 0
  const hasPrice = btcPrice != null && btcPrice > 0
  const diff = hasStrike && hasPrice ? btcPrice - strike_price : 0
  const aboveStrike = diff >= 0

  const RANGE = 500
  const pricePosition = hasStrike && hasPrice
    ? Math.min(100, Math.max(0, 50 + (diff / RANGE) * 50))
    : 50

  const fmtPrice = (n) => n ? '$' + n.toFixed(0).replace(/\B(?=(\d{3})+(?!\d))/g, ',') : '--'
  const fmtDiff = (n) => `${n >= 0 ? '+' : '-'}$${Math.abs(n).toFixed(0)}`

  return (
    <div className="card p-4 mb-4">
      {/* ── Timer Section ────────────────────────────────── */}
      <div className="flex items-center justify-between mb-1">
        <p className="text-[10px] text-gray-500 uppercase tracking-wider">Contract Timer</p>
        <p className={`text-[10px] font-semibold ${displaySeconds <= 60 ? 'text-red-400' : 'text-gray-600'}`}>
          {displaySeconds <= 60 ? 'EXPIRING' : 'ACTIVE'}
        </p>
      </div>

      <div className="text-center my-3">
        <p className={`text-4xl font-bold font-mono tracking-wider ${timerColor}`}>
          {timeStr}
        </p>
        <p className="text-[10px] text-gray-600 mt-1">remaining</p>
      </div>

      {/* Progress bar */}
      <div className="w-full bg-white/[0.04] rounded-full h-1.5 mb-5">
        <div
          className={`h-full rounded-full transition-all duration-1000 ${barColor}`}
          style={{ width: `${progressPct}%` }}
        />
      </div>

      {/* ── Price vs Strike Section ──────────────────────── */}
      {hasStrike && (
        <>
          <div className="flex items-center justify-between mb-2">
            <p className="text-[10px] text-gray-500 uppercase tracking-wider">Price vs Strike</p>
            {hasPrice && (
              <span className={`text-[11px] font-mono font-semibold ${aboveStrike ? 'text-green-400' : 'text-red-400'}`}>
                {aboveStrike ? 'YES winning' : 'NO winning'}
              </span>
            )}
          </div>

          {/* Visual bar */}
          <div className="relative h-8 mb-2 rounded-lg overflow-hidden">
            {/* Red zone (below strike) */}
            <div className="absolute inset-y-0 left-0 w-1/2 bg-red-500/10" />
            {/* Green zone (above strike) */}
            <div className="absolute inset-y-0 right-0 w-1/2 bg-green-500/10" />

            {/* Strike center line */}
            <div className="absolute inset-y-0 left-1/2 w-px bg-white/20" />

            {/* Strike diamond marker */}
            <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 z-10">
              <div className="w-2 h-2 bg-white/30 rotate-45" />
            </div>

            {/* Price indicator dot */}
            {hasPrice && (
              <div
                className="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 z-20 transition-all duration-700"
                style={{ left: `${pricePosition}%` }}
              >
                <div className={`w-3.5 h-3.5 rounded-full border-2 ${
                  aboveStrike
                    ? 'bg-green-500 border-green-400 shadow-[0_0_8px_rgba(34,197,94,0.5)]'
                    : 'bg-red-500 border-red-400 shadow-[0_0_8px_rgba(239,68,68,0.5)]'
                }`} />
              </div>
            )}
          </div>

          {/* Scale labels */}
          <div className="flex items-center justify-between text-[10px] font-mono mb-3">
            <span className="text-gray-600">{fmtPrice(strike_price - RANGE)}</span>
            <span className="text-gray-400">Strike {fmtPrice(strike_price)}</span>
            <span className="text-gray-600">{fmtPrice(strike_price + RANGE)}</span>
          </div>

          {/* BTC price + difference */}
          {hasPrice && (
            <div className="flex flex-col items-center gap-0.5">
              <div className="flex items-center gap-3">
                <span className="text-sm font-mono text-gray-300">BTC {fmtPrice(btcPrice)}</span>
                <span className={`text-sm font-mono font-semibold ${aboveStrike ? 'text-green-400' : 'text-red-400'}`}>
                  {fmtDiff(diff)}
                </span>
              </div>
              <span className="text-[9px] text-gray-600">60s avg (BRTI proxy)</span>
            </div>
          )}
        </>
      )}
    </div>
  )
}
