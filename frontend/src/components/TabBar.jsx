const ASSETS = [
  { id: 'btc', label: 'BTC', color: 'orange' },
  { id: 'eth', label: 'ETH', color: 'blue' },
  { id: 'sol', label: 'SOL', color: 'purple' },
]

const MODES = [
  { id: 'paper', label: 'Paper', color: 'amber' },
  { id: 'live', label: 'Live', color: 'green' },
]

export default function TabBar({
  activeAsset,
  activeMode,
  onAssetChange,
  onModeChange,
  allStatus,
}) {
  // Get running state for current asset's bots
  const assetStatus = allStatus?.[activeAsset] || {}

  return (
    <div className="space-y-2 mb-4">
      {/* Asset Row */}
      <div className="flex gap-1 p-1 bg-white/[0.03] rounded-lg">
        {ASSETS.map(asset => {
          // Check if any bot for this asset is running
          const status = allStatus?.[asset.id] || {}
          const anyRunning = status.paper?.running || status.live?.running
          return (
            <AssetTab
              key={asset.id}
              label={asset.label}
              color={asset.color}
              active={activeAsset === asset.id}
              running={anyRunning}
              onClick={() => onAssetChange(asset.id)}
            />
          )
        })}
      </div>
      {/* Mode Row */}
      <div className="flex gap-1 p-1 bg-white/[0.03] rounded-lg">
        {MODES.map(mode => (
          <ModeTab
            key={mode.id}
            label={mode.label}
            color={mode.color}
            active={activeMode === mode.id}
            running={assetStatus[mode.id]?.running}
            onClick={() => onModeChange(mode.id)}
          />
        ))}
      </div>
    </div>
  )
}

function AssetTab({ label, active, running, color, onClick }) {
  const colorClasses = {
    orange: {
      active: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
      inactive: 'text-gray-500 hover:text-orange-400 hover:bg-orange-500/10',
      dot: 'bg-orange-400',
    },
    blue: {
      active: 'bg-blue-500/20 text-blue-300 border-blue-500/30',
      inactive: 'text-gray-500 hover:text-blue-400 hover:bg-blue-500/10',
      dot: 'bg-blue-400',
    },
    purple: {
      active: 'bg-purple-500/20 text-purple-300 border-purple-500/30',
      inactive: 'text-gray-500 hover:text-purple-400 hover:bg-purple-500/10',
      dot: 'bg-purple-400',
    },
  }

  const colors = colorClasses[color] || colorClasses.orange

  return (
    <button
      onClick={onClick}
      className={`
        flex-1 flex items-center justify-center gap-2 px-4 py-2 rounded-md text-xs font-semibold transition-all
        ${active ? `${colors.active} border` : `${colors.inactive} border border-transparent`}
      `}
    >
      <span
        className={`w-1.5 h-1.5 rounded-full ${running ? `${colors.dot} pulse-live` : 'bg-gray-600'}`}
      />
      {label}
    </button>
  )
}

function ModeTab({ label, active, running, color, onClick }) {
  const colorClasses = {
    amber: {
      active: 'bg-amber-500/20 text-amber-300 border-amber-500/30',
      inactive: 'text-gray-500 hover:text-amber-400 hover:bg-amber-500/10',
      dot: 'bg-amber-400',
    },
    green: {
      active: 'bg-green-500/20 text-green-300 border-green-500/30',
      inactive: 'text-gray-500 hover:text-green-400 hover:bg-green-500/10',
      dot: 'bg-green-400',
    },
  }

  const colors = colorClasses[color] || colorClasses.amber

  return (
    <button
      onClick={onClick}
      className={`
        flex-1 flex items-center justify-center gap-2 px-4 py-2 rounded-md text-xs font-semibold transition-all
        ${active ? `${colors.active} border` : `${colors.inactive} border border-transparent`}
      `}
    >
      <span
        className={`w-1.5 h-1.5 rounded-full ${running ? `${colors.dot} pulse-live` : 'bg-gray-600'}`}
      />
      {label}
    </button>
  )
}
