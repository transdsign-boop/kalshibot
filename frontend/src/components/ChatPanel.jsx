import { useState, useRef, useEffect } from 'react'
import { postChat } from '../api'

export default function ChatPanel() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const boxRef = useRef(null)

  useEffect(() => {
    if (boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight
    }
  }, [messages])

  async function handleSubmit(e) {
    e.preventDefault()
    const msg = input.trim()
    if (!msg) return
    setInput('')
    setMessages(prev => [...prev, { role: 'user', text: msg }])
    setSending(true)

    try {
      const data = await postChat(msg)
      setMessages(prev => [...prev, { role: 'agent', text: data.reply }])
    } catch (err) {
      setMessages(prev => [...prev, { role: 'error', text: err.message }])
    }
    setSending(false)
  }

  return (
    <div>
      {messages.length > 0 && (
        <div
          ref={boxRef}
          className="chat-messages bg-black/20 rounded-lg p-3 mb-3 text-sm text-gray-300 space-y-2"
        >
          {messages.map((m, i) => (
            <div
              key={i}
              className={m.role === 'error' ? 'text-red-400' : m.role === 'user' ? 'text-blue-400' : 'text-gray-300'}
            >
              <span className={`font-medium ${
                m.role === 'user' ? 'text-gray-500' : m.role === 'agent' ? 'text-purple-400' : 'text-red-400'
              }`}>
                {m.role === 'user' ? 'You:' : m.role === 'agent' ? 'Agent:' : 'Error:'}
              </span>{' '}
              {m.text}
            </div>
          ))}
        </div>
      )}
      <form className="flex gap-2" onSubmit={handleSubmit}>
        <input
          type="text"
          value={input}
          onChange={e => setInput(e.target.value)}
          placeholder="Ask about strategy, decisions..."
          className="flex-1 bg-black/20 border border-white/[0.06] rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-blue-500/50"
        />
        <button
          type="submit"
          disabled={sending}
          className="px-3 py-2 rounded-lg bg-blue-500/20 text-blue-400 text-xs font-semibold hover:bg-blue-500/30 transition disabled:opacity-50"
        >
          {sending ? '...' : 'Send'}
        </button>
      </form>
    </div>
  )
}
