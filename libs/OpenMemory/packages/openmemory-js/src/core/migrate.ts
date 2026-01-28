import { env } from "./cfg";
import sqlite3 from "sqlite3";
import { Pool } from "pg";
import { validateDbName, quoteIdentifier, validateSchemaName } from "./utils/db_security";

const is_pg = env.metadata_backend === "postgres";

const log = (msg: string) => console.log(`[MIGRATE] ${msg}`);
const error = (msg: string) => console.error(`[MIGRATE][ERROR] ${msg}`);

/**
 * 确保数据库存在，如不存在则尝试创建
 * 
 * 此函数仅在迁移流程中调用（显式操作），需要管理员权限
 * OM_PG_AUTO_CREATE_DB=true 时启用（默认 false）
 * 
 * @param db_name 数据库名称
 * @param createPool 创建连接池的工厂函数
 */
async function ensureDatabaseExists(
    db_name: string,
    createPool: (db: string) => Pool
): Promise<void> {
    const auto_create_db = process.env.OM_PG_AUTO_CREATE_DB === "true";
    
    // 先校验数据库名称
    const [valid, errorMsg] = validateDbName(db_name);
    if (!valid) {
        throw new Error(`Invalid database name: ${errorMsg}`);
    }
    
    const testPool = createPool(db_name);
    try {
        await testPool.query("SELECT 1");
        log(`Database "${db_name}" exists`);
    } catch (err: any) {
        if (err.code === "3D000") {
            // 数据库不存在
            if (!auto_create_db) {
                error(`Database "${db_name}" does not exist.`);
                error(`Set OM_PG_AUTO_CREATE_DB=true to auto-create, or create it manually.`);
                throw err;
            }
            
            log(`Database "${db_name}" does not exist, creating...`);
            const adminPool = createPool("postgres");
            try {
                // 使用安全的标识符引用（双引号）防止注入和关键字冲突
                const quotedDbName = quoteIdentifier(db_name);
                await adminPool.query(`CREATE DATABASE ${quotedDbName}`);
                log(`Database "${db_name}" created successfully`);
            } catch (e: any) {
                // 42P04 = database already exists（并发创建场景）
                if (e.code !== "42P04") throw e;
                log(`Database "${db_name}" already exists (concurrent creation)`);
            } finally {
                await adminPool.end();
            }
        } else {
            throw err;
        }
    } finally {
        await testPool.end();
    }
}

/**
 * 预检：禁止使用 public schema（安全约束）
 * 
 * 当 OM_METADATA_BACKEND=postgres 时，强制要求使用非 public schema。
 * 这是为了：
 * 1. 避免与系统表/其他应用冲突
 * 2. 便于 pg_dump --schema 进行隔离备份
 * 3. 支持 DROP SCHEMA CASCADE 安全清理
 */
function precheck_schema_safety(): { ok: boolean; message: string } {
    if (!is_pg) {
        return { ok: true, message: "" };
    }
    
    const schema = process.env.OM_PG_SCHEMA || "public";
    const result = validateSchemaName(schema);
    
    if (result.ok) {
        log(`Schema safety check passed: using schema "${schema}"`);
    }
    
    return result;
}

interface Migration {
    version: string;
    desc: string;
    sqlite: string[];
    postgres: string[];
}

const migrations: Migration[] = [
    {
        version: "1.2.0",
        desc: "Multi-user tenant support",
        sqlite: [
            `ALTER TABLE memories ADD COLUMN user_id TEXT`,
            `CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id)`,
            `ALTER TABLE vectors ADD COLUMN user_id TEXT`,
            `CREATE INDEX IF NOT EXISTS idx_vectors_user ON vectors(user_id)`,
            `CREATE TABLE IF NOT EXISTS waypoints_new (
        src_id TEXT, dst_id TEXT NOT NULL, user_id TEXT,
        weight REAL NOT NULL, created_at INTEGER, updated_at INTEGER,
        PRIMARY KEY(src_id, user_id)
      )`,
            `INSERT INTO waypoints_new SELECT src_id, dst_id, NULL, weight, created_at, updated_at FROM waypoints`,
            `DROP TABLE waypoints`,
            `ALTER TABLE waypoints_new RENAME TO waypoints`,
            `CREATE INDEX IF NOT EXISTS idx_waypoints_src ON waypoints(src_id)`,
            `CREATE INDEX IF NOT EXISTS idx_waypoints_dst ON waypoints(dst_id)`,
            `CREATE INDEX IF NOT EXISTS idx_waypoints_user ON waypoints(user_id)`,
            `CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY, summary TEXT,
        reflection_count INTEGER DEFAULT 0,
        created_at INTEGER, updated_at INTEGER
      )`,
            `CREATE TABLE IF NOT EXISTS stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL, count INTEGER DEFAULT 1, ts INTEGER NOT NULL
      )`,
            `CREATE INDEX IF NOT EXISTS idx_stats_ts ON stats(ts)`,
            `CREATE INDEX IF NOT EXISTS idx_stats_type ON stats(type)`,
        ],
        postgres: [
            `ALTER TABLE {m} ADD COLUMN IF NOT EXISTS user_id TEXT`,
            `CREATE INDEX IF NOT EXISTS openmemory_memories_user_idx ON {m}(user_id)`,
            `ALTER TABLE {v} ADD COLUMN IF NOT EXISTS user_id TEXT`,
            `CREATE INDEX IF NOT EXISTS openmemory_vectors_user_idx ON {v}(user_id)`,
            `ALTER TABLE {w} ADD COLUMN IF NOT EXISTS user_id TEXT`,
            `ALTER TABLE {w} DROP CONSTRAINT IF EXISTS waypoints_pkey`,
            `ALTER TABLE {w} ADD PRIMARY KEY (src_id, user_id)`,
            `CREATE INDEX IF NOT EXISTS openmemory_waypoints_user_idx ON {w}(user_id)`,
            `CREATE TABLE IF NOT EXISTS {u} (
        user_id TEXT PRIMARY KEY, summary TEXT,
        reflection_count INTEGER DEFAULT 0,
        created_at BIGINT, updated_at BIGINT
      )`,
        ],
    },
];

async function get_db_version_sqlite(
    db: sqlite3.Database,
): Promise<string | null> {
    return new Promise((ok, no) => {
        db.get(
            `SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'`,
            (err, row: any) => {
                if (err) return no(err);
                if (!row) return ok(null);
                db.get(
                    `SELECT version FROM schema_version ORDER BY applied_at DESC LIMIT 1`,
                    (e, v: any) => {
                        if (e) return no(e);
                        ok(v?.version || null);
                    },
                );
            },
        );
    });
}

async function set_db_version_sqlite(
    db: sqlite3.Database,
    version: string,
): Promise<void> {
    return new Promise((ok, no) => {
        db.run(
            `CREATE TABLE IF NOT EXISTS schema_version (
        version TEXT PRIMARY KEY, applied_at INTEGER
      )`,
            (err) => {
                if (err) return no(err);
                db.run(
                    `INSERT OR REPLACE INTO schema_version VALUES (?, ?)`,
                    [version, Date.now()],
                    (e) => {
                        if (e) return no(e);
                        ok();
                    },
                );
            },
        );
    });
}

async function check_column_exists_sqlite(
    db: sqlite3.Database,
    table: string,
    column: string,
): Promise<boolean> {
    return new Promise((ok, no) => {
        db.all(`PRAGMA table_info(${table})`, (err, rows: any[]) => {
            if (err) return no(err);
            ok(rows.some((r) => r.name === column));
        });
    });
}

async function run_sqlite_migration(
    db: sqlite3.Database,
    m: Migration,
): Promise<void> {
    log(`Running migration: ${m.version} - ${m.desc}`);

    const has_user_id = await check_column_exists_sqlite(
        db,
        "memories",
        "user_id",
    );
    if (has_user_id) {
        log(
            `Migration ${m.version} already applied (user_id exists), skipping`,
        );
        await set_db_version_sqlite(db, m.version);
        return;
    }

    for (const sql of m.sqlite) {
        await new Promise<void>((ok, no) => {
            db.run(sql, (err) => {
                if (err && !err.message.includes("duplicate column")) {
                    log(`ERROR: ${err.message}`);
                    return no(err);
                }
                ok();
            });
        });
    }

    await set_db_version_sqlite(db, m.version);
    log(`Migration ${m.version} completed successfully`);
}

async function get_db_version_pg(pool: Pool): Promise<string | null> {
    try {
        const sc = process.env.OM_PG_SCHEMA || "public";
        const check = await pool.query(
            `SELECT EXISTS (
        SELECT FROM information_schema.tables
        WHERE table_schema = $1 AND table_name = 'schema_version'
      )`,
            [sc],
        );
        if (!check.rows[0].exists) return null;

        const ver = await pool.query(
            `SELECT version FROM "${sc}"."schema_version" ORDER BY applied_at DESC LIMIT 1`,
        );
        return ver.rows[0]?.version || null;
    } catch (e) {
        return null;
    }
}

async function set_db_version_pg(pool: Pool, version: string): Promise<void> {
    const sc = process.env.OM_PG_SCHEMA || "public";
    await pool.query(
        `CREATE TABLE IF NOT EXISTS "${sc}"."schema_version" (
      version TEXT PRIMARY KEY, applied_at BIGINT
    )`,
    );
    await pool.query(
        `INSERT INTO "${sc}"."schema_version" VALUES ($1, $2)
     ON CONFLICT (version) DO UPDATE SET applied_at = EXCLUDED.applied_at`,
        [version, Date.now()],
    );
}

async function check_column_exists_pg(
    pool: Pool,
    table: string,
    column: string,
): Promise<boolean> {
    const sc = process.env.OM_PG_SCHEMA || "public";
    const tbl = table.replace(/"/g, "").split(".").pop() || table;
    const res = await pool.query(
        `SELECT EXISTS (
      SELECT FROM information_schema.columns
      WHERE table_schema = $1 AND table_name = $2 AND column_name = $3
    )`,
        [sc, tbl, column],
    );
    return res.rows[0].exists;
}

async function run_pg_migration(pool: Pool, m: Migration): Promise<void> {
    log(`Running migration: ${m.version} - ${m.desc}`);

    const sc = process.env.OM_PG_SCHEMA || "public";
    const mt = process.env.OM_PG_TABLE || "openmemory_memories";
    const has_user_id = await check_column_exists_pg(pool, mt, "user_id");

    if (has_user_id) {
        log(
            `Migration ${m.version} already applied (user_id exists), skipping`,
        );
        await set_db_version_pg(pool, m.version);
        return;
    }

    const replacements: Record<string, string> = {
        "{m}": `"${sc}"."${mt}"`,
        "{v}": `"${sc}"."${process.env.OM_VECTOR_TABLE || "openmemory_vectors"}"`,
        "{w}": `"${sc}"."openmemory_waypoints"`,
        "{u}": `"${sc}"."openmemory_users"`,
    };

    for (let sql of m.postgres) {
        for (const [k, v] of Object.entries(replacements)) {
            sql = sql.replace(new RegExp(k, "g"), v);
        }

        try {
            await pool.query(sql);
        } catch (e: any) {
            if (
                !e.message.includes("already exists") &&
                !e.message.includes("duplicate")
            ) {
                log(`ERROR: ${e.message}`);
                throw e;
            }
        }
    }

    await set_db_version_pg(pool, m.version);
    log(`Migration ${m.version} completed successfully`);
}

/**
 * 确保 schema 存在，如果不存在则创建
 * @param pool PostgreSQL 连接池
 * @param schema schema 名称
 */
async function ensure_schema_exists(pool: Pool, schema: string): Promise<void> {
    if (schema === "public") return; // public schema 默认存在
    
    try {
        await pool.query(`CREATE SCHEMA IF NOT EXISTS "${schema}"`);
        log(`Schema "${schema}" ensured to exist`);
    } catch (e: any) {
        // 忽略并发创建导致的错误
        if (!e.message.includes("already exists")) {
            throw e;
        }
    }
}

/**
 * 基础 schema bootstrap：创建 OpenMemory 运行所需的全部表/索引
 * 
 * 此函数在空库/空 schema 时完整初始化，且幂等（可重复运行）
 * DDL 与 db.ts 中的建表逻辑保持一致
 * 
 * @param pool PostgreSQL 连接池
 * @param schema schema 名称
 */
async function bootstrap_pg_schema(pool: Pool, schema: string): Promise<void> {
    log(`Starting schema bootstrap for "${schema}"...`);
    
    const sc = schema;
    const m = `"${sc}"."${process.env.OM_PG_TABLE || "openmemory_memories"}"`;
    const v = `"${sc}"."${process.env.OM_VECTOR_TABLE || "openmemory_vectors"}"`;
    const w = `"${sc}"."openmemory_waypoints"`;
    const l = `"${sc}"."openmemory_embed_logs"`;
    const u = `"${sc}"."openmemory_users"`;
    const s = `"${sc}"."stats"`;
    const tf = `"${sc}"."temporal_facts"`;
    const te = `"${sc}"."temporal_edges"`;
    
    // === 表创建 (幂等 - IF NOT EXISTS) ===
    
    // 核心 memories 表
    await pool.query(`
        CREATE TABLE IF NOT EXISTS ${m} (
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
    log(`Table ${m} ensured`);
    
    // vectors 表
    await pool.query(`
        CREATE TABLE IF NOT EXISTS ${v} (
            id UUID,
            sector TEXT,
            user_id TEXT,
            v BYTEA,
            dim INTEGER NOT NULL,
            PRIMARY KEY(id, sector)
        )
    `);
    log(`Table ${v} ensured`);
    
    // waypoints 表
    await pool.query(`
        CREATE TABLE IF NOT EXISTS ${w} (
            src_id TEXT,
            dst_id TEXT NOT NULL,
            user_id TEXT,
            weight DOUBLE PRECISION NOT NULL,
            created_at BIGINT,
            updated_at BIGINT,
            PRIMARY KEY(src_id, user_id)
        )
    `);
    log(`Table ${w} ensured`);
    
    // embed_logs 表
    await pool.query(`
        CREATE TABLE IF NOT EXISTS ${l} (
            id TEXT PRIMARY KEY,
            model TEXT,
            status TEXT,
            ts BIGINT,
            err TEXT
        )
    `);
    log(`Table ${l} ensured`);
    
    // users 表
    await pool.query(`
        CREATE TABLE IF NOT EXISTS ${u} (
            user_id TEXT PRIMARY KEY,
            summary TEXT,
            reflection_count INTEGER DEFAULT 0,
            created_at BIGINT,
            updated_at BIGINT
        )
    `);
    log(`Table ${u} ensured`);
    
    // stats 表
    await pool.query(`
        CREATE TABLE IF NOT EXISTS ${s} (
            id SERIAL PRIMARY KEY,
            type TEXT NOT NULL,
            count INTEGER DEFAULT 1,
            ts BIGINT NOT NULL
        )
    `);
    log(`Table ${s} ensured`);
    
    // temporal_facts 表
    await pool.query(`
        CREATE TABLE IF NOT EXISTS ${tf} (
            id UUID PRIMARY KEY,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            valid_from BIGINT NOT NULL,
            valid_to BIGINT,
            confidence DOUBLE PRECISION NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            last_updated BIGINT NOT NULL,
            metadata TEXT,
            UNIQUE(subject, predicate, object, valid_from)
        )
    `);
    log(`Table ${tf} ensured`);
    
    // temporal_edges 表
    await pool.query(`
        CREATE TABLE IF NOT EXISTS ${te} (
            id UUID PRIMARY KEY,
            source_id UUID NOT NULL,
            target_id UUID NOT NULL,
            relation_type TEXT NOT NULL,
            valid_from BIGINT NOT NULL,
            valid_to BIGINT,
            weight DOUBLE PRECISION NOT NULL,
            metadata TEXT,
            FOREIGN KEY(source_id) REFERENCES ${tf}(id),
            FOREIGN KEY(target_id) REFERENCES ${tf}(id)
        )
    `);
    log(`Table ${te} ensured`);
    
    // schema_version 表（用于迁移版本追踪）
    await pool.query(`
        CREATE TABLE IF NOT EXISTS "${sc}"."schema_version" (
            version TEXT PRIMARY KEY,
            applied_at BIGINT
        )
    `);
    log(`Table "${sc}"."schema_version" ensured`);
    
    // === 索引创建 (幂等 - IF NOT EXISTS) ===
    
    // memories 表索引
    await pool.query(`CREATE INDEX IF NOT EXISTS openmemory_memories_sector_idx ON ${m}(primary_sector)`);
    await pool.query(`CREATE INDEX IF NOT EXISTS openmemory_memories_segment_idx ON ${m}(segment)`);
    await pool.query(`CREATE INDEX IF NOT EXISTS openmemory_memories_simhash_idx ON ${m}(simhash)`);
    await pool.query(`CREATE INDEX IF NOT EXISTS openmemory_memories_user_idx ON ${m}(user_id)`);
    log(`Indexes for ${m} ensured`);
    
    // vectors 表索引
    await pool.query(`CREATE INDEX IF NOT EXISTS openmemory_vectors_user_idx ON ${v}(user_id)`);
    log(`Indexes for ${v} ensured`);
    
    // waypoints 表索引
    await pool.query(`CREATE INDEX IF NOT EXISTS openmemory_waypoints_user_idx ON ${w}(user_id)`);
    log(`Indexes for ${w} ensured`);
    
    // stats 表索引
    await pool.query(`CREATE INDEX IF NOT EXISTS openmemory_stats_ts_idx ON ${s}(ts)`);
    await pool.query(`CREATE INDEX IF NOT EXISTS openmemory_stats_type_idx ON ${s}(type)`);
    log(`Indexes for ${s} ensured`);
    
    // temporal_facts 表索引
    await pool.query(`CREATE INDEX IF NOT EXISTS temporal_facts_subject_idx ON ${tf}(subject)`);
    await pool.query(`CREATE INDEX IF NOT EXISTS temporal_facts_predicate_idx ON ${tf}(predicate)`);
    await pool.query(`CREATE INDEX IF NOT EXISTS temporal_facts_validity_idx ON ${tf}(valid_from, valid_to)`);
    await pool.query(`CREATE INDEX IF NOT EXISTS temporal_facts_composite_idx ON ${tf}(subject, predicate, valid_from, valid_to)`);
    log(`Indexes for ${tf} ensured`);
    
    // temporal_edges 表索引
    await pool.query(`CREATE INDEX IF NOT EXISTS temporal_edges_source_idx ON ${te}(source_id)`);
    await pool.query(`CREATE INDEX IF NOT EXISTS temporal_edges_target_idx ON ${te}(target_id)`);
    await pool.query(`CREATE INDEX IF NOT EXISTS temporal_edges_validity_idx ON ${te}(valid_from, valid_to)`);
    log(`Indexes for ${te} ensured`);
    
    log(`Schema bootstrap completed for "${schema}"`);
}

/**
 * SQLite 基础表初始化（幂等）
 * @param db SQLite 数据库实例
 */
async function bootstrap_sqlite_schema(db: sqlite3.Database): Promise<void> {
    log("Starting SQLite schema bootstrap...");
    
    const sqlite_vector_table = process.env.OM_VECTOR_TABLE || "vectors";
    
    const run = (sql: string): Promise<void> => 
        new Promise((ok, no) => db.run(sql, (err) => err ? no(err) : ok()));
    
    // PRAGMA 设置
    await run("PRAGMA journal_mode=WAL");
    await run("PRAGMA synchronous=NORMAL");
    await run("PRAGMA temp_store=MEMORY");
    await run("PRAGMA cache_size=-8000");
    await run("PRAGMA mmap_size=134217728");
    await run("PRAGMA foreign_keys=OFF");
    await run("PRAGMA wal_autocheckpoint=20000");
    await run("PRAGMA locking_mode=NORMAL");
    await run("PRAGMA busy_timeout=5000");
    
    // === 表创建 (幂等) ===
    await run(`
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            segment INTEGER DEFAULT 0,
            content TEXT NOT NULL,
            simhash TEXT,
            primary_sector TEXT NOT NULL,
            tags TEXT,
            meta TEXT,
            created_at INTEGER,
            updated_at INTEGER,
            last_seen_at INTEGER,
            salience REAL,
            decay_lambda REAL,
            version INTEGER DEFAULT 1,
            mean_dim INTEGER,
            mean_vec BLOB,
            compressed_vec BLOB,
            feedback_score REAL DEFAULT 0
        )
    `);
    
    await run(`
        CREATE TABLE IF NOT EXISTS ${sqlite_vector_table} (
            id TEXT NOT NULL,
            sector TEXT NOT NULL,
            user_id TEXT,
            v BLOB NOT NULL,
            dim INTEGER NOT NULL,
            PRIMARY KEY(id, sector)
        )
    `);
    
    await run(`
        CREATE TABLE IF NOT EXISTS waypoints (
            src_id TEXT,
            dst_id TEXT NOT NULL,
            user_id TEXT,
            weight REAL NOT NULL,
            created_at INTEGER,
            updated_at INTEGER,
            PRIMARY KEY(src_id, user_id)
        )
    `);
    
    await run(`
        CREATE TABLE IF NOT EXISTS embed_logs (
            id TEXT PRIMARY KEY,
            model TEXT,
            status TEXT,
            ts INTEGER,
            err TEXT
        )
    `);
    
    await run(`
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            summary TEXT,
            reflection_count INTEGER DEFAULT 0,
            created_at INTEGER,
            updated_at INTEGER
        )
    `);
    
    await run(`
        CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            count INTEGER DEFAULT 1,
            ts INTEGER NOT NULL
        )
    `);
    
    await run(`
        CREATE TABLE IF NOT EXISTS temporal_facts (
            id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            valid_from INTEGER NOT NULL,
            valid_to INTEGER,
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            last_updated INTEGER NOT NULL,
            metadata TEXT,
            UNIQUE(subject, predicate, object, valid_from)
        )
    `);
    
    await run(`
        CREATE TABLE IF NOT EXISTS temporal_edges (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            valid_from INTEGER NOT NULL,
            valid_to INTEGER,
            weight REAL NOT NULL,
            metadata TEXT,
            FOREIGN KEY(source_id) REFERENCES temporal_facts(id),
            FOREIGN KEY(target_id) REFERENCES temporal_facts(id)
        )
    `);
    
    await run(`
        CREATE TABLE IF NOT EXISTS schema_version (
            version TEXT PRIMARY KEY,
            applied_at INTEGER
        )
    `);
    
    // === 索引创建 (幂等) ===
    await run("CREATE INDEX IF NOT EXISTS idx_memories_sector ON memories(primary_sector)");
    await run("CREATE INDEX IF NOT EXISTS idx_memories_segment ON memories(segment)");
    await run("CREATE INDEX IF NOT EXISTS idx_memories_simhash ON memories(simhash)");
    await run("CREATE INDEX IF NOT EXISTS idx_memories_ts ON memories(last_seen_at)");
    await run("CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id)");
    await run(`CREATE INDEX IF NOT EXISTS idx_vectors_user ON ${sqlite_vector_table}(user_id)`);
    await run("CREATE INDEX IF NOT EXISTS idx_waypoints_src ON waypoints(src_id)");
    await run("CREATE INDEX IF NOT EXISTS idx_waypoints_dst ON waypoints(dst_id)");
    await run("CREATE INDEX IF NOT EXISTS idx_waypoints_user ON waypoints(user_id)");
    await run("CREATE INDEX IF NOT EXISTS idx_stats_ts ON stats(ts)");
    await run("CREATE INDEX IF NOT EXISTS idx_stats_type ON stats(type)");
    await run("CREATE INDEX IF NOT EXISTS idx_temporal_subject ON temporal_facts(subject)");
    await run("CREATE INDEX IF NOT EXISTS idx_temporal_predicate ON temporal_facts(predicate)");
    await run("CREATE INDEX IF NOT EXISTS idx_temporal_validity ON temporal_facts(valid_from, valid_to)");
    await run("CREATE INDEX IF NOT EXISTS idx_temporal_composite ON temporal_facts(subject, predicate, valid_from, valid_to)");
    await run("CREATE INDEX IF NOT EXISTS idx_edges_source ON temporal_edges(source_id)");
    await run("CREATE INDEX IF NOT EXISTS idx_edges_target ON temporal_edges(target_id)");
    await run("CREATE INDEX IF NOT EXISTS idx_edges_validity ON temporal_edges(valid_from, valid_to)");
    
    log("SQLite schema bootstrap completed");
}

export async function run_migrations() {
    log("Checking for pending migrations...");
    
    // 预检：禁止使用 public schema
    const precheck = precheck_schema_safety();
    if (!precheck.ok) {
        error(precheck.message);
        throw new Error("Schema safety precheck failed: OM_PG_SCHEMA=public is forbidden");
    }

    if (is_pg) {
        const ssl =
            process.env.OM_PG_SSL === "require"
                ? { rejectUnauthorized: false }
                : process.env.OM_PG_SSL === "disable"
                  ? false
                  : undefined;

        const db_name = process.env.OM_PG_DB || "openmemory";
        
        // 校验数据库名称
        const [validDb, dbError] = validateDbName(db_name);
        if (!validDb) {
            error(`Invalid database name: ${dbError}`);
            throw new Error(`Invalid database name: ${dbError}`);
        }
        
        // 连接池工厂函数
        const createPool = (db: string) =>
            new Pool({
                host: process.env.OM_PG_HOST,
                port: process.env.OM_PG_PORT ? +process.env.OM_PG_PORT : undefined,
                database: db,
                user: process.env.OM_PG_USER,
                password: process.env.OM_PG_PASSWORD,
                ssl,
            });
        
        // Step 0: 确保数据库存在（OM_PG_AUTO_CREATE_DB=true 时自动创建）
        log("Step 0: Checking database existence...");
        await ensureDatabaseExists(db_name, createPool);
        
        const pool = createPool(db_name);

        // 根据环境变量设置连接角色（用于 RLS 权限分离）
        // 示例: OM_PG_SET_ROLE=openmemory_migrator
        const setRole = process.env.OM_PG_SET_ROLE;
        if (setRole) {
            pool.on('connect', async (client) => {
                try {
                    await client.query(`SET ROLE "${setRole}"`);
                    log(`Connection role set to: ${setRole}`);
                } catch (e: any) {
                    error(`Failed to SET ROLE "${setRole}": ${e.message}`);
                }
            });
        }

        // 确保目标 schema 存在
        const sc = process.env.OM_PG_SCHEMA || "public";
        await ensure_schema_exists(pool, sc);
        log(`Using schema: ${sc}`);

        // Step 1: 基础 schema bootstrap（在空库时完整初始化，幂等）
        log("Step 1: Schema bootstrap...");
        await bootstrap_pg_schema(pool, sc);

        // Step 2: 运行增量迁移
        log("Step 2: Running incremental migrations...");
        const current = await get_db_version_pg(pool);
        log(`Current database version: ${current || "none"}`);

        for (const m of migrations) {
            if (!current || m.version > current) {
                await run_pg_migration(pool, m);
            }
        }

        await pool.end();
    } else {
        const db_path = process.env.OM_DB_PATH || "./data/openmemory.sqlite";
        const fs = await import("node:fs");
        const path = await import("node:path");
        const dir = path.dirname(db_path);
        if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
        
        const db = new sqlite3.Database(db_path);

        // Step 1: 基础 schema bootstrap（在空库时完整初始化，幂等）
        log("Step 1: Schema bootstrap...");
        await bootstrap_sqlite_schema(db);

        // Step 2: 运行增量迁移
        log("Step 2: Running incremental migrations...");
        const current = await get_db_version_sqlite(db);
        log(`Current database version: ${current || "none"}`);

        for (const m of migrations) {
            if (!current || m.version > current) {
                await run_sqlite_migration(db, m);
            }
        }

        await new Promise<void>((ok) => db.close(() => ok()));
    }

    log("All migrations completed");
}

// CLI 入口
if (require.main === module || process.argv[1]?.endsWith("migrate.ts")) {
    console.log("OpenMemory Database Migration Tool\n");
    run_migrations()
        .then(() => {
            console.log("\n[SUCCESS] Migration completed");
            process.exit(0);
        })
        .catch((e) => {
            console.error("\n[ERROR] Migration failed:", e);
            process.exit(1);
        });
}
