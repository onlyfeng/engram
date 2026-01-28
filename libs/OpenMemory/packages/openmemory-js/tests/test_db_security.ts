/**
 * db_security.ts 共享安全工具模块的单元测试
 * 
 * 测试内容：
 * - validateDbName: 数据库名称验证
 * - quoteIdentifier: SQL 标识符转义
 * - validateSchemaName: schema 名称验证
 * 
 * 运行方式:
 *   npx ts-node tests/test_db_security.ts
 */

import {
    validateDbName,
    quoteIdentifier,
    validateSchemaName,
    DB_NAME_PATTERN,
} from "../src/core/utils/db_security";

const log = (msg: string) => console.log(`[TEST] ${msg}`);
const logOk = (msg: string) => console.log(`[TEST] ✓ ${msg}`);
const logFail = (msg: string) => console.error(`[TEST] ✗ ${msg}`);

interface TestResult {
    passed: boolean;
    message: string;
}

/**
 * 测试 validateDbName 函数
 */
function testValidateDbName(): TestResult {
    log("Testing validateDbName...");
    
    // 合法名称
    const validNames = [
        "openmemory",
        "project_a_db",
        "test123",
        "a",
        "a_b_c_123",
        "x".repeat(63),  // 最大长度
    ];
    
    for (const name of validNames) {
        const [valid, error] = validateDbName(name);
        if (!valid) {
            return { passed: false, message: `Expected "${name}" to be valid, but got error: ${error}` };
        }
    }
    
    // 非法名称
    const invalidNames = [
        { name: "", reason: "空字符串" },
        { name: "123abc", reason: "以数字开头" },
        { name: "Project-DB", reason: "包含大写和连字符" },
        { name: "my-database", reason: "包含连字符" },
        { name: "My_Table", reason: "包含大写字母" },
        { name: "_hidden", reason: "以下划线开头" },
        { name: "x".repeat(64), reason: "超过63字符" },
        { name: "test db", reason: "包含空格" },
        { name: "test;drop", reason: "包含分号（SQL注入尝试）" },
    ];
    
    for (const { name, reason } of invalidNames) {
        const [valid, error] = validateDbName(name);
        if (valid) {
            return { passed: false, message: `Expected "${name}" to be invalid (${reason}), but it passed` };
        }
    }
    
    return { passed: true, message: "validateDbName passed all test cases" };
}

/**
 * 测试 quoteIdentifier 函数
 */
function testQuoteIdentifier(): TestResult {
    log("Testing quoteIdentifier...");
    
    const testCases = [
        { input: "user", expected: '"user"' },
        { input: "my_table", expected: '"my_table"' },
        { input: "SELECT", expected: '"SELECT"' },  // SQL 关键字
        { input: 'say"hi', expected: '"say""hi"' },  // 内部双引号
        { input: 'a""b', expected: '"a""""b"' },  // 多个双引号
        { input: "", expected: '""' },  // 空字符串
        { input: "table-name", expected: '"table-name"' },
        { input: "123", expected: '"123"' },
    ];
    
    for (const { input, expected } of testCases) {
        const result = quoteIdentifier(input);
        if (result !== expected) {
            return { 
                passed: false, 
                message: `quoteIdentifier("${input}") = "${result}", expected "${expected}"` 
            };
        }
    }
    
    return { passed: true, message: "quoteIdentifier passed all test cases" };
}

/**
 * 测试 validateSchemaName 函数
 */
function testValidateSchemaName(): TestResult {
    log("Testing validateSchemaName...");
    
    // 合法 schema 名称
    const validSchemas = [
        "openmemory",
        "project_a_openmemory",
        "tenant_123",
        "my_custom_schema",
    ];
    
    for (const schema of validSchemas) {
        const result = validateSchemaName(schema);
        if (!result.ok) {
            return { passed: false, message: `Expected schema "${schema}" to be valid, but got: ${result.message}` };
        }
    }
    
    // 非法 schema 名称（public 被禁止）
    const result = validateSchemaName("public");
    if (result.ok) {
        return { passed: false, message: 'Expected "public" schema to be rejected' };
    }
    
    // 验证错误消息包含关键信息
    if (!result.message.includes("禁止") && !result.message.includes("FATAL")) {
        return { passed: false, message: 'Error message should contain warning about forbidden public schema' };
    }
    
    return { passed: true, message: "validateSchemaName passed all test cases" };
}

/**
 * 测试 DB_NAME_PATTERN 正则表达式
 */
function testDbNamePattern(): TestResult {
    log("Testing DB_NAME_PATTERN regex...");
    
    const matchCases = [
        "a",
        "abc",
        "a123",
        "a_b",
        "openmemory",
    ];
    
    const noMatchCases = [
        "1abc",
        "ABC",
        "_abc",
        "a-b",
        "",
    ];
    
    for (const name of matchCases) {
        if (!DB_NAME_PATTERN.test(name)) {
            return { passed: false, message: `Pattern should match "${name}"` };
        }
    }
    
    for (const name of noMatchCases) {
        if (DB_NAME_PATTERN.test(name)) {
            return { passed: false, message: `Pattern should NOT match "${name}"` };
        }
    }
    
    return { passed: true, message: "DB_NAME_PATTERN regex passed all test cases" };
}

/**
 * 测试 SQL 注入防护
 */
function testSqlInjectionPrevention(): TestResult {
    log("Testing SQL injection prevention...");
    
    // 模拟 SQL 注入尝试
    const injectionAttempts = [
        "test; DROP TABLE users;--",
        "test'; DROP TABLE users;--",
        'test"; DROP TABLE users;--',
        "test\"; DROP TABLE users;--",
        "test\nDROP TABLE users",
        "test' OR '1'='1",
        "test\" OR \"1\"=\"1",
        "admin'--",
        "1 OR 1=1",
        "UNION SELECT * FROM users",
    ];
    
    for (const attempt of injectionAttempts) {
        // validateDbName 应该拒绝所有注入尝试
        const [valid] = validateDbName(attempt);
        if (valid) {
            return { passed: false, message: `SQL injection attempt should be rejected: "${attempt}"` };
        }
        
        // quoteIdentifier 应该安全转义
        const quoted = quoteIdentifier(attempt);
        // 确保结果被双引号包裹且内部双引号被转义
        if (!quoted.startsWith('"') || !quoted.endsWith('"')) {
            return { passed: false, message: `quoteIdentifier should wrap with double quotes: "${attempt}"` };
        }
    }
    
    return { passed: true, message: "SQL injection prevention passed all test cases" };
}

/**
 * 运行所有测试
 */
async function runAllTests(): Promise<void> {
    log("=".repeat(60));
    log("db_security.ts 单元测试");
    log("=".repeat(60));
    
    const tests = [
        { name: "validateDbName", fn: testValidateDbName },
        { name: "quoteIdentifier", fn: testQuoteIdentifier },
        { name: "validateSchemaName", fn: testValidateSchemaName },
        { name: "DB_NAME_PATTERN", fn: testDbNamePattern },
        { name: "SQL Injection Prevention", fn: testSqlInjectionPrevention },
    ];
    
    let passed = 0;
    let failed = 0;
    
    for (const test of tests) {
        log("-".repeat(40));
        
        try {
            const result = test.fn();
            
            if (result.passed) {
                logOk(result.message);
                passed++;
            } else {
                logFail(result.message);
                failed++;
            }
        } catch (e: any) {
            logFail(`${test.name} threw exception: ${e.message}`);
            failed++;
        }
    }
    
    log("=".repeat(60));
    log(`测试结果: ${passed} 通过, ${failed} 失败`);
    log("=".repeat(60));
    
    if (failed > 0) {
        process.exit(1);
    }
}

// 执行测试
runAllTests().catch((err) => {
    logFail(`测试执行失败: ${err.message}`);
    process.exit(1);
});
