"""SQLite 存取层：建表、迁移与连接管理。"""
import sqlite3
from contextlib import contextmanager

# readings.revision 为单调修订号：初始为 1，每次更正 / 撤销（直接操作或争议批准执行）
# 都会 +1。争议申请时冻结该修订号，批准时在同一事务内核对，不一致即判定为过期。
SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    device_id   TEXT NOT NULL,
    event_no    TEXT NOT NULL,
    sampled_at  TEXT NOT NULL,          -- UTC ISO-8601，固定微秒宽度，可直接排序
    temperature REAL NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,  -- 撤销采用墓碑标记，保留审计痕迹
    revision    INTEGER NOT NULL DEFAULT 1,  -- 单调修订号，每次内容写操作 +1
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (device_id, event_no)
);

CREATE TABLE IF NOT EXISTS disputes (
    device_id          TEXT NOT NULL,
    dispute_no         TEXT NOT NULL,    -- 设备内唯一争议号
    target_event_no    TEXT NOT NULL,    -- 被争议读数的事件号
    action             TEXT NOT NULL,    -- correct | revoke
    reason             TEXT NOT NULL,    -- 申请原因（非空）
    target_sampled_at  TEXT,             -- action=correct 时的目标采样时间（UTC 归一化）
    target_temperature REAL,             -- action=correct 时的目标温度
    frozen_sampled_at  TEXT NOT NULL,    -- 申请时冻结的读数采样时间
    frozen_temperature REAL NOT NULL,    -- 申请时冻结的读数温度
    frozen_revision    INTEGER NOT NULL, -- 申请时冻结的读数修订号
    status             TEXT NOT NULL,    -- pending | approved | rejected | expired
    decided_at         TEXT,             -- 首次作出决定的时间
    decided_reason     TEXT,             -- 决定时附带的说明（可选）
    expiry_reason      TEXT,             -- 批准时发现过期的原因说明
    execution          TEXT,             -- 首次决定的执行结果（JSON 存档，重试原样返回）
    created_at         TEXT NOT NULL,
    PRIMARY KEY (device_id, dispute_no)
);

-- 离线网关成批上报：批次回执存档。整批写入与回执在同一事务内完成；
-- 同批次号 + 规范化后的有序内容重试时原样返回首次回执，异内容则 409。
CREATE TABLE IF NOT EXISTS reading_batches (
    device_id      TEXT NOT NULL,
    batch_no       TEXT NOT NULL,        -- 设备内唯一批次号
    request_hash   TEXT NOT NULL,        -- 规范化后有序内容的指纹（幂等 / 冲突判定）
    receipt        TEXT NOT NULL,        -- 首次回执全文（JSON），重试原样返回
    created_at     TEXT NOT NULL,
    PRIMARY KEY (device_id, batch_no)
);
"""

# 旧版本库（无 revision 列）的一次性迁移；默认值 1 即历史读数的初始修订号。
_ADD_REVISION_SQL = (
    "ALTER TABLE readings ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """确保所有表 / 列存在（幂等），并对旧库做增量迁移。"""
    # executescript 会先提交当前事务，因此必须在业务事务（BEGIN IMMEDIATE）之前调用。
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(readings)")}
    if "revision" not in columns:
        conn.execute(_ADD_REVISION_SQL)
    conn.commit()


def init_db(path: str) -> None:
    """初始化数据库 schema（幂等），服务启动时调用。"""
    conn = sqlite3.connect(path)
    try:
        ensure_schema(conn)
    finally:
        conn.close()


@contextmanager
def connect(path: str):
    """打开一个连接并确保 schema 存在（幂等，便于任意入口直接使用）。"""
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        ensure_schema(conn)
        yield conn
    finally:
        conn.close()
