/**
 * 3D 场景的取色工具。
 *
 * 组件样式里禁止字面色值（ADR-013 / NFR-05），场景材质同理：颜色只能来自
 * `src/styles/tokens.css` 的语义 token，故运行时读计算样式。
 */

/** 读取语义 token 的色值；token 缺失时返回空串，由调用方交给材质默认色。 */
export function readColorToken(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}
