"""指标公式统一计算路径。

指标定义（metric_def）和口径注册（metric_alias）原先各自解析公式、精度位数
和字段版本。这里把三件事收拢成唯一实现：

1. 公式解析：把「计算公式」规范化后编译成受限 AST，只允许四则、取模、乘方、
   ``abs/min/max/round``，不执行任意代码；无法解析的历史值原样保留。
2. 精度解析：``精度位数`` 兼容 ``"4"``、``"精度4"``、``"4位"``、``4.0``，
   历史脏数据回退到默认 4 位并保留原文，保证旧记录仍能读取。
3. 版本解析：同一指标存在「历史公式」和「当前公式」时，按策略选择实际口径。
   默认 ``历史优先``，历史样例继续得到原口径结果；``按版本时间`` 则取生效
   时间最新的当前口径，并同时返回历史结果用于差异单列。

计算统一使用 Decimal + 四舍五入（ROUND_HALF_UP），避免浮点误差导致重构前后
同一批输入得到不同结果。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from decimal import Decimal, DivisionByZero, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any, Iterable

DEFAULT_PRECISION = 4
MAX_PRECISION = 12
POLICY_HISTORICAL = "历史优先"
POLICY_TIME = "按版本时间"
POLICIES = (POLICY_HISTORICAL, POLICY_TIME)
_POLICY_ALIASES = {
    "historical": POLICY_HISTORICAL,
    "history": POLICY_HISTORICAL,
    "历史": POLICY_HISTORICAL,
    "time": POLICY_TIME,
    "latest": POLICY_TIME,
    "当前": POLICY_TIME,
}

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_LEGACY = "legacy-unparsed"
STATUS_MISSING_VARIABLE = "missing-variable"
STATUS_ERROR = "error"

_ALLOWED_FUNCTIONS = {"abs", "min", "max", "round"}
_OPERATOR_NODES = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow)

_FULLWIDTH_TRANSLATION = str.maketrans({
    "（": " (", "）": ") ", "×": " * ", "÷": " / ", "－": " - ", "＋": " + ", "　": " ",
})


class FormulaError(ValueError):
    """公式取值/语法错误，调用方据此降级为只读状态。"""


class MissingVariables(FormulaError):
    """公式引用了未提供的变量。"""

    def __init__(self, names: Iterable[str]) -> None:
        self.names = tuple(sorted({str(name) for name in names}))
        super().__init__(f"缺少变量：{'、'.join(self.names)}")


@dataclass(frozen=True)
class PrecisionInfo:
    raw: Any
    digits: int
    status: str  # ok / normalized / default / legacy-fallback
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "精度原文": _json_safe(self.raw),
            "精度位数": self.digits,
            "精度状态": self.status,
            "精度说明": self.message,
        }


@dataclass(frozen=True)
class ParsedFormula:
    raw: str
    normalized: str
    expression: str | None
    variables: tuple[str, ...]
    status: str
    message: str = ""
    tree: ast.Expression | None = field(default=None, compare=False, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "公式原文": self.raw,
            "公式规范化": self.normalized,
            "解析表达式": self.expression,
            "变量": list(self.variables),
            "解析状态": self.status,
            "解析说明": self.message,
        }


@dataclass(frozen=True)
class CalculationOutcome:
    formula_raw: str
    parsed: ParsedFormula
    precision: PrecisionInfo
    variables: dict[str, Any]
    status: str
    display: str | None
    value: float | None
    raw_value: str | None
    message: str
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "公式原文": self.formula_raw,
            "公式规范化": self.parsed.normalized,
            "解析表达式": self.parsed.expression,
            "变量": {name: _json_safe(value) for name, value in self.variables.items()},
            "精度原文": _json_safe(self.precision.raw),
            "精度位数": self.precision.digits,
            "精度状态": self.precision.status,
            "结果": self.display,
            "数值": self.value,
            "未舍入值": self.raw_value,
            "状态": self.status,
            "说明": self.message,
            "提示": list(self.warnings),
        }


def normalize_formula(raw: Any) -> str:
    """去掉全角运算符和多余空白，不改变原始公式存档。"""
    text = "" if raw is None else str(raw)
    text = text.translate(_FULLWIDTH_TRANSLATION)
    return re.sub(r"\s+", " ", text).strip()


def resolve_precision(raw: Any) -> PrecisionInfo:
    """把不同历史写法的精度解析成 0-12 位小数，解析失败时保留原文并回退。"""
    warnings: list[str] = []
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return PrecisionInfo(raw, DEFAULT_PRECISION, "default", f"未填写精度，按默认 {DEFAULT_PRECISION} 位计算")

    if isinstance(raw, bool):
        return PrecisionInfo(raw, DEFAULT_PRECISION, "legacy-fallback", "精度不是数字，按默认精度读取，原文已保留")

    if isinstance(raw, int):
        digits, status = raw, "ok"
    elif isinstance(raw, float):
        if not raw.is_integer():
            return PrecisionInfo(raw, DEFAULT_PRECISION, "legacy-fallback", "精度必须为整数，按默认精度读取，原文已保留")
        digits, status = int(raw), "normalized"
    else:
        text = str(raw).strip()
        matched = (
            re.fullmatch(r"(\d+)", text)
            or re.fullmatch(r"(\d+)\.0+", text)
            or re.fullmatch(r"精度\s*[:：]?\s*(\d+)", text)
            or re.fullmatch(r"(\d+)\s*位", text)
        )
        if not matched:
            return PrecisionInfo(raw, DEFAULT_PRECISION, "legacy-fallback", "精度不是可识别数字，按默认精度读取，原文已保留")
        digits, status = int(matched.group(1)), "normalized"

    if digits < 0:
        return PrecisionInfo(raw, 0, "normalized", "精度为负数，按 0 位读取，原文已保留")
    if digits > MAX_PRECISION:
        warnings.append(f"精度 {digits} 超过上限 {MAX_PRECISION}，已收敛到上限")
        digits = MAX_PRECISION
    message = f"按 {digits} 位小数四舍五入"
    if warnings:
        message = f"{message}；{'；'.join(warnings)}"
    return PrecisionInfo(raw, digits, status, message)


def parse_formula(raw: Any) -> ParsedFormula:
    """解析公式为受限 AST；非法/历史文本不抛异常，返回可读状态。"""
    raw_text = "" if raw is None else str(raw)
    normalized = normalize_formula(raw_text)
    if not normalized:
        return ParsedFormula(raw_text, normalized, None, (), STATUS_EMPTY, "计算公式为空")

    try:
        tree = ast.parse(normalized, mode="eval")
        _validate_node(tree)
    except SyntaxError as exc:
        return ParsedFormula(raw_text, normalized, None, (), STATUS_LEGACY, f"公式无法解析：{exc.msg}")
    except FormulaError as exc:
        return ParsedFormula(raw_text, normalized, None, (), STATUS_LEGACY, str(exc))

    variables = tuple(sorted(_collect_variables(tree)))
    # 中文标识符虽然是合法 Python 变量名，但在本领域里通常是把说明文字误填进了
    # 公式栏（例如「指标定义样例1」）。这类历史值按原文保留，不臆造变量取值。
    non_ascii = [name for name in variables if not name.isascii()]
    if non_ascii:
        return ParsedFormula(
            raw_text, normalized, None, (), STATUS_LEGACY,
            f"公式包含中文描述「{'、'.join(non_ascii)}」，不是可计算表达式，按原文保留",
        )
    expression = ast.unparse(tree)
    return ParsedFormula(raw_text, normalized, expression, variables, STATUS_OK, "", tree)


def default_sample_variables(formulas: Iterable[str]) -> dict[str, Any]:
    """给没有外部入参的公式生成稳定的历史样例变量，保证前后两次计算结果一致。"""
    names: set[str] = set()
    for formula in formulas:
        names.update(parse_formula(formula).variables)
    return {name: _default_variable_value(name) for name in sorted(names)}


def calculate_formula(
    formula_raw: Any,
    precision_raw: Any,
    variables: dict[str, Any] | None = None,
) -> CalculationOutcome:
    """统一计算公式：解析、校验变量、Decimal 运算、按精度四舍五入。"""
    parsed = parse_formula(formula_raw)
    precision = resolve_precision(precision_raw)
    values = {str(name): value for name, value in (variables or {}).items()}
    warnings = tuple(filter(None, [precision.message if precision.status != "ok" else ""]))

    def outcome(status: str, message: str, *, display: str | None = None,
                value: float | None = None, raw_value: str | None = None) -> CalculationOutcome:
        return CalculationOutcome(
            formula_raw=parsed.raw, parsed=parsed, precision=precision, variables=values,
            status=status, display=display, value=value, raw_value=raw_value,
            message=message, warnings=warnings,
        )

    if parsed.status != STATUS_OK or parsed.tree is None:
        return outcome(parsed.status, parsed.message or "公式不是可计算的表达式，按原文保留")

    missing = [name for name in parsed.variables if name not in values]
    if missing:
        return outcome(STATUS_MISSING_VARIABLE, MissingVariables(missing).args[0])

    try:
        with localcontext() as context:
            context.prec = 28
            raw_value = _eval_node(parsed.tree, values)
        rounded = _quantize(raw_value, precision.digits)
        if rounded == 0:
            rounded = abs(rounded)
        display = f"{rounded:.{precision.digits}f}"
        numeric = _to_float(rounded)
    except MissingVariables as exc:
        return outcome(STATUS_MISSING_VARIABLE, exc.args[0])
    except (FormulaError, InvalidOperation, DivisionByZero, ArithmeticError, OverflowError) as exc:
        return outcome(STATUS_ERROR, f"公式计算失败：{exc}")
    except Exception as exc:  # noqa: BLE001 - 历史脏数据不能打断列表/详情
        return outcome(STATUS_ERROR, f"公式计算失败：{exc}")

    return outcome(STATUS_OK, precision.message, display=display, value=numeric,
                   raw_value=_decimal_text(raw_value))


# ---------------------------------------------------------------------------
# 版本解析：历史公式 vs 当前公式
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormulaVersion:
    metric_code: str
    role: str  # definition / historical / current
    label: str
    formula: str
    precision_raw: Any
    effective_time: str
    source: str
    source_id: int | None
    version_label: str
    historical_fields: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "指标编号": self.metric_code,
            "角色": self.role,
            "版本名称": self.label,
            "口径版本": self.version_label,
            "计算公式": self.formula,
            "精度位数": _json_safe(self.precision_raw),
            "生效时间": self.effective_time or None,
            "来源模块": self.source,
            "来源id": self.source_id,
            "历史字段": _json_safe(self.historical_fields),
        }


@dataclass(frozen=True)
class VersionResolution:
    metric_code: str
    policy: str
    selected: FormulaVersion
    historical: FormulaVersion
    current: FormulaVersion
    definition: FormulaVersion
    candidates: tuple[FormulaVersion, ...]
    conflict: bool
    conflict_reason: str
    notes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "指标编号": self.metric_code,
            "策略": self.policy,
            "选中版本": self.selected.as_dict(),
            "历史口径": self.historical.formula,
            "历史精度位数": _json_safe(self.historical.precision_raw),
            "当前口径": self.current.formula,
            "当前精度位数": _json_safe(self.current.precision_raw),
            "定义公式": self.definition.formula,
            "定义精度位数": _json_safe(self.definition.precision_raw),
            "版本冲突": self.conflict,
            "冲突说明": self.conflict_reason,
            "说明": list(self.notes),
            "候选版本": [candidate.as_dict() for candidate in self.candidates],
        }


def validate_policy(policy: str | None) -> str:
    resolved = (policy or POLICY_HISTORICAL).strip()
    resolved = _POLICY_ALIASES.get(resolved.casefold(), _POLICY_ALIASES.get(resolved, resolved))
    if resolved not in POLICIES:
        raise ValueError(f"未知的版本解析策略「{policy}」，可选：{'、'.join(POLICIES)}")
    return resolved


def resolve_versions(
    definition_row: dict[str, Any],
    alias_rows: Iterable[dict[str, Any]],
    policy: str | None = POLICY_HISTORICAL,
) -> VersionResolution:
    """读取指标定义与旧/新口径，产出唯一版本解析结果。

    历史公式取最早生效的「旧口径」，缺失时回退到指标定义公式；
    当前公式取最晚生效的「新口径」，缺失时回退到指标定义公式。
    历史/当前口径的精度优先使用口径行里留档的 ``旧精度位数``/``新精度位数``，
    没有留档才读取指标定义精度，保证迁移到共用路径时不丢历史字段。
    """
    policy = validate_policy(policy)
    metric_code = str(definition_row.get("指标编号", ""))
    definition = FormulaVersion(
        metric_code=metric_code,
        role="definition",
        label="指标定义当前版本",
        formula="" if definition_row.get("计算公式") is None else str(definition_row.get("计算公式")),
        precision_raw=definition_row.get("精度位数"),
        effective_time="",
        source="metric_def",
        source_id=_as_int(definition_row.get("id")),
        version_label=str(definition_row.get("口径版本") or ""),
        historical_fields=dict(definition_row),
    )

    aliases = [row for row in alias_rows if str(row.get("指标编号", "")) == metric_code]
    historical_candidates: list[FormulaVersion] = []
    current_candidates: list[FormulaVersion] = []
    candidates: list[FormulaVersion] = [definition]
    for row in aliases:
        effective = str(row.get("生效时间") or "").strip()
        source_id = _as_int(row.get("id"))
        version_label = str(row.get("版本编号") or "")
        old_formula = str(row.get("旧口径") or "").strip()
        if old_formula:
            version = FormulaVersion(
                metric_code=metric_code, role="historical", label=f"历史口径（{version_label or source_id}）",
                formula=old_formula,
                precision_raw=_archived_precision(row, "旧精度位数", definition.precision_raw),
                effective_time=effective, source="metric_alias", source_id=source_id,
                version_label=version_label, historical_fields=dict(row),
            )
            historical_candidates.append(version)
            candidates.append(version)
        new_formula = str(row.get("新口径") or "").strip()
        if new_formula:
            version = FormulaVersion(
                metric_code=metric_code, role="current", label=f"当前口径（{version_label or source_id}）",
                formula=new_formula,
                precision_raw=_archived_precision(row, "新精度位数", definition.precision_raw),
                effective_time=effective, source="metric_alias", source_id=source_id,
                version_label=version_label, historical_fields=dict(row),
            )
            current_candidates.append(version)
            candidates.append(version)

    historical = max(historical_candidates, key=_version_sort_key) if historical_candidates else definition
    current = max(current_candidates, key=_version_sort_key) if current_candidates else definition
    selected = historical if policy == POLICY_HISTORICAL else current

    definition_formula = normalize_formula(definition.formula)
    historical_formula = normalize_formula(historical.formula)
    current_formula = normalize_formula(current.formula)
    historical_precision = resolve_precision(historical.precision_raw).digits
    current_precision = resolve_precision(current.precision_raw).digits
    conflict = (
        historical_formula != current_formula
        or historical_precision != current_precision
        or (historical.role == "historical" and historical_formula != definition_formula)
        or (current.role == "current" and current_formula != definition_formula)
    )
    notes: list[str] = []
    if conflict:
        conflict_reason = (
            "同一指标同时存在历史公式和当前公式："
            f"历史口径「{historical.formula}」（精度 {historical_precision}），"
            f"当前口径「{current.formula}」（精度 {current_precision}）"
        )
        if policy == POLICY_HISTORICAL:
            notes.append("按历史优先解析：历史样例继续得到原口径结果，当前结果仅作对照")
        else:
            notes.append("按版本时间解析：当前样例使用新口径结果，历史差异单独列出")
            notes.append(
                f"历史差异：历史口径「{historical.formula}」按精度 {historical_precision} 的结果"
                "与当前口径结果不同，可用历史优先策略回查"
            )
    else:
        conflict_reason = "历史公式与当前公式一致，无版本冲突"
        notes.append("历史公式与当前公式一致，按统一计算路径解析")

    if historical.role == "definition" and current.role == "current":
        notes.append("未找到旧口径留档，历史公式回退为指标定义公式")
    if current.role == "definition" and historical.role == "historical":
        notes.append("未找到新口径记录，当前公式回退为指标定义公式")
    if policy == POLICY_TIME and not current.effective_time:
        notes.append("当前口径缺少生效时间，按现有记录使用当前公式")

    return VersionResolution(
        metric_code=metric_code,
        policy=policy,
        selected=selected,
        historical=historical,
        current=current,
        definition=definition,
        candidates=tuple(candidates),
        conflict=conflict,
        conflict_reason=conflict_reason,
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class VersionEvaluation:
    """一次版本解析后的三口径计算结果：指标定义、历史、当前与选中。"""

    resolution: VersionResolution
    sample: dict[str, Any]
    definition: CalculationOutcome
    historical: CalculationOutcome
    current: CalculationOutcome
    selected: CalculationOutcome
    selected_role: str

    @property
    def ok(self) -> bool:
        return self.selected.status == STATUS_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "指标编号": self.resolution.metric_code,
            "策略": self.resolution.policy,
            "历史样例": {name: _json_safe(value) for name, value in self.sample.items()},
            "指标定义": self.definition.as_dict(),
            "历史口径": self.historical.as_dict(),
            "当前口径": self.current.as_dict(),
            "选中口径": self.selected_role,
            "选中结果": self.selected.as_dict(),
            "版本冲突": self.resolution.conflict,
            "冲突说明": self.resolution.conflict_reason,
            "版本说明": list(self.resolution.notes),
            "候选版本": [candidate.as_dict() for candidate in self.resolution.candidates],
            "可复算": self.ok,
        }


def evaluate_versions(
    resolution: VersionResolution,
    variables: dict[str, Any] | None = None,
) -> VersionEvaluation:
    """指标定义/历史口径/当前口径只在这里各算一次，其他模块统一调用。"""
    sample = dict(variables or default_sample_variables([
        resolution.definition.formula,
        resolution.historical.formula,
        resolution.current.formula,
    ]))
    definition = calculate_formula(
        resolution.definition.formula, resolution.definition.precision_raw, sample)
    historical = calculate_formula(
        resolution.historical.formula, resolution.historical.precision_raw, sample)
    current = calculate_formula(
        resolution.current.formula, resolution.current.precision_raw, sample)
    selected = historical if resolution.policy == POLICY_HISTORICAL else current
    return VersionEvaluation(
        resolution=resolution,
        sample=sample,
        definition=definition,
        historical=historical,
        current=current,
        selected=selected,
        selected_role="历史" if resolution.policy == POLICY_HISTORICAL else "当前",
    )


@dataclass(frozen=True)
class LedgerCheck:
    """口径记录对 V031 留档的复算结果，口径注册列表/详情/消费端共用。"""

    entry: dict[str, Any]
    archive: dict[str, Any] | None
    old_precision: Any
    new_precision: Any
    sample: dict[str, Any]
    old: CalculationOutcome
    new: CalculationOutcome

    @property
    def archive_result(self) -> str | None:
        return self.archive.get("历史结果") if self.archive else None

    @property
    def matches(self) -> bool:
        return bool(self.archive) and self.old.display == self.archive_result

    @property
    def status(self) -> str:
        if self.archive is None:
            return "留档缺失"
        return "与留档一致" if self.matches else "留档不一致"

    def as_dict(self) -> dict[str, Any]:
        return {
            "留档批次": self.archive.get("batch") if self.archive else None,
            "留档历史公式": self.archive.get("历史公式") if self.archive else None,
            "留档精度位数": self.old_precision,
            "留档当前精度位数": self.new_precision,
            "历史样例": {name: _json_safe(value) for name, value in self.sample.items()},
            "旧口径结果": self.old.as_dict(),
            "新口径结果": self.new.as_dict(),
            "V031历史结果": self.archive_result,
            "与留档一致": self.matches,
            "核对状态": self.status,
            "说明": (
                "读取 V031 留档复算完成" if self.archive
                else "尚未完成 V031 核对，当前按本条口径记录精度回退复算"
            ),
        }


def check_against_archive(
    entry: dict[str, Any],
    archive: dict[str, Any] | None,
) -> LedgerCheck:
    """读留档里的公式/精度和历史样例，复算一条口径记录的旧/新口径。

    留档里分别保存了历史与当前两套精度：旧口径用历史精度，新口径用当前精度；
    任一侧留档缺失时才回退到本条口径记录自己的精度字段。
    """
    def archived(key: str, fallback: Any) -> Any:
        value = archive.get(key) if archive else None
        if value is None or (isinstance(value, str) and not value.strip()):
            return fallback
        return value

    old_precision = archived("历史精度位数", entry.get("旧精度位数"))
    new_precision = archived("当前精度位数", entry.get("新精度位数"))
    sample = dict(archive.get("历史样例") or {}) if archive else {}
    return LedgerCheck(
        entry=entry,
        archive=archive,
        old_precision=old_precision,
        new_precision=new_precision,
        sample=sample,
        old=calculate_formula(entry.get("旧口径"), old_precision, sample),
        new=calculate_formula(entry.get("新口径"), new_precision, sample),
    )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _validate_node(node: ast.AST) -> None:
    if isinstance(node, ast.Expression):
        _validate_node(node.body)
        return
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise FormulaError("公式只能包含数字常量")
        return
    if isinstance(node, ast.Name):
        if not isinstance(node.ctx, ast.Load):
            raise FormulaError("公式不支持变量赋值")
        return
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _OPERATOR_NODES):
            raise FormulaError("公式只支持 + - * / % ** 运算")
        _validate_node(node.left)
        _validate_node(node.right)
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.UAdd, ast.USub)):
            raise FormulaError("公式不支持该一元运算")
        _validate_node(node.operand)
        return
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS:
            raise FormulaError("公式只支持 abs/min/max/round 函数")
        if node.keywords:
            raise FormulaError("公式函数不支持关键字参数")
        for argument in node.args:
            _validate_node(argument)
        return
    raise FormulaError(f"公式包含不支持的语法：{type(node).__name__}")


def _collect_variables(tree: ast.Expression) -> set[str]:
    """收集真正的变量名；``round(...)`` 这类函数名不算变量。"""
    function_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function_names.add(node.func.id)
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        and node.id not in function_names
    }


def _eval_node(node: ast.AST, variables: dict[str, Any]) -> Decimal:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, variables)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise FormulaError("公式常量必须是数字")
        return Decimal(str(node.value))
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise MissingVariables([node.id])
        return _to_decimal(variables[node.id], node.id)
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, variables)
        right = _eval_node(node.right, variables)
        return _apply_binary(node.op, left, right)
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, variables)
        return -operand if isinstance(node.op, ast.USub) else operand
    if isinstance(node, ast.Call):
        name = node.func.id  # type: ignore[attr-defined]
        args = [_eval_node(argument, variables) for argument in node.args]
        return _call_function(name, args)
    raise FormulaError("公式包含不支持的语法")


def _apply_binary(operator: ast.operator, left: Decimal, right: Decimal) -> Decimal:
    if isinstance(operator, ast.Add):
        return left + right
    if isinstance(operator, ast.Sub):
        return left - right
    if isinstance(operator, ast.Mult):
        return left * right
    if isinstance(operator, ast.Div):
        if right == 0:
            raise FormulaError("除数为 0")
        return left / right
    if isinstance(operator, ast.Mod):
        if right == 0:
            raise FormulaError("取模的除数为 0")
        return left % right
    if isinstance(operator, ast.Pow):
        if right != right.to_integral_value():
            raise FormulaError("指数必须是整数")
        exponent = int(right)
        if abs(exponent) > 12:
            raise FormulaError("指数超出允许范围（-12 到 12）")
        if left == 0 and exponent < 0:
            raise FormulaError("0 不能作为负指数底数")
        return left ** exponent
    raise FormulaError("不支持的运算符")


def _call_function(name: str, args: list[Decimal]) -> Decimal:
    if name == "abs" and len(args) == 1:
        return abs(args[0])
    if name == "min" and args:
        return min(args)
    if name == "max" and args:
        return max(args)
    if name == "round" and len(args) in (1, 2):
        digits = 0
        if len(args) == 2:
            if args[1] != args[1].to_integral_value():
                raise FormulaError("round 的位数必须是整数")
            digits = int(args[1])
        if digits < 0 or digits > MAX_PRECISION:
            raise FormulaError(f"round 的位数必须在 0 到 {MAX_PRECISION} 之间")
        return args[0].quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    raise FormulaError(f"函数 {name} 参数不合法")


def _to_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise FormulaError(f"变量「{name}」不是数字")
    if isinstance(value, Decimal):
        converted = value
    elif isinstance(value, (int, float)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise FormulaError(f"变量「{name}」不是有限数字")
        converted = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise FormulaError(f"变量「{name}」取值为空")
        try:
            converted = Decimal(text)
        except InvalidOperation as exc:
            raise FormulaError(f"变量「{name}」不是数字：{value}") from exc
    else:
        raise FormulaError(f"变量「{name}」类型不支持")
    if not converted.is_finite():
        raise FormulaError(f"变量「{name}」不是有限数字")
    return converted


def _quantize(value: Decimal, digits: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)


def _to_float(value: Decimal) -> float | None:
    try:
        return float(value)
    except (OverflowError, ValueError):
        return None


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _default_variable_value(name: str) -> int:
    known = {
        "clicks": 25, "impressions": 100, "valid_impressions": 80, "refunds": 5,
        "orders": 40, "users": 20, "revenue": 1200, "cost": 800,
        "conversions": 12, "visits": 200, "total": 100, "amount": 500,
    }
    if name in known:
        return known[name]
    return 1 + sum(ord(char) for char in name) % 100


def _version_sort_key(version: FormulaVersion) -> tuple[str, int]:
    return (version.effective_time or "", version.source_id or 0)


def _archived_precision(row: dict[str, Any], key: str, fallback: Any) -> Any:
    """口径行留档的精度优先；留档值缺失或为空才回退指标定义精度。"""
    value = row.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return fallback
    return value


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)
