#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
artifact_cli.py - 制品管理 CLI 工具

统一的制品存储管理入口，命令转发到以下模块：
- engram_step1.artifact_store: 核心存储抽象层
- artifact_audit.py: 制品完整性审计
- artifact_gc.py: 制品垃圾回收

输出格式: JSON (符合 docs/02_tools_contract.md)
    成功: {"ok": true, ...}
    失败: {"ok": false, "code": "<ERROR_CODE>", "message": "...", "detail": {...}}

错误码:
    PATH_TRAVERSAL (exit_code=2): 路径穿越攻击检测
    NOT_FOUND (exit_code=11): 制品不存在
    CHECKSUM_MISMATCH (exit_code=12): SHA256 校验失败
    FILE_EXISTS (exit_code=12): 文件已存在且覆盖被拒绝
    WRITE_DISABLED (exit_code=13): 制品存储只读模式，写入被禁止

只读模式配置:
    通过配置文件 [artifacts.policy].read_only = true 或
    环境变量 ENGRAM_ARTIFACTS_READ_ONLY=true 启用只读模式

命令:
    write   - 写入制品到 ArtifactStore
    read    - 读取制品内容
    exists  - 检查制品是否存在
    delete  - 删除制品
    audit   - 审计制品完整性（转发到 artifact_audit.py）
    gc      - 垃圾回收未引用的制品（转发到 artifact_gc.py）
    migrate - 跨后端迁移制品（转发到 artifact_migrate.py）

使用示例:
    # 写入制品
    python artifact_cli.py write --path scm/test.diff --content "diff content"
    
    # 读取制品
    python artifact_cli.py read --path scm/test.diff
    python artifact_cli.py read --path scm/test.diff --json  # 返回元数据
    
    # 检查制品是否存在
    python artifact_cli.py exists --path scm/test.diff
    
    # 删除制品
    python artifact_cli.py delete --path scm/test.diff
    python artifact_cli.py delete --path scm/test.diff --force  # 忽略不存在错误
    
    # 审计制品完整性
    python artifact_cli.py audit --table patch_blobs --limit 100
    python artifact_cli.py audit --table all --sample-rate 0.1
    
    # 垃圾回收
    python artifact_cli.py gc --prefix scm/  # dry-run 模式
    python artifact_cli.py gc --prefix scm/ --delete  # 执行删除
    python artifact_cli.py gc --prefix scm/ --older-than-days 30 --delete
    
    # 迁移制品
    python artifact_cli.py migrate --source-backend local --target-backend object

等价于:
    python step1_cli.py artifacts <command> [options]
"""

import sys
import os

# 添加当前目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from step1_cli import artifacts_app


def main():
    """
    主入口 - 直接运行 artifacts 子命令组
    
    所有命令转发到:
    - write/read/exists/delete: engram_step1.artifact_store
    - audit: artifact_audit.ArtifactAuditor
    - gc: artifact_gc.run_gc
    - migrate: artifact_migrate.run_migration
    """
    artifacts_app()


if __name__ == "__main__":
    main()
