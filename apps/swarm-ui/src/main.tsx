import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { CapacityScreen } from './Capacity'
import './styles.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <CapacityScreen />
  </StrictMode>,
)
