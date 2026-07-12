import logging
from typing import Tuple

from .foreground_flow_runner import run_flow


def run_xm_background(logger: logging.Logger) -> Tuple[bool, str]:
    return run_flow(logger, "XM", stop_after_step="运行中确认")


def run_cj_background(logger: logging.Logger) -> Tuple[bool, str]:
    return run_flow(logger, "CJ")

