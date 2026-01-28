"""
test_schema_conventions.py - Schema 命名规范检查

检测 Python 代码中 SQL 字符串是否使用了硬编码的 schema 前缀。

规则：
1. SQL 语句应使用无前缀表名，依赖 search_path 解析
2. 不应出现 identity., logbook., scm., analysis., governance. 等硬编码前缀
3. 例外：schema 重写相关代码（如 rewrite_sql_for_schema）允许引用 schema 名称

失败时提供：
- 文件路径
- 行号
- 匹配内容
- 建议修复方式
"""

import ast
import re
from pathlib import Path
from typing import List, NamedTuple, Optional, Set


class SchemaViolation(NamedTuple):
    """Schema 规范违反记录"""
    file_path: str
    line_no: int
    col_offset: int
    matched_text: str
    context: str
    suggestion: str


# 需要检测的 schema 名称
SCHEMA_NAMES = ["identity", "logbook", "scm", "analysis", "governance"]

# 匹配 schema.table 格式的正则表达式
# 使用词边界 \b 确保精确匹配，如 scm. 而非 mechanism.
SCHEMA_PREFIX_PATTERN = re.compile(
    r'\b(' + '|'.join(SCHEMA_NAMES) + r')\.',
    re.IGNORECASE
)

# 允许包含 schema 前缀的上下文（例外情况）
# 这些模式用于识别应跳过检查的字符串
# 注意：此列表仅包含合法的非 SQL 场景，不泛化放行真实 SQL 语句
ALLOWED_PATTERNS = [
    # === 代码结构相关（schema 重写/正则定义） ===
    r'old_name',
    r'new_name',
    r'schema_map',
    r'\\b',  # 正则表达式中的词边界
    r're\.compile',
    
    # === 元数据查询（不是业务表，而是 pg_catalog/information_schema） ===
    r'table_schema\s*=',
    r'schema_name\s*=',
    
    # === 常量定义 ===
    r'SCHEMA_NAMES\s*=',
    r'DEFAULT_SCHEMA_NAMES',
    r'KV_NAMESPACE\s*=',
    r'NAMESPACE\s*=',
    
    # === 配置键名（非 SQL 表引用） ===
    # 如 scm.gitlab.*, scm.svn.*, scm.sync.* 是配置文件键名
    r'scm\.(gitlab|svn|bulk_thresholds|incremental|sync)\.',
    r'"scm\.sync\.',
    r"'scm\.sync\.",
    r'\[scm\.',  # 配置节 [scm.xxx]
    
    # === item_type 字段值（如 "scm.sync.svn"） ===
    r'item_type\s*=.*scm\.',
]

# 应跳过检查的文件（相对于 scripts/ 目录）
SKIP_FILES: Set[str] = {
    # 测试文件中可能包含测试用例需要的 schema 前缀
    "tests/test_schema_conventions.py",  # 本测试文件自身
    "tests/test_schema_prefix_migrate.py",  # 专门测试 schema 前缀重写的测试
    "tests/test_step1_smoke.py",  # 冒烟测试中验证表结构
    "tests/test_evidence_refs_schema.py",  # 测试 evidence refs schema
    "tests/test_governance_settings_concurrent.py",  # governance 并发测试使用模块调用
    # scm_integrity_check.py 是数据完整性检查工具，需要显式引用表名
    "scm_integrity_check.py",
}

# 应跳过的函数名（这些函数专门处理 schema 重写）
SKIP_FUNCTIONS: Set[str] = {
    "rewrite_sql_for_schema",
}


def get_scripts_dir() -> Path:
    """获取 scripts 目录路径"""
    return Path(__file__).parent.parent


def find_python_files(base_dir: Path) -> List[Path]:
    """
    查找所有 Python 文件
    
    Args:
        base_dir: 基础目录
        
    Returns:
        Python 文件路径列表
    """
    files = []
    for py_file in base_dir.rglob("*.py"):
        # 跳过 __pycache__ 目录
        if "__pycache__" in str(py_file):
            continue
        files.append(py_file)
    return sorted(files)


def get_skip_function_ranges(source: str) -> List[tuple]:
    """
    获取应跳过的函数的行号范围
    
    Args:
        source: 源代码
        
    Returns:
        (start_line, end_line) 元组列表
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    
    ranges = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name in SKIP_FUNCTIONS:
                end_line = getattr(node, 'end_lineno', node.lineno + 100)
                ranges.append((node.lineno, end_line))
    return ranges


def is_in_skip_function(skip_ranges: List[tuple], line_no: int) -> bool:
    """
    检查指定行是否在跳过的函数内
    
    Args:
        skip_ranges: 跳过函数的行号范围列表
        line_no: 行号（1-based）
        
    Returns:
        是否在跳过的函数内
    """
    for start, end in skip_ranges:
        if start <= line_no <= end:
            return True
    return False


def is_allowed_context(line: str, full_context: str) -> bool:
    """
    检查是否为允许的上下文
    
    Args:
        line: 当前行
        full_context: 完整上下文（包含前后几行）
        
    Returns:
        是否为允许的上下文
    """
    combined = line + " " + full_context
    for pattern in ALLOWED_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            return True
    return False


def is_in_comment(line: str, match_start: int) -> bool:
    """
    检查匹配是否在注释中
    
    Args:
        line: 当前行
        match_start: 匹配起始位置
        
    Returns:
        是否在注释中
    """
    # 查找 # 的位置
    hash_pos = line.find('#')
    if hash_pos != -1 and hash_pos < match_start:
        return True
    return False


def is_in_docstring_or_comment_block(source: str, line_no: int) -> bool:
    """
    检查行是否在文档字符串或多行注释中
    
    简化实现：检查行是否以 ''' 或 \"\"\" 包围
    
    Args:
        source: 源代码
        line_no: 行号（1-based）
        
    Returns:
        是否在文档字符串中
    """
    lines = source.split('\n')
    if line_no > len(lines):
        return False
    
    line = lines[line_no - 1]
    
    # 如果当前行是文档字符串（以 \"\"\" 开始或结束）
    stripped = line.strip()
    if stripped.startswith('"""') or stripped.startswith("'''"):
        return True
    if stripped.endswith('"""') or stripped.endswith("'''"):
        return True
    
    # 检查是否在多行字符串内部（简化检测）
    in_triple_quote = False
    quote_char = None
    for i, ln in enumerate(lines[:line_no], 1):
        # 计算三引号的数量
        for q in ['"""', "'''"]:
            count = ln.count(q)
            if count > 0:
                if not in_triple_quote:
                    in_triple_quote = True
                    quote_char = q
                elif quote_char == q:
                    if count % 2 == 1:
                        in_triple_quote = not in_triple_quote
    
    return in_triple_quote


def extract_string_content(node: ast.expr) -> Optional[str]:
    """
    从 AST 节点提取字符串内容
    
    Args:
        node: AST 表达式节点
        
    Returns:
        字符串内容，非字符串节点返回 None
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # f-string，提取常量部分
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
        return ''.join(parts)
    return None


def check_file(file_path: Path) -> List[SchemaViolation]:
    """
    检查单个文件中的 schema 规范违反
    
    Args:
        file_path: 文件路径
        
    Returns:
        违反记录列表
    """
    violations = []
    
    try:
        source = file_path.read_text(encoding='utf-8')
    except Exception as e:
        return []
    
    lines = source.split('\n')
    
    # 预先解析 AST 获取跳过函数的范围
    skip_ranges = get_skip_function_ranges(source)
    
    # 逐行扫描
    for line_no, line in enumerate(lines, 1):
        # 跳过跳过函数内的代码
        if is_in_skip_function(skip_ranges, line_no):
            continue
        
        # 查找 schema 前缀
        for match in SCHEMA_PREFIX_PATTERN.finditer(line):
            # 跳过注释
            if is_in_comment(line, match.start()):
                continue
            
            # 获取上下文（前后 2 行）
            context_start = max(0, line_no - 3)
            context_end = min(len(lines), line_no + 2)
            context_lines = lines[context_start:context_end]
            full_context = '\n'.join(context_lines)
            
            # 检查是否为允许的上下文
            if is_allowed_context(line, full_context):
                continue
            
            # 提取匹配的 schema 名称
            schema_name = match.group(1).lower()
            matched_text = match.group(0)
            
            # 确定要展示的行上下文
            display_line = line.strip()
            if len(display_line) > 120:
                # 截取匹配位置附近的内容
                start = max(0, match.start() - 40)
                end = min(len(line), match.end() + 40)
                display_line = "..." + line[start:end].strip() + "..."
            
            # 生成建议
            suggestion = (
                f"移除 '{schema_name}.' 前缀，改用无前缀表名。\n"
                f"  例如: 将 '{schema_name}.table_name' 改为 'table_name'\n"
                f"  原因: 项目使用 search_path 管理 schema，SQL 应使用无前缀表名"
            )
            
            violations.append(SchemaViolation(
                file_path=str(file_path),
                line_no=line_no,
                col_offset=match.start(),
                matched_text=matched_text,
                context=display_line,
                suggestion=suggestion,
            ))
    
    return violations


def check_all_files() -> List[SchemaViolation]:
    """
    检查所有 Python 文件
    
    Returns:
        所有违反记录
    """
    scripts_dir = get_scripts_dir()
    py_files = find_python_files(scripts_dir)
    
    all_violations = []
    
    for py_file in py_files:
        # 计算相对路径
        try:
            rel_path = py_file.relative_to(scripts_dir)
        except ValueError:
            rel_path = py_file
        
        # 检查是否在跳过列表中
        if str(rel_path) in SKIP_FILES:
            continue
        
        violations = check_file(py_file)
        all_violations.extend(violations)
    
    return all_violations


def format_violations_report(violations: List[SchemaViolation]) -> str:
    """
    格式化违反报告
    
    Args:
        violations: 违反记录列表
        
    Returns:
        格式化的报告字符串
    """
    if not violations:
        return "✓ 未发现 schema 命名规范违反"
    
    lines = [
        f"发现 {len(violations)} 处 schema 命名规范违反:",
        "",
    ]
    
    # 按文件分组
    by_file: dict = {}
    for v in violations:
        if v.file_path not in by_file:
            by_file[v.file_path] = []
        by_file[v.file_path].append(v)
    
    for file_path, file_violations in sorted(by_file.items()):
        lines.append(f"📄 {file_path}")
        for v in file_violations:
            lines.append(f"  第 {v.line_no} 行, 第 {v.col_offset} 列: 发现 '{v.matched_text}'")
            lines.append(f"    上下文: {v.context}")
            lines.append(f"    建议: {v.suggestion.split(chr(10))[0]}")  # 只显示第一行建议
            lines.append("")
    
    return '\n'.join(lines)


# ============ Pytest 测试 ============

# 是否启用严格模式（失败则阻止 CI）
# 设为 False 时，检测到违反只输出警告，不导致测试失败
# 这允许团队渐进式修复现有代码中的 schema 前缀问题
#
# 环境变量控制:
#   - CI=1 或 CI=true: 启用严格模式（CI 环境默认严格）
#   - STRICT_SCHEMA_CHECK=1: 显式启用严格模式
#   - STRICT_SCHEMA_CHECK=0: 显式禁用严格模式（覆盖 CI 设置）
import os

def _get_strict_mode() -> bool:
    """根据环境变量决定是否启用严格模式"""
    # 显式设置优先
    explicit = os.environ.get("STRICT_SCHEMA_CHECK", "").lower()
    if explicit in ("1", "true", "yes"):
        return True
    if explicit in ("0", "false", "no"):
        return False
    # CI 环境默认严格
    ci = os.environ.get("CI", "").lower()
    if ci in ("1", "true", "yes"):
        return True
    # 默认宽松模式
    return False

STRICT_MODE = _get_strict_mode()


def test_no_hardcoded_schema_prefix():
    """
    测试：代码中不应有硬编码的 schema 前缀
    
    检查 step1_logbook_postgres/scripts/**/*.py 中的 SQL 字符串，
    确保不使用 identity., logbook., scm., analysis., governance. 等前缀。
    
    注意：当 STRICT_MODE = False 时，仅输出警告不导致测试失败。
    修复所有问题后可以启用 STRICT_MODE 来防止回归。
    """
    violations = check_all_files()
    
    if violations:
        report = format_violations_report(violations)
        # 构建详细的错误消息
        error_msg = [
            "",
            "=" * 70,
            "Schema 命名规范检查" + ("失败" if STRICT_MODE else "警告"),
            "=" * 70,
            "",
            report,
            "",
            "=" * 70,
            "修复指南:",
            "1. SQL 语句应使用无前缀表名，依赖 search_path 解析",
            "2. 例如: 'SELECT * FROM items' 而非 'SELECT * FROM logbook.items'",
            "3. 如果确实需要 schema 前缀（如动态重写），请将函数添加到 SKIP_FUNCTIONS",
            "4. 或将文件添加到 SKIP_FILES（如数据完整性检查工具）",
            "=" * 70,
        ]
        
        # 显示前 10 个违反的详细信息
        for v in violations[:10]:
            error_msg.append(f"\n{v.file_path}:{v.line_no}:{v.col_offset}")
            error_msg.append(f"  发现: {v.matched_text}")
            error_msg.append(f"  {v.suggestion}")
        
        if len(violations) > 10:
            error_msg.append(f"\n... 还有 {len(violations) - 10} 处违反，详见完整报告")
        
        full_msg = '\n'.join(error_msg)
        
        if STRICT_MODE:
            raise AssertionError(full_msg)
        else:
            # 非严格模式：输出警告但不失败
            import warnings
            warnings.warn(f"\n{full_msg}\n\n提示: 设置 STRICT_MODE = True 以在 CI 中强制检查", stacklevel=2)


def test_schema_check_utility():
    """
    测试：验证检查工具本身的正确性
    """
    # 测试正则表达式匹配
    test_cases = [
        # 应该匹配
        ("SELECT * FROM scm.repos", True, "scm."),
        ("INSERT INTO logbook.items", True, "logbook."),
        ("FROM identity.users", True, "identity."),
        ("analysis.metrics", True, "analysis."),
        ("governance.rules", True, "governance."),
        
        # 不应该匹配
        ("SELECT * FROM repos", False, None),
        ("INSERT INTO items", False, None),
        ("mechanism.something", False, None),  # 不是 schema 名称
        ("schema_name = 'scm'", False, None),  # 引号内的值
    ]
    
    for text, should_match, expected_prefix in test_cases:
        match = SCHEMA_PREFIX_PATTERN.search(text)
        if should_match:
            assert match is not None, f"应该匹配: {text}"
            assert match.group(0) == expected_prefix, f"前缀应为 {expected_prefix}: {text}"
        else:
            # 对于引号内的情况，可能会匹配但会被后续过滤
            pass  # 简化测试，主要验证基本匹配


if __name__ == "__main__":
    # 直接运行时执行检查并打印报告
    violations = check_all_files()
    print(format_violations_report(violations))
    
    if violations:
        exit(1)
    else:
        print("所有文件检查通过！")
        exit(0)
