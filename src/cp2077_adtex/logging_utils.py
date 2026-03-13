"""Pipeline logger setup — file + console dual output.

Each stage creates a timestamped log file (output/pipeline_<timestamp>.log)
and also streams to the console.  The logger name includes stage + timestamp
to avoid collisions when multiple stages run in the same process.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from .io_utils import ensure_dir


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def setup_pipeline_logger(output_dir: Path, stage: str) -> tuple[logging.Logger, Path]:
    ensure_dir(output_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = output_dir / f"pipeline_{timestamp}.log"

    logger = logging.getLogger(f"cp2077_adtex.{stage}.{timestamp}")
    logger.setLevel(logging.INFO)
    logger.handlers = []
    logger.propagate = False

    formatter = logging.Formatter(LOG_FORMAT)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger, log_path
