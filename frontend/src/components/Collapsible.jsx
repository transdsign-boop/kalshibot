import { useState } from 'react'

export default function Collapsible({ title, badge, children }) {
  const [open, setOpen] = useState(false)

  return (
    <div className="card overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-4 py-3 hover:bg-white/[0.02] transition"
      >
        <span className="text-xs font-medium text-gray-400">{title}</span>
        <div className="flex items-center gap-2">
          {badge && <span className="text-[10px] font-mono text-gray-600">{badge}</span>}
          <svg
            className={`w-3.5 h-3.5 text-gray-600 transition-transform ${open ? 'rotate-180' : ''}`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
          </svg>
        </div>
      </button>
      <div className={`collapse-body ${open ? 'open' : ''}`}>
        <div className="px-4 pb-4">
          {children}
        </div>
      </div>
    </div>
  )
}
