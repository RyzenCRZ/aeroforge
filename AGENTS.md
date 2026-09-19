# AGENTS.md — AeroForge 航天飞行器参数化建模与性能评估平台

> Agent 每次会话启动自动加载。本文件由 `AeroForge-Spec.md` 附录 D 物化而来，随规格同步维护。

## 项目结构
- /backend/    FastAPI + build123d 后端（api / params / geometry / propulsion / sizing / orbital / data）
- /frontend/   React 19 + Vite + three 前端（views / components / adapters / r3f / store）
- /desktop/    桌面壳与打包：pywebview 原生窗口 + PyInstaller（ADR-015，M0.5 起）
- /mcp/        自建领域 MCP Server
- /skills/     Agent Skills（SKILL.md）
- /specs/      跨模块接口与构型规格（先改这里，再改实现）
- /data/       数据快照与 CEA 预计算表（不可变，禁止手改）+ `contours/<id>.json` 母线存档（M1 起由 `POST /api/geometry/contour` 写入）
- /artifacts/  内容寻址产物：`<key>/{model.step, model_lod1.glb, model_lod2.glb, metrics.json, provenance.json}`，`key = sha256(canonical_json + kernel_version + spec_version)`（§16.3）。派生数据，不入库
- /tools/      preflight.py（环境预检 CLI，仅转调 `aeroforge.selfcheck`——R-30 要求唯一实现）；cea_tablegen.py / gcat_etl.py / model_import.py / benchmark.py 随 M3–M5 落地，**当前不存在**
- /docs/       未采纳想法与留白索引：`backlog.md`（R-16 / R-20 的落点，OI-14）· `adr/`（占位）
- /.github/    持续集成：`workflows/ci.yml`（规格 §13.9）
- AeroForge-Spec.md  唯一真理源，架构级变更必须先改本文档

> **交付形态**：Windows 桌面应用，**双击 exe 启动**，不是网页、不是在线服务（ADR-015）。
> 开发期可用浏览器访问 Vite dev server 加速迭代，但那是**开发手段而非交付形态**，任何实现不得依赖它存在。

## 代码规范
- Python：PEP 8（ruff format 强制）；类型注解全覆盖，`mypy --strict` 必须通过
- TypeScript：strict 全开，禁止 `any`；后端类型由 OpenAPI 生成，禁止手写重复类型
- 命名：模块 snake_case；类 PascalCase；常量 UPPER_SNAKE
- 文档字符串：公共 API 必须说明参数单位
- 测试：pytest（后端）+ hypothesis（属性）+ vitest（前端），每个模块必须有单元测试

## 关键命令
- 环境：`uv sync`
- 环境预检（字体完整性 / OCCT 内核 / CEA 点火）：`uv run python tools/preflight.py`
- 冻结产物预检（同一实现，验证冻结后依赖真的可用）：`dist-desktop\AeroForge\AeroForge.exe --preflight`
- 后端：`uv run uvicorn aeroforge.api.main:app --reload`
- 测试：`uv run pytest -q` ；静态检查：`uv run ruff check . && uv run mypy backend`
- 前端：`cd frontend && npm run dev` ；构建：`npm run build`
- 前端检查：`npm run test && npm run typecheck`
- 前端非功能（双通道一致性 / 资源释放 / 零物理公式 / token 合规，规格 §13.6）：**当前并入 `npm run test`**（M1 已覆盖示意通道包围盒一致性与防抖调度器）；§13.6 余项（500 次重建的资源释放扫描、stylelint token 规则）尚未落地，落地后再拆出独立的 `test:nfr` 脚本（该脚本**当前不存在**）
- OpenAPI 契约刷新（后端接口变更后必跑，在**仓库根**执行）：`uv run python -c "import json,pathlib;from aeroforge.api.main import app;pathlib.Path('specs/openapi.json').write_text(json.dumps(app.openapi(),ensure_ascii=False,indent=2)+chr(10),encoding='utf-8')"` 然后 `cd frontend && npm run gen:api`
- 几何闭环验证（解析 vs 内核体积、双通道包络、母线往返）：`uv run pytest backend/tests/unit/test_geometry_closed_loop.py`
- 工程自检门禁（文档命令可执行性 / 附录 D 同步 / 测试隔离 / 夹具合法性 / 验收记录留白 / 性能预算，规格 §13.8）：随 `uv run pytest -q` 一并运行
- 桌面端本地运行（不经浏览器）：`uv run python desktop/launcher.py`
- 桌面端打包（onedir，产物在 `dist-desktop/`）：`uv run pyinstaller desktop/packaging/aeroforge.spec --distpath dist-desktop --workpath .tools/pyinstaller-build --noconfirm`
- 桌面端冒烟（冻结后仍能起后端 + 渲染 3D）：见规格 §16.2 退出准则
- 持续集成（规格 §13.9）：`push` / `pull_request` 触发，跑后端 `ruff format --check` / `ruff check` / `mypy` / `pytest`、前端 `typecheck` / `test` / `build`，以及 OpenAPI 契约新鲜度两步比对；**端到端预览延迟、`--probe` 首帧、断网复跑三项不在 CI 内**（属人工 / 产物实测，见规格 §13.5.1 / §16.2）

## 禁止操作
- 不要修改 /data/snapshots/ 与 /data/tables/ 下的已发布数据（快照不可变）
- 不要直接操作 OCCT 的 TopoDS_Shape 内部结构（走 build123d Builder API）
- 不要在前端做任何几何运算（前端只消费 GLB）
- **不要在前端实现任何物理公式或物理常量**（ADR-011；前端只格式化接口返回的数值）
- **不要把示意网格用于计算、导出或作为几何依据**（ADR-012）
- **不要把 `artifacts/` 下的内容寻址产物入库或手工改写**（可由参数 + 内核版本 + 规格版本重建；`provenance.json` 是溯源链本身，改动即断链）
- **不要绕过 `cache/store.py` 的内容寻址缓存直连几何内核**（M1 已落地，§16 顺序铁律第 2 条要求缓存不得推迟，否则 M4–M6 迭代成本失控）
- **不要在组件样式里写字面色值**，必须引用语义 token（ADR-013）
- **不要在未经 AeroForge-Spec.md §1.7 裁决的情况下新增参数或系数**（裁决即规格，口径必须唯一）
- **不要把 CEA 的 `Isp` / `Isp_vacuum` 字段当秒用**（ADR-014：该字段量纲是有效排气速度 m/s，转秒必须除以 `g₀`）
- **不要在 CEA 产物列表中把凝相物种放在中部**（ADR-014：固定"纯气相 + 凝相仅置于末位"；任何求解结果必须先过健全性门禁，禁止只看 `last_error`）
- **不要用发行名 `cadquery-ocp` 判依赖缺失**（实际发行名为 `cadquery-ocp-novtk`）
- **不要把 `typescript` 升到 7.x**（不再暴露 Compiler API，`npm run gen:api` 会在模块加载期崩溃，见 §3.2 / R-31）
- **不要把 npm 缓存改回系统目录**（本机沙箱拒绝写入，`frontend/.npmrc` 的 `cache=../.tools/npm-cache` 是必需项，不是优化）
- **不要把交付形态做成"浏览器访问 localhost"**（交付物是**双击即用的 exe**，ADR-015）
- **不要在前端硬编码主机名或端口**（必须用同源相对路径 `/api/...`、`/ws/...`；打包后服务跑在运行时选定的动态端口上）
- **不要用 `onefile` 冻结含 OCCT 的后端**（每次启动解压数百 MB 且易被杀软拦截，用 `onedir`，见规格 §16.2）
- **不要在 `tools/preflight.py` 或任何别处复制一份预检实现**（唯一实现是 `backend/aeroforge/selfcheck.py`；R-30 要求预检随交付产物冻结，复制品不会进包）
- **不要只写 PyInstaller hook 而不建立导入路径**（hook 仅对**导入图内出现过**的包执行；没有 `import` 回指该包时 hook 静默不执行，产物会缺库且构建不报错）
- **不要把产物安装或解压到含非 ASCII 字符的路径**（NASA `cea` 的 C 扩展用窄字符 API 打开数据表，中文路径下必定加载失败，且报错会误导成"thermo.lib 找不到"；启动器已加前置判定返回码 5，见规格 §16.2 坑位 4。同理 M7 安装器默认路径**不得**选 `%LOCALAPPDATA%`——用户名可能含中文）
- **不要为了让测试变绿而放宽断言或跳过守卫**（规格 §13.8：夹具非法就修夹具，产品校验器不得因测试而放宽；`warn` / `skip` 必须显式留痕）
- 不要在计算路径内实时调用 CEA（只读预计算表）
- 不要用网格（GLB/STL）作为任何计算的输入
- 不要引入新依赖（须先走 AeroForge-Spec.md §3.4 审计流程）
- 不要在无来源的情况下输出数值结论
- **不要引入 i18n / 多语言框架**（规格 §1.3 非目标：界面文案只做简体中文，OI-09）
- **不要在组件里就地格式化单位**（规格 §6.4：显示单位固定一套，换算只在 API 边界发生，OI-07）
- **不要用经验系数编造方案诊断的 `impact`**（规格 §6.5：必须由 §8.7 敏感度实算，M2 不输出该字段、M4 起补，OI-11）
- **不要把未采纳的想法直接排期**（先进 `docs/backlog.md`；要"毕业"须回到 `AeroForge-Spec.md` 走 §1.7 裁决或立 ADR，R-16 / R-20 / OI-14）
- **不要把 3D 的剖切 / 爆炸 / 分级显隐写回参数或当作几何依据**（三者是**视图状态**：不触发后端请求、不改几何、不可导出；P1 / ADR-012 / OI-19）
- **不要把箱体比例当作用户可自由给定的输入**（`V_ox / V_fuel = (O/F) · (ρ_fuel / ρ_ox)` 由后端派生，规格 §5.9 / OI-18；用户显式给定箱长时须标注来源冲突，禁止静默覆盖）
- **不要在交互路径上跑蒙特卡洛**（纬度 / 轨道编辑只跑解析点值并置 `interval_pending`，MC 走独立后台作业；OI-25 / R-39——把 10 000 样本塞进交互路径只会得到一个既不即时也不准的数字）
- **不要把 2D 工程剖面图做成通用工程制图**（只做**整箭纵剖一张图** + 规格 §5.9 的固定尺寸清单；不做多视图 / 公差 / 形位公差 / 尺寸链 / 图框，R-38）
- **不要为内置模板另建一套数字**（模板与规格 §13.2 基准表**同源**；模板值须标注为公开资料对照，**不得**当作本平台的计算结论展示；R-40）

## 领域规则
- 所有几何参数必须有物理约束校验（壁厚 < 直径/2 等）
- 齐奥尔科夫斯基计算必须使用多级累加模式，禁止单级近似替代多级
- 数据库查询必须走标签索引，禁止全表扫描
- 质量估算必须双来源交叉校验（几何解析 + GCAT 回归），单来源结果禁止作为结论
- 多级求解必须外层 GLOW 迭代 + 内层正向计算，残差收敛判据 1e-6
- 性能结论必须附带不确定度区间（P5/P50/P95）
- 单位必须在变量名或类型中显式声明，禁止隐式单位假设
- 1 级选型看密度比冲，上面级选型看比冲
- **2D 工程剖面图与 3D 装配树必须共用 §5.9 的 9 段分区枚举**（无漏件、无错序；分区高度与尺寸标注数值**一律后端下发**）
- **轨道计算须同时输出 C3 与 ΔV**，且二者可由同一组 `μ` / `r_p` 互相反算（规格 §8.10；TMI 结果必须记录窗口 / 相位假设，否则不可复现）
- **发射场纬度是自转加成与转向损失的唯一输入**，不得另设常量；固定其余参数时**纬度 ↓ ⟹ 运力 ↑** 必须成立（FR-19）

## 协作约定
- 跨模块改动先改 /specs/，接口冻结后再并行实现
- 涉及几何 / 推进 / 总体三层中任意两层的改动，拆成独立 PR
- 几何或计算模块的 PR 必须附与黄金基准火箭的偏差数字（AeroForge-Spec.md §13.2）
- 新增任何系数或常量必须在 PR 中标注来源出处（文献 / 数据 / 标准），无来源不予合并
- 前端改动必须通过 §13.6 非功能测试（双通道一致性 / 资源释放 / 零物理公式 / token 合规）
- **交付链路必须持续可验证**：M0.5 探针打通后，任何改动若使"冻结 → 启动 → 原生窗口 → 3D 渲染"任一环失效，视为阻断级缺陷，优先于功能开发修复
