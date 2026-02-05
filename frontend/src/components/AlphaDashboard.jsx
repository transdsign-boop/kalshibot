import { useState, useRef, useEffect } from 'react'
import { postConfig } from '../api'

function CollapsibleSection({ title, badge, summary, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="pt-2 border-t border-white/[0.04] first:border-t-0 first:pt-0">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between pb-1 cursor-pointer hover:bg-white/[0.02] -mx-1 px-1 rounded transition"
      >
        <div className="flex items-center gap-2">
          <span className="text-gray-600 text-[9px] w-3">{open ? '▼' : '▶'}</span>
          <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">{title}</span>
        </div>
        <div className="flex items-center gap-2">
          {!open && summary}
          {badge}
        </div>
      </button>
      {open && <div className="mt-1">{children}</div>}
    </div>
  )
}

function SectionHeader({ title, badge }) {
  return (
    <div className="flex items-center justify-between pt-2 pb-1 border-t border-white/[0.04] first:border-t-0 first:pt-0">
      <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">{title}</span>
      {badge}
    </div>
  )
}

// Inline-editable threshold: click to edit, Enter/blur to save
function Editable({ configKey, display, type = 'int', scale = 1 }) {
  const [editing, setEditing] = useState(false)
  const [val, setVal] = useState('')
  const [flash, setFlash] = useState(null) // 'ok' | 'err'
  const ref = useRef(null)

  useEffect(() => {
    if (editing && ref.current) ref.current.focus()
  }, [editing])

  useEffect(() => {
    if (!flash) return
    const t = setTimeout(() => setFlash(null), 800)
    return () => clearTimeout(t)
  }, [flash])

  const save = async () => {
    setEditing(false)
    let parsed = type === 'float' ? parseFloat(val) : parseInt(val, 10)
    if (isNaN(parsed)) return
    if (scale !== 1) parsed = parsed / scale
    try {
      await postConfig({ [configKey]: parsed })
      setFlash('ok')
      window.dispatchEvent(new Event('config-updated'))
    } catch {
      setFlash('err')
    }
  }

  if (editing) {
    return (
      <input
        ref={ref}
        type="number"
        value={val}
        onChange={e => setVal(e.target.value)}
        onBlur={save}
        onKeyDown={e => { if (e.key === 'Enter') save(); if (e.key === 'Escape') setEditing(false) }}
        className="w-14 bg-gray-800 border border-blue-500/50 rounded px-1 text-[10px] font-mono text-blue-300 outline-none"
        step={type === 'float' ? '0.01' : '1'}
      />
    )
  }

  const flashClass = flash === 'ok' ? 'text-green-400' : flash === 'err' ? 'text-red-400' : 'text-gray-500'
  return (
    <span
      className={`text-[10px] font-mono cursor-pointer hover:text-blue-400 transition-colors ${flashClass} flex-shrink-0`}
      title={`Click to edit ${configKey}`}
      onClick={() => { setVal(''); setEditing(true) }}
    >
      {display}
    </span>
  )
}

// Toggle for boolean config (e.g., EDGE_EXIT_ENABLED)
function Toggle({ configKey, enabled }) {
  const [flash, setFlash] = useState(null)

  useEffect(() => {
    if (!flash) return
    const t = setTimeout(() => setFlash(null), 800)
    return () => clearTimeout(t)
  }, [flash])

  const toggle = async () => {
    try {
      await postConfig({ [configKey]: !enabled })
      setFlash('ok')
    } catch {
      setFlash('err')
    }
  }

  const flashClass = flash === 'ok' ? 'ring-1 ring-green-500' : flash === 'err' ? 'ring-1 ring-red-500' : ''

  return (
    <span
      className={`text-[9px] font-bold px-1 py-0.5 rounded cursor-pointer transition-colors ${flashClass} ${enabled ? 'bg-green-500/15 text-green-400 hover:bg-green-500/25' : 'bg-gray-700 text-gray-500 hover:bg-gray-600'}`}
      title={`Click to ${enabled ? 'disable' : 'enable'} ${configKey}`}
      onClick={toggle}
    >
      {enabled ? 'ON' : 'OFF'}
    </span>
  )
}

const dotColors = {
  green: 'bg-green-500',
  red: 'bg-red-500',
  amber: 'bg-amber-500',
  purple: 'bg-purple-500',
  gray: 'bg-gray-600',
}

function Row({ dot, label, value, threshold, badge }) {
  return (
    <div className="flex items-center gap-2 py-0.5">
      <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${dotColors[dot] || 'bg-gray-600'}`} />
      <span className="text-[10px] text-gray-500 w-20 flex-shrink-0">{label}</span>
      <span className="text-[11px] font-mono text-gray-300 flex-1 truncate">{value}</span>
      {threshold}
      {badge}
    </div>
  )
}

// Compact meter for collapsed summaries
function MiniMeter({ value, max = 100, color = 'blue', label }) {
  const pct = Math.min(100, Math.max(0, (value / max) * 100))
  const colors = {
    green: 'bg-green-500',
    red: 'bg-red-500',
    amber: 'bg-amber-500',
    blue: 'bg-blue-500',
    purple: 'bg-purple-500',
    gray: 'bg-gray-500',
  }
  return (
    <div className="flex items-center gap-1.5">
      {label && <span className="text-[9px] text-gray-500">{label}</span>}
      <div className="w-12 h-1.5 bg-white/[0.06] rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${colors[color] || colors.blue}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-[9px] font-mono text-gray-400">{Math.round(value)}%</span>
    </div>
  )
}

export default function AlphaDashboard({ status }) {
  const db = status.dashboard
  const alpha = status.alpha || {}
  const ob = status.orderbook || {}

  if (!db) {
    return (
      <div className="card px-4 py-3 mb-4">
        <p className="text-[10px] text-gray-600 text-center">Dashboard loading...</p>
      </div>
    )
  }

  const fv = db.fair_value
  const guards = db.guards || {}
  const exits = db.exits || {}
  const momentum = alpha.delta_momentum || 0
  const absMom = Math.abs(momentum)
  const vol = alpha.volatility || {}
  const vel = alpha.price_velocity || {}
  const secsLeft = status.seconds_to_close || 0

  // --- Alpha Signals ---
  const signal = status.alpha_signal || 'NEUTRAL'
  const signalDiff = status.alpha_signal_diff || 0
  const override = status.alpha_override

  const momDot = absMom >= db.extreme_delta_threshold ? 'red'
    : absMom >= db.delta_threshold ? 'green'
    : absMom >= db.delta_threshold * 0.6 ? 'amber'
    : 'gray'
  const momBadge = absMom >= db.extreme_delta_threshold
    ? <span className="text-[9px] font-bold px-1 py-0.5 rounded bg-red-500/15 text-red-400">EXTREME</span>
    : null

  const anchorActive = secsLeft > 0 && secsLeft < db.anchor_seconds_threshold && guards.same_side?.holding !== 'NONE'
  const anchorDot = anchorActive ? 'amber' : 'gray'

  // --- Strategy ---
  const midpoint = ob.best_bid != null && ob.best_ask != null ? Math.round((ob.best_bid + ob.best_ask) / 2) : null
  const fairCents = fv ? fv.fair_yes_cents : null
  const yesEdge = db.yes_edge || 0
  const noEdge = db.no_edge || 0
  const minEdge = db.min_edge_cents || 5
  const hasEdge = yesEdge >= minEdge || noEdge >= minEdge

  const regimeColors = { high: 'bg-purple-500/15 text-purple-400', medium: 'bg-blue-500/15 text-blue-400', low: 'bg-gray-700 text-gray-500' }
  const regimeBadge = vol.regime
    ? <span className={`text-[9px] font-bold px-1 py-0.5 rounded ${regimeColors[vol.regime] || regimeColors.low}`}>{(vol.regime || '').toUpperCase()}</span>
    : null

  const dirArrow = (vel.direction_1m || 0) > 0 ? '\u2191' : (vel.direction_1m || 0) < 0 ? '\u2193' : '\u2194'

  // --- Confidence ---
  const confidence = status.confidence || 0
  const confPct = Math.round(confidence * 100)
  const minConf = db.min_confidence || 0.6
  const minConfPct = Math.round(minConf * 100)
  const confAbove = confidence >= minConf
  const decision = status.decision || 'HOLD'
  const decisionColors = {
    BUY_YES: 'bg-green-500/15 text-green-400',
    BUY_NO: 'bg-red-500/15 text-red-400',
    HOLD: 'bg-white/[0.06] text-gray-500',
  }

  // Rolling average confidence
  const rollingAvgConf = db.rolling_avg_confidence || 0
  const rollingAvgPct = Math.round(rollingAvgConf * 100)
  // Rolling average of max confidence values (per-market maxes)
  const rollingAvgMax = db.rolling_avg_max_confidence || 0
  const rollingAvgMaxPct = Math.round(rollingAvgMax * 100)

  const tf = db.time_factor || 0
  const tfPct = Math.round(tf * 100)
  const tfColor = tfPct > 50 ? 'bg-green-500' : tfPct > 20 ? 'bg-amber-500' : 'bg-red-500'

  // --- Guards ---
  const guardEntries = [
    { key: 'time', label: 'Time', g: guards.time, fmt: g => `${g.value}s`,
      th: g => <Editable configKey="MIN_SECONDS_TO_CLOSE" display={`>${g.threshold}s`} /> },
    { key: 'spread', label: 'Spread', g: guards.spread, fmt: g => `${g.value}c`,
      th: g => <Editable configKey="MAX_SPREAD_CENTS" display={`<${g.threshold}c`} /> },
    { key: 'daily_loss', label: 'Daily Loss', g: guards.daily_loss, fmt: g => `$${g.value}`,
      th: () => <span className="text-[10px] font-mono text-gray-600 flex-shrink-0">auto</span> },
    { key: 'hold_expiry', label: 'Hold Expiry', g: guards.hold_expiry, fmt: g => `${g.value}s`,
      th: g => <Editable configKey="HOLD_EXPIRY_SECS" display={`>${g.threshold}s`} /> },
    { key: 'price_min', label: 'Price Min', g: guards.price_min, fmt: g => `Y:${g.value_yes}c N:${g.value_no}c`,
      th: g => <Editable configKey="MIN_CONTRACT_PRICE" display={`>${g.threshold}c`} /> },
    { key: 'price_max', label: 'Price Max', g: guards.price_max, fmt: g => `Y:${g.value_yes}c N:${g.value_no}c`,
      th: g => <Editable configKey="MAX_CONTRACT_PRICE" display={`<${g.threshold}c`} /> },
    { key: 'exposure', label: 'Exposure', g: guards.exposure, fmt: g => `$${g.value}`,
      th: g => <span className="text-[10px] font-mono text-gray-600 flex-shrink-0">{`<$${g.threshold}`}</span> },
    { key: 'position_size', label: 'Position', g: guards.position_size, fmt: g => `${g.value}/${g.threshold}`,
      th: () => null },
    { key: 'same_side', label: 'Same-Side', g: guards.same_side, fmt: g => g.holding,
      th: () => null },
    { key: 'tp_reentry', label: 'TP Re-entry', g: guards.tp_reentry,
      fmt: () => guards.tp_reentry?.blocked ? 'BLOCKED' : 'OK',
      th: () => null },
    { key: 'edge_reentry', label: 'Edge Re-entry', g: guards.edge_reentry,
      fmt: g => g.blocked ? `${Math.round(g.cooldown_left)}s cooldown` : 'OK',
      th: g => <span className="text-[10px] font-mono text-gray-600 flex-shrink-0">+{g.premium}c premium</span> },
  ]
  const blockedCount = guardEntries.filter(e => e.g?.blocked).length
  const passingCount = guardEntries.length - blockedCount

  const guardBadge = (
    <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${blockedCount > 0 ? 'bg-red-500/15 text-red-400' : 'bg-green-500/15 text-green-400'}`}>
      {blockedCount > 0 ? `${blockedCount} blocking` : `${passingCount} pass`}
    </span>
  )

  // --- Exit rules ---
  const hasPosition = guards.same_side?.holding !== 'NONE'
  const exitEntries = [
    { key: 'stop_loss', label: 'Stop-Loss', e: exits.stop_loss, fmt: e => `${e.value}c loss`,
      th: e => <span className="text-[10px] text-gray-400 font-mono">{e.threshold}c</span> },
    { key: 'hit_and_run', label: 'Hit & Run', e: exits.hit_and_run, fmt: e => `${e.value}% gain`,
      th: e => <Editable configKey="HIT_RUN_PCT" display={e.enabled ? `${e.threshold}%` : 'OFF'} type="float" /> },
    { key: 'profit_take', label: 'Profit Take', e: exits.profit_take, fmt: e => `${e.value}% gain`,
      th: e => <Editable configKey="PROFIT_TAKE_PCT" display={`${e.threshold}%`} /> },
    { key: 'free_roll', label: 'Free Roll', e: exits.free_roll, fmt: e => `${e.value}c (${e.qty}x)`,
      th: e => <Editable configKey="FREE_ROLL_PRICE" display={`${e.threshold}c`} /> },
    { key: 'edge_exit', label: 'Edge Exit', e: exits.edge_exit,
      fmt: e => `${e.remaining_edge}c edge / ${e.threshold}c thr`,
      th: e => <Editable configKey="EDGE_EXIT_THRESHOLD_CENTS" display={`${db.edge_exit_threshold || 2}c`} />,
      badge: e => (
        <span className="flex items-center gap-1">
          {e.count > 0 && <span className="text-[9px] font-bold px-1 py-0.5 rounded bg-amber-500/15 text-amber-400">{e.count}x</span>}
          <Toggle configKey="EDGE_EXIT_ENABLED" enabled={e.enabled} />
        </span>
      ),
    },
  ]

  const overrideBadge = override
    ? <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${override === 'BUY_YES' ? 'bg-green-500/15 text-green-400' : 'bg-red-500/15 text-red-400'}`}>{override}</span>
    : <span className="text-[9px] font-bold px-1.5 py-0.5 rounded bg-white/[0.06] text-gray-500">NONE</span>

  const posBadge = hasPosition
    ? <span className="text-[9px] font-bold px-1 py-0.5 rounded bg-blue-500/15 text-blue-400">{guards.same_side?.holding}</span>
    : <span className="text-[9px] text-gray-600">no position</span>

  // Summary meter for Alpha Signals
  const alphaStrength = signal !== 'NEUTRAL' ? 80 : absMom >= db.delta_threshold ? 60 : absMom >= db.delta_threshold * 0.6 ? 40 : 20
  const alphaColor = signal !== 'NEUTRAL' ? 'green' : absMom >= db.extreme_delta_threshold ? 'red' : absMom >= db.delta_threshold ? 'amber' : 'gray'
  const alphaSummary = (
    <MiniMeter value={alphaStrength} color={alphaColor} label="Signal" />
  )

  // Summary for Strategy (confidence meter always visible)
  const confColor = confAbove ? 'green' : confPct > minConfPct * 0.7 ? 'amber' : 'red'
  const strategySummary = (
    <MiniMeter value={confPct} color={confColor} label="Conf" />
  )

  // Summary for Guards
  const guardsSummary = (
    <span className="text-[9px] font-mono text-gray-400">{passingCount}/{guardEntries.length}</span>
  )

  return (
    <div className="card px-4 py-3 mb-4">
      {/* Always-visible confidence meter at top */}
      <div className="flex items-center gap-2 pb-2 mb-1 border-b border-white/[0.04]">
        <span className="text-[10px] text-gray-500 shrink-0">Confidence</span>
        <div className="flex-1 h-2 bg-white/[0.04] rounded-full overflow-hidden relative">
          {/* Threshold marker (gray) */}
          <div className="absolute top-0 bottom-0 w-px bg-gray-500/50 z-10" style={{ left: `${minConfPct}%` }} title={`Threshold: ${minConfPct}%`} />
          {/* Rolling average marker (blue dot) */}
          {rollingAvgPct > 0 && (
            <div
              className="absolute top-1/2 -translate-y-1/2 w-2 h-2 rounded-full bg-blue-500 border border-blue-300 z-20"
              style={{ left: `calc(${rollingAvgPct}% - 4px)` }}
              title={`Rolling avg: ${rollingAvgPct}%`}
            />
          )}
          {/* Rolling avg of max confidence marker (purple triangle) */}
          {rollingAvgMaxPct > 0 && rollingAvgMaxPct !== rollingAvgPct && (
            <div
              className="absolute -top-0.5 w-0 h-0 border-l-[4px] border-r-[4px] border-b-[6px] border-l-transparent border-r-transparent border-b-purple-500 z-20"
              style={{ left: `calc(${rollingAvgMaxPct}% - 4px)` }}
              title={`Avg max: ${rollingAvgMaxPct}%`}
            />
          )}
          {/* Current confidence bar */}
          <div
            className={`h-full rounded-full transition-all duration-500 ${confAbove ? 'bg-green-500' : confPct > minConfPct * 0.7 ? 'bg-amber-500' : 'bg-red-500'}`}
            style={{ width: `${confPct}%` }}
          />
        </div>
        <span className={`text-sm font-mono font-bold shrink-0 ${confAbove ? 'text-green-400' : 'text-gray-500'}`}>{confPct}%</span>
        {rollingAvgPct > 0 && <span className="text-[9px] text-blue-400 shrink-0" title="Rolling average">avg:{rollingAvgPct}%</span>}
        <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded shrink-0 ${decisionColors[decision] || decisionColors.HOLD}`}>{decision.replace('_', ' ')}</span>
      </div>

      {/* Section 1: Alpha Signals */}
      <CollapsibleSection title="Alpha Signals" badge={overrideBadge} summary={alphaSummary}>
        <Row
          dot={momDot}
          label="Momentum"
          value={`${momentum >= 0 ? '+' : ''}${momentum.toFixed(1)}`}
          threshold={<Editable configKey="DELTA_THRESHOLD" display={`\u00b1$${db.delta_threshold}`} />}
          badge={momBadge}
        />
        <Row
          dot={anchorDot}
          label="Anchor"
          value={anchorActive ? `${Math.round(secsLeft)}s / ${guards.same_side?.holding}` : `${Math.round(secsLeft)}s`}
          threshold={<Editable configKey="ANCHOR_SECONDS_THRESHOLD" display={`<${db.anchor_seconds_threshold}s`} />}
        />
      </CollapsibleSection>

      {/* Section 2: Strategy */}
      <CollapsibleSection
        title="Strategy"
        badge={<span className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${decisionColors[decision] || decisionColors.HOLD}`}>{decision.replace('_', ' ')}</span>}
        summary={strategySummary}
      >
        {/* Detailed confidence with progress bar */}
        <div className="flex items-center gap-2 py-0.5">
          <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${confAbove ? 'bg-green-500' : 'bg-red-500'}`} />
          <span className="text-[10px] text-gray-500 w-20 flex-shrink-0">Confidence</span>
          <div className="flex-1 h-1.5 bg-white/[0.04] rounded-full overflow-hidden relative">
            <div className="absolute top-0 bottom-0 w-px bg-gray-500/50 z-10" style={{ left: `${minConfPct}%` }} />
            <div
              className={`h-full rounded-full transition-all duration-700 ${confAbove ? 'bg-green-500' : confPct > minConfPct * 0.7 ? 'bg-amber-500' : 'bg-red-500'}`}
              style={{ width: `${confPct}%` }}
            />
          </div>
          <span className={`text-[11px] font-mono font-semibold flex-shrink-0 ${confAbove ? 'text-green-400' : 'text-gray-500'}`}>{confPct}%</span>
          <Editable configKey="MIN_AGENT_CONFIDENCE" display={`≥${minConfPct}%`} type="float" scale={100} />
        </div>
        <Row
          dot={fairCents != null && midpoint != null && Math.abs(fairCents - midpoint) >= minEdge ? 'green' : 'gray'}
          label="Fair Value"
          value={fairCents != null ? `${fairCents}c YES (${Math.round((fv.fair_yes_prob || 0) * 100)}%)` : '--'}
          threshold={midpoint != null ? <span className="text-[10px] font-mono text-gray-600 flex-shrink-0">mid {midpoint}c</span> : null}
        />
        <Row
          dot={hasEdge ? 'green' : 'gray'}
          label="Edge"
          value={`Y:${yesEdge >= 0 ? '+' : ''}${yesEdge}c  N:${noEdge >= 0 ? '+' : ''}${noEdge}c`}
          threshold={<Editable configKey="MIN_EDGE_CENTS" display={`\u2265${minEdge}c`} />}
        />
        <Row
          dot={vol.regime === 'high' ? 'purple' : vol.regime === 'low' ? 'gray' : 'amber'}
          label="Volatility"
          value={`$${(vol.vol_dollar_per_min || 0).toFixed(1)}/min`}
          threshold={null}
          badge={regimeBadge}
        />
        <Row
          dot={(vel.direction_1m || 0) !== 0 ? 'green' : 'gray'}
          label="Velocity"
          value={`${dirArrow} $${(vel.price_change_1m || 0) >= 0 ? '+' : ''}${(vel.price_change_1m || 0).toFixed(0)}/1m`}
        />
        {/* Time factor with progress bar */}
        <div className="flex items-center gap-2 py-0.5">
          <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${tfPct > 50 ? 'bg-green-500' : tfPct > 20 ? 'bg-amber-500' : 'bg-red-500'}`} />
          <span className="text-[10px] text-gray-500 w-20 flex-shrink-0">Time Factor</span>
          <div className="flex-1 h-1 bg-white/[0.04] rounded-full overflow-hidden">
            <div className={`h-full rounded-full transition-all duration-700 ${tfColor}`} style={{ width: `${tfPct}%` }} />
          </div>
          <span className="text-[10px] font-mono text-gray-600 flex-shrink-0">{tfPct}%</span>
        </div>
      </CollapsibleSection>

      {/* Section 3: Guards */}
      <CollapsibleSection title="Guards" badge={guardBadge} summary={guardsSummary}>
        {guardEntries.map(({ key, label, g, fmt, th }) => g && (
          <Row
            key={key}
            dot={g.blocked ? 'red' : 'green'}
            label={label}
            value={fmt(g)}
            threshold={th(g)}
          />
        ))}
      </CollapsibleSection>

      {/* Section 4: Exit Rules */}
      <CollapsibleSection title="Exit Rules" badge={posBadge}>
        {exitEntries.map(({ key, label, e, fmt, th, badge }) => e && (
          <Row
            key={key}
            dot={e.triggered ? 'red' : 'gray'}
            label={label}
            value={hasPosition ? fmt(e) : '--'}
            threshold={th(e)}
            badge={badge ? badge(e) : null}
          />
        ))}
      </CollapsibleSection>
    </div>
  )
}
