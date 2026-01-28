"""
Memory Card 生成器
负责：将输入字段组装为标准 Markdown 格式的记忆卡片，并实现内容裁剪与 SHA 计算

字段（强制）：
- Kind：FACT / PROCEDURE / PITFALL / DECISION / REVIEW_GUIDE / REFLECTION
- Owner：<user_id>
- Module：系统/模块/路径前缀
- Summary：一句话可检索结论
- Details：3–5 条要点（含验证口径）
- Evidence：commit/rev/mr/event_id/patch_sha/uri/hash
- Confidence：high/mid/low
- Visibility：team/private/org
- TTL：long/mid/short（bulk 变更默认 short）

裁剪策略：
- 超长内容截断（默认每字段 500 字符）
- diff/log 仅保留指针（uri）+ sha，不存储原文
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from enum import Enum

logger = logging.getLogger(__name__)


# ======================== 枚举类型 ========================

class MemoryKind(str, Enum):
    """记忆卡片类型"""
    FACT = "FACT"
    PROCEDURE = "PROCEDURE"
    PITFALL = "PITFALL"
    DECISION = "DECISION"
    REVIEW_GUIDE = "REVIEW_GUIDE"
    REFLECTION = "REFLECTION"


class Confidence(str, Enum):
    """置信度"""
    HIGH = "high"
    MID = "mid"
    LOW = "low"


class Visibility(str, Enum):
    """可见性"""
    TEAM = "team"
    PRIVATE = "private"
    ORG = "org"


class TTL(str, Enum):
    """生存周期"""
    LONG = "long"
    MID = "mid"
    SHORT = "short"


# ======================== Evidence 结构 ========================

@dataclass
class Evidence:
    """证据链结构
    
    uri 可解析性要求：必须为可回溯的有效 URI
    - scheme 限 memory://, svn://, git://, https://
    
    hash 校验：sha256 必填，用于验证原文完整性
    """
    uri: str                                    # 资源 URI（memory://、svn://、git://、https://）
    sha256: str                                 # 内容哈希（必填）
    event_id: Optional[str] = None              # 事件 ID
    svn_rev: Optional[str] = None               # SVN 版本号
    git_commit: Optional[str] = None            # Git commit hash
    mr: Optional[str] = None                    # MR/PR 编号
    
    # 允许的 URI scheme
    ALLOWED_SCHEMES = ("memory://", "svn://", "git://", "https://")
    
    def validate(self) -> List[str]:
        """验证 Evidence 有效性，返回错误列表"""
        errors = []
        
        # 验证 URI
        if not self.uri:
            errors.append("Evidence.uri 不能为空")
        elif not any(self.uri.startswith(s) for s in self.ALLOWED_SCHEMES):
            errors.append(f"Evidence.uri scheme 必须为 {self.ALLOWED_SCHEMES} 之一: {self.uri}")
        
        # 验证 SHA256
        if not self.sha256:
            errors.append("Evidence.sha256 不能为空")
        elif not re.match(r"^[a-fA-F0-9]{64}$", self.sha256):
            errors.append(f"Evidence.sha256 格式无效: {self.sha256}")
        
        return errors
    
    def to_markdown_lines(self) -> List[str]:
        """转换为 Markdown 行列表"""
        lines = [f"- uri={self.uri}"]
        lines.append(f"  sha256={self.sha256}")
        
        if self.event_id:
            lines.append(f"  event_id={self.event_id}")
        if self.svn_rev:
            lines.append(f"  svn_rev={self.svn_rev}")
        if self.git_commit:
            lines.append(f"  git_commit={self.git_commit}")
        if self.mr:
            lines.append(f"  mr={self.mr}")
        
        return lines


# ======================== 裁剪配置 ========================

@dataclass
class TrimConfig:
    """裁剪配置"""
    max_summary_length: int = 200       # Summary 最大字符数
    max_detail_length: int = 500        # 单条 Detail 最大字符数
    max_details_count: int = 10         # Details 最大条目数
    max_evidence_count: int = 10        # Evidence 最大条目数
    max_total_length: int = 4000        # 总 Markdown 最大字符数
    
    # diff/log 检测模式
    diff_pattern: re.Pattern = field(default_factory=lambda: re.compile(
        r"^[-+]{3}\s|^@@\s|^diff\s--git|^Index:\s", re.MULTILINE
    ))
    log_pattern: re.Pattern = field(default_factory=lambda: re.compile(
        r"^\[\d{4}-\d{2}-\d{2}|^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}|^[A-Z]+:\s|^\[INFO\]|^\[DEBUG\]|^\[ERROR\]|^\[WARN\]", 
        re.MULTILINE
    ))


# ======================== 裁剪结果 ========================

@dataclass
class TrimResult:
    """裁剪结果"""
    content: str                        # 裁剪后的内容
    was_trimmed: bool                   # 是否发生裁剪
    original_length: int                # 原始长度
    trimmed_length: int                 # 裁剪后长度
    reason: Optional[str] = None        # 裁剪原因


# ======================== Memory Card 结构 ========================

@dataclass
class MemoryCard:
    """记忆卡片
    
    包含生成 Markdown 和计算 payload_sha 的核心逻辑
    """
    kind: Union[MemoryKind, str]
    owner: str
    module: str
    summary: str
    details: List[str]
    evidence: List[Evidence]
    confidence: Union[Confidence, str] = Confidence.MID
    visibility: Union[Visibility, str] = Visibility.TEAM
    ttl: Union[TTL, str] = TTL.MID
    
    # 内部状态
    _trim_config: TrimConfig = field(default_factory=TrimConfig)
    _trim_logs: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        """规范化枚举值"""
        if isinstance(self.kind, str):
            self.kind = MemoryKind(self.kind.upper())
        if isinstance(self.confidence, str):
            self.confidence = Confidence(self.confidence.lower())
        if isinstance(self.visibility, str):
            self.visibility = Visibility(self.visibility.lower())
        if isinstance(self.ttl, str):
            self.ttl = TTL(self.ttl.lower())
    
    def validate(self) -> List[str]:
        """验证卡片有效性，返回错误列表"""
        errors = []
        
        # 验证必填字段
        if not self.owner:
            errors.append("owner 不能为空")
        if not self.module:
            errors.append("module 不能为空")
        if not self.summary:
            errors.append("summary 不能为空")
        if not self.details:
            errors.append("details 不能为空（至少需要一条要点）")
        if not self.evidence:
            errors.append("evidence 不能为空（至少需要一条证据）")
        
        # 验证 Evidence
        for i, ev in enumerate(self.evidence):
            for err in ev.validate():
                errors.append(f"evidence[{i}]: {err}")
        
        return errors
    
    def _trim_text(
        self, 
        text: str, 
        max_length: int, 
        field_name: str
    ) -> TrimResult:
        """裁剪文本到指定长度"""
        original_length = len(text)
        
        if original_length <= max_length:
            return TrimResult(
                content=text,
                was_trimmed=False,
                original_length=original_length,
                trimmed_length=original_length
            )
        
        # 裁剪并添加省略标记
        trimmed = text[:max_length - 20] + "... [内容已截断]"
        reason = f"{field_name} 超长截断: {original_length} -> {len(trimmed)}"
        self._trim_logs.append(reason)
        
        return TrimResult(
            content=trimmed,
            was_trimmed=True,
            original_length=original_length,
            trimmed_length=len(trimmed),
            reason=reason
        )
    
    def _is_diff_content(self, text: str) -> bool:
        """检测是否为 diff 内容"""
        return bool(self._trim_config.diff_pattern.search(text))
    
    def _is_log_content(self, text: str) -> bool:
        """检测是否为日志内容"""
        return bool(self._trim_config.log_pattern.search(text))
    
    def _trim_diff_or_log(
        self, 
        text: str, 
        content_type: str,
        uri: Optional[str] = None,
        sha256: Optional[str] = None
    ) -> str:
        """裁剪 diff/log 内容，仅保留指针与 sha
        
        降级策略：team/private 空间不存储原文内容，仅存储指针（uri）+ hash
        """
        if not uri:
            uri = "memory://local/content"
        if not sha256:
            sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        
        replacement = f"[{content_type} 内容已移除，仅保留指针]\n- uri: {uri}\n- sha256: {sha256}"
        self._trim_logs.append(f"{content_type} 内容替换为指针: uri={uri}, sha256={sha256[:16]}...")
        
        return replacement
    
    def _process_details(self) -> List[str]:
        """处理 details 列表：裁剪超长、处理 diff/log"""
        processed = []
        config = self._trim_config
        
        for i, detail in enumerate(self.details[:config.max_details_count]):
            # 检测 diff/log 内容
            if self._is_diff_content(detail):
                content_sha = hashlib.sha256(detail.encode("utf-8")).hexdigest()
                detail = self._trim_diff_or_log(detail, "diff", sha256=content_sha)
            elif self._is_log_content(detail):
                content_sha = hashlib.sha256(detail.encode("utf-8")).hexdigest()
                detail = self._trim_diff_or_log(detail, "log", sha256=content_sha)
            
            # 裁剪超长内容
            result = self._trim_text(detail, config.max_detail_length, f"details[{i}]")
            processed.append(result.content)
        
        # 记录超出条目数
        if len(self.details) > config.max_details_count:
            self._trim_logs.append(
                f"details 条目数截断: {len(self.details)} -> {config.max_details_count}"
            )
        
        return processed
    
    def to_markdown(self) -> str:
        """生成 Markdown 格式的记忆卡片
        
        Returns:
            符合模板规范的 Markdown 字符串
        """
        self._trim_logs.clear()
        config = self._trim_config
        
        lines = []
        
        # 元数据头
        lines.append(f"[Kind] {self.kind.value}")
        lines.append(f"[Owner] {self.owner}")
        lines.append(f"[Module] {self.module}")
        lines.append(f"[Visibility] {self.visibility.value}")
        lines.append(f"[TTL] {self.ttl.value}")
        lines.append(f"[Confidence] {self.confidence.value}")
        lines.append("")
        
        # Summary（带裁剪）
        summary_result = self._trim_text(self.summary, config.max_summary_length, "summary")
        lines.append("[Summary]")
        lines.append(summary_result.content)
        lines.append("")
        
        # Details（带裁剪和 diff/log 处理）
        processed_details = self._process_details()
        lines.append("[Details]")
        for i, detail in enumerate(processed_details, 1):
            lines.append(f"{i}) {detail}")
        lines.append("")
        
        # Evidence（限制数量）
        lines.append("[Evidence]")
        evidence_list = self.evidence[:config.max_evidence_count]
        for ev in evidence_list:
            lines.extend(ev.to_markdown_lines())
        
        if len(self.evidence) > config.max_evidence_count:
            self._trim_logs.append(
                f"evidence 条目数截断: {len(self.evidence)} -> {config.max_evidence_count}"
            )
        
        # 组装最终内容
        markdown = "\n".join(lines)
        
        # 最终长度检查
        if len(markdown) > config.max_total_length:
            # 强制截断
            markdown = markdown[:config.max_total_length - 50] + "\n\n[... 内容已截断 ...]"
            self._trim_logs.append(
                f"总长度截断: -> {config.max_total_length}"
            )
        
        return markdown
    
    def compute_payload_sha(self) -> str:
        """计算 payload_sha = sha256(payload_md)
        
        用于：
        - 审计日志（governance.write_audit.payload_sha）
        - Outbox 队列（logbook.outbox_memory.payload_sha）
        - 去重检测
        
        Returns:
            SHA256 哈希值（64 字符小写十六进制）
        """
        payload_md = self.to_markdown()
        return hashlib.sha256(payload_md.encode("utf-8")).hexdigest()
    
    def get_trim_logs(self) -> List[str]:
        """获取裁剪日志"""
        return self._trim_logs.copy()


# ======================== 便捷函数 ========================

def create_memory_card(
    kind: str,
    owner: str,
    module: str,
    summary: str,
    details: List[str],
    evidence: List[Dict[str, Any]],
    confidence: str = "mid",
    visibility: str = "team",
    ttl: str = "mid",
    trim_config: Optional[TrimConfig] = None
) -> MemoryCard:
    """创建记忆卡片的便捷函数
    
    Args:
        kind: 类型 (FACT/PROCEDURE/PITFALL/DECISION/REVIEW_GUIDE/REFLECTION)
        owner: 所有者用户 ID
        module: 模块/路径前缀
        summary: 一句话结论
        details: 要点列表
        evidence: 证据列表，每项为 dict 包含 uri, sha256 等字段
        confidence: 置信度 (high/mid/low)
        visibility: 可见性 (team/private/org)
        ttl: 生存周期 (long/mid/short)
        trim_config: 裁剪配置（可选）
    
    Returns:
        MemoryCard 实例
    """
    # 转换 evidence
    evidence_list = []
    for ev_dict in evidence:
        ev = Evidence(
            uri=ev_dict.get("uri", ""),
            sha256=ev_dict.get("sha256", ""),
            event_id=ev_dict.get("event_id"),
            svn_rev=ev_dict.get("svn_rev"),
            git_commit=ev_dict.get("git_commit"),
            mr=ev_dict.get("mr"),
        )
        evidence_list.append(ev)
    
    card = MemoryCard(
        kind=kind,
        owner=owner,
        module=module,
        summary=summary,
        details=details,
        evidence=evidence_list,
        confidence=confidence,
        visibility=visibility,
        ttl=ttl,
    )
    
    if trim_config:
        card._trim_config = trim_config
    
    return card


def generate_memory_markdown(
    kind: str,
    owner: str,
    module: str,
    summary: str,
    details: List[str],
    evidence: List[Dict[str, Any]],
    confidence: str = "mid",
    visibility: str = "team",
    ttl: str = "mid",
) -> tuple[str, str]:
    """生成记忆卡片 Markdown 并计算 SHA
    
    Args:
        （同 create_memory_card）
    
    Returns:
        (payload_md, payload_sha) 元组
    """
    card = create_memory_card(
        kind=kind,
        owner=owner,
        module=module,
        summary=summary,
        details=details,
        evidence=evidence,
        confidence=confidence,
        visibility=visibility,
        ttl=ttl,
    )
    
    payload_md = card.to_markdown()
    payload_sha = card.compute_payload_sha()
    
    return payload_md, payload_sha


def compute_content_sha(content: str) -> str:
    """计算内容的 SHA256 哈希
    
    Args:
        content: 任意文本内容
    
    Returns:
        SHA256 哈希值（64 字符小写十六进制）
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def trim_diff_content(diff_text: str, uri: Optional[str] = None) -> tuple[str, str]:
    """裁剪 diff 内容，返回指针文本和原始 SHA
    
    Args:
        diff_text: diff 原始内容
        uri: 可选的 URI 指针
    
    Returns:
        (trimmed_text, original_sha) 元组
    """
    original_sha = compute_content_sha(diff_text)
    if not uri:
        uri = f"memory://diff/{original_sha[:16]}"
    
    trimmed = f"[diff 内容已移除，仅保留指针]\n- uri: {uri}\n- sha256: {original_sha}"
    return trimmed, original_sha


def trim_log_content(log_text: str, uri: Optional[str] = None) -> tuple[str, str]:
    """裁剪 log 内容，返回指针文本和原始 SHA
    
    Args:
        log_text: log 原始内容
        uri: 可选的 URI 指针
    
    Returns:
        (trimmed_text, original_sha) 元组
    """
    original_sha = compute_content_sha(log_text)
    if not uri:
        uri = f"memory://log/{original_sha[:16]}"
    
    trimmed = f"[log 内容已移除，仅保留指针]\n- uri: {uri}\n- sha256: {original_sha}"
    return trimmed, original_sha
