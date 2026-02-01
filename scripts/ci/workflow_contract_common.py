#!/usr/bin/env python3
"""
Workflow Contract Common Utilities

提供 workflow contract 处理的公共函数和常量，供以下脚本复用：
- check_workflow_contract_docs_sync.py
- workflow_contract_drift_report.py
- validate_workflows.py
- suggest_workflow_contract_updates.py

主要功能：
1. METADATA_KEYS: 定义 contract 中的元数据字段（非 workflow 定义）
2. discover_workflow_keys(): 动态发现 contract 中的 workflow 定义 key
3. find_fuzzy_match(): step/job name 模糊匹配
4. build_workflows_view(): 构造 contract workflows dict 视图（兼容无 workflows 包装的格式）

============================================================================
Step Name 匹配优先级与策略
============================================================================

当 workflow 中的 step name 与 contract 中的 required_step 不完全匹配时，
validate_workflows.py 按以下优先级尝试匹配：

  优先级 1: EXACT 精确匹配
    - 大小写敏感的字符串完全相等
    - 结果: 匹配成功，无 warning/error

  优先级 2: ALIAS 别名匹配
    - 在 contract.step_name_aliases 中查找 canonical_step 的别名列表
    - 如果 workflow 中的 step name 在别名列表中，视为匹配
    - 结果: 匹配成功，产生 step_name_alias_matched WARNING
    - 用途: 支持渐进式重命名，提供迁移窗口

  优先级 3: FUZZY 模糊匹配
    - 使用 find_fuzzy_match() 函数尝试模糊匹配
    - 匹配策略详见 FUZZY_MATCH_STRATEGIES
    - 结果:
      - 如果 step 在 frozen_step_text.allowlist 中: ERROR (frozen_step_name_changed)
      - 否则: WARNING (step_name_changed)

  优先级 4: MISSING 未匹配
    - 以上策略都未匹配到
    - 结果: ERROR (missing_step)

Alias 生命周期与冻结项交互
============================================================================

1. **Alias 允许窗口**:
   - step_name_aliases 用于在重命名过渡期内同时接受新旧名称
   - 推荐在 2-3 个迭代周期后移除旧别名，避免无限期累积
   - 文档锚点: contract.md#56-step_name_aliases-别名映射

2. **与冻结项的交互**:
   - frozen_step_text.allowlist 中的 step 属于"冻结"项，不允许改名
   - 如果 canonical_step 在 frozen_step_text 中：
     - 通过 alias 匹配时仍产生 WARNING（提醒更新 workflow）
     - 通过 fuzzy 匹配时产生 ERROR（阻止 CI）
   - 如果 canonical_step 不在 frozen_step_text 中：
     - 通过 alias 或 fuzzy 匹配时都只产生 WARNING

3. **别名冻结策略**:
   - step_name_aliases 中的别名不纳入 frozen_step_text
   - 别名仅作为过渡兼容手段，最终目标是使用 canonical name
   - 文档锚点: maintenance.md#62-冻结-step-rename-标准流程
"""

from __future__ import annotations

from typing import Any, Optional

# ============================================================================
# Constants
# ============================================================================

# Metadata/legacy 字段排除列表 - 这些 key 不是 workflow 定义
#
# 包含以下类型：
# 1. JSON Schema 相关: $schema
# 2. 版本信息: version, description, last_updated
# 3. 全局配置: make (make targets 配置)
# 4. 冻结配置: frozen_step_text, frozen_job_names
# 5. 别名配置: step_name_aliases
#
# 注意：以下划线 (_) 开头的字段通过前缀检查排除：
# - _changelog_* (版本变更记录)
# - _*_note (注释字段)
# - _comment (注释字段)
METADATA_KEYS: frozenset[str] = frozenset(
    [
        "$schema",
        "version",
        "description",
        "last_updated",
        "make",
        "frozen_step_text",
        "frozen_job_names",
        "step_name_aliases",
    ]
)

# ============================================================================
# Step Name 匹配优先级常量
# ============================================================================
#
# 匹配优先级（数字越小优先级越高）


class MatchPriority:
    """Step/Job name 匹配优先级常量

    匹配按优先级顺序执行，首次匹配即返回。

    文档锚点: contract.md#56-step_name_aliases-别名映射
    """

    EXACT = 1  # 精确匹配（大小写敏感）
    ALIAS = 2  # 别名匹配（step_name_aliases 中定义）
    FUZZY = 3  # 模糊匹配（包含匹配或词语重叠）
    NONE = 99  # 未匹配


# ============================================================================
# Fuzzy 匹配策略与阈值
# ============================================================================
#
# FUZZY_MATCH_WORD_OVERLAP_THRESHOLD: 词语重叠匹配的最小比例
# - 当 target 和 candidate 的词语重叠数量 >= target 词语数 * 此阈值时，视为匹配
# - 默认 0.5 表示至少 50% 的词语重叠
# - 设计考量:
#   - 过高（如 0.8）: 可能漏掉合理的重命名（如 "Run lint" -> "Run lint check"）
#   - 过低（如 0.3）: 可能误匹配不相关的 step
#
# 文档锚点: contract.md#53-模糊匹配策略
FUZZY_MATCH_WORD_OVERLAP_THRESHOLD: float = 0.5

# Fuzzy 匹配策略顺序（按优先级）
# 策略 1: 完全匹配（忽略大小写）
# 策略 2: 包含匹配（target 包含 candidate 或反之）
# 策略 3: 词语匹配（至少 FUZZY_MATCH_WORD_OVERLAP_THRESHOLD 词语重叠）
FUZZY_MATCH_STRATEGIES: tuple[str, ...] = (
    "case_insensitive_exact",
    "substring_contains",
    "word_overlap",
)

# ============================================================================
# 同分冲突处理策略
# ============================================================================
#
# 当多个 candidate 与 target 的匹配程度相同时（如都是 substring 匹配），
# 采用以下策略选择最终匹配结果：
#
# FIRST_MATCH: 返回第一个匹配到的 candidate（按 candidates 列表顺序）
# - 优点: 简单、确定性强
# - 缺点: 结果依赖 candidates 列表顺序
#
# 设计决策: 使用 FIRST_MATCH 策略
# - candidates 列表通常来自 workflow 中的 step 顺序，具有语义意义
# - 避免复杂的评分算法引入不可预测性
#
# 文档锚点: contract.md#54-同分冲突处理
FUZZY_MATCH_CONFLICT_STRATEGY: str = "first_match"


# ============================================================================
# Helper Functions
# ============================================================================


def is_metadata_key(key: str) -> bool:
    """判断 key 是否为 metadata/非 workflow 字段

    Args:
        key: contract 的顶层 key

    Returns:
        如果是 metadata key 或下划线前缀字段返回 True
    """
    # 规则 1: 下划线前缀（changelog, notes, comments 等）
    if key.startswith("_"):
        return True

    # 规则 2: 已知 metadata 字段
    if key in METADATA_KEYS:
        return True

    return False


def discover_workflow_keys(contract: dict[str, Any]) -> list[str]:
    """动态发现 contract 中的 workflow 定义 key

    通过扫描顶层 dict，筛选符合 workflow 结构特征的 key：
    1. value 是 dict 类型
    2. value 包含 "file" 字段（workflow 定义的必需字段）
    3. key 不在 METADATA_KEYS 排除列表中
    4. key 不以下划线开头（排除 _changelog_*, _*_note 等注释字段）

    设计原则：
    - 使用 "file" 字段作为 workflow 定义的结构特征判断
    - 新增 metadata key 时只需更新 METADATA_KEYS，不影响 workflow 发现逻辑
    - 新增 workflow 时只需添加包含 "file" 字段的定义，自动被发现

    Args:
        contract: 加载的 contract JSON dict

    Returns:
        发现的 workflow key 列表，按字母序排序

    Example:
        >>> contract = {
        ...     "$schema": "...",
        ...     "version": "2.14.0",
        ...     "_changelog_v2.14.0": "...",
        ...     "ci": {"file": ".github/workflows/ci.yml", ...},
        ...     "nightly": {"file": ".github/workflows/nightly.yml", ...},
        ... }
        >>> discover_workflow_keys(contract)
        ['ci', 'nightly']
    """
    workflow_keys: list[str] = []

    for key, value in contract.items():
        # 排除 metadata 字段（含下划线前缀）
        if is_metadata_key(key):
            continue

        # 检查是否符合 workflow 结构特征：dict 且包含 "file" 字段
        if isinstance(value, dict) and "file" in value:
            workflow_keys.append(key)

    return sorted(workflow_keys)


def find_fuzzy_match(target: str, candidates: list[str]) -> Optional[str]:
    """模糊匹配 step/job name

    此函数实现 Step Name 匹配优先级中的 FUZZY 级别匹配（优先级 3）。
    在 EXACT 和 ALIAS 匹配失败后调用。

    匹配策略（按 FUZZY_MATCH_STRATEGIES 顺序）：
    1. case_insensitive_exact: 完全匹配（忽略大小写）
    2. substring_contains: 包含匹配（target 包含 candidate 或反之）
    3. word_overlap: 词语匹配（至少 FUZZY_MATCH_WORD_OVERLAP_THRESHOLD 词语重叠）

    同分冲突处理（FUZZY_MATCH_CONFLICT_STRATEGY）：
    - 当同一策略下有多个 candidate 匹配时，返回 candidates 列表中第一个匹配到的
    - 这确保了结果的确定性和可预测性

    阈值配置：
    - FUZZY_MATCH_WORD_OVERLAP_THRESHOLD: 词语重叠的最小比例（默认 0.5 = 50%）
      例如: target="Run lint check"(3词) 至少需要 2 个词重叠才能匹配

    Args:
        target: 要查找的目标名称（来自 contract 的 required_step）
        candidates: 候选名称列表（来自 workflow 的实际 step names）

    Returns:
        匹配到的候选名称，未匹配返回 None

    Example:
        >>> find_fuzzy_match("Run lint", ["run lint", "Run tests"])
        'run lint'  # case_insensitive_exact 匹配
        >>> find_fuzzy_match("Run lint check", ["Run lint check (v2)", "Build"])
        'Run lint check (v2)'  # substring_contains 匹配
        >>> find_fuzzy_match("Run unit tests", ["Execute unit tests", "Build"])
        'Execute unit tests'  # word_overlap 匹配 (2/3 = 67% > 50%)

    文档锚点:
        - contract.md#53-模糊匹配策略
        - contract.md#54-同分冲突处理
    """
    target_lower = target.lower()

    # 策略 1: case_insensitive_exact - 完全匹配（忽略大小写）
    for candidate in candidates:
        if candidate.lower() == target_lower:
            return candidate

    # 策略 2: substring_contains - 包含匹配
    # 同分冲突处理: 返回第一个匹配（FIRST_MATCH 策略）
    for candidate in candidates:
        if target_lower in candidate.lower() or candidate.lower() in target_lower:
            return candidate

    # 策略 3: word_overlap - 词语匹配（主要词语相同）
    # 使用 FUZZY_MATCH_WORD_OVERLAP_THRESHOLD 作为阈值
    target_words = set(target_lower.split())
    for candidate in candidates:
        candidate_words = set(candidate.lower().split())
        overlap = len(target_words & candidate_words)
        if overlap >= len(target_words) * FUZZY_MATCH_WORD_OVERLAP_THRESHOLD:
            return candidate

    return None


def build_workflows_view(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """构造 contract workflows dict 视图

    支持两种 contract 格式：
    1. v1.0 格式: {"workflows": {"ci": {...}, "nightly": {...}}}
    2. v1.1+ 格式: {"ci": {...}, "nightly": {...}} (无 workflows 包装)

    此函数返回一个统一的 workflows 字典视图，供调用者遍历使用。

    Args:
        contract: 加载的 contract JSON dict

    Returns:
        workflow_key -> workflow_config 的字典

    Example:
        >>> contract = {"ci": {"file": "ci.yml"}, "version": "1.0.0"}
        >>> build_workflows_view(contract)
        {'ci': {'file': 'ci.yml'}}
    """
    # 优先使用 v1.0 格式的 workflows 字段
    workflows = contract.get("workflows", {})

    if not workflows:
        # v1.1+ 格式：使用 discover_workflow_keys 动态发现
        for key in discover_workflow_keys(contract):
            workflows[key] = contract[key]

    return workflows


# ============================================================================
# Artifact Path Normalization
# ============================================================================


class ArtifactPathError(ValueError):
    """Artifact 路径规范化错误"""

    pass


def normalize_artifact_path(path: str, *, allow_empty: bool = False) -> str:
    """标准化单个 artifact 路径

    规范化规则（按顺序应用）：
    1. 去除首尾空白
    2. 空路径检查（如果 allow_empty=False 则抛出 ArtifactPathError）
    3. 统一分隔符：将 Windows 反斜杠转换为正斜杠
    4. 处理 `./` 前缀：移除开头的 `./`
    5. 处理重复斜杠：将 `//` 替换为 `/`
    6. 处理末尾斜杠：目录路径保留末尾 `/`，文件路径不添加
    7. 处理 `**` 通配符：保持 `**` 模式不变（用于 glob 匹配）

    Args:
        path: 原始路径字符串
        allow_empty: 是否允许空路径，默认 False

    Returns:
        标准化后的路径字符串

    Raises:
        ArtifactPathError: 如果路径为空且 allow_empty=False

    Example:
        >>> normalize_artifact_path("./artifacts/results/")
        'artifacts/results/'
        >>> normalize_artifact_path(".\\\\artifacts\\\\file.json")
        'artifacts/file.json'
        >>> normalize_artifact_path("artifacts//dir//")
        'artifacts/dir/'
        >>> normalize_artifact_path("**/*.xml")
        '**/*.xml'
    """
    # 1. 去除首尾空白
    path = path.strip()

    # 2. 空路径检查
    if not path:
        if allow_empty:
            return ""
        raise ArtifactPathError("Artifact path cannot be empty")

    # 3. 统一分隔符：Windows 反斜杠 -> 正斜杠
    path = path.replace("\\", "/")

    # 4. 处理 `./` 前缀
    while path.startswith("./"):
        path = path[2:]

    # 5. 处理重复斜杠
    while "//" in path:
        path = path.replace("//", "/")

    # 6. 处理开头的单个斜杠（相对路径不应以 / 开头，除非是绝对路径）
    # 保留绝对路径的开头斜杠，但移除相对路径的意外开头斜杠
    # 注意：大多数 artifact 路径应该是相对路径
    if path.startswith("/") and not path.startswith("//"):
        # 检查是否是类似 /home/... 的绝对路径（保留）还是意外的 /artifacts/... （移除）
        # 简化处理：如果第一个组件看起来像目录名而非根，则移除
        # 但为了安全，我们保留这种行为不变，让调用者决定
        pass

    # 7. 路径为空时（经过上述处理后）
    if not path:
        if allow_empty:
            return ""
        raise ArtifactPathError("Artifact path cannot be empty after normalization")

    return path


def normalize_artifact_paths(
    paths: list[str],
    *,
    allow_empty: bool = False,
    deduplicate: bool = True,
    sort: bool = True,
) -> list[str]:
    """标准化 artifact 路径列表

    规范化规则：
    1. 对每个路径应用 normalize_artifact_path()
    2. 可选去重：移除重复路径（基于标准化后的值）
    3. 可选排序：按字母序排序（确保稳定性）

    路径等价性判断（用于去重）：
    - 标准化后完全相同的路径视为等价
    - 例如 "./artifacts/" 和 "artifacts/" 是等价的
    - 例如 "artifacts\\\\dir" 和 "artifacts/dir" 是等价的

    Args:
        paths: 原始路径列表
        allow_empty: 是否允许空路径，默认 False
        deduplicate: 是否去重，默认 True
        sort: 是否排序，默认 True

    Returns:
        标准化后的路径列表

    Raises:
        ArtifactPathError: 如果任何路径为空且 allow_empty=False

    Example:
        >>> normalize_artifact_paths(["./a/", "b/c", "./a/", "b\\\\c"])
        ['a/', 'b/c']
        >>> normalize_artifact_paths(["z", "a", "m"], sort=True)
        ['a', 'm', 'z']
        >>> normalize_artifact_paths(["z", "a", "m"], sort=False)
        ['z', 'a', 'm']
    """
    # 1. 标准化每个路径
    normalized: list[str] = []
    for path in paths:
        try:
            norm_path = normalize_artifact_path(path, allow_empty=allow_empty)
            # 跳过空路径（如果允许）
            if norm_path or not allow_empty:
                normalized.append(norm_path)
        except ArtifactPathError:
            # 重新抛出，附加原始路径信息
            raise ArtifactPathError(f"Invalid artifact path: '{path}'") from None

    # 2. 去重（保持首次出现的顺序）
    if deduplicate:
        seen: set[str] = set()
        deduped: list[str] = []
        for p in normalized:
            if p not in seen:
                seen.add(p)
                deduped.append(p)
        normalized = deduped

    # 3. 排序
    if sort:
        normalized = sorted(normalized)

    return normalized


def paths_are_equivalent(path1: str, path2: str) -> bool:
    """判断两个路径是否等价（标准化后相同）

    Args:
        path1: 第一个路径
        path2: 第二个路径

    Returns:
        如果两个路径标准化后相同则返回 True

    Example:
        >>> paths_are_equivalent("./artifacts/", "artifacts/")
        True
        >>> paths_are_equivalent("a\\\\b", "a/b")
        True
        >>> paths_are_equivalent("a/b", "a/c")
        False
    """
    try:
        norm1 = normalize_artifact_path(path1, allow_empty=True)
        norm2 = normalize_artifact_path(path2, allow_empty=True)
        return norm1 == norm2
    except ArtifactPathError:
        return False


def is_valid_artifact_path(path: str) -> bool:
    """检查路径是否是有效的 artifact 路径

    Args:
        path: 要检查的路径

    Returns:
        如果路径有效返回 True

    Example:
        >>> is_valid_artifact_path("artifacts/results.json")
        True
        >>> is_valid_artifact_path("")
        False
        >>> is_valid_artifact_path("   ")
        False
    """
    try:
        normalize_artifact_path(path, allow_empty=False)
        return True
    except ArtifactPathError:
        return False


def normalize_glob_pattern(pattern: str) -> str:
    """标准化 glob 模式

    在 normalize_artifact_path 基础上，额外处理 glob 特殊字符：
    - `**` 保持不变（递归匹配）
    - `*` 保持不变（单级匹配）
    - `?` 保持不变（单字符匹配）
    - `[...]` 保持不变（字符类匹配）

    Args:
        pattern: 原始 glob 模式

    Returns:
        标准化后的 glob 模式

    Example:
        >>> normalize_glob_pattern("./**/*.xml")
        '**/*.xml'
        >>> normalize_glob_pattern("./logs/[abc]*.log")
        'logs/[abc]*.log'
    """
    return normalize_artifact_path(pattern, allow_empty=False)


# ============================================================================
# Set Diff Utilities
# ============================================================================


def compute_set_diff(
    contract_set: set[str],
    actual_set: set[str],
) -> tuple[set[str], set[str]]:
    """计算两个集合的差异

    用于比较 contract 中声明的项与实际存在的项。

    Args:
        contract_set: 合约中声明的项集合
        actual_set: 实际存在的项集合

    Returns:
        (removed, added) 元组：
        - removed: 合约中有但实际不存在的项
        - added: 实际存在但合约中没有的项

    Example:
        >>> compute_set_diff({'a', 'b', 'c'}, {'b', 'c', 'd'})
        ({'a'}, {'d'})
    """
    removed = contract_set - actual_set
    added = actual_set - contract_set
    return removed, added


def is_string_similar(s1: str, s2: str, *, threshold: float = 0.5) -> bool:
    """判断两个字符串是否相似

    用于 frozen allowlist 检测、step/job name 模糊比较等场景。

    匹配策略（按顺序）：
    1. 完全相同（忽略大小写）
    2. 包含关系（一方包含另一方）
    3. 词语重叠（重叠比例 >= threshold）

    Args:
        s1: 第一个字符串
        s2: 第二个字符串
        threshold: 词语重叠的最小比例（默认 0.5 = 50%）

    Returns:
        如果两个字符串相似返回 True

    Example:
        >>> is_string_similar("Run lint", "run lint")
        True
        >>> is_string_similar("Run lint check", "Run lint")
        True
        >>> is_string_similar("Run unit tests", "Execute tests")
        True  # "tests" 重叠
        >>> is_string_similar("Build", "Deploy")
        False
    """
    s1_lower = s1.lower()
    s2_lower = s2.lower()

    # 策略 1: 完全相同（忽略大小写）
    if s1_lower == s2_lower:
        return True

    # 策略 2: 包含关系
    if s1_lower in s2_lower or s2_lower in s1_lower:
        return True

    # 策略 3: 词语重叠
    s1_words = set(s1_lower.split())
    s2_words = set(s2_lower.split())
    if s1_words and s2_words:
        overlap = len(s1_words & s2_words)
        min_len = min(len(s1_words), len(s2_words))
        if min_len > 0 and overlap / min_len >= threshold:
            return True

    return False


def classify_step_change(
    contract_step: str,
    actual_step_names: list[str],
) -> tuple[str, str | None]:
    """分类 step 变更类型

    根据合约中的 step 名称在实际 workflow 中的匹配情况，
    返回变更类型和匹配到的实际 step 名称。

    变更类型：
    - "exact": 精确匹配（大小写敏感）
    - "fuzzy": 模糊匹配（可能是重命名）
    - "removed": 未找到匹配（step 被移除）

    Args:
        contract_step: 合约中声明的 step 名称
        actual_step_names: 实际 workflow 中的所有 step 名称列表

    Returns:
        (change_type, matched_step) 元组：
        - change_type: 变更类型 ("exact", "fuzzy", "removed")
        - matched_step: 匹配到的实际 step 名称（exact/fuzzy 时非 None）

    Example:
        >>> classify_step_change("Run lint", ["Run lint", "Test"])
        ('exact', 'Run lint')
        >>> classify_step_change("Run lint check", ["Run lint check (v2)", "Test"])
        ('fuzzy', 'Run lint check (v2)')
        >>> classify_step_change("Deploy", ["Run lint", "Test"])
        ('removed', None)
    """
    # 精确匹配
    if contract_step in actual_step_names:
        return ("exact", contract_step)

    # 模糊匹配
    fuzzy_match = find_fuzzy_match(contract_step, actual_step_names)
    if fuzzy_match:
        return ("fuzzy", fuzzy_match)

    # 未匹配
    return ("removed", None)
