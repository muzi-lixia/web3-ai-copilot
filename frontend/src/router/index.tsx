import { createBrowserRouter, Navigate } from 'react-router-dom'

import AppLayout from '../components/AppLayout'
import Copilot from '../views/Copilot'
import Dashboard from '../views/Dashboard'
import Knowledge from '../views/Knowledge'
import Portfolio from '../views/Portfolio'
import Staking from '../views/Staking'

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppLayout />,
    children: [
      { index: true, element: <Navigate to="/dashboard" replace /> },
      { path: 'dashboard', element: <Dashboard /> },
      { path: 'portfolio', element: <Portfolio /> },
      { path: 'staking', element: <Staking /> },
      { path: 'copilot', element: <Copilot /> },
      { path: 'knowledge', element: <Knowledge /> },
    ],
  },
])
