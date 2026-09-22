# 第三方依赖与许可声明（v0.7.1）

> 依据规格 §16 M7「文档（含第三方依赖与许可声明）」与 §3.4 依赖审计流程建立。
> 清单来源为**实测**：后端取 `.venv`（uv.lock 锁定全集）发行版元数据逐个读取（103 个发行版，
> 2026-09-22 实测）；前端取 `frontend/node_modules` 各包 `package.json` 的 `license` 字段实测。
> 少数包元数据缺许可字段的，逐个以**分类器 / 轮内 LICENSE 文件 / 上游仓库 API** 补核，并在表内注明依据。
> 版本升级后本清单必须同步刷新（§3.4 新依赖先审计后引入）。

## 一、冲突核查结论（红线 2：禁 GPL / AGPL / SSPL）

**运行时依赖中 GPL / AGPL / SSPL：0 项。** 需要说明的边界项如下：

| 项 | 许可 | 判定 |
|---|---|---|
| PyInstaller（仅构建期，dev 组） | GPL-2.0-or-later **WITH PyInstaller-Bootloader-Exception** | 该例外明确允许以**任意许可**分发其打包的产物，不传染本项目源码（规格 §3.4 例外一，已裁决）。升级版本时须复核例外条款仍在 |
| pyinstaller-hooks-contrib（仅构建期） | Apache-2.0 **OR** GPL-2.0（双许可） | 取 Apache-2.0 侧即可，无传染 |
| certifi / hypothesis / pathspec | MPL-2.0 | 文件级弱 copyleft：作为库链接使用不要求应用开源，合规 |
| numpy / packaging / python-dateutil / greenlet 等 | 宽松多许可（BSD/Apache/MIT/PSF 组合） | 均可取宽松侧，无传染 |
| Microsoft Edge WebView2 Runtime | 微软专有、**免费可再分发**的系统组件 | 唯一非开源依赖（规格 §3.4 例外二）；安装器采用「检测 + 引导安装」，不强制捆绑 |
| OCCT 几何内核（经 cadquery-ocp-novtk 发行，包元数据 Apache-2.0） | 内核本体 LGPL-2.1 WITH OCCT 例外 | 该例外允许应用程序闭源分发，不受 LGPL 传染条款约束 |

**数据许可**（非代码 copyleft，义务为署名）：
- **GCAT**（data/snapshots/gcat-2026Q3）：CC-BY（未标注具体版本号，义务按原文=署名）。许可核实结论见
  `data/snapshots/gcat-2026Q3/LICENSE-NOTICE.md`（先核许可后抓取门禁的落点）。**随数据使用持续生效的署名义务**：
  - 短引用：`data from GCAT (J. McDowell, planet4589.org/space/gcat)`
  - 完整引用：`McDowell, Jonathan C., 2020. General Catalog of Artificial Space Objects, Release 1.8.7, https://planet4589.org/space/gcat`
  - 本清单即文档侧署名落点；应用内「数据面板/关于」处须另行出现同一引用（§7.1）。
- **NASA cea**（热力学库 thermo.lib / trans.lib 及其原生层）：Apache-2.0（随包声明）。
- **CEA 预计算表**（data/cea/*.npz）：由 NASA cea 生成（Apache-2.0 工具链），为本项目自产数据。
- **材料库**：自建（来源逐条标注 MIL-HDBK-5J / 厂商 datasheet，§7.4）；SPACEMATDB 因许可不明**未采用**（R-10 结论）。

## 二、后端依赖（uv.lock 全量，.venv 实测 103 项；★ = pyproject 运行时直接依赖）

开发期工具（pytest / ruff / mypy / PyInstaller 及其传递依赖）随构建链安装，**不进入冻结产物**（spec excludes）。

| 包 | 版本 | 许可证 | 备注 |
|---|---|---|---|
| ★ build123d | 0.11.1 | Apache-2.0 | 几何内核封装 |
| ★ cea | 3.3.4 | Apache-2.0 | NASA CEA 原生绑定 |
| ★ fastapi | 0.141.1 | MIT | Web 框架 |
| ★ numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | 数值基础 |
| ★ pandas | 3.0.6 | BSD-3-Clause | GCAT ETL |
| ★ pyarrow | 25.0.1 | Apache-2.0 | 快照 parquet |
| ★ pydantic | 2.13.5 | MIT | 参数 Schema |
| ★ pydantic-settings | 2.15.0 | MIT | 配置来源 |
| ★ pywebview | 6.2.1 | BSD-3-Clause | 桌面原生窗口 |
| ★ scipy | 1.18.1 | BSD-3-Clause | 数值积分 / qmc |
| ★ sqlalchemy | 2.0.54 | MIT | GCAT 目录库 |
| ★ uvicorn | 0.53.0 | BSD-3-Clause | ASGI 服务器 |
| ★ websockets | 17.1 | BSD-3-Clause | 作业进度通道 |
| altgraph | 0.17.5 | MIT | PyInstaller 传递（构建期） |
| annotated-doc | 0.0.5 | MIT | fastapi 传递 |
| annotated-types | 0.8.0 | MIT | pydantic 传递 |
| anyio | 4.15.1 | MIT | starlette 传递 |
| anytree | 2.13.0 | Apache-2.0 | build123d 传递 |
| ast_serialize | 0.11.2 | MIT | build123d 传递 |
| asttokens | 3.0.2 | Apache-2.0 | IPython 传递 |
| bottle | 0.13.4 | MIT | pywebview 传递 |
| cadquery-ocp-novtk | 7.9.3.1.1 | Apache-2.0 | OCP 发行（OCCT 绑定，无 VTK） |
| cadquery-ocp-proxy | 7.9.3.1.1 | Apache-2.0 | OCP 伴生包 |
| certifi | 2026.7.22 | MPL-2.0 | requests 传递 |
| cffi | 2.1.1 | MIT-0 | pythonnet 传递 |
| charset-normalizer | 3.5.1 | MIT | requests 传递 |
| click | 8.5.0 | BSD-3-Clause | uvicorn 传递 |
| cloudpickle | 3.1.2 | BSD-3-Clause | build123d 传递 |
| clr_loader | 0.3.1 | MIT | pythonnet 传递；包元数据未声明，以上游仓库 pythonnet/clr-loader（GitHub API 2026-09-22 核实）为准 |
| colorama | 0.4.6 | BSD（分类器） | uvicorn 传递；包元数据未声明，按 PyPI 分类器补核 |
| executing | 2.2.1 | MIT | IPython 传递 |
| ezdxf | 1.4.4 | MIT（分类器） | build123d 传递 |
| fonttools | 4.65.0 | MIT | build123d 传递 |
| greenlet | 3.5.6 | MIT AND PSF-2.0 | sqlalchemy 传递 |
| h11 | 0.16.0 | MIT | httpcore 传递 |
| httptools | 0.8.0 | MIT | uvicorn[standard] 传递 |
| idna | 3.20 | BSD-3-Clause | anyio 传递 |
| ipython | 9.17.1 | BSD-3-Clause | build123d 传递 |
| ipython-pygments-lexers | 1.1.1 | BSD（分类器） | IPython 传递 |
| jedi | 0.20.0 | MIT | IPython 传递 |
| joblib | 1.6.0 | BSD-3-Clause | scikit-learn 传递 |
| lib3mf | 2.5.0 | BSD（分类器） | build123d 网格导出 |
| librt | 0.15.0 | MIT | build123d 传递 |
| matplotlib-inline | 0.2.2 | BSD-3-Clause | IPython 传递 |
| mpmath | 1.3.0 | BSD | sympy 传递 |
| narwhals | 2.26.0 | MIT | pandas 3 传递 |
| ocp_gordon | 0.2.2 | Apache-2.0 | OCP 相关发行 |
| ocpsvg | 0.6.0 | Apache-2.0 | 包元数据未声明，以**轮内 LICENSE 文件**核实 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | 双许可 |
| parso | 0.8.7 | MIT | jedi 传递 |
| prompt_toolkit | 3.0.53 | BSD（分类器） | IPython 传递 |
| proxy_tools | 0.1.0 | MIT | bottle 传递 |
| psutil | 7.2.2 | BSD-3-Clause | 运行时（进程池） |
| pure_eval | 0.2.4 | MIT | IPython 传递 |
| py-cpuinfo2 | 10.1.1 | MIT | psutil 传递 |
| pycparser | 3.0 | BSD-3-Clause | cffi 传递 |
| pydantic_core | 2.46.5 | MIT | pydantic 传递 |
| Pygments | 2.21.0 | BSD-2-Clause | IPython 传递 |
| pyparsing | 3.3.2 | MIT | svgpathtools 传递 |
| python-dateutil | 2.9.0.post0 | Apache-2.0 OR BSD-3-Clause | 双许可；pandas 传递 |
| python-dotenv | 1.2.3 | BSD-3-Clause | uvicorn[standard] 传递 |
| pythonnet | 3.1.0 | MIT | pywebview WinForms 后端 |
| PyYAML | 6.0.3 | MIT | uvicorn[standard] 传递 |
| requests | 2.34.2 | Apache-2.0 | 数据抓取 |
| scikit-learn | 1.9.1 | BSD-3-Clause | build123d 传递 |
| setuptools | 84.0.0 | MIT | 构建链传递 |
| six | 1.17.0 | MIT | python-dateutil 传递 |
| sortedcontainers | 2.4.0 | Apache-2.0 | hypothesis 传递（dev） |
| stack-data | 0.6.3 | MIT | IPython 传递 |
| starlette | 1.6.0 | BSD-3-Clause | fastapi 传递 |
| svgelements | 1.9.6 | MIT | build123d 传递 |
| svgpathtools | 1.8.0 | MIT | build123d 传递 |
| svgwrite | 1.4.3 | MIT | build123d 传递 |
| sympy | 1.14.0 | BSD | build123d 传递 |
| threadpoolctl | 3.7.0 | BSD-3-Clause | scikit-learn 传递 |
| traitlets | 5.16.1 | BSD-3-Clause | IPython 传递 |
| trianglesolver | 1.2 | MIT | build123d 传递 |
| typing-inspection | 0.4.4 | MIT | pydantic 传递 |
| typing_extensions | 4.16.0 | PSF-2.0 | 传递 |
| tzdata | 2026.4 | Apache-2.0 | pandas 传递 |
| urllib3 | 2.8.0 | MIT | requests 传递 |
| watchfiles | 1.2.0 | MIT | uvicorn[standard] 传递 |
| wcwidth | 0.8.4 | MIT | prompt_toolkit 传递 |
| webcolors | 24.8.0 | BSD-3-Clause | svgwrite 传递 |
| aeroforge | 0.1.0 | 本项目（平台源码私有） | 非第三方 |
| **以下为开发期工具（dev 组及传递，不进冻结产物）** | | | |
| coverage | 7.16.1 | Apache-2.0 | pytest-cov 传递 |
| httpx | 0.28.1 | BSD-3-Clause | dev 直接依赖 |
| httpcore | 1.0.9 | BSD-3-Clause | httpx 传递 |
| hypothesis | 6.168.0 | MPL-2.0 | dev 直接依赖 |
| iniconfig | 2.3.0 | MIT | pytest 传递 |
| mypy | 2.3.1 | MIT | dev 直接依赖 |
| mypy_extensions | 1.1.0 | MIT | mypy 传递 |
| pathspec | 1.1.1 | MPL-2.0 | mypy 传递 |
| pefile | 2024.8.26 | MIT | PyInstaller 传递 |
| pluggy | 1.6.0 | MIT | pytest 传递 |
| pyinstaller | 6.22.3 | GPL-2.0-or-later WITH PyInstaller-Bootloader-Exception | **仅构建期**；例外允许任意许可分发产物（§3.4 例外一） |
| pyinstaller-hooks-contrib | 2026.7 | Apache-2.0 OR GPL-2.0（分类器，双许可） | 取 Apache-2.0 侧 |
| pywin32-ctypes | 0.2.3 | BSD-3-Clause | PyInstaller 传递 |
| pytest | 9.1.1 | MIT | dev 直接依赖 |
| pytest-asyncio | 1.4.0 | Apache-2.0 | dev 直接依赖 |
| pytest-benchmark | 5.3.0 | BSD-2-Clause | dev 直接依赖 |
| pytest-cov | 7.1.0 | MIT | dev 直接依赖 |
| ruff | 0.16.8 | MIT | dev 直接依赖 |

## 三、前端依赖（frontend/node_modules 实测 license 字段）

运行时依赖进 `frontend/dist` 构建产物；dev 依赖仅参与构建 / 测试 / 类型生成，不随产物分发。

| 包 | 版本 | 许可证 | 类别 |
|---|---|---|---|
| react | 19.2.8 | MIT | 运行时 |
| react-dom | 19.2.8 | MIT | 运行时 |
| three | 0.186.0 | MIT | 运行时 |
| @react-three/fiber | 9.7.0 | MIT | 运行时 |
| @react-three/drei | 10.7.8 | MIT | 运行时 |
| zustand | 5.0.15 | MIT | 运行时 |
| typescript | 5.9.3 | Apache-2.0 | dev |
| vite | 8.3.0 | MIT | dev |
| vitest | 5.0.1 | MIT | dev |
| jsdom | 30.1.0 | MIT | dev |
| openapi-typescript | 7.13.0 | MIT | dev |
| @vitejs/plugin-react | 6.1.1 | MIT | dev |
| @testing-library/react | 16.3.3 | MIT | dev |
| @testing-library/jest-dom | 7.0.1 | MIT | dev |
| @testing-library/user-event | 14.6.7 | MIT | dev |
| @types/react / react-dom / three | ~19.2 / ~19.2 / ~0.186 | MIT（DefinitelyTyped） | dev |

## 四、系统组件与运行前提

| 组件 | 许可 | 分发口径 |
|---|---|---|
| Microsoft Edge WebView2 Runtime（Evergreen） | 微软专有、免费可再分发 | **不捆绑**：安装器检测（HKLM/HKCU EdgeUpdate Clients）后引导至官方页 `https://developer.microsoft.com/microsoft-edge/webview2/`（§3.4 例外二） |
| Microsoft Visual C++ 运行库 | 随依赖链再分发条款 | 由各依赖自带或系统预装，未单独捆绑 |
