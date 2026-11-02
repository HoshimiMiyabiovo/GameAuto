import json

from game_auto.storage.workflow_db import WorkflowDB
from game_auto.settings.paths import TEMPLATES_DIR, WORKFLOW_DB_PATH


def seed_xm_templates(db_path: str):
    db = WorkflowDB(db_path)
    db.upsert_image_template(
        {
            "template_key": "xm.enter_offline",
            "template_name": "XM 进入脱机",
            "image_path": str(TEMPLATES_DIR / "进入脱机.png"),
            "confidence": 0.85,
            "region_json": json.dumps([]),
            "tags": "xm,offline",
            "notes": "XM流程步骤1",
        }
    )
    db.upsert_image_template(
        {
            "template_key": "xm.login",
            "template_name": "XM 登录",
            "image_path": str(TEMPLATES_DIR / "XM登录.png"),
            "confidence": 0.85,
            "region_json": json.dumps([]),
            "tags": "xm,login",
            "notes": "XM流程步骤2",
        }
    )
    db.upsert_image_template(
        {
            "template_key": "xm.start",
            "template_name": "XM 开始",
            "image_path": str(TEMPLATES_DIR / "XM开始.png"),
            "confidence": 0.85,
            "region_json": json.dumps([]),
            "tags": "xm,start",
            "notes": "XM流程步骤3",
        }
    )
    db.upsert_image_template(
        {
            "template_key": "xm.running",
            "template_name": "XM 运行中",
            "image_path": str(TEMPLATES_DIR / "XM运行中.png"),
            "confidence": 0.85,
            "region_json": json.dumps([]),
            "tags": "xm,running",
            "notes": "XM流程运行状态判定",
        }
    )


if __name__ == "__main__":
    seed_xm_templates(str(WORKFLOW_DB_PATH))
    print("XM 模板已写入数据库")
