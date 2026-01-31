const TZ = 'America/Los_Angeles'

/** "3:45pm" — compact time for trade rows */
export function toPacific(isoStr) {
  if (!isoStr) return ''
  const d = new Date(isoStr)
  return d
    .toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: TZ, hour12: true })
    .toLowerCase()
    .replace(' ', '')
}

/** "3:45:30pm" — with seconds for log panel */
export function toPacificSec(isoStr) {
  if (!isoStr) return ''
  const d = new Date(isoStr)
  return d
    .toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', second: '2-digit', timeZone: TZ, hour12: true })
    .toLowerCase()
    .replace(' ', '')
}
