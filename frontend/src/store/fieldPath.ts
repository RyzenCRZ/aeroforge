/**
 * 字段路径（`field_path`）的解析与读写（规格 §6.3 末注 / §1.7.3 OI-32）。
 *
 * 为什么要有这个模块：◤路径◢ 在本工程是**跨层共享的词汇**——后端用它标出缺陷位置
 * （§6.3 校验与 §6.5 诊断两条通路同构），前端用同一个字符串标控件（`data-field-path`）
 * 并按它定位。若前端把"改某个字段"写成一套自己的结构（如逐字段的 setter），
 * 两边就会各说各话：诊断指得出、界面却定位不到。
 *
 * ⚠ 路径一律用**数组下标**（`stages[0]`）而非级号，与 pydantic 的 `loc` 同构；
 * 本模块不做任何数值计算，只做结构定位（ADR-011）。
 */

/** 路径中的一段：对象键名或数组下标。 */
export type FieldPathToken = string | number

const TOKEN = /([^.[\]]+)|\[(\d+)\]/g

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

/**
 * 拆解路径：`stages[0].engine.isp_vacuum_s` → `['stages', 0, 'engine', 'isp_vacuum_s']`。
 *
 * 无法识别的字符被忽略（与后端 `field_path` 的取值口径一致：那里只会出现
 * `标识符` 与 `[数字]`）。空串返回空数组，由调用方决定是报错还是忽略。
 */
export function parseFieldPath(path: string): FieldPathToken[] {
  const tokens: FieldPathToken[] = []
  for (const match of path.matchAll(TOKEN)) {
    const [, name, index] = match
    tokens.push(index === undefined ? String(name) : Number(index))
  }
  return tokens
}

/** 沿路径读取；路径不可达时返回 `undefined`（读取用于渲染，缺值不是错误）。 */
export function readFieldPath(root: unknown, path: string): unknown {
  let cursor: unknown = root
  for (const token of parseFieldPath(path)) {
    if (Array.isArray(cursor)) {
      if (typeof token !== 'number') return undefined
      cursor = cursor[token]
    } else if (isRecord(cursor)) {
      cursor = cursor[token]
    } else {
      return undefined
    }
  }
  return cursor
}

/** 结构化深拷贝。参数对象是纯 JSON（后端 `model_dump(mode="json")`），故无需 `structuredClone`。 */
function cloneJson<T>(root: T): T {
  return JSON.parse(JSON.stringify(root)) as T
}

function step(cursor: unknown, token: FieldPathToken): unknown {
  if (Array.isArray(cursor) && typeof token === 'number') return cursor[token]
  if (isRecord(cursor) && typeof token === 'string') return cursor[token]
  return undefined
}

/**
 * 写回路径指向的字段，返回**新的**对象（原对象不变，zustand 靠引用变化触发重渲染）。
 *
 * 路径不可达时**抛出**而不是静默返回原对象：路径来自本模块的字段清单与后端的
 * `field_path`，一旦对不上就是实现缺陷，静默会让"改了但没生效"变成一个查不出的现象
 * （同族教训：PyInstaller hook 静默不触发）。
 */
export function writeFieldPath<T>(root: T, path: string, value: unknown): T {
  const tokens = parseFieldPath(path)
  if (tokens.length === 0) {
    throw new Error(`字段路径为空或无法解析：${path}`)
  }

  const clone = cloneJson(root)
  let cursor: unknown = clone
  for (const token of tokens.slice(0, -1)) {
    const next = step(cursor, token)
    if (next === undefined) throw new Error(`字段路径不可达：${path}`)
    cursor = next
  }

  const last = tokens[tokens.length - 1]
  if (Array.isArray(cursor) && typeof last === 'number') {
    if (last < 0 || last >= cursor.length) throw new Error(`数组下标越界：${path}`)
    cursor[last] = value
    return clone
  }
  if (isRecord(cursor) && typeof last === 'string') {
    cursor[last] = value
    return clone
  }
  throw new Error(`字段路径的父节点不是可写容器：${path}`)
}
