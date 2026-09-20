"""CEA 预计算表加载与查询（规格 §8.2 / ADR-004；量纲契约 ADR-014）。

表由 ``tools/cea_tablegen.py`` 离线生成，落盘 ``data/cea/<prop>.npz``（纯数据，可分发）
+ ``data/cea/manifest.json``（量纲声明 / 网格 / sha256 / 黄金锚点核对结果）。本模块：

- 只读：缺表、未收录、sha256 篡改、域外查询**一律显式报错**——禁止静默回退常数或
  外推（R-29 同族的"没报错 ≠ 正确"）；
- 对数坐标插值（Pc / ε 取 log10，O/F 与 Pamb 线性，§8.2），插值器构造一次、按
  (推进剂, 目录) 进程级缓存，查询 < 1 ms；
- 量纲：``isp_vacuum_m_s`` / ``isp_ambient_m_s`` 是**有效排气速度 m/s**（ADR-014
  契约①），转秒必须除以 :data:`aeroforge.perf.G0`。
"""

from __future__ import annotations

import hashlib
import json
import math
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, Field
from scipy.interpolate import RegularGridInterpolator

from aeroforge.errors import AeroForgeError
from aeroforge.paths import cea_tables_root

#: 本片（M4 第一片）落地的四组液体推进剂表键。
#: 生成侧权威注册表在 ``tools/cea_tablegen.py``，二者键集必须一致（tablegen 启动时自检）。
KNOWN_PROPELLANTS: tuple[str, ...] = ("lox_rp1", "lox_lh2", "lox_ch4", "n2o4_mmh")

#: (O/F, Pc, ε) 平面上随点存储的 6 个字段；``isp_ambient_m_s`` 另带 Pamb 第 4 维。
GRID_FIELDS: tuple[str, ...] = (
    "isp_vacuum_m_s",
    "c_star_m_s",
    "t_chamber_k",
    "gamma",
    "mw",
    "chamber_density_kg_m3",
)
#: 环境压比冲字段名（第 4 维 = Pamb；Pamb=0 处与 ``isp_vacuum_m_s`` 恒等）。
AMBIENT_FIELD = "isp_ambient_m_s"

#: manifest 结构版本（表格式演进时递增；旧版本拒载）。
MANIFEST_VERSION = 1


class CeaPoint(BaseModel):
    """一次表查询的结果。全部字段为 **CEA 理想值**，未经 §8.3 效率修正。"""

    of: float = Field(description="混合比 O/F（质量比）")
    pc_bar: float = Field(description="室压（bar）")
    eps: float = Field(description="喷管面积比 ε")
    pamb_bar: float = Field(description="环境压（bar）；0 = 真空")
    isp_vacuum_m_s: float = Field(
        description="真空比冲，量纲为有效排气速度 m/s（ADR-014 契约①；转秒 ÷ G0）"
    )
    c_star_m_s: float = Field(description="特征速度 c*（m/s）")
    t_chamber_k: float = Field(description="燃烧室温度 Tc（K）")
    gamma: float = Field(description="燃烧室 γ_s（比热比，平衡流）")
    mw: float = Field(description="燃烧室燃气摩尔质量（g/mol）")
    chamber_density_kg_m3: float = Field(description="燃烧室燃气密度（kg/m³）")
    isp_ambient_m_s: float = Field(
        description=(
            "环境压比冲（CEA 环境点 = 最优膨胀至 Pamb 的 Isp_opt，m/s）。"
            "Pamb ≥ Pc 的角点物理无解，为 NaN"
        )
    )


class CeaTable:
    """单份推进剂 CEA 表：manifest 校验 + 对数坐标插值器。

    构造一次（约几十 ms），之后每次查询 < 1 ms。不要自行实例化——经
    :func:`load_table`（带进程级缓存与完整性校验）获取。
    """

    def __init__(self, propellant: str, npz_path: Path, manifest_entry: dict[str, Any]) -> None:
        self.propellant = propellant
        self.npz_path = npz_path
        with np.load(npz_path) as npz:
            of = np.asarray(npz["of"], dtype=float)
            pc = np.asarray(npz["pc_bar"], dtype=float)
            eps = np.asarray(npz["eps"], dtype=float)
            pamb = np.asarray(npz["pamb_bar"], dtype=float)
            grid_values = np.stack([np.asarray(npz[f], dtype=float) for f in GRID_FIELDS], axis=-1)
            ambient_values = np.asarray(npz[AMBIENT_FIELD], dtype=float)

        expected: tuple[int, ...] = (of.size, pc.size, eps.size)
        manifest_grid = manifest_entry["grid"]
        assert isinstance(manifest_grid, dict)
        counts = (
            manifest_grid["of"]["count"],
            manifest_grid["pc_bar"]["count"],
            manifest_grid["eps"]["count"],
        )
        if expected != tuple(int(c) for c in counts) or ambient_values.shape != (
            *expected,
            pamb.size,
        ):
            msg = f"{npz_path} 的数组形状与 manifest 网格声明不一致"
            raise AeroForgeError(
                msg,
                suggestion="data/ 表不可手改（§15）；请用 tools/cea_tablegen.py 重新生成",
                code="CEA_TABLE_INTEGRITY",
                stage="perf",
            )

        # 门禁产物自证：生成器已逐点断言过 last_error / Tc；这里只做加载期再确认，
        # 防止表文件在生成后被改动引入 NaN（sha256 之外的第二道便宜防线）。
        grid_finite = bool(np.isfinite(grid_values).all())
        ambient_finite_mask = np.isfinite(ambient_values)
        degenerate = manifest_entry.get("gates", {}).get("degenerate_ambient_points", 0)
        if not grid_finite or int(degenerate) != int((~ambient_finite_mask).sum()):
            msg = f"{npz_path} 含未声明的 NaN（表被改动或生成器门禁被绕过）"
            raise AeroForgeError(
                msg,
                suggestion="data/ 表不可手改（§15）；请用 tools/cea_tablegen.py 重新生成",
                code="CEA_TABLE_INTEGRITY",
                stage="perf",
            )

        self._of = of
        self._pc = pc
        self._eps = eps
        self._pamb = pamb
        # 对数坐标插值（§8.2）：Pc / ε 取 log10；O/F 与 Pamb 线性。
        self._interp = RegularGridInterpolator(
            (of, np.log10(pc), np.log10(eps)), grid_values, method="linear", bounds_error=True
        )
        self._interp_ambient = RegularGridInterpolator(
            (of, np.log10(pc), np.log10(eps), pamb),
            ambient_values,
            method="linear",
            bounds_error=True,
        )

    @property
    def of_grid(self) -> np.ndarray:
        """O/F 网格向量（只读副本；供工作点一维扫描使用）。"""
        return self._of.copy()

    def domain(self) -> str:
        """表覆盖域的可读描述（用于域外报错信息）。"""
        return (
            f"O/F ∈ [{self._of[0]:g}, {self._of[-1]:g}]（{self._of.size} 点）、"
            f"Pc ∈ [{self._pc[0]:g}, {self._pc[-1]:g}] bar、"
            f"ε ∈ [{self._eps[0]:g}, {self._eps[-1]:g}]、"
            f"Pamb ∈ [{self._pamb[0]:g}, {self._pamb[-1]:g}] bar"
        )

    def lookup(self, of: float, pc_bar: float, eps: float, pamb_bar: float) -> CeaPoint:
        """查询一点；域外（含 Pamb≥Pc 之外的任何越界）显式报错，不外推。"""
        key = np.array([of, math.log10(pc_bar), math.log10(eps)], dtype=float)
        try:
            values = np.asarray(self._interp(key)).ravel()
            if pamb_bar == 0.0:
                # 真空口径：Pamb=0 列与 isp_vacuum_m_s 恒等（生成器写表保证），
                # 免掉 4 维插值——这是上面级最常见路径。
                ambient = float(values[0])
            else:
                ambient = float(
                    np.asarray(self._interp_ambient(np.append(key, pamb_bar))).ravel()[0]
                )
        except ValueError as exc:
            msg = (
                f"查询 ({of:g}, {pc_bar:g} bar, ε={eps:g}, Pamb={pamb_bar:g} bar) "
                f"超出表覆盖域：{self.domain()}"
            )
            raise AeroForgeError(
                msg,
                suggestion=(
                    "插值不外推（§8.2 口径）。请把查询点收回表域内；若确实需要更大的"
                    " Pc/ε/O/F 范围，用 tools/cea_tablegen.py 以新网格重新生成表"
                ),
            ) from exc
        return CeaPoint(
            of=of,
            pc_bar=pc_bar,
            eps=eps,
            pamb_bar=pamb_bar,
            isp_vacuum_m_s=float(values[0]),
            c_star_m_s=float(values[1]),
            t_chamber_k=float(values[2]),
            gamma=float(values[3]),
            mw=float(values[4]),
            chamber_density_kg_m3=float(values[5]),
            isp_ambient_m_s=ambient,
        )


def _load_uncached(propellant: str, root: Path) -> CeaTable:
    """实际加载（无缓存）：存在性、收录、完整性三道校验。"""
    if propellant not in KNOWN_PROPELLANTS:
        msg = f"推进剂组合 {propellant!r} 未收录 CEA 表；已收录：{', '.join(KNOWN_PROPELLANTS)}"
        raise AeroForgeError(
            msg,
            suggestion=(
                "固体与其它组合属后续批次（§8.2 网格枚举）；如需新增组合，"
                "在 tools/cea_tablegen.py 注册物性与网格后重新生成"
            ),
        )

    npz_path = root / f"cea_{propellant}.npz"
    manifest_path = root / "manifest.json"
    if not npz_path.is_file() or not manifest_path.is_file():
        msg = (
            f"CEA 表缺失：{npz_path}（或 {manifest_path}）不存在。"
            f"请先离线生成：uv run python tools/cea_tablegen.py --propellant {propellant}"
        )
        raise AeroForgeError(
            msg,
            suggestion=(
                "先离线生成表（仓库根执行）："
                f"uv run python tools/cea_tablegen.py --propellant {propellant}"
            ),
            code="CEA_TABLE_NOT_FOUND",
        )

    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"manifest.json 不可读或非法 JSON：{exc}"
        raise AeroForgeError(
            msg,
            suggestion="data/ 表不可手改（§15）；请用 tools/cea_tablegen.py 重新生成",
            code="CEA_TABLE_INTEGRITY",
        ) from exc

    if int(manifest.get("version", -1)) != MANIFEST_VERSION:
        msg = f"manifest 版本 {manifest.get('version')!r} ≠ 支持版本 {MANIFEST_VERSION}"
        raise AeroForgeError(
            msg,
            suggestion="表格式已演进；请用 tools/cea_tablegen.py 重新生成全部表",
            code="CEA_TABLE_INTEGRITY",
        )

    entry = manifest.get("tables", {}).get(propellant)
    if not isinstance(entry, dict):
        msg = f"manifest.json 中没有 {propellant!r} 的表条目"
        raise AeroForgeError(
            msg,
            suggestion=f"uv run python tools/cea_tablegen.py --propellant {propellant}",
            code="CEA_TABLE_NOT_FOUND",
        )

    data = npz_path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if str(entry.get("sha256")) != digest:
        msg = f"{npz_path} 的 sha256 与 manifest 声明不符（表被改动？）"
        raise AeroForgeError(
            msg,
            suggestion=(
                "data/ 表不可变、禁止手改（§15）。若确需更新，"
                f"用 tools/cea_tablegen.py --propellant {propellant} 重新生成"
            ),
            code="CEA_TABLE_INTEGRITY",
        )

    return CeaTable(propellant, npz_path, entry)


@cache
def _load_cached(propellant: str, root: str) -> CeaTable:
    return _load_uncached(propellant, Path(root))


def load_table(propellant: str, root: Path | None = None) -> CeaTable:
    """加载（带缓存的）推进剂 CEA 表。

    ``root`` 缺省用 :func:`aeroforge.paths.cea_tables_root`（源码态 = 仓库 ``data/cea``）。
    """
    resolved = (root if root is not None else cea_tables_root()).resolve()
    return _load_cached(propellant, str(resolved))


def lookup(
    propellant: str,
    of: float,
    pc_bar: float,
    eps: float,
    pamb_bar: float = 0.0,
) -> CeaPoint:
    """查询 API（§8.2）：目标 < 1 ms（插值器构造一次，进程级缓存）。

    ``pamb_bar`` 缺省 0（真空）——上面级的常见口径；一级海平面工况显式传 1.013。
    """
    return load_table(propellant).lookup(of, pc_bar, eps, pamb_bar)
