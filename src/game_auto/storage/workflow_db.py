import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

_logger = logging.getLogger("GameAutomation.WorkflowDB")


class WorkflowDB:
    """
    通用自动化流程数据库。
    - workflows: 一个可执行流程（对应某个启动脚本）
    - steps: 流程步骤（按 step_order 执行）
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None
        self._init_schema()
        self._enable_wal()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """打开连接并保证退出时关闭。

        历史实现直接返回 Connection，调用方 `with self._connect() as conn` 只会
        提交事务、从不 close，长时间运行会持续泄漏 sqlite 连接与文件句柄
        （多视图并发访问时尤其明显）。改为上下文管理器后调用点无需改动。
        """
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _enable_wal(self) -> None:
        """开启 WAL 日志模式，消除多视图并发访问时的读写互斥。

        GUI 每切一个视图就会起后台线程访问本库，默认的 rollback journal 模式下
        写事务会独占整个库，所有读者被阻塞（实测单次查询可被拖到数十秒，表现为
        界面卡死）。WAL 下读写互不阻塞；该设置持久化在数据库文件里，故只需一次。
        """
        try:
            with self._connect() as conn:
                row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                mode = str(row[0]).lower() if row else ""
                if mode != "wal":
                    _logger.warning("[WorkflowDB] WAL 未启用，当前模式=%s", mode or "未知")
        except Exception as exc:
            # 启用失败不阻断启动，退回默认模式仍可正常工作
            _logger.debug("[WorkflowDB] 启用 WAL 失败: %s", exc)

    def _init_schema(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    exe_path TEXT NOT NULL,
                    work_dir TEXT,
                    args_json TEXT NOT NULL DEFAULT '[]',
                    window_title_keyword TEXT NOT NULL DEFAULT '',
                    startup_delay REAL NOT NULL DEFAULT 2.0,
                    wait_window_timeout INTEGER NOT NULL DEFAULT 60,
                    notes TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id INTEGER NOT NULL,
                    step_order INTEGER NOT NULL,
                    step_type TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    name TEXT NOT NULL DEFAULT '',
                    params_json TEXT NOT NULL DEFAULT '{}',
                    on_fail TEXT NOT NULL DEFAULT 'stop',
                    FOREIGN KEY(workflow_id) REFERENCES workflows(id),
                    UNIQUE(workflow_id, step_order)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_steps_workflow_order ON steps(workflow_id, step_order)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS image_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_key TEXT NOT NULL UNIQUE,
                    template_name TEXT NOT NULL DEFAULT '',
                    image_path TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.85,
                    region_json TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS resolution_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_name TEXT NOT NULL UNIQUE,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    scale_x REAL NOT NULL DEFAULT 1.0,
                    scale_y REAL NOT NULL DEFAULT 1.0,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id INTEGER NOT NULL,
                    workflow_name TEXT NOT NULL,
                    script_name TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    duration_sec INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'running',
                    error_message TEXT NOT NULL DEFAULT '',
                    screenshot_dir TEXT NOT NULL DEFAULT '',
                    extra_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow_id ON workflow_runs(workflow_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_workflow_runs_started_at ON workflow_runs(started_at)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_launch_preferences (
                    app_key TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    script_choice TEXT NOT NULL DEFAULT '',
                    helper_enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(app_launch_preferences)").fetchall()}
            if "helper_enabled" not in columns:
                conn.execute(
                    "ALTER TABLE app_launch_preferences ADD COLUMN helper_enabled INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # 凭据表（2026-10-19 新增）：token / did / api_key 等敏感值不再存
            # app_config.json，改存这里；value 列存 DPAPI 密文（"dpapi:<base64>"），
            # 加密在 set_credential / get_credential 内完成，绕不过去。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS credentials (
                    category TEXT NOT NULL,
                    cred_key TEXT NOT NULL,
                    value TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(category, cred_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gacha_imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_code TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    source_hash TEXT NOT NULL DEFAULT '',
                    uigf_version TEXT NOT NULL DEFAULT '',
                    export_app TEXT NOT NULL DEFAULT '',
                    export_app_version TEXT NOT NULL DEFAULT '',
                    export_timestamp TEXT NOT NULL DEFAULT '',
                    lang TEXT NOT NULL DEFAULT '',
                    timezone TEXT NOT NULL DEFAULT '',
                    total_records INTEGER NOT NULL DEFAULT 0,
                    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    notes TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_gacha_imports_game_uid ON gacha_imports(game_code, uid)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gacha_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_code TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    import_id INTEGER,
                    gacha_type TEXT NOT NULL DEFAULT '',
                    uigf_gacha_type TEXT NOT NULL DEFAULT '',
                    item_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    item_type TEXT NOT NULL DEFAULT '',
                    rank_type TEXT NOT NULL DEFAULT '',
                    count INTEGER NOT NULL DEFAULT 1,
                    time TEXT NOT NULL DEFAULT '',
                    lang TEXT NOT NULL DEFAULT '',
                    timezone TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(import_id) REFERENCES gacha_imports(id),
                    UNIQUE(game_code, uid, record_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_gacha_records_game_uid_time ON gacha_records(game_code, uid, time DESC)"
            )
            # 失败记录表（2026-10-18 新增）：把"步骤/任务失败"从日志自由文本升级为
            # 可查询的结构化记录，配套 artifacts/flow_failures/ 的失败当刻截图。
            # 之前失败只落在 logs/auto_runner.log 的 warning 里，跨天回看只能全文 grep；
            # workflow_runs 表虽有完整字段但无人调用（恒空）。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS flow_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL DEFAULT 0,
                    date_key TEXT NOT NULL DEFAULT '',
                    flow_key TEXT NOT NULL DEFAULT '',
                    game_key TEXT NOT NULL DEFAULT '',
                    step TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    hwnd INTEGER NOT NULL DEFAULT 0,
                    window_title TEXT NOT NULL DEFAULT '',
                    image_path TEXT NOT NULL DEFAULT '',
                    ocr_texts TEXT NOT NULL DEFAULT '[]',
                    extra_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_flow_failures_ts ON flow_failures(ts DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_flow_failures_flow ON flow_failures(flow_key, step)"
            )

    def upsert_image_template(self, template: Dict[str, Any]) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM image_templates WHERE template_key = ?", (template["template_key"],)).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE image_templates
                    SET template_name = ?, image_path = ?, confidence = ?, region_json = ?, tags = ?, notes = ?
                    WHERE id = ?
                    """,
                    (
                        template.get("template_name", ""),
                        template["image_path"],
                        float(template.get("confidence", 0.85)),
                        template.get("region_json", ""),
                        template.get("tags", ""),
                        template.get("notes", ""),
                        row["id"],
                    ),
                )
                return int(row["id"])

            cur = conn.execute(
                """
                INSERT INTO image_templates(template_key, template_name, image_path, confidence, region_json, tags, notes)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    template["template_key"],
                    template.get("template_name", ""),
                    template["image_path"],
                    float(template.get("confidence", 0.85)),
                    template.get("region_json", ""),
                    template.get("tags", ""),
                    template.get("notes", ""),
                ),
            )
            return int(cur.lastrowid)

    def upsert_resolution_profile(self, profile: Dict[str, Any]) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM resolution_profiles WHERE profile_name = ?", (profile["profile_name"],)).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE resolution_profiles
                    SET width = ?, height = ?, scale_x = ?, scale_y = ?, is_default = ?, notes = ?
                    WHERE id = ?
                    """,
                    (
                        int(profile["width"]),
                        int(profile["height"]),
                        float(profile.get("scale_x", 1.0)),
                        float(profile.get("scale_y", 1.0)),
                        int(profile.get("is_default", 0)),
                        profile.get("notes", ""),
                        row["id"],
                    ),
                )
                return int(row["id"])
            cur = conn.execute(
                """
                INSERT INTO resolution_profiles(profile_name, width, height, scale_x, scale_y, is_default, notes)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile["profile_name"],
                    int(profile["width"]),
                    int(profile["height"]),
                    float(profile.get("scale_x", 1.0)),
                    float(profile.get("scale_y", 1.0)),
                    int(profile.get("is_default", 0)),
                    profile.get("notes", ""),
                ),
            )
            return int(cur.lastrowid)

    def start_workflow_run(self, run: Dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO workflow_runs(workflow_id, workflow_name, script_name, started_at, status, screenshot_dir, extra_json)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(run["workflow_id"]),
                    run["workflow_name"],
                    run.get("script_name", ""),
                    run["started_at"],
                    run.get("status", "running"),
                    run.get("screenshot_dir", ""),
                    run.get("extra_json", "{}"),
                ),
            )
            return int(cur.lastrowid)

    def finish_workflow_run(
        self,
        run_id: int,
        ended_at: str,
        duration_sec: int,
        status: str,
        error_message: str = "",
    ):
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE workflow_runs
                SET ended_at = ?, duration_sec = ?, status = ?, error_message = ?
                WHERE id = ?
                """,
                (ended_at, int(duration_sec), status, error_message, int(run_id)),
            )

    def record_flow_failure(self, record: Dict[str, Any]) -> int:
        """写一条失败记录，返回自增 id（供日志里交叉引用）。"""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO flow_failures(
                    ts, date_key, flow_key, game_key, step, error, retry_count,
                    hwnd, window_title, image_path, ocr_texts, extra_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(record.get("ts") or 0.0),
                    str(record.get("date_key") or ""),
                    str(record.get("flow_key") or ""),
                    str(record.get("game_key") or ""),
                    str(record.get("step") or ""),
                    str(record.get("error") or ""),
                    int(record.get("retry_count") or 0),
                    int(record.get("hwnd") or 0),
                    str(record.get("window_title") or ""),
                    str(record.get("image_path") or ""),
                    json.dumps(record.get("ocr_texts") or [], ensure_ascii=False),
                    json.dumps(record.get("extra") or {}, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid)

    def query_flow_failures(
        self,
        days: int = 7,
        limit: int = 200,
        flow_key: str = "",
        game_key: str = "",
    ) -> List[Dict[str, Any]]:
        """按时间倒序查失败记录（days<=0 表示不限时间）。"""
        clauses = []
        params: List[Any] = []
        if days and days > 0:
            clauses.append("ts >= ?")
            params.append(time.time() - days * 86400)
        if flow_key:
            clauses.append("flow_key = ?")
            params.append(flow_key)
        if game_key:
            clauses.append("game_key = ?")
            params.append(game_key)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, ts, date_key, flow_key, game_key, step, error, retry_count,
                       hwnd, window_title, image_path, ocr_texts, extra_json
                FROM flow_failures
                {where}
                ORDER BY ts DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key, default in (("ocr_texts", []), ("extra_json", {})):
                raw = item.pop(key)
                try:
                    item[key.replace("_json", "")] = json.loads(raw) if raw else default
                except Exception:
                    item[key.replace("_json", "")] = default
            out.append(item)
        return out

    def summarize_flow_failures(
        self, days: int = 7, group_limit: int = 20
    ) -> List[Dict[str, Any]]:
        """按 (flow_key, step) 聚合失败次数，用于"哪一步最常失败/最该优化"。"""
        clauses = []
        params: List[Any] = []
        if days and days > 0:
            clauses.append("ts >= ?")
            params.append(time.time() - days * 86400)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT flow_key, step, COUNT(*) AS fail_count,
                       MAX(ts) AS last_ts, MIN(ts) AS first_ts,
                       MAX(error) AS sample_error,
                       MAX(CASE WHEN image_path != '' THEN 1 ELSE 0 END) AS has_image
                FROM flow_failures
                {where}
                GROUP BY flow_key, step
                ORDER BY fail_count DESC, last_ts DESC
                LIMIT ?
                """,
                (*params, int(group_limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def purge_old_flow_failures(self, keep_days: int = 30) -> int:
        """删除过期失败记录，返回删除条数。"""
        if not keep_days or keep_days <= 0:
            return 0
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM flow_failures WHERE ts < ?", (time.time() - keep_days * 86400,)
            )
            return int(cur.rowcount or 0)

    def upsert_workflow(self, workflow: Dict[str, Any]) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM workflows WHERE name = ?", (workflow["name"],)).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE workflows
                    SET enabled = ?, exe_path = ?, work_dir = ?, args_json = ?, window_title_keyword = ?,
                        startup_delay = ?, wait_window_timeout = ?, notes = ?
                    WHERE id = ?
                    """,
                    (
                        int(workflow.get("enabled", 1)),
                        workflow["exe_path"],
                        workflow.get("work_dir"),
                        workflow.get("args_json", "[]"),
                        workflow.get("window_title_keyword", ""),
                        float(workflow.get("startup_delay", 2.0)),
                        int(workflow.get("wait_window_timeout", 60)),
                        workflow.get("notes", ""),
                        row["id"],
                    ),
                )
                return int(row["id"])

            cur = conn.execute(
                """
                INSERT INTO workflows(name, enabled, exe_path, work_dir, args_json, window_title_keyword, startup_delay, wait_window_timeout, notes)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow["name"],
                    int(workflow.get("enabled", 1)),
                    workflow["exe_path"],
                    workflow.get("work_dir"),
                    workflow.get("args_json", "[]"),
                    workflow.get("window_title_keyword", ""),
                    float(workflow.get("startup_delay", 2.0)),
                    int(workflow.get("wait_window_timeout", 60)),
                    workflow.get("notes", ""),
                ),
            )
            return int(cur.lastrowid)

    def replace_steps(self, workflow_id: int, steps: List[Dict[str, Any]]):
        with self._connect() as conn:
            conn.execute("DELETE FROM steps WHERE workflow_id = ?", (workflow_id,))
            for i, step in enumerate(steps, start=1):
                conn.execute(
                    """
                    INSERT INTO steps(workflow_id, step_order, step_type, enabled, name, params_json, on_fail)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workflow_id,
                        int(step.get("step_order", i)),
                        step["step_type"],
                        int(step.get("enabled", 1)),
                        step.get("name", ""),
                        step.get("params_json", "{}"),
                        step.get("on_fail", "stop"),
                    ),
                )

    def get_workflow_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM workflows WHERE name = ? AND enabled = 1", (name,)).fetchone()
            if not row:
                return None
            return dict(row)

    def get_steps(self, workflow_id: int) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM steps WHERE workflow_id = ? AND enabled = 1 ORDER BY step_order ASC",
                (workflow_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def save_app_launch_preference(
        self,
        app_key: str,
        enabled: bool,
        script_choice: Optional[str] = None,
        helper_enabled: Optional[bool] = None,
    ):
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT helper_enabled FROM app_launch_preferences WHERE app_key = ?",
                (app_key,),
            ).fetchone()
            helper_value = int(bool(helper_enabled)) if helper_enabled is not None else int(existing["helper_enabled"]) if existing else 0
            conn.execute(
                """
                INSERT INTO app_launch_preferences(app_key, enabled, script_choice, helper_enabled, updated_at)
                VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(app_key) DO UPDATE SET
                    enabled = excluded.enabled,
                    script_choice = excluded.script_choice,
                    helper_enabled = excluded.helper_enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    app_key,
                    int(bool(enabled)),
                    script_choice or "",
                    helper_value,
                ),
            )

    def get_app_launch_preference(self, app_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM app_launch_preferences WHERE app_key = ?",
                (app_key,),
            ).fetchone()
            return dict(row) if row else None

    def get_all_app_launch_preferences(self) -> Dict[str, Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM app_launch_preferences").fetchall()
            return {row["app_key"]: dict(row) for row in rows}

    def set_task_meta(self, key: str, value: str) -> None:
        """通用任务元数据 kv（如每周任务的最后执行周标识）。"""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_meta(key, value, updated_at)
                VALUES(?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, str(value)),
            )

    def get_task_meta(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM task_meta WHERE key = ?",
                (key,),
            ).fetchone()
            return row["value"] if row else None

    # ── 凭据（DPAPI 加密存储）──────────────────────────────────────

    def _secure_store(self):
        """懒加载 DPAPI 工具；不可用（非 Windows / 无 pywin32）时返回 None。"""
        try:
            from ..services import secure_store
            return secure_store
        except Exception:
            return None

    def set_credential(self, category: str, key: str, value: str) -> bool:
        """写入单个凭据。值经 DPAPI 加密后落库。

        Returns:
            True 表示以密文写入；False 表示 DPAPI 不可用、按明文写入。
        """
        sec = self._secure_store()
        stored = str(value or "")
        encrypted = False
        if stored and sec is not None:
            stored, encrypted = sec.try_protect(stored)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO credentials(category, cred_key, value, updated_at)
                VALUES(?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(category, cred_key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (str(category), str(key), stored),
            )
        _logger.info(
            "[WorkflowDB] 凭据写入 %s.%s (encrypted=%s, len=%d)",
            category, key, encrypted, len(stored),
        )
        return encrypted

    def get_credential(self, category: str, key: str) -> str:
        """读取单个凭据明文；不存在返回空串。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM credentials WHERE category = ? AND cred_key = ?",
                (str(category), str(key)),
            ).fetchone()
        raw = str(row["value"] or "") if row else ""
        if not raw:
            return ""
        sec = self._secure_store()
        return sec.resolve(raw) if sec is not None else raw

    def get_credential_section(self, category: str) -> Dict[str, str]:
        """读取某分类下全部凭据（key → 明文）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT cred_key, value FROM credentials WHERE category = ?",
                (str(category),),
            ).fetchall()
        sec = self._secure_store()
        result: Dict[str, str] = {}
        for row in rows:
            raw = str(row["value"] or "")
            result[str(row["cred_key"])] = sec.resolve(raw) if sec is not None else raw
        return result

    def has_credential(self, category: str, key: str) -> bool:
        """只判断是否存在（不解密）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM credentials WHERE category = ? AND cred_key = ?",
                (str(category), str(key)),
            ).fetchone()
        return bool(row and str(row["value"] or ""))

    def delete_credential(self, category: str, key: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM credentials WHERE category = ? AND cred_key = ?",
                (str(category), str(key)),
            )
            return int(cur.rowcount or 0)

    def start_gacha_import(self, meta: Dict[str, Any]) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO gacha_imports(
                    game_code, uid, source_path, source_hash, uigf_version,
                    export_app, export_app_version, export_timestamp,
                    lang, timezone, total_records, notes
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    meta["game_code"],
                    meta["uid"],
                    meta["source_path"],
                    meta.get("source_hash", ""),
                    meta.get("uigf_version", ""),
                    meta.get("export_app", ""),
                    meta.get("export_app_version", ""),
                    meta.get("export_timestamp", ""),
                    meta.get("lang", ""),
                    str(meta.get("timezone", "")),
                    int(meta.get("total_records", 0)),
                    meta.get("notes", ""),
                ),
            )
            return int(cur.lastrowid)

    def upsert_gacha_records(
        self,
        game_code: str,
        uid: str,
        import_id: int,
        records: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        record_ids = [str(record.get("record_id", "")).strip() for record in records if str(record.get("record_id", "")).strip()]
        existing_ids = set()

        with self._connect() as conn:
            chunk_size = 800
            for index in range(0, len(record_ids), chunk_size):
                chunk = record_ids[index:index + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT record_id FROM gacha_records WHERE game_code = ? AND uid = ? AND record_id IN ({placeholders})",
                    (game_code, uid, *chunk),
                ).fetchall()
                existing_ids.update(str(row["record_id"]) for row in rows)

            conn.executemany(
                """
                INSERT INTO gacha_records(
                    game_code, uid, record_id, import_id, gacha_type, uigf_gacha_type,
                    item_id, name, item_type, rank_type, count, time, lang, timezone, raw_json
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_code, uid, record_id) DO UPDATE SET
                    import_id = excluded.import_id,
                    gacha_type = excluded.gacha_type,
                    uigf_gacha_type = excluded.uigf_gacha_type,
                    item_id = excluded.item_id,
                    name = excluded.name,
                    item_type = excluded.item_type,
                    rank_type = excluded.rank_type,
                    count = excluded.count,
                    time = excluded.time,
                    lang = excluded.lang,
                    timezone = excluded.timezone,
                    raw_json = excluded.raw_json
                """,
                [
                    (
                        game_code,
                        uid,
                        str(record.get("record_id", "")).strip(),
                        int(import_id),
                        str(record.get("gacha_type", "") or ""),
                        str(record.get("uigf_gacha_type", "") or ""),
                        str(record.get("item_id", "") or ""),
                        str(record.get("name", "") or ""),
                        str(record.get("item_type", "") or ""),
                        str(record.get("rank_type", "") or ""),
                        int(record.get("count", 1) or 1),
                        str(record.get("time", "") or ""),
                        str(record.get("lang", "") or ""),
                        str(record.get("timezone", "") or ""),
                        str(record.get("raw_json", "{}") or "{}"),
                    )
                    for record in records
                    if str(record.get("record_id", "")).strip()
                ],
            )

        inserted_count = sum(1 for record_id in record_ids if record_id not in existing_ids)
        updated_count = len(record_ids) - inserted_count
        return {
            "inserted_count": inserted_count,
            "updated_count": updated_count,
            "total_count": len(record_ids),
        }

    def get_gacha_record_count(self, game_code: str, uid: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM gacha_records WHERE game_code = ? AND uid = ?",
                (game_code, uid),
            ).fetchone()
            return int(row["count"] if row else 0)

    def delete_gacha_records_by_uid(self, game_code: str, uid: str) -> int:
        """清空某游戏某 uid 下全部 gacha_records（不动 gacha_imports 历史）。

        用于在重新全量导入前清掉旧记录，避免业务键 record_id 不同的旧数据残留造成重复。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM gacha_records WHERE game_code = ? AND uid = ?",
                (game_code, uid),
            )
            return int(cur.rowcount or 0)

    def migrate_gacha_uid(
        self,
        game_code: str,
        old_uid: str,
        new_uid: str,
        record_id_prefix: str = "",
    ) -> int:
        """把某游戏下 old_uid 的所有 gacha_records + gacha_imports 迁移到 new_uid。

        若提供 record_id_prefix（如 "endfield_"），同时重写 record_id，
        保持 `record_id = prefix + new_uid + "_" + (旧 record_id 中 prefix+old_uid+"_" 之后的部分)`。
        用于把历史遗留的 uid="" 数据迁移到真实 uid，使后续 upsert 能正常合并去重。

        返回迁移的 gacha_records 行数。
        """
        if old_uid == new_uid:
            return 0
        with self._connect() as conn:
            if record_id_prefix:
                old_prefix = record_id_prefix + old_uid + "_"
                new_prefix = record_id_prefix + new_uid + "_"
                # 1) 重写符合 prefix 约定的 record_id；不符合的留作旧 id 但 uid 仍会更新
                conn.execute(
                    """
                    UPDATE gacha_records
                       SET record_id = ? || substr(record_id, ?)
                     WHERE game_code = ? AND uid = ? AND record_id LIKE ? ESCAPE '\\'
                    """,
                    (
                        new_prefix,
                        len(old_prefix) + 1,
                        game_code,
                        old_uid,
                        # 转义 LIKE 中的 _ 与 %：old_prefix 由 prefix+uid+_ 组成，
                        # uid 通常是纯数字，prefix 含字母+下划线，必须转义 _
                        old_prefix.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%") + "%",
                    ),
                )
            # 2) 统一更新 uid 列
            cur = conn.execute(
                "UPDATE gacha_records SET uid = ? WHERE game_code = ? AND uid = ?",
                (new_uid, game_code, old_uid),
            )
            migrated = int(cur.rowcount or 0)
            # 3) 同步 gacha_imports 的 uid（不依赖 record_id）
            conn.execute(
                "UPDATE gacha_imports SET uid = ? WHERE game_code = ? AND uid = ?",
                (new_uid, game_code, old_uid),
            )
            return migrated

    def get_gacha_accounts(self, game_code: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT gi.uid,
                       gi.lang,
                       gi.timezone,
                       gi.source_path,
                       gi.imported_at,
                       gi.total_records,
                       COUNT(*) AS import_count
                FROM gacha_imports gi
                INNER JOIN (
                    SELECT uid, MAX(id) AS latest_id
                    FROM gacha_imports
                    WHERE game_code = ?
                    GROUP BY uid
                ) latest ON latest.latest_id = gi.id
                WHERE gi.game_code = ?
                GROUP BY gi.uid, gi.lang, gi.timezone, gi.source_path, gi.imported_at, gi.total_records
                ORDER BY gi.imported_at DESC, gi.uid ASC
                """,
                (game_code, game_code),
            ).fetchall()
            results: List[Dict[str, Any]] = []
            for row in rows:
                import_count_row = conn.execute(
                    "SELECT COUNT(*) AS count FROM gacha_imports WHERE game_code = ? AND uid = ?",
                    (game_code, row["uid"]),
                ).fetchone()
                item = dict(row)
                item["import_count"] = int(import_count_row["count"] if import_count_row else 0)
                results.append(item)
            return results

    def get_latest_gacha_import(self, game_code: str, uid: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM gacha_imports
                WHERE game_code = ? AND uid = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (game_code, uid),
            ).fetchone()
            return dict(row) if row else None

    def get_gacha_records(self, game_code: str, uid: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM gacha_records
                WHERE game_code = ? AND uid = ?
                ORDER BY time ASC, record_id ASC
                """,
                (game_code, uid),
            ).fetchall()
            return [dict(row) for row in rows]

