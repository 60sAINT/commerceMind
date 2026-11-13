import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import './styles/index.css'
import './styles/tokens.less'
import './styles/base.less'
import './styles/animations.less'

import App from './App'

const container = document.getElementById('root')

if (!container) {
  throw new Error('缺少 #root 挂载节点')
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
