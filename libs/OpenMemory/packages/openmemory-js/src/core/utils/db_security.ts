/**
 * 数据库安全工具模块
 * 
 * 包含数据库名称验证、标识符转义等安全相关的共享函数
 * 供 db.ts 和 migrate.ts 共同使用
 */

/**
 * 数据库名称白名单正则：仅允许小写字母、数字、下划线，且以字母开头
 * 长度限制 1-63 字符（PostgreSQL 限制）
 * 与 Step1 db_migrate.py 中的 validate_db_name 保持一致
 */
export const DB_NAME_PATTERN = /^[a-z][a-z0-9_]{0,62}$/;

/**
 * 校验数据库名称是否符合安全命名规范
 * 
 * 命名规则（白名单）：
 * - 仅允许小写字母、数字、下划线
 * - 必须以小写字母开头
 * - 长度 1-63 字符
 * 
 * @param db_name 待校验的数据库名称
 * @returns [valid, error_message] - valid 为 true 表示合法
 * 
 * @example
 * // 合法示例
 * validateDbName("openmemory")      // [true, ""]
 * validateDbName("project_a_db")    // [true, ""]
 * validateDbName("test123")         // [true, ""]
 * 
 * @example
 * // 非法示例
 * validateDbName("")                // [false, "数据库名称不能为空"]
 * validateDbName("123abc")          // [false, "...不符合命名规范..."]
 * validateDbName("Project-DB")      // [false, "...不符合命名规范..."]
 */
export function validateDbName(db_name: string): [boolean, string] {
    if (!db_name) {
        return [false, "数据库名称不能为空"];
    }
    
    if (db_name.length > 63) {
        return [false, `数据库名称过长（最大 63 字符）：${db_name.length} 字符`];
    }
    
    if (!DB_NAME_PATTERN.test(db_name)) {
        return [false, `数据库名称 '${db_name}' 不符合命名规范：仅允许小写字母、数字、下划线，且必须以小写字母开头`];
    }
    
    return [true, ""];
}

/**
 * 转义 PostgreSQL 标识符（使用双引号）
 * 防止 SQL 注入和关键字冲突
 * 
 * @param identifier 标识符名称
 * @returns 双引号包裹的安全标识符
 * 
 * @example
 * quoteIdentifier("user")        // '"user"'
 * quoteIdentifier("my_table")    // '"my_table"'
 * quoteIdentifier('say"hi')      // '"say""hi"'  // 内部双引号被转义
 */
export function quoteIdentifier(identifier: string): string {
    // 双引号内的双引号需要转义为两个双引号
    return `"${identifier.replace(/"/g, '""')}"`;
}

/**
 * 校验 PostgreSQL schema 名称是否安全
 * 
 * 禁止使用 public schema（安全约束）：
 * 1. public schema 是 PostgreSQL 默认 schema，可能包含其他应用的表
 * 2. 无法使用 pg_dump --schema 进行隔离备份
 * 3. DROP SCHEMA public CASCADE 会破坏整个数据库
 * 
 * @param schema schema 名称
 * @returns { ok: boolean, message: string }
 * 
 * @example
 * validateSchemaName("openmemory")  // { ok: true, message: "" }
 * validateSchemaName("public")      // { ok: false, message: "..." }
 */
export function validateSchemaName(schema: string): { ok: boolean; message: string } {
    if (schema === "public") {
        return {
            ok: false,
            message: `[FATAL] OM_PG_SCHEMA=public 是禁止的配置！

原因：
  1. public schema 是 PostgreSQL 默认 schema，可能包含其他应用的表
  2. 无法使用 pg_dump --schema 进行隔离备份
  3. DROP SCHEMA public CASCADE 会破坏整个数据库

解决方案：
  设置环境变量 OM_PG_SCHEMA 为非 public 值，例如：
  - OM_PG_SCHEMA=openmemory
  - OM_PG_SCHEMA=\${PROJECT_KEY}_openmemory

参考 docker-compose.unified.yml 中的配置：
  OM_PG_SCHEMA: \${PROJECT_KEY:-default}_openmemory
`,
        };
    }
    
    return { ok: true, message: "" };
}
