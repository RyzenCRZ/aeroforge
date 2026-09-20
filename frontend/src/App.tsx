import { useEffect } from 'react'

import { CalcPanel } from './components/CalcPanel'
import { StatusBar } from './statusbar/StatusBar'
import { usePerfStore } from './store/perf'
import { useVehicleStore } from './store/vehicle'
import { Workspace } from './views/Workspace'
import './App.css'

/**
 * 应用外壳。
 *
 * M1 起渲染三栏工作区（规格 §16.3：左 = 组件树 + 段参数 / 中 = 3D 视口 / 右 = 母线编辑器），
 * 底部沿用状态栏作为全局唯一反馈位（§11.11）。M4 第四片起：
 *
 * - 顶部工具栏承载「计算」按钮与「自动更新」开关（§11.5 顶部导航承载高频全局操作）；
 * - 计算评估面板（§11.5 ③）在 main 与状态栏**之间**弹出——工作区收缩而非覆盖，
 *   结构上不遮挡中栏 3D 视口；
 * - 自动更新开启时，参数变更经防抖（200 ms，§11.4）自动触发两阶段评估并弹出面板；
 *   关闭 = 冻结（OI-04：参数变更不再触发自动重算，重开时立即按当前输入重算一次）。
 */
export function App() {
  const panelOpen = usePerfStore((state) => state.panelOpen)
  const autoUpdate = usePerfStore((state) => state.autoUpdate)
  const openPanel = usePerfStore((state) => state.openPanel)
  const runEvaluate = usePerfStore((state) => state.runEvaluate)
  const setAutoUpdate = usePerfStore((state) => state.setAutoUpdate)

  // 自动更新的触发接入（OI-04 / §11.5 ③）：订阅参数唯一可写来源（store/vehicle），
  // vehicle 引用变化 → 自动更新开启时防抖自动评估。冻结语义本体不在本片重实现。
  useEffect(() => {
    return useVehicleStore.subscribe((state, prev) => {
      if (state.vehicle === prev.vehicle) return
      const perf = usePerfStore.getState()
      if (!perf.autoUpdate || state.vehicle === null) return
      perf.openPanel()
      perf.requestAutoEvaluate()
    })
  }, [])

  return (
    <div className="app">
      <header className="app__header">
        <h1 className="app__title">AeroForge</h1>
        <p className="label">M1 几何闭环 · 参数化航天器设计与评估平台</p>
        <div className="app__actions">
          <label className="app__action">
            <input
              type="checkbox"
              checked={autoUpdate}
              onChange={(event) => {
                setAutoUpdate(event.target.checked)
              }}
            />
            <span className="label">自动更新</span>
          </label>
          <button
            type="button"
            className="app__calc-button"
            onClick={() => {
              openPanel()
              runEvaluate()
            }}
          >
            计算
          </button>
        </div>
      </header>
      <main className="app__main">
        <Workspace />
      </main>
      {panelOpen ? <CalcPanel /> : null}
      <StatusBar />
    </div>
  )
}
