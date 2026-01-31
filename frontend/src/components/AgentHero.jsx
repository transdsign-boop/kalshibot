export default function AgentHero({ status }) {
  const { running, last_action, decision, confidence, reasoning, alpha_override } = status
  const conf = Math.round((confidence || 0) * 100)
  const action = last_action || 'Idle'

  // Determine accent color based on state
  let accent = 'border-gray-700'
  let dotColor = 'bg-gray-600'
  if (!running) {
    accent = 'border-gray-800'
    dotColor = 'bg-gray-700'
  } else if (action.includes('Placed') || action.includes('Filled')) {
    accent = 'border-green-500/50'
    dotColor = 'bg-green-500'
  } else if (action.includes('guard') || action.includes('Guard') || action.includes('too cheap')) {
    accent = 'border-yellow-500/50'
    dotColor = 'bg-yellow-500'
  } else if (action.includes('Error') || action.includes('rejected')) {
    accent = 'border-red-500/50'
    dotColor = 'bg-red-500'
  } else {
    accent = 'border-blue-500/30'
    dotColor = 'bg-blue-500'
  }

  // Decision badge
  let badgeBg = 'bg-white/[0.06] text-gray-400'
  if (decision === 'BUY_YES') badgeBg = 'bg-green-500/15 text-green-400'
  else if (decision === 'BUY_NO') badgeBg = 'bg-red-500/15 text-red-400'

  return (
    <div className={`card p-5 mb-4 border-l-2 ${accent}`}>
      {/* Status line */}
      <div className="flex items-center gap-2 mb-3">
        {running ? (
          <div className="w-4 h-4 rounded-full border-2 border-blue-500 border-t-transparent animate-spin flex-shrink-0" />
        ) : (
          <div className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${dotColor}`} />
        )}
        <span className="text-sm font-medium text-gray-200 truncate flex-1">
          {running ? action : 'Bot stopped'}
        </span>
        {decision && decision !== '—' && (
          <span className={`text-[11px] font-bold px-2 py-0.5 rounded-full ${badgeBg}`}>
            {decision}
          </span>
        )}
      </div>

      {/* Reasoning */}
      {reasoning ? (
        <p className="text-sm text-gray-400 leading-relaxed mb-3">
          {reasoning}
        </p>
      ) : (
        <p className="text-sm text-gray-600 mb-3">Waiting for analysis...</p>
      )}

      {/* Confidence bar + alpha override */}
      <div className="flex items-center gap-3">
        <div className="flex-1 h-1.5 bg-white/[0.06] rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all duration-700 ${
              conf >= 75 ? 'bg-green-500' : conf >= 50 ? 'bg-yellow-500' : 'bg-red-500'
            }`}
            style={{ width: `${conf}%` }}
          />
        </div>
        <span className="text-[11px] font-mono text-gray-500 w-8 text-right">{conf}%</span>
      </div>

      {alpha_override && (
        <p className="text-[11px] text-purple-400 mt-2">
          Alpha override: {alpha_override}
        </p>
      )}
    </div>
  )
}
