#!/usr/bin/env python3

from __future__ import annotations

import argparse
import configparser
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://funpay.com"
RAISE_URL = f"{BASE_URL}/lots/raise"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

logger = logging.getLogger("funpay_autoup")


class FunPayError(Exception):
    pass


class UnauthorizedError(FunPayError):
    pass


class RequestError(FunPayError):
    pass


class RaiseError(FunPayError):
    def __init__(self, message: str, wait_time: Optional[int] = None, raw=None):
        super().__init__(message)
        self.message = message
        self.wait_time = wait_time
        self.raw = raw


@dataclass
class SubCategory:
    id: int
    name: str
    type: str


@dataclass
class Category:
    id: int
    name: str
    subcategories: list = field(default_factory=list)

    @property
    def common_subcategory_ids(self):
        return [s.id for s in self.subcategories if s.type == "lots"]


@dataclass
class Config:
    golden_key: str
    user_agent: str = DEFAULT_USER_AGENT
    request_timeout: float = 15.0
    proxy: Optional[str] = None
    interval: int = 14400
    category_cooldown: int = 14400
    category_delay: float = 3.0
    retry_delay: float = 30.0
    max_retries: int = 0
    ignore_cooldown: bool = False
    games: list = field(default_factory=list)
    log_level: str = "INFO"
    log_file: str = "logs/funpay_autoup.log"


def _parse_wait_time(message: str) -> Optional[int]:
    text = message.lower()
    numbers = re.findall(r"\d+", message)
    if "час" in text:
        return int(numbers[0]) * 3600 if numbers else 3600
    if "мин" in text:
        return (int(numbers[0]) - 1) * 60 if numbers else 60
    if "сек" in text:
        return int(numbers[0]) if numbers else 2
    if "подождите" in text:
        return 60
    return None


class FunPayClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers["User-Agent"] = cfg.user_agent
        self.session.headers["Accept-Language"] = "ru-RU,ru;q=0.9,en;q=0.8"
        self.session.cookies.set("golden_key", cfg.golden_key, domain="funpay.com")
        self.proxies = {"http": cfg.proxy, "https": cfg.proxy} if cfg.proxy else None
        self.csrf_token: Optional[str] = None
        self.username: Optional[str] = None

    def _get(self, url: str) -> requests.Response:
        try:
            return self.session.get(
                url, timeout=self.cfg.request_timeout, proxies=self.proxies
            )
        except requests.RequestException as exc:
            raise RequestError(f"Ошибка сети при запросе {url}: {exc}") from exc

    def fetch_account(self) -> tuple[str, list]:
        response = self._get(BASE_URL)
        if response.status_code == 403:
            raise UnauthorizedError("FunPay вернул 403 (неверный/устаревший golden_key).")
        if response.status_code != 200:
            raise RequestError(f"GET {BASE_URL} вернул статус {response.status_code}.")

        html = response.content.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")

        name_div = soup.find("div", {"class": "user-link-name"})
        if not name_div:
            raise UnauthorizedError(
                "Не удалось найти данные аккаунта, скорее всего golden_key невалиден."
            )
        self.username = name_div.text.strip()

        body = soup.find("body")
        if body and body.has_attr("data-app-data"):
            try:
                self.csrf_token = json.loads(body["data-app-data"]).get("csrf-token")
            except (ValueError, TypeError):
                self.csrf_token = None

        return self.username, self._parse_categories(html)

    @staticmethod
    def _parse_categories(html: str) -> list:
        soup = BeautifulSoup(html, "html.parser")
        lists = soup.find_all("div", {"class": "promo-game-list"})
        if not lists:
            return []
        table = lists[1] if len(lists) > 1 else lists[0]

        categories = []
        for item in table.find_all("div", {"class": "promo-game-item"}):
            title_div = item.find("div", {"class": "game-title"})
            if not title_div or not title_div.get("data-id"):
                continue
            link_tag = item.find("a")
            game_id = int(title_div["data-id"])
            game_name = link_tag.text.strip() if link_tag else str(game_id)

            category = Category(id=game_id, name=game_name)
            for li in item.find_all("li"):
                link = li.find("a")
                if not link or not link.get("href"):
                    continue
                match = re.search(r"/(?:lots|chips)/(\d+)", link["href"])
                if not match:
                    continue
                sub_type = "chips" if "chips" in link["href"] else "lots"
                category.subcategories.append(
                    SubCategory(int(match.group(1)), link.text.strip(), sub_type)
                )
            categories.append(category)
        return categories

    def raise_category(self, category: Category) -> None:
        sub_ids = category.common_subcategory_ids
        if not sub_ids:
            return

        data = {
            "game_id": str(category.id),
            "node_id": str(sub_ids[0]),
            "node_ids[]": [str(i) for i in sub_ids],
        }
        headers = {
            "accept": "*/*",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
            "referer": f"{BASE_URL}/",
        }

        try:
            response = self.session.post(
                RAISE_URL,
                data=data,
                headers=headers,
                timeout=self.cfg.request_timeout,
                proxies=self.proxies,
            )
        except requests.RequestException as exc:
            raise RaiseError(f"Ошибка сети при поднятии: {exc}") from exc

        if response.status_code == 403:
            raise UnauthorizedError("FunPay вернул 403 при поднятии лотов.")
        if response.status_code != 200:
            raise RaiseError(f"HTTP {response.status_code} при поднятии лотов.")

        try:
            payload = response.json()
        except ValueError:
            raise RaiseError(f"Неожиданный ответ FunPay: {response.text[:200]!r}")

        if not payload.get("error"):
            return

        message = payload.get("msg") or payload.get("MSG") or f"FunPay вернул ошибку: {payload}"
        raise RaiseError(message, wait_time=_parse_wait_time(message), raw=payload)


class AutoRaiser:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = FunPayClient(cfg)
        self.last_raise: dict = {}
        self._stop = False

    def stop(self, *_) -> None:
        if not self._stop:
            logger.info("Получен сигнал завершения, останавливаюсь...")
        self._stop = True

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while not self._stop and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))

    def run_forever(self) -> None:
        logger.info(
            "Запуск. Интервал цикла: %d c, кд категории: %d c, пауза между категориями: %.1f c, "
            "повторов на ошибку: %s, retry_delay: %.1f c.",
            self.cfg.interval,
            self.cfg.category_cooldown,
            self.cfg.category_delay,
            self.cfg.max_retries if self.cfg.max_retries else "бесконечно",
            self.cfg.retry_delay,
        )
        while not self._stop:
            started = time.monotonic()
            try:
                self.cycle()
            except UnauthorizedError as exc:
                logger.error("Авторизация не удалась: %s", exc)
            except FunPayError as exc:
                logger.error("Ошибка цикла: %s", exc)
            except Exception:
                logger.exception("Непредвиденная ошибка цикла")

            if self._stop:
                break
            remaining = self.cfg.interval - (time.monotonic() - started)
            if remaining > 0:
                logger.info("Следующий цикл через %d c.", int(remaining))
                self._sleep(remaining)
        logger.info("Остановлено.")

    def cycle(self) -> None:
        username, categories = self.client.fetch_account()
        logger.info("Аккаунт: %s. Найдено категорий: %d.", username, len(categories))

        if not categories:
            logger.warning(
                "Категории не найдены. Возможно, изменилась вёрстка FunPay или у аккаунта нет лотов."
            )

        if self.cfg.games:
            categories = [c for c in categories if c.id in self.cfg.games]
            logger.info("После фильтра по games осталось категорий: %d.", len(categories))

        success = 0
        skipped = 0
        failed = 0
        for category in categories:
            if self._stop:
                break
            result = self.raise_one(category)
            if result == "ok":
                success += 1
            elif result == "skip":
                skipped += 1
            else:
                failed += 1
            self._sleep(self.cfg.category_delay)

        logger.info(
            "Цикл завершён. Поднято: %d, пропущено: %d, с ошибкой: %d.",
            success, skipped, failed,
        )

    def raise_one(self, category: Category) -> str:
        if not category.common_subcategory_ids:
            logger.warning("Категория \"%s\" не содержит обычных лотов, пропуск.", category.name)
            return "skip"

        now = time.time()
        last = self.last_raise.get(category.id, 0)
        if now - last < self.cfg.category_cooldown:
            logger.info(
                "Категория \"%s\" на кд, осталось %d c.",
                category.name, int(self.cfg.category_cooldown - (now - last)),
            )
            return "skip"

        attempt = 0
        while not self._stop:
            attempt += 1
            try:
                self.client.raise_category(category)
                self.last_raise[category.id] = time.time()
                logger.info("Категория \"%s\": лоты подняты (попытка %d).", category.name, attempt)
                return "ok"
            except UnauthorizedError:
                raise
            except RaiseError as exc:
                if exc.wait_time is not None:
                    self.last_raise[category.id] = time.time()
                    logger.warning(
                        "Категория \"%s\": FunPay просит подождать %d c. (%s)",
                        category.name, exc.wait_time, exc.message,
                    )
                    if not self.cfg.ignore_cooldown:
                        return "skip"
                    self._sleep(exc.wait_time)
                else:
                    logger.warning(
                        "Категория \"%s\": ошибка поднятия (попытка %d): %s",
                        category.name, attempt, exc.message,
                    )
            except Exception as exc:
                logger.warning(
                    "Категория \"%s\": ошибка запроса (попытка %d): %s",
                    category.name, attempt, exc,
                )

            if self.cfg.max_retries and attempt >= self.cfg.max_retries:
                logger.error(
                    "Категория \"%s\": исчерпаны попытки (%d), пропускаю.", category.name, attempt
                )
                return "fail"
            self._sleep(self.cfg.retry_delay)

        return "fail"


def setup_logging(cfg: Config) -> None:
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    log_path = cfg.log_file
    log_dir = os.path.dirname(os.path.abspath(log_path))
    os.makedirs(log_dir, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )

    logger.addHandler(console)
    logger.addHandler(file_handler)


def _as_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "да")


def load_config(path: str) -> Config:
    parser = configparser.ConfigParser()
    if not parser.read(path, encoding="utf-8"):
        raise FileNotFoundError(f"Не найден файл конфигурации: {path}")

    def get(section: str, option: str, default: Optional[str] = None) -> Optional[str]:
        if parser.has_option(section, option):
            value = parser.get(section, option).strip()
            return value if value != "" else default
        return default

    golden_key = os.environ.get("FUNPAY_GOLDEN_KEY") or get("account", "golden_key")
    if not golden_key:
        raise ValueError(
            "Не задан golden_key. Укажите его в config.ini ([account] golden_key) "
            "или в переменной окружения FUNPAY_GOLDEN_KEY."
        )

    games_raw = get("raise", "games", "") or ""
    games = [int(x) for x in re.split(r"[,\s]+", games_raw) if x.strip().isdigit()]

    cfg = Config(
        golden_key=golden_key,
        user_agent=get("account", "user_agent", DEFAULT_USER_AGENT),
        request_timeout=float(get("account", "request_timeout", "15")),
        proxy=get("account", "proxy"),
        interval=int(get("raise", "interval", "14400")),
        category_cooldown=int(get("raise", "category_cooldown", "14400")),
        category_delay=float(get("raise", "category_delay", "3")),
        retry_delay=float(get("raise", "retry_delay", "30")),
        max_retries=int(get("raise", "max_retries", "0")),
        ignore_cooldown=_as_bool(get("raise", "ignore_cooldown"), False),
        games=games,
        log_level=get("logging", "level", "INFO"),
        log_file=get("logging", "file", "logs/funpay_autoup.log"),
    )

    if cfg.interval <= 0:
        raise ValueError("Параметр interval должен быть больше 0.")
    if cfg.category_cooldown < 0:
        raise ValueError("Параметр category_cooldown не может быть отрицательным.")
    return cfg


def main() -> int:
    arg_parser = argparse.ArgumentParser(description="FunPayAutoUP")
    arg_parser.add_argument(
        "-c", "--config", default="config.ini", help="Путь к файлу конфигурации"
    )
    args = arg_parser.parse_args()

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg)

    raiser = AutoRaiser(cfg)
    signal.signal(signal.SIGINT, raiser.stop)
    try:
        signal.signal(signal.SIGTERM, raiser.stop)
    except (ValueError, AttributeError):
        pass

    try:
        raiser.run_forever()
    except KeyboardInterrupt:
        raiser.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
