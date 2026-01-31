export default function MarketCard({ status }) {
  const { market, orderbook, alpha } = status
  const ob = orderbook || {}
  const a = alpha || {}

  const bestBid = ob.best_bid ?? '--'
  const bestAsk = ob.best_ask ?? '--'
  const spread = ob.spread ?? '--'
  const yesDepth = ob.yes_depth || 0
  const noDepth = ob.no_depth || 0
  const maxDepth = Math.max(yesDepth, noDepth, 1)

  const binPrice = a.binance_price
  const cbPrice = a.coinbase_price
  const fmtPrice = (n) => n ? '$' + n.toFixed(0).replace(/\B(?=(\d{3})+(?!\d))/g, ',') : '--'

  return (
    <div className="card p-4 mb-4">
      {/* Market name + BTC price */}
      <div className="flex items-center justify-between mb-3">
        <p className="text-xs text-gray-500 truncate flex-1">{market}</p>
        <p className="text-xs font-mono text-gray-500">
          BTC {fmtPrice(cbPrice)}
        </p>
      </div>

      {/* Bid / Ask */}
      <div className="flex items-baseline gap-2 mb-3">
        <span className="text-xl font-bold font-mono text-green-400">{bestBid}c</span>
        <span className="text-gray-600">/</span>
        <span className="text-xl font-bold font-mono text-red-400">{bestAsk}c</span>
        <span className="text-[11px] text-gray-600 ml-1">spread {spread}c</span>
      </div>

      {/* Depth bars */}
      <div className="flex gap-2 items-end h-6">
        <div className="flex-1">
          <div className="flex justify-between text-[10px] text-gray-500 mb-0.5">
            <span>YES bids</span>
            <span className="font-mono text-green-400">{yesDepth}</span>
          </div>
          <div className="w-full bg-white/[0.04] rounded-full h-1.5">
            <div
              className="bg-green-500/60 h-full rounded-full transition-all"
              style={{ width: `${(yesDepth / maxDepth) * 100}%` }}
            />
          </div>
        </div>
        <div className="flex-1">
          <div className="flex justify-between text-[10px] text-gray-500 mb-0.5">
            <span>NO bids</span>
            <span className="font-mono text-red-400">{noDepth}</span>
          </div>
          <div className="w-full bg-white/[0.04] rounded-full h-1.5">
            <div
              className="bg-red-500/60 h-full rounded-full transition-all"
              style={{ width: `${(noDepth / maxDepth) * 100}%` }}
            />
          </div>
        </div>
      </div>

      {/* Connection status dots */}
      <div className="flex items-center gap-3 mt-3 text-[10px] text-gray-600">
        <span className="flex items-center gap-1">
          <span className={`w-1.5 h-1.5 rounded-full ${a.binance_connected ? 'bg-green-500' : 'bg-red-500'}`} />
          Binance
        </span>
        <span className="flex items-center gap-1">
          <span className={`w-1.5 h-1.5 rounded-full ${a.coinbase_connected ? 'bg-green-500' : 'bg-red-500'}`} />
          Coinbase
        </span>
        <span className="flex items-center gap-1">
          <span className={`w-1.5 h-1.5 rounded-full ${a.kalshi_connected ? 'bg-green-500' : 'bg-red-500'}`} />
          Kalshi
        </span>
      </div>
    </div>
  )
}
