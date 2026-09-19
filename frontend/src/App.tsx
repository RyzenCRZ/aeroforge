import { StatusBar } from './statusbar/StatusBar'
import { Workspace } from './views/Workspace'
import './App.css'

/**
 * 应用外壳。
 *
 * M1 起渲染三栏工作区（规格 §16.3：左 = 组件树 + 段参数 / 中 = 3D 视口 / 右 = 母线编辑器），
 * 底部沿用状态栏作为全局唯一反馈位（§11.11）。
 */
export function App() {
  return (
    <div className="app">
      <header className="app__header">
        <h1 className="app__title">AeroForge</h1>
        <p className="label">M1 几何闭环 · 参数化航天器设计与评估平台</p>
      </header>
      <main className="app__main">
        <Workspace />
      </main>
      <StatusBar />
    </div>
  )
}
