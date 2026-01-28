#!/usr/bin/env bash
# ==============================================================================
# OpenMemory 补丁生成与管理脚本
# ==============================================================================
#
# 用法: ./scripts/generate_om_patches.sh [generate|apply|verify|verify-apply-dry-run|bundle-hash|backfill]
#
# ==============================================================================
# 路径 B 策略说明 (2026-01 更新)
# ==============================================================================
# 
# 当前项目采用"路径 B（不强制 patch 文件）"策略：
#
# 1. patch 文件的生成是可选的，用于记录和审计目的
# 2. CI/Nightly 默认不强制要求 patch 文件存在（OPENMEMORY_PATCH_FILES_REQUIRED=0）
# 3. 严格校验仅在以下场景启用：
#    - upstream_ref 变更时（CI 自动启用）
#    - release 分支准备时（手动触发）
#    - 明确需要 patch 审计时
#
# patch 文件生成前提条件：
#   - 需要上游基线文件: archives/openmemory-base-<version>/
#   - 运行: ./scripts/generate_om_patches.sh generate v1.3.0
#
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PATCHES_DIR="$PROJECT_ROOT/patches/openmemory"
PATCHES_JSON="$PROJECT_ROOT/openmemory_patches.json"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# 检查依赖
check_deps() {
    local deps=("diff" "patch" "sha256sum" "jq")
    for dep in "${deps[@]}"; do
        if ! command -v "$dep" &> /dev/null; then
            log_error "缺少依赖: $dep"
            exit 1
        fi
    done
}

# 生成单个补丁
# 用法: generate_patch <base_file> <patched_file> <output_patch> <repo_relative_path>
# repo_relative_path: 用于 patch 头部的路径，如 "libs/OpenMemory/packages/openmemory-js/src/core/migrate.ts"
# 生成的 patch 使用 a/ 和 b/ 前缀，可用 patch -p1 应用
generate_patch() {
    local base_file="$1"
    local patched_file="$2"
    local output_patch="$3"
    local repo_rel_path="${4:-}"

    if [[ ! -f "$base_file" ]]; then
        log_error "基线文件不存在: $base_file"
        return 1
    fi

    if [[ ! -f "$patched_file" ]]; then
        log_error "补丁后文件不存在: $patched_file"
        return 1
    fi

    mkdir -p "$(dirname "$output_patch")"
    
    # 使用 diff -u 生成统一格式补丁
    # 使用 --label 选项设置 repo 相对路径，避免包含 archives/openmemory-base-* 的绝对路径
    # 格式: a/<repo_path> 和 b/<repo_path>，支持 patch -p1 应用
    # diff 返回 1 表示有差异，这是正常的
    if [[ -n "$repo_rel_path" ]]; then
        diff -u --label "a/$repo_rel_path" --label "b/$repo_rel_path" \
            "$base_file" "$patched_file" > "$output_patch" || true
    else
        diff -u "$base_file" "$patched_file" > "$output_patch" || true
    fi
    
    if [[ -s "$output_patch" ]]; then
        log_info "已生成补丁: $output_patch"
        sha256sum "$output_patch" | awk '{print $1}'
    else
        log_warn "无差异，跳过: $output_patch"
        rm -f "$output_patch"
    fi
}

# 应用单个补丁
# 用法: apply_patch <patch_file>
# 注意: 生成的 patch 使用 a/b 前缀格式，需使用 -p1 应用
#       必须在项目根目录 (PROJECT_ROOT) 执行
apply_patch() {
    local patch_file="$1"

    if [[ ! -f "$patch_file" ]]; then
        log_error "补丁文件不存在: $patch_file"
        return 1
    fi

    # 先尝试 dry-run (-p1 匹配生成规则中的 a/b 前缀)
    if patch --dry-run -p1 -d "$PROJECT_ROOT" < "$patch_file" &> /dev/null; then
        patch -p1 -d "$PROJECT_ROOT" < "$patch_file"
        log_info "已应用补丁: $patch_file"
    else
        log_error "补丁无法应用 (可能已应用或冲突): $patch_file"
        return 1
    fi
}

# 验证补丁文件 SHA256
verify_patches() {
    log_info "验证补丁文件..."
    local all_valid=true

    for category in A B C; do
        local category_dir="$PATCHES_DIR/$category"
        if [[ -d "$category_dir" ]]; then
            # 使用 find 避免 glob 空匹配问题
            while IFS= read -r -d '' patch_file; do
                local sha256
                sha256=$(sha256sum "$patch_file" | awk '{print $1}')
                echo "$sha256  $patch_file"
            done < <(find "$category_dir" -maxdepth 1 -name "*.patch" -type f -print0 2>/dev/null | sort -z)
        fi
    done

    log_info "验证完成"
}

# 计算 bundle SHA256 (所有补丁文件的联合哈希)
compute_bundle_hash() {
    local all_hashes=""
    
    for category in A B C; do
        local category_dir="$PATCHES_DIR/$category"
        if [[ -d "$category_dir" ]]; then
            for patch_file in $(find "$category_dir" -name "*.patch" -type f | sort); do
                local hash
                hash=$(sha256sum "$patch_file" | awk '{print $1}')
                all_hashes="${all_hashes}${hash}"
            done
        fi
    done

    if [[ -n "$all_hashes" ]]; then
        local bundle_hash
        bundle_hash=$(echo -n "$all_hashes" | sha256sum | awk '{print $1}')
        echo "$bundle_hash"
        log_info "Bundle SHA256: $bundle_hash"
    else
        log_warn "未找到任何补丁文件"
    fi
}

# 从 base 快照批量生成所有补丁
# 需要: archives/openmemory-base-v1.3.0/ 目录包含上游原始文件
# 生成的 patch 使用 repo 相对路径 (a/libs/... b/libs/...)，可用 patch -p1 应用
generate_all_patches() {
    local base_version="${1:-v1.3.0}"
    local base_dir="$PROJECT_ROOT/archives/openmemory-base-$base_version"

    if [[ ! -d "$base_dir" ]]; then
        log_error "基线目录不存在: $base_dir"
        log_info "请先下载上游源码到: $base_dir"
        log_info "例如: git clone --depth 1 --branch $base_version https://github.com/CaviraOSS/OpenMemory.git $base_dir"
        exit 1
    fi

    log_info "从基线 $base_version 生成补丁..."

    # 从 patches.json 读取文件列表并生成
    local files
    files=$(jq -r '.patches[].file' "$PATCHES_JSON")

    for file in $files; do
        local rel_path="${file#libs/OpenMemory/}"
        local base_file="$base_dir/$rel_path"
        local patched_file="$PROJECT_ROOT/$file"

        if [[ -f "$base_file" ]] && [[ -f "$patched_file" ]]; then
            local filename
            filename=$(basename "$file" .ts)
            local output="$PATCHES_DIR/COMBINED/${filename}.patch"
            # 传递 repo 相对路径，生成可用 patch -p1 应用的补丁
            generate_patch "$base_file" "$patched_file" "$output" "$file"
        else
            log_warn "跳过 $file (base 或 patched 文件不存在)"
        fi
    done

    # 计算并更新 bundle hash
    local bundle_hash
    bundle_hash=$(compute_bundle_hash)
    if [[ -n "$bundle_hash" ]]; then
        # 使用 jq 更新 JSON (如果可用)
        if command -v jq &> /dev/null; then
            local tmp_json
            tmp_json=$(mktemp)
            jq --arg hash "$bundle_hash" '.summary.bundle_sha256 = $hash' "$PATCHES_JSON" > "$tmp_json"
            mv "$tmp_json" "$PATCHES_JSON"
            log_info "已更新 bundle_sha256 到 $PATCHES_JSON"
        fi
    fi
}

# 应用所有补丁 (按分类顺序: A -> B -> C)
apply_all_patches() {
    log_info "应用所有补丁 (A -> B -> C)..."
    
    for category in A B C; do
        local category_dir="$PATCHES_DIR/$category"
        if [[ -d "$category_dir" ]]; then
            for patch_file in $(find "$category_dir" -name "*.patch" -type f | sort); do
                apply_patch "$patch_file" || true
            done
        fi
    done

    log_info "补丁应用完成"
}

# 验证所有补丁的 dry-run 应用
# 对每个 patch 文件执行 patch --dry-run -p1，汇总失败列表
verify_apply_dry_run() {
    log_info "验证所有补丁 dry-run 应用..."
    
    local failed_patches=()
    local success_count=0
    local total_count=0

    for category in A B C COMBINED; do
        local category_dir="$PATCHES_DIR/$category"
        if [[ -d "$category_dir" ]]; then
            while IFS= read -r -d '' patch_file; do
                ((total_count++))
                if patch --dry-run -p1 -d "$PROJECT_ROOT" < "$patch_file" &> /dev/null; then
                    ((success_count++))
                    log_info "[OK] $(basename "$patch_file")"
                else
                    failed_patches+=("$patch_file")
                    log_error "[FAIL] $(basename "$patch_file")"
                fi
            done < <(find "$category_dir" -maxdepth 1 -name "*.patch" -type f -print0 2>/dev/null | sort -z)
        fi
    done

    echo ""
    log_info "========== Dry-run 验证结果 =========="
    log_info "总计: $total_count 个补丁"
    log_info "成功: $success_count 个"
    log_info "失败: ${#failed_patches[@]} 个"

    if [[ ${#failed_patches[@]} -gt 0 ]]; then
        echo ""
        log_error "失败的补丁列表:"
        for pf in "${failed_patches[@]}"; do
            echo "  - $pf"
        done
        echo ""
        log_warn "提示: 可使用 'patch --dry-run -p1 < <patch_file>' 查看详细错误"
        return 1
    else
        log_info "所有补丁均可成功应用"
        return 0
    fi
}

# 回填补丁 SHA256 到 openmemory_patches.json
# 遍历所有 patch_file，计算 sha256 写回 patch_sha256，并写入 summary.bundle_sha256
backfill_patch_hashes() {
    log_info "回填补丁 SHA256..."

    if [[ ! -f "$PATCHES_JSON" ]]; then
        log_error "配置文件不存在: $PATCHES_JSON"
        exit 1
    fi

    local tmp_json
    tmp_json=$(mktemp)
    cp "$PATCHES_JSON" "$tmp_json"

    local updated_count=0
    local missing_count=0

    # 遍历所有 patches[].changes[].patch_file
    local patch_files
    patch_files=$(jq -r '.patches[].changes[].patch_file // empty' "$PATCHES_JSON")

    for patch_file in $patch_files; do
        local full_path="$PROJECT_ROOT/$patch_file"
        
        if [[ -f "$full_path" ]]; then
            local sha256
            sha256=$(sha256sum "$full_path" | awk '{print $1}')
            
            # 使用 jq 更新对应的 patch_sha256
            # 找到 patch_file 匹配的条目并更新其 patch_sha256
            jq --arg pf "$patch_file" --arg hash "$sha256" '
                .patches |= map(
                    .changes |= map(
                        if .patch_file == $pf then .patch_sha256 = $hash else . end
                    )
                )
            ' "$tmp_json" > "${tmp_json}.new"
            mv "${tmp_json}.new" "$tmp_json"
            
            log_info "已更新: $patch_file -> ${sha256:0:16}..."
            ((updated_count++))
        else
            log_warn "补丁文件不存在: $patch_file"
            ((missing_count++))
        fi
    done

    # 计算并写入 bundle_sha256
    local bundle_hash
    bundle_hash=$(compute_bundle_hash)
    if [[ -n "$bundle_hash" ]]; then
        jq --arg hash "$bundle_hash" '.summary.bundle_sha256 = $hash' "$tmp_json" > "${tmp_json}.new"
        mv "${tmp_json}.new" "$tmp_json"
        log_info "已更新 bundle_sha256: ${bundle_hash:0:16}..."
    fi

    # 写回原文件
    mv "$tmp_json" "$PATCHES_JSON"

    log_info "回填完成: 更新 $updated_count 个，缺失 $missing_count 个"
}

# 主入口
main() {
    check_deps

    local command="${1:-help}"

    case "$command" in
        generate)
            generate_all_patches "${2:-v1.3.0}"
            ;;
        apply)
            apply_all_patches
            ;;
        verify)
            verify_patches
            ;;
        verify-apply-dry-run)
            verify_apply_dry_run
            ;;
        bundle-hash)
            compute_bundle_hash
            ;;
        backfill)
            backfill_patch_hashes
            ;;
        help|--help|-h)
            echo "OpenMemory 补丁管理脚本"
            echo ""
            echo "用法: $0 <command> [options]"
            echo ""
            echo "命令:"
            echo "  generate [version]    从 base 快照生成所有补丁 (默认: v1.3.0)"
            echo "  apply                 应用所有补丁到当前工作目录"
            echo "  verify                验证补丁文件 SHA256"
            echo "  verify-apply-dry-run  对所有补丁执行 dry-run 验证，汇总失败列表"
            echo "  bundle-hash           计算所有补丁的联合 SHA256"
            echo "  backfill              回填补丁 SHA256 到 openmemory_patches.json"
            echo "  help                  显示此帮助信息"
            echo ""
            echo "示例:"
            echo "  $0 generate v1.3.0       # 基于 v1.3.0 生成补丁"
            echo "  $0 verify                # 验证补丁完整性"
            echo "  $0 verify-apply-dry-run  # dry-run 检查所有补丁是否可应用"
            echo "  $0 backfill              # 回填现有 patch 文件的 SHA256"
            echo ""
            echo "应用补丁说明:"
            echo "  apply 命令将按顺序应用 A -> B -> C 类别的补丁"
            echo "  生成的补丁使用 a/b 前缀格式，必须使用 patch -p1 应用"
            echo "  示例: cd <project_root> && patch -p1 < patches/openmemory/A/migrate-001-db-name-validation.patch"
            echo ""
            echo "路径 B 策略说明 (2026-01):"
            echo "  当前项目采用非强制 patch 文件策略，patch 文件用于审计和记录目的"
            echo "  CI 默认不强制要求 patch 文件存在 (OPENMEMORY_PATCH_FILES_REQUIRED=0)"
            echo "  严格校验仅在 upstream_ref 变更或 release 分支准备时启用"
            ;;
        *)
            log_error "未知命令: $command"
            echo "使用 '$0 help' 查看帮助"
            exit 1
            ;;
    esac
}

main "$@"
