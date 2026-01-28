import { env } from "../../core/cfg";
import crypto from "crypto";

/**
 * 认证拒绝原因枚举（便于审计/观测）
 */
export enum AuthRejectReason {
    NO_KEY_PROVIDED = "no_key_provided",
    INVALID_KEY = "invalid_key",
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded",
}

const rate_limit_store = new Map<
    string,
    { count: number; reset_time: number }
>();
const auth_config = {
    api_key: env.api_key, // 向后兼容
    api_keys: env.api_keys, // 多 key 支持
    api_key_header: "x-api-key",
    rate_limit_enabled: env.rate_limit_enabled,
    rate_limit_window_ms: env.rate_limit_window_ms,
    rate_limit_max_requests: env.rate_limit_max_requests,
    public_endpoints: [
        "/health",
        "/api/system/health",
        "/api/system/stats",
        "/dashboard/health",
    ],
};

/**
 * 计算 key 的 hash 前缀（用于日志，隐藏完整 key）
 */
function get_key_hash_prefix(key: string, length: number = 8): string {
    return crypto.createHash("sha256").update(key).digest("hex").slice(0, length);
}

function is_public_endpoint(path: string): boolean {
    return auth_config.public_endpoints.some(
        (e) => path === e || path.startsWith(e),
    );
}

function extract_api_key(req: any): string | null {
    const x_api_key = req.headers[auth_config.api_key_header];
    if (x_api_key) return x_api_key;
    const auth_header = req.headers["authorization"];
    if (auth_header) {
        if (auth_header.startsWith("Bearer ")) return auth_header.slice(7);
        if (auth_header.startsWith("ApiKey ")) return auth_header.slice(7);
    }
    return null;
}

/**
 * 对单个 key 进行时间安全比较
 */
function timing_safe_compare(provided: string, expected: string): boolean {
    if (!provided || !expected) return false;
    // 长度不同时仍进行比较以防止时序攻击
    const provided_buf = Buffer.from(provided);
    const expected_buf = Buffer.from(expected);
    if (provided_buf.length !== expected_buf.length) {
        // 使用 expected 与自身比较，保持时间一致性
        crypto.timingSafeEqual(expected_buf, expected_buf);
        return false;
    }
    return crypto.timingSafeEqual(provided_buf, expected_buf);
}

/**
 * 验证 API key，支持多 key 匹配
 * 对每个候选 key 做 timingSafeEqual，任一匹配即通过
 */
function validate_api_key(provided: string): boolean {
    const candidates = auth_config.api_keys;
    if (!candidates || candidates.length === 0) {
        // 无配置 key 时回退到单 key 检查（向后兼容）
        if (auth_config.api_key) {
            return timing_safe_compare(provided, auth_config.api_key);
        }
        return false;
    }
    // 遍历所有候选 key，任一匹配即通过
    for (const candidate of candidates) {
        if (timing_safe_compare(provided, candidate)) {
            return true;
        }
    }
    return false;
}

/**
 * 检查是否有任何有效的 API key 配置
 */
function has_configured_keys(): boolean {
    return (
        (auth_config.api_keys && auth_config.api_keys.length > 0) ||
        (!!auth_config.api_key && auth_config.api_key !== "")
    );
}

function check_rate_limit(client_id: string): {
    allowed: boolean;
    remaining: number;
    reset_time: number;
} {
    if (!auth_config.rate_limit_enabled)
        return { allowed: true, remaining: -1, reset_time: -1 };
    const now = Date.now();
    const data = rate_limit_store.get(client_id);
    if (!data || now >= data.reset_time) {
        const new_data = {
            count: 1,
            reset_time: now + auth_config.rate_limit_window_ms,
        };
        rate_limit_store.set(client_id, new_data);
        return {
            allowed: true,
            remaining: auth_config.rate_limit_max_requests - 1,
            reset_time: new_data.reset_time,
        };
    }
    data.count++;
    rate_limit_store.set(client_id, data);
    const remaining = auth_config.rate_limit_max_requests - data.count;
    return {
        allowed: data.count <= auth_config.rate_limit_max_requests,
        remaining: Math.max(0, remaining),
        reset_time: data.reset_time,
    };
}

function get_client_id(req: any, api_key: string | null): string {
    if (api_key)
        return crypto
            .createHash("sha256")
            .update(api_key)
            .digest("hex")
            .slice(0, 16);
    return req.ip || req.connection.remoteAddress || "unknown";
}

export function authenticate_api_request(req: any, res: any, next: any) {
    const path = req.path || req.url;
    if (is_public_endpoint(path)) return next();
    if (!has_configured_keys()) {
        console.warn("[AUTH] No API key configured");
        return next();
    }
    const provided = extract_api_key(req);
    if (!provided) {
        console.warn(
            `[AUTH] Rejected: ${AuthRejectReason.NO_KEY_PROVIDED} path=${path}`,
        );
        return res.status(401).json({
            error: "authentication_required",
            message: "API key required",
            reason: AuthRejectReason.NO_KEY_PROVIDED,
        });
    }
    if (!validate_api_key(provided)) {
        console.warn(
            `[AUTH] Rejected: ${AuthRejectReason.INVALID_KEY} key_hash=${get_key_hash_prefix(provided)}... path=${path}`,
        );
        return res.status(403).json({
            error: "invalid_api_key",
            reason: AuthRejectReason.INVALID_KEY,
        });
    }
    const client_id = get_client_id(req, provided);
    const rl = check_rate_limit(client_id);
    if (auth_config.rate_limit_enabled) {
        res.setHeader("X-RateLimit-Limit", auth_config.rate_limit_max_requests);
        res.setHeader("X-RateLimit-Remaining", rl.remaining);
        res.setHeader("X-RateLimit-Reset", Math.floor(rl.reset_time / 1000));
    }
    if (!rl.allowed) {
        console.warn(
            `[AUTH] Rejected: ${AuthRejectReason.RATE_LIMIT_EXCEEDED} client_id=${client_id} path=${path}`,
        );
        return res.status(429).json({
            error: "rate_limit_exceeded",
            reason: AuthRejectReason.RATE_LIMIT_EXCEEDED,
            retry_after: Math.ceil((rl.reset_time - Date.now()) / 1000),
        });
    }
    next();
}

export function log_authenticated_request(req: any, res: any, next: any) {
    const key = extract_api_key(req);
    if (key)
        console.log(
            `[AUTH] ${req.method} ${req.path} [${get_key_hash_prefix(key)}...]`,
        );
    next();
}

setInterval(
    () => {
        const now = Date.now();
        for (const [id, data] of rate_limit_store.entries())
            if (now >= data.reset_time) rate_limit_store.delete(id);
    },
    5 * 60 * 1000,
);

// 导出内部函数用于测试
export const __testing = {
    timing_safe_compare,
    get_key_hash_prefix,
    has_configured_keys,
    extract_api_key,
    auth_config,
};
