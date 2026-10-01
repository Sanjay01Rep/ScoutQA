import { useState } from 'react'
import './App.css'
import { CrawlPage } from './pages/CrawlPage'
import { Dashboard } from './pages/Dashboard'
import { ExportPage } from './pages/ExportPage'
import { GeneratePage } from './pages/GeneratePage'
import { LoginPage } from './pages/LoginPage'
import { ReviewPage } from './pages/ReviewPage'

const TABS = [
  { key: 'dashboard', label: 'Dashboard', render: () => <Dashboard /> },
  { key: 'login', label: 'Login', render: () => <LoginPage /> },
  { key: 'crawl', label: 'Crawl', render: () => <CrawlPage /> },
  { key: 'generate', label: 'Generate', render: () => <GeneratePage /> },
  { key: 'review', label: 'Review', render: () => <ReviewPage /> },
  { key: 'export', label: 'Export', render: () => <ExportPage /> },
] as const

type TabKey = (typeof TABS)[number]['key']

function App() {
  const [tab, setTab] = useState<TabKey>('dashboard')
  const active = TABS.find((t) => t.key === tab) ?? TABS[0]

  return (
    <div className="app-shell">
      <nav className="side-nav">
        <h1 className="brand">ScoutQA</h1>
        <ul>
          {TABS.map((t) => (
            <li key={t.key}>
              <button
                className={t.key === tab ? 'nav-item active' : 'nav-item'}
                onClick={() => setTab(t.key)}
              >
                {t.label}
              </button>
            </li>
          ))}
        </ul>
      </nav>
      <main className="content">{active.render()}</main>
    </div>
  )
}

export default App
