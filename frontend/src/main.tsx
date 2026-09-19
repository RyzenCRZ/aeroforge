import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from './App'
import './styles/global.css'

const container = document.getElementById('root')
if (container === null) {
  throw new Error('未找到 #root 挂载点，检查 index.html')
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
