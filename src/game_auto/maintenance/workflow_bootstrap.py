import json
from typing import List, Dict, Any

from game_auto.storage.workflow_db import WorkflowDB
from game_auto.settings.paths import ASSETS_DIR, WORKFLOW_DB_PATH


def seed_demo_workflow(db_path: str):
    """
    初始化一个演示流程（可按你的实际路径改）。
    """
    db = WorkflowDB(db_path)
    workflow_id = db.upsert_workflow(
        {
            "name": "DemoGenericTask",
            "enabled": 1,
            "exe_path": r"D:\Path\To\YourLauncher.exe",
            "work_dir": r"D:\Path\To",
            "args_json": json.dumps(["--example"]),
            "window_title_keyword": "示例窗口标题",
            "startup_delay": 2.0,
            "wait_window_timeout": 60,
            "notes": "通用演示流程",
        }
    )

    steps: List[Dict[str, Any]] = [
        {
            "step_order": 1,
            "step_type": "screenshot",
            "name": "启动后截图",
            "params_json": json.dumps({"name": "demo_before"}),
            "on_fail": "continue",
        },
        {
            "step_order": 2,
            "step_type": "wait_image",
            "name": "等待开始按钮出现",
            "params_json": json.dumps(
                {
                    "image_path": str(ASSETS_DIR / "start_btn.png"),
                    "timeout": 30,
                    "confidence": 0.85,
                }
            ),
            "on_fail": "stop",
        },
        {
            "step_order": 3,
            "step_type": "click_image",
            "name": "点击开始按钮",
            "params_json": json.dumps(
                {
                    "image_path": str(ASSETS_DIR / "start_btn.png"),
                    "timeout": 10,
                    "confidence": 0.85,
                    "clicks": 1,
                    "delay_after": 1.0,
                }
            ),
            "on_fail": "stop",
        },
        {
            "step_order": 4,
            "step_type": "click_point",
            "name": "点击确认坐标",
            "params_json": json.dumps(
                {
                    "x": 1200,
                    "y": 720,
                    "clicks": 1,
                    "delay_after": 1.0,
                }
            ),
            "on_fail": "continue",
        },
        {
            "step_order": 5,
            "step_type": "sleep",
            "name": "等待加载",
            "params_json": json.dumps({"seconds": 3}),
            "on_fail": "continue",
        },
        {
            "step_order": 6,
            "step_type": "screenshot",
            "name": "任务后截图",
            "params_json": json.dumps({"name": "demo_after"}),
            "on_fail": "continue",
        },
    ]
    db.replace_steps(workflow_id, steps)


if __name__ == "__main__":
    demo_db = str(WORKFLOW_DB_PATH)
    seed_demo_workflow(demo_db)
    print(f"已写入演示流程到: {demo_db}")
