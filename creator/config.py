"""Загрузка конфигурации из окружения / файла .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Ключи цен, которые админ может менять из панели.
# Денежные значения — в копейках (MONEY_KEYS), остальные — целые числа.
PRICE_KEYS = {
    "PRICE_CLICKER": "Шаблон «Кликер бот», ₽",
    "PRICE_REFBOT": "Шаблон «Реф. бот», ₽",
    "PRICE_STARBOT": "Шаблон «Звёздный бот», ₽",
    "HOSTING_DAY_PRICE": "Хостинг, ₽ в сутки",
    "START_HOSTING_DAYS": "Бесплатных дней при создании",
    "VIP_PRICE": "VIP для бота, ₽",
    "REF_PERCENT": "Партнёрский процент",
    "RESALE_FEE_PERCENT": "Комиссия перепродажи, %",
    "LAVE_USD_RUB": "LavePay: курс ₽ за 1 $ (счёт выставляется в $)",
    # TGrass (Доп заработок)
    "TGRASS_REWARD": "TGrass: награда владельцу за 1 ОП, ₽",
    "TGRASS_MIN_USERS": "TGrass: минимум пользователей в боте",
    # PiarFlow (Доп заработок)
    "PIARFLOW_REWARD": "PiarFlow: награда владельцу за 1 задание, ₽",
    "PIARFLOW_MIN_USERS": "PiarFlow: минимум пользователей в боте",
}

MONEY_KEYS = frozenset({"PRICE_CLICKER", "PRICE_REFBOT", "PRICE_STARBOT", "HOSTING_DAY_PRICE", "VIP_PRICE",
                        "TGRASS_REWARD", "PIARFLOW_REWARD"})

PRICE_DEFAULTS = {
    "PRICE_CLICKER": 22500,      # 225 ₽
    "PRICE_REFBOT": 22500,       # 225 ₽
    "PRICE_STARBOT": 30000,      # 300 ₽
    "HOSTING_DAY_PRICE": 450,    # 4,50 ₽ в сутки (7/30/90 дн. = 32/135/405 ₽)
    "START_HOSTING_DAYS": 7,
    "VIP_PRICE": 15000,          # 150 ₽
    "REF_PERCENT": 10,
    "RESALE_FEE_PERCENT": 5,
    "LAVE_USD_RUB": 85,
    "TGRASS_REWARD": 50,         # 0,50 ₽ за ОП
    "TGRASS_MIN_USERS": 50,
    "PIARFLOW_REWARD": 50,       # 0,50 ₽ за засчитанное задание
    "PIARFLOW_MIN_USERS": 50,
}


def load_dotenv(path: Path) -> None:
    """Мини-загрузчик .env: не перезаписывает уже заданные переменные."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Config:
    bot_token: str = ""
    admin_ids: set[int] = field(default_factory=set)
    chat_url: str = ""
    channel_url: str = ""
    admin_url: str = ""
    data_dir: Path = PROJECT_ROOT / "data"
    launch_date: str = ""
    log_level: str = "INFO"
    prices: dict[str, int] = field(default_factory=lambda: dict(PRICE_DEFAULTS))
    lavepay_api_key: str = ""
    lavepay_base_url: str = "https://laveshka.io/merchant/v1"
    lavepay_webhook_secret: str = ""
    lavepay_webhook_host: str = "127.0.0.1"
    lavepay_webhook_port: int = 0  # 0 — вебхук выключен, работает только опрос
    tgrass_factory_token: str = ""  # BOT_MARKET_TOKEN фабрики ботов (выдаёт @tgrass_support_bot)
    tgrass_base_url: str = "https://tgrass.space"
    tgrass_owner_id: int = 0        # Telegram ID владельца аккаунта TGrass (по умолчанию первый из ADMIN_IDS)
    tgrass_owner_username: str = ""  # его логин в Telegram без @
    piarflow_token: str = ""        # API token аккаунта PiarFlow (Личный кабинет → Интеграция API)
    piarflow_base_url: str = "https://piarflow.com/v1"
    piarflow_hub_api_key: str = ""  # API key хаб-бота для режима «трансляция» (или data/piarflow_hub.json)
    tgrass_hub_api_key: str = ""    # auth_token хаб-бота TGrass для «трансляции» (или data/tgrass_hub.json)

    @property
    def piarflow_enabled(self) -> bool:
        return bool(self.piarflow_token)

    @property
    def tgrass_enabled(self) -> bool:
        return bool(self.tgrass_factory_token)

    @property
    def lavepay_enabled(self) -> bool:
        return bool(self.lavepay_api_key)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "creator.db"

    @property
    def bots_dir(self) -> Path:
        return self.data_dir / "bots"


def load_config(env_file: Path | None = None) -> Config:
    load_dotenv(env_file or PROJECT_ROOT / ".env")
    admin_ids = {
        int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()
    }
    data_dir = Path(os.environ.get("DATA_DIR", "") or PROJECT_ROOT / "data")
    if not data_dir.is_absolute():
        data_dir = (PROJECT_ROOT / data_dir).resolve()
    return Config(
        bot_token=os.environ.get("BOT_TOKEN", "").strip(),
        admin_ids=admin_ids,
        chat_url=os.environ.get("CHAT_URL", "").strip(),
        channel_url=os.environ.get("CHANNEL_URL", "").strip(),
        admin_url=os.environ.get("ADMIN_URL", "").strip(),
        data_dir=data_dir,
        launch_date=os.environ.get("LAUNCH_DATE", "").strip(),
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO",
        prices={k: _int(k, v) for k, v in PRICE_DEFAULTS.items()},
        lavepay_api_key=os.environ.get("LAVEPAY_API_KEY", "").strip(),
        lavepay_base_url=(os.environ.get("LAVEPAY_BASE_URL", "").strip()
                          or "https://laveshka.io/merchant/v1").rstrip("/"),
        lavepay_webhook_secret=os.environ.get("LAVEPAY_WEBHOOK_SECRET", "").strip(),
        lavepay_webhook_host=os.environ.get("LAVEPAY_WEBHOOK_HOST", "").strip() or "127.0.0.1",
        lavepay_webhook_port=_int("LAVEPAY_WEBHOOK_PORT", 0),
        tgrass_factory_token=(os.environ.get("TGRASS_FACTORY_TOKEN", "").strip()
                              or os.environ.get("TGRASS_API_KEY", "").strip()),
        tgrass_base_url=(os.environ.get("TGRASS_BASE_URL", "").strip() or "https://tgrass.space").rstrip("/"),
        tgrass_owner_id=_int("TGRASS_OWNER_ID", min(admin_ids) if admin_ids else 0),
        tgrass_owner_username=os.environ.get("TGRASS_OWNER_USERNAME", "").strip().lstrip("@"),
        piarflow_token=os.environ.get("PIARFLOW_TOKEN", "").strip(),
        piarflow_base_url=(os.environ.get("PIARFLOW_BASE_URL", "").strip() or "https://piarflow.com/v1").rstrip("/"),
        piarflow_hub_api_key=os.environ.get("PIARFLOW_HUB_API_KEY", "").strip(),
        tgrass_hub_api_key=os.environ.get("TGRASS_HUB_API_KEY", "").strip(),
    )
