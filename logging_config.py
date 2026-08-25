import logging
from logging.handlers import RotatingFileHandler
import os


def setup_logging(name: str = "sp3_bot") -> logging.Logger:
    """
    Единая настройка логирования для всего проекта.

    Использование в любом модуле:
        from logging_config import setup_logging
        logger = setup_logging(__name__)
    """

    logger = logging.getLogger(name)

    if logger.handlers:
        # Логгер уже настроен (например, при повторном импорте)
        return logger

    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ------------------------------------------------------------
    # Console handler
    # ------------------------------------------------------------

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # ------------------------------------------------------------
    # File handler (с ротацией, чтобы лог-файлы не росли бесконечно)
    # ------------------------------------------------------------

    log_dir = os.environ.get("LOG_DIR", "logs")
    os.makedirs(log_dir, exist_ok=True)

    file_handler = RotatingFileHandler(
        filename=os.path.join(log_dir, "app.log"),
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger