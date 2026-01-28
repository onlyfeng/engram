/**
 * 多 Schema 隔离集成测试
 * 
 * 验证 OpenMemory 在不同 PostgreSQL schema 下可独立迁移且互不影响
 * 
 * 使用方法:
 *   设置环境变量后运行:
 *   export OM_PG_HOST=localhost
 *   export OM_PG_PORT=5432
 *   export OM_PG_DB=openmemory_test
 *   export OM_PG_USER=postgres
 *   export OM_PG_PASSWORD=your_password
 *   export OM_METADATA_BACKEND=postgres
 *   npx ts-node tests/test_multi_schema.ts
 */

import { Pool } from "pg";
import { validateDbName, quoteIdentifier, validateSchemaName } from "../src/core/utils/db_security";

const log = (msg: string) => console.log(`[TEST] ${msg}`);
const logOk = (msg: string) => console.log(`[TEST] ✓ ${msg}`);
const logFail = (msg: string) => console.error(`[TEST] ✗ ${msg}`);

interface TestResult {
    passed: boolean;
    message: string;
}

/**
 * 创建数据库连接池
 */
function createPool(): Pool {
    const ssl =
        process.env.OM_PG_SSL === "require"
            ? { rejectUnauthorized: false }
            : process.env.OM_PG_SSL === "disable"
                ? false
                : undefined;

    return new Pool({
        host: process.env.OM_PG_HOST || "localhost",
        port: process.env.OM_PG_PORT ? +process.env.OM_PG_PORT : 5432,
        database: process.env.OM_PG_DB || "openmemory_test",
        user: process.env.OM_PG_USER || "postgres",
        password: process.env.OM_PG_PASSWORD,
        ssl,
    });
}

/**
 * 确保 schema 存在
 */
async function ensureSchema(pool: Pool, schema: string): Promise<void> {
    await pool.query(`CREATE SCHEMA IF NOT EXISTS "${schema}"`);
}

/**
 * 清理 schema
 */
async function cleanupSchema(pool: Pool, schema: string): Promise<void> {
    await pool.query(`DROP SCHEMA IF EXISTS "${schema}" CASCADE`);
}

/**
 * 在指定 schema 下创建测试表（模拟 migrate 行为）
 */
async function createTestTables(pool: Pool, schema: string): Promise<void> {
    // 创建 schema_version 表
    await pool.query(`
        CREATE TABLE IF NOT EXISTS "${schema}"."schema_version" (
            version TEXT PRIMARY KEY,
            applied_at BIGINT
        )
    `);
    
    // 创建 memories 表
    await pool.query(`
        CREATE TABLE IF NOT EXISTS "${schema}"."openmemory_memories" (
            id UUID PRIMARY KEY,
            user_id TEXT,
            segment INTEGER DEFAULT 0,
            content TEXT NOT NULL,
            simhash TEXT,
            primary_sector TEXT NOT NULL,
            tags TEXT,
            meta TEXT,
            created_at BIGINT,
            updated_at BIGINT,
            last_seen_at BIGINT,
            salience DOUBLE PRECISION,
            decay_lambda DOUBLE PRECISION,
            version INTEGER DEFAULT 1,
            mean_dim INTEGER,
            mean_vec BYTEA,
            compressed_vec BYTEA,
            feedback_score DOUBLE PRECISION DEFAULT 0
        )
    `);
    
    // 创建 vectors 表
    await pool.query(`
        CREATE TABLE IF NOT EXISTS "${schema}"."openmemory_vectors" (
            id UUID,
            sector TEXT,
            user_id TEXT,
            v BYTEA,
            dim INTEGER NOT NULL,
            PRIMARY KEY(id, sector)
        )
    `);
    
    // 创建 users 表
    await pool.query(`
        CREATE TABLE IF NOT EXISTS "${schema}"."openmemory_users" (
            user_id TEXT PRIMARY KEY,
            summary TEXT,
            reflection_count INTEGER DEFAULT 0,
            created_at BIGINT,
            updated_at BIGINT
        )
    `);
}

/**
 * 插入测试数据
 */
async function insertTestData(pool: Pool, schema: string, testId: string): Promise<void> {
    await pool.query(
        `INSERT INTO "${schema}"."schema_version" (version, applied_at) 
         VALUES ($1, $2)
         ON CONFLICT (version) DO UPDATE SET applied_at = EXCLUDED.applied_at`,
        [`test_${testId}`, Date.now()]
    );
    
    await pool.query(
        `INSERT INTO "${schema}"."openmemory_memories" 
         (id, content, primary_sector, created_at) 
         VALUES ($1, $2, $3, $4)`,
        [
            `00000000-0000-0000-0000-00000000${testId.padStart(4, '0')}`,
            `Test memory in schema ${schema}`,
            "test",
            Date.now()
        ]
    );
}

/**
 * 验证数据存在于指定 schema
 */
async function verifyDataExists(pool: Pool, schema: string, testId: string): Promise<boolean> {
    const result = await pool.query(
        `SELECT COUNT(*) as cnt FROM "${schema}"."openmemory_memories" 
         WHERE id = $1`,
        [`00000000-0000-0000-0000-00000000${testId.padStart(4, '0')}`]
    );
    return parseInt(result.rows[0].cnt) === 1;
}

/**
 * 验证数据不存在于指定 schema
 */
async function verifyDataNotExists(pool: Pool, schema: string, testId: string): Promise<boolean> {
    try {
        const result = await pool.query(
            `SELECT COUNT(*) as cnt FROM "${schema}"."openmemory_memories" 
             WHERE id = $1`,
            [`00000000-0000-0000-0000-00000000${testId.padStart(4, '0')}`]
        );
        return parseInt(result.rows[0].cnt) === 0;
    } catch (e: any) {
        // 表不存在也视为数据不存在
        if (e.message.includes("does not exist")) {
            return true;
        }
        throw e;
    }
}

/**
 * 测试：两个不同 schema 的独立性
 */
async function testSchemaIsolation(): Promise<TestResult> {
    const pool = createPool();
    const schema1 = "tenant_a_openmemory";
    const schema2 = "tenant_b_openmemory";
    
    try {
        log(`Testing schema isolation between "${schema1}" and "${schema2}"`);
        
        // 清理旧数据
        await cleanupSchema(pool, schema1);
        await cleanupSchema(pool, schema2);
        
        // 创建两个独立的 schema
        await ensureSchema(pool, schema1);
        await ensureSchema(pool, schema2);
        
        // 在两个 schema 中分别创建表
        await createTestTables(pool, schema1);
        await createTestTables(pool, schema2);
        
        // 在 schema1 插入数据（testId=1）
        await insertTestData(pool, schema1, "1");
        
        // 在 schema2 插入数据（testId=2）
        await insertTestData(pool, schema2, "2");
        
        // 验证 schema1 只有 testId=1 的数据
        const schema1Has1 = await verifyDataExists(pool, schema1, "1");
        const schema1Has2 = await verifyDataNotExists(pool, schema1, "2");
        
        // 验证 schema2 只有 testId=2 的数据
        const schema2Has1 = await verifyDataNotExists(pool, schema2, "1");
        const schema2Has2 = await verifyDataExists(pool, schema2, "2");
        
        if (!schema1Has1) {
            return { passed: false, message: "Schema1 should have testId=1 data" };
        }
        if (!schema1Has2) {
            return { passed: false, message: "Schema1 should NOT have testId=2 data" };
        }
        if (!schema2Has1) {
            return { passed: false, message: "Schema2 should NOT have testId=1 data" };
        }
        if (!schema2Has2) {
            return { passed: false, message: "Schema2 should have testId=2 data" };
        }
        
        // 清理
        await cleanupSchema(pool, schema1);
        await cleanupSchema(pool, schema2);
        
        return { passed: true, message: "Schema isolation verified successfully" };
    } catch (e: any) {
        return { passed: false, message: `Error: ${e.message}` };
    } finally {
        await pool.end();
    }
}

/**
 * 测试：重复迁移幂等性
 */
async function testMigrationIdempotency(): Promise<TestResult> {
    const pool = createPool();
    const schema = "test_idempotency_openmemory";
    
    try {
        log(`Testing migration idempotency in schema "${schema}"`);
        
        // 清理旧数据
        await cleanupSchema(pool, schema);
        
        // 创建 schema
        await ensureSchema(pool, schema);
        
        // 第一次创建表
        await createTestTables(pool, schema);
        await insertTestData(pool, schema, "1");
        
        // 第二次创建表（模拟重复迁移）
        await createTestTables(pool, schema);
        
        // 验证数据仍然存在
        const dataExists = await verifyDataExists(pool, schema, "1");
        
        if (!dataExists) {
            return { passed: false, message: "Data should persist after repeated migration" };
        }
        
        // 清理
        await cleanupSchema(pool, schema);
        
        return { passed: true, message: "Migration idempotency verified successfully" };
    } catch (e: any) {
        return { passed: false, message: `Error: ${e.message}` };
    } finally {
        await pool.end();
    }
}

/**
 * 测试：与 Step1 schema 的隔离性
 */
async function testStep1Isolation(): Promise<TestResult> {
    const pool = createPool();
    const omSchema = "proj_test_openmemory";
    const step1Schemas = ["governance", "logbook"];
    
    try {
        log(`Testing isolation between OpenMemory and Step1 schemas`);
        
        // 清理
        await cleanupSchema(pool, omSchema);
        for (const s of step1Schemas) {
            await cleanupSchema(pool, s);
        }
        
        // 创建 OpenMemory schema
        await ensureSchema(pool, omSchema);
        await createTestTables(pool, omSchema);
        await insertTestData(pool, omSchema, "1");
        
        // 模拟 Step1 的 governance schema
        await ensureSchema(pool, "governance");
        await pool.query(`
            CREATE TABLE IF NOT EXISTS "governance"."settings" (
                project_key TEXT PRIMARY KEY,
                team_write_enabled BOOLEAN DEFAULT FALSE,
                policy_json JSONB DEFAULT '{}',
                updated_by TEXT,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        `);
        await pool.query(`
            INSERT INTO "governance"."settings" (project_key, team_write_enabled)
            VALUES ('test_project', true)
        `);
        
        // 验证 OpenMemory 数据存在
        const omDataExists = await verifyDataExists(pool, omSchema, "1");
        
        // 验证 Step1 数据存在
        const step1Result = await pool.query(
            `SELECT COUNT(*) as cnt FROM "governance"."settings" WHERE project_key = 'test_project'`
        );
        const step1DataExists = parseInt(step1Result.rows[0].cnt) === 1;
        
        // 验证两者独立
        if (!omDataExists) {
            return { passed: false, message: "OpenMemory data should exist" };
        }
        if (!step1DataExists) {
            return { passed: false, message: "Step1 data should exist" };
        }
        
        // 验证 OpenMemory schema 没有 Step1 的表
        const omHasSettings = await pool.query(`
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_schema = $1 AND table_name = 'settings'
            )
        `, [omSchema]);
        
        if (omHasSettings.rows[0].exists) {
            return { passed: false, message: "OpenMemory schema should not have Step1 tables" };
        }
        
        // 清理
        await cleanupSchema(pool, omSchema);
        for (const s of step1Schemas) {
            await cleanupSchema(pool, s);
        }
        
        return { passed: true, message: "Step1 isolation verified successfully" };
    } catch (e: any) {
        return { passed: false, message: `Error: ${e.message}` };
    } finally {
        await pool.end();
    }
}

/**
 * 测试：db_security 模块功能
 * 这些测试不需要 PostgreSQL 连接
 */
async function testDbSecurityModule(): Promise<TestResult> {
    log("Testing db_security module functions...");
    
    // 测试 validateDbName
    const [validName, _] = validateDbName("test_database");
    if (!validName) {
        return { passed: false, message: "validateDbName failed for valid name" };
    }
    
    const [invalidName, error] = validateDbName("Invalid-DB");
    if (invalidName) {
        return { passed: false, message: "validateDbName should reject invalid name" };
    }
    
    // 测试 quoteIdentifier
    const quoted = quoteIdentifier("user");
    if (quoted !== '"user"') {
        return { passed: false, message: `quoteIdentifier failed: got ${quoted}` };
    }
    
    const quotedWithQuotes = quoteIdentifier('say"hi');
    if (quotedWithQuotes !== '"say""hi"') {
        return { passed: false, message: `quoteIdentifier escaping failed: got ${quotedWithQuotes}` };
    }
    
    // 测试 validateSchemaName
    const publicResult = validateSchemaName("public");
    if (publicResult.ok) {
        return { passed: false, message: "validateSchemaName should reject 'public'" };
    }
    
    const validSchemaResult = validateSchemaName("openmemory");
    if (!validSchemaResult.ok) {
        return { passed: false, message: "validateSchemaName should accept valid schema" };
    }
    
    return { passed: true, message: "db_security module functions work correctly" };
}

/**
 * 运行所有测试
 */
async function runAllTests(): Promise<void> {
    log("=".repeat(60));
    log("OpenMemory 多 Schema 隔离集成测试");
    log("=".repeat(60));
    
    // 首先运行不需要 PostgreSQL 的测试
    log("-".repeat(40));
    log("Running: db_security Module Tests (no PG required)");
    const securityResult = await testDbSecurityModule();
    if (securityResult.passed) {
        logOk(securityResult.message);
    } else {
        logFail(securityResult.message);
    }
    
    // 检查必要的环境变量
    if (!process.env.OM_PG_HOST) {
        log("-".repeat(40));
        logFail("请设置 OM_PG_HOST 环境变量以运行 PostgreSQL 集成测试");
        log("使用方法:");
        log("  export OM_PG_HOST=localhost");
        log("  export OM_PG_PORT=5432");
        log("  export OM_PG_DB=openmemory_test");
        log("  export OM_PG_USER=postgres");
        log("  export OM_PG_PASSWORD=your_password");
        log("  npx ts-node tests/test_multi_schema.ts");
        log("=".repeat(60));
        log(`测试结果: ${securityResult.passed ? 1 : 0} 通过 (PostgreSQL 测试已跳过)`);
        log("=".repeat(60));
        process.exit(securityResult.passed ? 0 : 1);
    }
    
    const tests = [
        { name: "Schema Isolation", fn: testSchemaIsolation },
        { name: "Migration Idempotency", fn: testMigrationIdempotency },
        { name: "Step1 Isolation", fn: testStep1Isolation },
    ];
    
    // 从 db_security 测试开始计数
    let passed = securityResult.passed ? 1 : 0;
    let failed = securityResult.passed ? 0 : 1;
    
    for (const test of tests) {
        log("-".repeat(40));
        log(`Running: ${test.name}`);
        
        const result = await test.fn();
        
        if (result.passed) {
            logOk(result.message);
            passed++;
        } else {
            logFail(result.message);
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
