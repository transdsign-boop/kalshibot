import { useState } from 'react'
import { postControl, postPaperReset } from '../api'

export default function Header({ status, onAction, asset = 'btc', bot = 'paper' }) {
  const { running, paper_mode } = status
  const [loading, setLoading] = useState(false)

  async function handleControl(action) {
    setLoading(true)
    try {
      await postControl(action, asset, bot)
      setTimeout(onAction, 300)
    } finally {
      setTimeout(() => setLoading(false), 500)
    }
  }

  async function handlePaperReset() {
    if (!confirm(`Reset ${asset.toUpperCase()} paper trading? This will stop the bot, clear all positions, and reset your balance to starting amount.`)) return
    setLoading(true)
    try {
      await postPaperReset(asset)
      setTimeout(onAction, 300)
    } finally {
      setTimeout(() => setLoading(false), 500)
    }
  }

  const isLive = bot === 'live'

  return (
    <header className="flex items-center justify-between mb-6">
      <div className="flex items-center gap-3">
        <div
          className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${running ? 'bg-green-500 pulse-live' : 'bg-gray-600'}`}
        />
        <div>
          <h1 className="text-lg font-semibold tracking-tight leading-tight">Up/Down 15</h1>
          <p className="text-[11px] text-gray-500">
            {isLive ? (
              <span className="text-green-400 font-medium">LIVE</span>
            ) : (
              <span className="text-amber-400 font-medium">PAPER</span>
            )}
            {' '}&middot; {running ? 'Running' : 'Stopped'}
          </p>
        </div>
      </div>
      <div className="flex items-center gap-2">
        {paper_mode && (
          <button
            onClick={handlePaperReset}
            disabled={loading}
            className="px-2.5 py-1.5 rounded-lg text-[11px] font-medium text-amber-400/70 hover:text-amber-300 bg-amber-500/10 hover:bg-amber-500/20 transition disabled:opacity-50"
          >
            Reset
          </button>
        )}
        {running ? (
          <button
            onClick={() => handleControl('stop')}
            disabled={loading}
            className="px-3 py-1.5 rounded-lg bg-red-500/20 text-red-400 text-xs font-semibold hover:bg-red-500/30 transition disabled:opacity-50"
          >
            {loading ? 'Stopping...' : 'Stop'}
          </button>
        ) : (
          <button
            onClick={() => handleControl('start')}
            disabled={loading}
            className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition disabled:opacity-50 ${
              isLive
                ? 'bg-green-500/20 text-green-400 hover:bg-green-500/30'
                : 'bg-amber-500/20 text-amber-400 hover:bg-amber-500/30'
            }`}
          >
            {loading ? 'Starting...' : 'Start'}
          </button>
        )}
      </div>
    </header>
  )
}
