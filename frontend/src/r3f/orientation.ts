/**
 * 双通道朝向契约（ADR-012 / R-25 / §13.6）。
 *
 * **结论：两个通道都用恒等变换，谁都不许加旋转。**
 *
 * 理由（M2 首项目视核对实测，非推演）：OCCT 导出器按 glTF 规范把几何从 **Z-up 转成 Y-up**，
 * 方式是给**根节点**写一个 `Rx(-90°)` 的四元数旋转；`GLTFLoader` 加载时会把它施加到顶点上，
 * 故**渲染器看到的 GLB 已经是 Y-up**（柱 r=1 L=3 的世界包围盒为 x/z ∈ ±R、y ∈ [0, L]），
 * 与 `LatheGeometry`（其轴本身就是世界 Y，见 `SchematicMesh.tsx`）天然同向。
 *
 * ⚠ **曾经的错误**：误把 GLB **访问器**的 min/max 当世界坐标——那是 Z-up 的**局部**坐标
 * （x/y ∈ ±R、z ∈ [0, L]），据此又转了 -90°，与导出器的节点旋转叠加成 **-180°**，
 * 结果权威模型在视口里"躺倒"、与示意通道分叉（包围盒尺寸却完全一致，故尺寸类门禁全绿）。
 *
 * 因此本常量**必须保持恒等**：它不是"多余的占位"，而是把这条结论钉在代码里的锚点——
 * `orientation.test.ts`（真实 three 对象）与后端 `test_dual_channel_orientation.py`
 * （真实 GLB 顶点）各自断言它，两处都改了才算改对。
 */
export const AUTHORITATIVE_ROTATION: [number, number, number] = [0, 0, 0]
