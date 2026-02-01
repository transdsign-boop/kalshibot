import { toPacificSec } from '../utils/time'

const LEVEL_COLORS = {
  ERROR: 'text-red-400',
  TRADE: 'text-green-400',
  GUARD: 'text-yellow-400',
  AGENT: 'text-blue-400',
  ALPHA: 'text-purple-400',
  SIM: 'text-amber-400',
}

export default function LogPanel({ logs }) {
  if (!logs) return null

  return (
    <div className="log-box bg-black/20 rounded-lg p-3 text-gray-400">
      {logs.map((l, i) => {
        const isError = l.level === 'ERROR'
        const color = LEVEL_COLORS[l.level] || 'text-gray-500'
        return (
          <div key={i} className={`${color}${isError ? ' bg-red-500/10 rounded px-1 -mx-1' : ''}`}>
            <span className="text-gray-600">{toPacificSec(l.ts)}</span>{isError ? ' \u{1F6A9}' : ''} [{l.level}] {l.message}
          </div>
        )
      })}
    </div>
  )
}
