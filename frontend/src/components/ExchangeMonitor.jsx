export default function ExchangeMonitor({ status }) {
  const alpha = status.alpha || {}
  const exchangePrices = alpha.exchange_prices || {}
  const globalPrice = alpha.weighted_global_price || 0
  const leadLagSpread = alpha.lead_lag_spread || 0
  const connected = alpha.exchanges_connected || 0
  const total = alpha.exchanges_total || 6

  return (
    <div className="card px-4 py-4 mb-4">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold text-gray-200">Multi-Exchange Monitor</h2>
        <div className="flex items-center gap-3 text-xs">
          <span className="text-gray-400">
            {connected}/{total} Connected
          </span>
          {globalPrice > 0 && (
            <span className="text-blue-400 font-mono">
              ${globalPrice.toFixed(2)}
            </span>
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2 mb-3">
        {Object.entries(exchangePrices)
          .sort((a, b) => b[1].weight - a[1].weight)
          .map(([exchange, data]) => (
            <ExchangeCard key={exchange} exchange={exchange} data={data} />
          ))}
      </div>

      {leadLagSpread !== 0 && (
        <div className="text-xs text-gray-500 border-t border-gray-800 pt-2 mt-2">
          Lead/Lag Spread:
          <span className={`ml-1 font-mono ${Math.abs(leadLagSpread) > 50 ? 'text-yellow-400' : 'text-gray-400'}`}>
            ${leadLagSpread.toFixed(2)}
          </span>
          <span className="ml-2 text-gray-600">
            (Fast exchanges vs Settlement influences)
          </span>
        </div>
      )}
    </div>
  )
}

function ExchangeCard({ exchange, data }) {
  const { price, connected, weight, tier, role, label } = data
  const weightPct = (weight * 100).toFixed(0)

  const roleColor = role === 'lead' ? 'text-purple-400' : 'text-blue-400'
  const roleBadge = role === 'lead' ? 'LEAD' : 'SETTLE'

  return (
    <div className={`rounded-lg border ${connected ? 'border-gray-700 bg-gray-800/30' : 'border-gray-800 bg-gray-900/50 opacity-60'} px-3 py-2`}>
      <div className="flex items-start justify-between mb-1">
        <div>
          <div className="flex items-center gap-1.5">
            <div className={`w-1.5 h-1.5 rounded-full ${connected ? 'bg-green-500' : 'bg-red-500'}`} />
            <span className="text-xs font-semibold text-gray-200">{label}</span>
          </div>
          <div className="flex items-center gap-1.5 mt-0.5">
            <span className={`text-[9px] uppercase font-bold ${roleColor}`}>{roleBadge}</span>
            <span className="text-[9px] text-gray-600">Tier {tier}</span>
          </div>
        </div>
        <span className="text-[10px] font-mono text-gray-500">{weightPct}%</span>
      </div>

      {connected && price > 0 ? (
        <div className="text-sm font-mono text-gray-300">
          ${price.toFixed(2)}
        </div>
      ) : (
        <div className="text-xs text-gray-600 italic">
          {connected ? 'Loading...' : 'Disconnected'}
        </div>
      )}
    </div>
  )
}
