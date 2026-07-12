import os
import sqlite3
from typing import Any, Dict, List, Optional


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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

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

