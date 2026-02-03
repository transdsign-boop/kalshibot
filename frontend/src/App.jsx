import { useState } from 'react'
import { usePolling } from './hooks/usePolling'
import { fetchStatusAll, fetchLogs, fetchTrades } from './api'
import TabBar from './components/TabBar'
import Header from './components/Header'
import ContractCard from './components/ContractCard'
import BotStatus from './components/BotStatus'
import TradeLog from './components/TradeLog'
import ExchangeMonitor from './components/ExchangeMonitor'
import AlphaDashboard from './components/AlphaDashboard'
import Collapsible from './components/Collapsible'
import ChatPanel from './components/ChatPanel'
import ConfigPanel from './components/ConfigPanel'
import LogPanel from './components/LogPanel'
import AnalyticsPanel from './components/AnalyticsPanel'

export default function App() {
  const [activeAsset, setActiveAsset] = useState('btc')
  const [activeMode, setActiveMode] = useState('paper')

  // Fetch all bots' status in one call
  const { data: allStatus, refresh } = usePolling(fetchStatusAll, 2000)
  const { data: logs } = usePolling(() => fetchLogs(activeAsset), 3000)

  // Get current asset/mode status
  const status = allStatus?.[activeAsset]?.[activeMode]
  const tradeMode = activeMode === 'live' ? 'live' : 'paper'

  const { data: tradeData } = usePolling(() => fetchTrades(tradeMode, activeAsset), 2000)

  // Loading state
  if (!allStatus) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center">
          <div className="w-6 h-6 rounded-full border-2 border-blue-500 border-t-transparent animate-spin mx-auto mb-3" />
          <p className="text-gray-500 text-sm">Connecting...</p>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen max-w-2xl mx-auto px-4 py-6 md:py-10">
      <TabBar
        activeAsset={activeAsset}
        activeMode={activeMode}
        onAssetChange={setActiveAsset}
        onModeChange={setActiveMode}
        allStatus={allStatus}
      />
      {status ? (
        <>
          <Header status={status} onAction={refresh} asset={activeAsset} bot={activeMode} />
          <ContractCard status={status} />
          <BotStatus status={status} tradeData={tradeData} />
          <ExchangeMonitor status={status} />
          <AlphaDashboard status={status} asset={activeAsset} bot={activeMode} />
          <TradeLog tradeData={tradeData} mode={tradeMode} />

          <div className="mt-6 space-y-2">
            <Collapsible title="Trade Analytics" badge={tradeData?.summary?.total_trades ? `${tradeData.summary.total_trades} trades` : null}>
              <AnalyticsPanel mode={tradeMode} asset={activeAsset} />
            </Collapsible>
            <Collapsible title="Chat with Agent">
              <ChatPanel asset={activeAsset} bot={activeMode} />
            </Collapsible>
            <Collapsible title="Configuration">
              <ConfigPanel asset={activeAsset} bot={activeMode} />
            </Collapsible>
            <Collapsible title="Event Log" badge={`cycle ${status.cycle_count}`}>
              <LogPanel logs={logs} />
            </Collapsible>
          </div>

          <footer className="text-center text-xs text-gray-700 mt-8 pb-4">
            <span className="mr-2 text-gray-500">{activeAsset.toUpperCase()}</span>
            {status.trading_enabled ? (
              activeMode === 'live' ? (
                <span className="text-red-400 font-medium">LIVE TRADING ENABLED</span>
              ) : (
                <span className="text-amber-400 font-medium">PAPER TRADING</span>
              )
            ) : (
              'Trading disabled'
            )}
          </footer>
        </>
      ) : (
        <div className="text-center text-gray-500 text-sm py-8">
          Loading {activeAsset.toUpperCase()} {activeMode} bot status...
        </div>
      )}
    </div>
  )
}
