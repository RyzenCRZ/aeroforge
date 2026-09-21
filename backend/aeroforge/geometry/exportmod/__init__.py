"""多格式导出编排（规格 §5.8 / §10.1 ``POST /api/export``，M5 第三片）。

独立模块，不与建模职责耦合（§5.8 规则 2）：本模块消费**已定义好的**装配实体
（:func:`aeroforge.geometry.assembly.build_assembly`）与参数 / 性能链，只负责
「验证门禁 → 逐格式写出 → 产物登记 → provenance」。逐格式 writer 在
:mod:`aeroforge.geometry.exportmod.writers`。

§5.8 四条规则的落地
--------------------
1. **STEP 是唯一权威格式**：AP214（build123d ``export_step``，``unit=M``）；
   IGES / STL / GLB / 报告全部是衍生——provenance 逐格式声明精度口径。
2. **独立模块 + 独立 API**：本包 + ``POST /api/export``（异步走既有几何作业
   体系——OCCT 专用单线程，§9.1 硬规则 3）。
3. **provenance**：格式 / 容差 / 内核版本 / SPEC_VERSION / 导出时刻随结果下发。
4. **规则 4（验证门禁）**：导出**精确格式**（STEP / IGES）前强制跑 §5.7 验证
   链——参数硬约束（§6.3，API 层同步拦截）+ 装配校验（意图断言 / §5.5 共底
   四校验，作业线程内构建后判定）。验证未通过：
   - 精确格式 → **拒绝**（API 层 4xx 带 diagnostics；作业层
     :class:`~aeroforge.errors.ExportValidationError`）；
   - 网格格式（STL / GLB）→ **仍允许**，结果附警告（非精确几何，仅供预览）；
   - 报告类不受几何验证门禁（不走 OCCT），但参数硬约束违反时在 API 层一并
     拒绝（「只允许导出网格格式」的规则 4 字面口径）。

缓存与产物落位（§9.2 纪律：导出不新建几何缓存键——它是构建产物的衍生）
--------------------------------------------------------------------
- 几何格式 → ``artifacts/<构建键>/export/``（``export.step`` / ``export.iges`` /
  ``export.stl`` / ``export.glb``）；构建键 = ``compute_vehicle_key``（与
  ``POST /api/geometry/build`` 车辆形态同键——同参数 ⟹ 同键 ⟹ 导出与构建产物
  同目录共存）。
- 报告类 → ``artifacts/report-<sha256(canonical+SPEC_VERSION)>/export/``
  （``export.params.json`` / ``export.mass.csv`` / ``export.perf.json``）——报告
  不走 OCCT，键不含 kernel 版本（几何内核升级不得使报告缓存全量失效）。
- 取回统一走既有 ``GET /api/artifacts/{key}/{file}``（白名单 + export/ 子目录）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from aeroforge import SPEC_VERSION
from aeroforge.cache.store import (
    ARTIFACT_EXPORT_GLB,
    ARTIFACT_EXPORT_IGES,
    ARTIFACT_EXPORT_MASS,
    ARTIFACT_EXPORT_PARAMS,
    ARTIFACT_EXPORT_PERF,
    ARTIFACT_EXPORT_STEP,
    ARTIFACT_EXPORT_STL,
    ArtifactStore,
    compute_vehicle_key,
    report_cache_key,
)
from aeroforge.errors import ExportValidationError
from aeroforge.geometry.assembly import build_assembly
from aeroforge.geometry.exportmod.writers import (
    GLB_ANGULAR,
    GLB_DEFLECTION,
    STL_ANGULAR,
    STL_DEFLECTION,
    write_glb,
    write_iges,
    write_mass_csv,
    write_params_json,
    write_perf_json,
    write_step,
    write_stl,
)
from aeroforge.geometry.revolve import kernel_version
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.schema import Vehicle

#: 导出格式枚举（§5.8 表的落地子集：STEP / IGES / STL / GLB + 三类报告）。
ExportFormat = Literal["step", "iges", "stl", "glb", "params_json", "mass_csv", "perf_json"]

#: 精确格式（BREP）——§5.8 规则 4 的验证门禁对象。
PRECISE_FORMATS: frozenset[str] = frozenset({"step", "iges"})

#: 网格格式——验证未通过时仍允许导出（附警告）。
MESH_FORMATS: frozenset[str] = frozenset({"stl", "glb"})

#: 报告类（不走 OCCT）。
REPORT_FORMATS: frozenset[str] = frozenset({"params_json", "mass_csv", "perf_json"})

#: 全部合法格式（API 层请求校验与错误提示共用）。
ALL_FORMATS: tuple[str, ...] = (
    "step",
    "iges",
    "stl",
    "glb",
    "params_json",
    "mass_csv",
    "perf_json",
)

#: 格式 → 产物逻辑名（``artifacts/<key>/export/`` 下的文件名）。
_ARTIFACT_NAMES: dict[str, str] = {
    "step": ARTIFACT_EXPORT_STEP,
    "iges": ARTIFACT_EXPORT_IGES,
    "stl": ARTIFACT_EXPORT_STL,
    "glb": ARTIFACT_EXPORT_GLB,
    "params_json": ARTIFACT_EXPORT_PARAMS,
    "mass_csv": ARTIFACT_EXPORT_MASS,
    "perf_json": ARTIFACT_EXPORT_PERF,
}


class ExportFileResult(BaseModel):
    """一个格式的写出结果（随作业 metrics 下发）。"""

    format: str = Field(description="导出格式名（请求枚举原样）")
    artifact: str = Field(description="产物逻辑名（GET /api/artifacts/{key}/{file} 的 file）")
    key: str = Field(description="产物所在缓存键（几何=构建键；报告=report- 键）")
    bytes: int = Field(description="文件大小（字节；>0 是写出成功的判据）")


class ExportResult(BaseModel):
    """``run_export`` 的结果（作业 metrics 主体；规则 4 拒绝路径经异常上抛）。"""

    validation_passed: bool = Field(description="§5.7 验证链是否通过（规则 4 门禁）")
    files: tuple[ExportFileResult, ...] = Field(description="逐格式写出结果")
    warnings: tuple[str, ...] = Field(description="警告（含规则 4 的网格格式降级警告）")
    diagnostics: tuple[dict[str, Any], ...] = Field(
        description="验证诊断摘要（硬约束裁定 + 装配校验 fail 项；通过时为空）"
    )
    keys: dict[str, str] = Field(description="产物键：geometry=构建键（若涉及），report=报告键")
    provenance: dict[str, str] = Field(
        description="§5.8 规则 3：格式 / 容差 / 内核版本 / SPEC_VERSION / 导出时刻"
    )


def run_export(vehicle: Vehicle, formats: tuple[str, ...], store: ArtifactStore) -> ExportResult:
    """导出编排入口（**只在几何作业线程内调用**——含 OCCT 构建与写出）。

    流程：验证链（约束 + 装配校验）→ 逐格式写出（网格 / 报告先行）→ 规则 4
    拒绝（精确格式被请求且验证未通过时，**在网格 / 报告写完后**抛
    :class:`ExportValidationError`——已写出文件记入错误详情，不静默丢弃）。
    """
    ordered = _dedupe(formats)
    warnings: list[str] = []
    diagnostics: list[dict[str, Any]] = []

    # ── §5.7 验证链第一环：参数硬约束（API 层已同步拦截含非网格格式的请求；
    #    此处兜底——直连 runner 的调用同样受规则 4 约束） ──
    violations = check_vehicle(vehicle)
    hard = [item for item in violations if item.level == "hard"]
    diagnostics.extend(item.model_dump(mode="json") for item in hard)

    geometry_formats = [fmt for fmt in ordered if fmt not in REPORT_FORMATS]

    # ── §5.7 验证链第二环：装配校验（意图断言 + §5.5 共底四校验）──
    #    只在请求含几何格式时构建（报告类不触 OCCT）；布局不可行（铺不满 /
    #    矢高干涉）由 build_assembly → plan_stage 抛 AssemblyError，作业失败。
    checks_failed: list[Any] = []
    assembly = build_assembly(vehicle) if geometry_formats else None
    if assembly is not None:
        checks_failed = [check for check in assembly.checks if not check.ok]
        diagnostics.extend(
            {
                "check": check.check,
                "severity": check.severity,
                "detail": check.detail,
                "stage_index": check.stage_index,
            }
            for check in checks_failed
        )

    validation_passed = not hard and not checks_failed
    rejected = [fmt for fmt in ordered if fmt in PRECISE_FORMATS and not validation_passed]
    if not validation_passed and any(fmt in MESH_FORMATS for fmt in ordered):
        warnings.append(
            "§5.7 验证未通过——网格格式（STL/GLB）按 §5.8 规则 4 附警告导出："
            "网格是近似几何，仅供预览 / 快速验证，不得作为权威交换格式使用"
        )

    build_key = compute_vehicle_key(vehicle).key
    report_key = report_cache_key(vehicle)
    files: list[ExportFileResult] = []

    for fmt in ordered:
        if fmt in rejected:
            continue
        name = _ARTIFACT_NAMES[fmt]
        key = report_key if fmt in REPORT_FORMATS else build_key
        part = store.export_dir(key) / f"{name}.part"
        if fmt == "step":
            assert assembly is not None
            write_step(assembly.root, part)
        elif fmt == "iges":
            assert assembly is not None
            write_iges(assembly.root, part)
        elif fmt == "stl":
            assert assembly is not None
            write_stl(assembly.root, part)
        elif fmt == "glb":
            assert assembly is not None
            write_glb(assembly.root, part)
        elif fmt == "params_json":
            write_params_json(vehicle, part)
        elif fmt == "mass_csv":
            write_mass_csv(vehicle, part)
        else:
            write_perf_json(vehicle, part)
        target = store.register_export(key, name, part)
        files.append(
            ExportFileResult(format=fmt, artifact=name, key=key, bytes=target.stat().st_size)
        )

    provenance = _build_provenance(ordered, build_key, report_key)

    if rejected:
        # 规则 4：精确格式拒绝——已写出的网格 / 报告产物在错误详情中如实列出
        raise ExportValidationError(
            f"§5.7 验证未通过，拒绝导出精确格式：{', '.join(rejected)}（§5.8 规则 4）",
            suggestion=(
                "按 diagnostics 修复验证问题（约束违反 / 装配校验失败）后重试；"
                "或仅请求网格格式（stl / glb，附警告导出）"
            ),
            details={
                "diagnostics": diagnostics,
                "rejected_formats": rejected,
                "exported_files": [item.artifact for item in files],
                "keys": {"geometry": build_key, "report": report_key},
                "kernel_version": kernel_version(),
                "spec_version": SPEC_VERSION,
            },
        )

    return ExportResult(
        validation_passed=validation_passed,
        files=tuple(files),
        warnings=tuple(warnings),
        diagnostics=tuple(diagnostics),
        keys={"geometry": build_key, "report": report_key},
        provenance=provenance,
    )


def _dedupe(formats: tuple[str, ...]) -> tuple[str, ...]:
    """去重保序（请求方传重复格式时同一文件只写一次）。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for fmt in formats:
        if fmt not in seen:
            seen.add(fmt)
            ordered.append(fmt)
    return tuple(ordered)


def _build_provenance(formats: tuple[str, ...], build_key: str, report_key: str) -> dict[str, str]:
    """§5.8 规则 3：格式 / 容差 / 内核版本 / SPEC_VERSION / 导出时刻。"""
    provenance = {
        "kernel_version": kernel_version(),
        "spec_version": SPEC_VERSION,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "keys.geometry": build_key,
        "keys.report": report_key,
        "validation": (
            "§5.8 规则 4：精确格式导出前强制 §5.7 验证"
            "（参数硬约束〔§6.3〕+ 装配校验〔意图断言 / §5.5 共底四校验〕）"
        ),
    }
    if "step" in formats:
        provenance["formats.step"] = "STEP AP214（build123d export_step，unit=M，精确 NURBS）"
    if "iges" in formats:
        provenance["formats.iges"] = "IGES（OCCT IGESControl_Writer，write.iges.unit=M，精确）"
    if "stl" in formats:
        provenance["formats.stl"] = (
            f"ASCII STL，线性容差 {STL_DEFLECTION} m / 角容差 {STL_ANGULAR} rad"
            "（网格近似，非精确几何）"
        )
    if "glb" in formats:
        provenance["formats.glb"] = (
            f"GLB 二进制，线性偏差 {GLB_DEFLECTION} m / 角偏差 {GLB_ANGULAR} rad（LOD2 网格）"
        )
    if "params_json" in formats:
        provenance["formats.params_json"] = (
            "Vehicle canonical 参数 + 模板 sourced_fields（车辆名命中内置模板时附带）"
        )
    if "mass_csv" in formats:
        provenance["formats.mass_csv"] = (
            "逐级质量预算（perf.capacity.vehicle_ledger：几何解析推进剂 + σ 派生干重；"
            "GLOW 与 /api/perf/evaluate 同源）"
        )
    if "perf_json" in formats:
        provenance["formats.perf_json"] = (
            "/api/perf/evaluate 点值输出原样（perf.evaluate.compute_point_evaluation）"
        )
    return provenance


__all__ = [
    "ALL_FORMATS",
    "MESH_FORMATS",
    "PRECISE_FORMATS",
    "REPORT_FORMATS",
    "ExportFileResult",
    "ExportFormat",
    "ExportResult",
    "run_export",
]
