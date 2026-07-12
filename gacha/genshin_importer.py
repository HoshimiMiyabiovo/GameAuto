from __future__ import annotations

import sys
from typing import List

from .hoyo_importer import (
    BANNER_NAMES,
    GAME_CODE,
    GENSHIN_GAME_CODE,
    HOYO_GAME_OPTIONS,
    HOYO_GACHA_GAME_CONFIGS,
    WORKFLOW_DB_PATH,
    get_banner_name,
    get_game_config,
    get_game_display_name,
    import_genshin_uigf,
    import_uigf,
    main as _generic_main,
)


def main(argv: List[str]) -> int:
    return _generic_main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
