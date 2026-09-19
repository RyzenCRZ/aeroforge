import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

import { StatusBar } from './StatusBar'

function healthOk(version: string): Response {
  return new Response(JSON.stringify({ status: 'ok', name: 'aeroforge', version }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('StatusBar', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('初始显示连接中', () => {
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise(() => {})))

    render(<StatusBar />)

    expect(screen.getByRole('status')).toHaveTextContent('正在连接后端')
  })

  it('后端可达时显示就绪与版本号', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(healthOk('9.9.9')))

    render(<StatusBar />)

    expect(await screen.findByText('aeroforge v9.9.9')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('就绪')
  })

  it('后端不可达时显示未连接且不显示版本号', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('connection refused')))

    render(<StatusBar />)

    expect(await screen.findByText('后端未连接')).toBeInTheDocument()
    expect(screen.queryByText(/^aeroforge v/)).not.toBeInTheDocument()
  })

  it('响应不符合契约时按不可达处理', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ unexpected: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    render(<StatusBar />)

    expect(await screen.findByText('后端未连接')).toBeInTheDocument()
  })
})
