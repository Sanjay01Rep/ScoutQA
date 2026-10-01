import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { readTokenFromUrl } from './api/client'
import './index.css'
import App from './App.tsx'

readTokenFromUrl() // must run before any /api/* call does

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
