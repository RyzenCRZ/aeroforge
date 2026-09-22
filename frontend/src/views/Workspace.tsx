import { useEffect } from 'react'

import { ChatPanel } from '../components/ChatPanel'
import { DiagnosticsPanel } from '../components/DiagnosticsPanel'
import { MeridianEditor } from '../components/MeridianEditor'
import { OptimizePanel } from '../components/OptimizePanel'
import { Profile2D } from '../components/Profile2D'
import { RecoveryPanel } from '../components/RecoveryPanel'
import { SegmentPanel } from '../components/SegmentPanel'
import { ThresholdPanel } from '../components/ThresholdPanel'
import { VehiclePanel } from '../components/VehiclePanel'
import { VehicleSummaryPanel } from '../components/VehicleSummaryPanel'
import { Viewport } from '../r3f/Viewport'
import { useModelStore } from '../store/model'
import { useParamsStore } from '../store/params'
import { LIGHT_INTENSITY_MAX, LIGHT_INTENSITY_MIN, useViewStore, type ClipAxis } from '../store/view'
import './Workspace.css'

/** 剖切法向的中文名（§11.4：法向沿箭体轴向或径向）。 */
const CLIP_AXIS_TEXT: Record<ClipAxis, string> = {
  axial: '轴向',
  radial: '径向',
}

/** 视口控制条：亮度滑杆 + 着色开关 + 剖切。**只影响视觉，不触发任何后端请求**（§11.3 / §11.6）。 */
function ViewControls() {
  const lightIntensity = useViewStore((state) => state.lightIntensity)
  const showGrid = useViewStore((state) => state.showGrid)
  const autoRotate = useViewStore((state) => state.autoRotate)
  const wireframe = useViewStore((state) => state.wireframe)
  const clip = useViewStore((state) => state.clip)
  const setLightIntensity = useViewStore((state) => state.setLightIntensity)
  const toggleGrid = useViewStore((state) => state.toggleGrid)
  const toggleAutoRotate = useViewStore((state) => state.toggleAutoRotate)
  const toggleWireframe = useViewStore((state) => state.toggleWireframe)
  const setClipEnabled = useViewStore((state) => state.setClipEnabled)
  const setClipAxis = useViewStore((state) => state.setClipAxis)
  const setClipPosition = useViewStore((state) => state.setClipPosition)

  return (
    <div className="workspace__controls">
      <label className="workspace__control">
        <span className="label">亮度</span>
        <input
          type="range"
          min={LIGHT_INTENSITY_MIN}
          max={LIGHT_INTENSITY_MAX}
          step={0.05}
          value={lightIntensity}
          onChange={(event) => setLightIntensity(Number(event.target.value))}
        />
        <span className="num">{lightIntensity.toFixed(2)}</span>
      </label>
      <label className="workspace__control">
        <input type="checkbox" checked={showGrid} onChange={toggleGrid} />
        <span className="label">参考网格</span>
      </label>
      <label className="workspace__control">
        <input type="checkbox" checked={autoRotate} onChange={toggleAutoRotate} />
        <span className="label">自动旋转</span>
      </label>
      <label className="workspace__control">
        <input type="checkbox" checked={wireframe} onChange={toggleWireframe} />
        <span className="label">线框</span>
      </label>
      <label className="workspace__control">
        <input
          type="checkbox"
          checked={clip.enabled}
          onChange={(event) => setClipEnabled(event.target.checked)}
        />
        <span className="label">剖切</span>
      </label>
      <label className="workspace__control">
        <span className="label">法向</span>
        <select
          value={clip.axis}
          disabled={!clip.enabled}
          onChange={(event) => setClipAxis(event.target.value as ClipAxis)}
        >
          {(Object.keys(CLIP_AXIS_TEXT) as ClipAxis[]).map((axis) => (
            <option key={axis} value={axis}>
              {CLIP_AXIS_TEXT[axis]}
            </option>
          ))}
        </select>
      </label>
      <label className="workspace__control">
        <span className="label">位置</span>
        <input
          type="range"
          min={0}
          max={1}
          step={0.01}
          value={clip.position}
          disabled={!clip.enabled}
          onChange={(event) => setClipPosition(Number(event.target.value))}
        />
        <span className="num">{clip.position.toFixed(2)}</span>
      </label>
    </div>
  )
}

/**
 * 三栏工作区（规格 §11.5 / M2 左栏接入参数系统）。
 *
 * 左 = 组件树 + 段参数 / 参数面板 / 诊断清单 / 阈值设置；中 = 3D 视口；
 * 右 = 母线编辑器 + 2D 视图组（外观图 + 工程剖面图，§11.10 / OI-37）。
 *
 * 母线草稿的任何变化都会触发一次**防抖后**的后端校验——几何数值一律由后端给出，
 * 前端只消费与格式化（ADR-011）。左栏新增的三个面板同理：诊断结论与阈值生效值
 * **全部**来自后端（§6.5 / §18.4），前端只负责渲染与定位。右栏的 2D 视图组
 * 自行按 vehicle 防抖拉取分区数据，失败降级为提示行（不白屏）。
 */
export function Workspace() {
  const profile = useParamsStore((state) => state.profile)
  const runValidation = useModelStore((state) => state.runValidation)

  useEffect(() => {
    runValidation(profile)
  }, [profile, runValidation])

  return (
    <div className="workspace">
      <aside className="workspace__pane surface">
        <SegmentPanel />
        <VehiclePanel />
        <DiagnosticsPanel />
        <ThresholdPanel />
      </aside>
      <section className="workspace__pane workspace__pane--center">
        <Viewport />
        <ViewControls />
      </section>
      <aside className="workspace__pane surface">
        <MeridianEditor />
        <Profile2D />
        {/* 整箭数据 + 轨道运力（§11.5 布局：右栏 2D 视图组下方，FR-10 / FR-11） */}
        <VehicleSummaryPanel />
        {/* 优化与权衡（§14 / M6：多目标优化 / 权衡研究 / 批量扫描 / 逆向设计，四标签同一面板） */}
        <OptimizePanel />
        {/* 回收质量代价（§8.9 / M5：三项分解可解释；未启用时常驻「无代价」提示） */}
        <RecoveryPanel />
      </aside>
      {/* AI 助手（§10.2 / OI-10 / M7：右下角悬浮入口，未配置自动隐藏，三降级收敛为事件） */}
      <ChatPanel />
    </div>
  )
}
