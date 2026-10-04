#!/usr/bin/env python3
"""
Замер скорости скачивания.

Делает N последовательных запросов к URL, считает среднее время,
перцентили времени, объём скачанного и итоговую скорость.

Использование:
    python speedtest.py <url> [-n 10] [-v]
"""
import argparse
import logging
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from statistics import mean, quantiles
from urllib.error import URLError
from urllib.request import Request, urlopen

MB = 1_000_000              # десятичный мегабайт, как в тарифах провайдеров
PERCENTILES = (50, 90, 99)  # какие перцентили времени запроса считать
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"

# Логи о ходе работы идут в stderr, результаты замеров — print в stdout.
# Так вывод можно перенаправить в файл (> result.txt) без служебного шума.
log = logging.getLogger("speedtest")


# ── Модели данных ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Sample:
    """Результат одного запроса."""
    seconds: float
    size: int  # байт

    @property
    def speed(self) -> float:
        """Скорость этого запроса, байт/с."""
        return self.size / self.seconds


@dataclass(frozen=True)
class Report:
    """Сводная статистика по всей серии запросов."""
    avg: float                     # среднее время запроса, с
    percentiles: dict[int, float]  # {50: 0.42, 90: ...}, с
    total_size: int                # всего скачано, байт
    speed: float                   # общий объём / общее время, байт/с


# ── Измерение: единственное место с сетевым вводом-выводом ───────────

Fetcher = Callable[[str], Sample]


class UnexpectedContent(Exception):
    """Сервер ответил успешно, но прислал не файл (например, HTML-заглушку)."""


def http_fetch(url: str) -> Sample:
    """Скачивает ресурс целиком и замеряет, сколько это заняло."""
    # Многие хостинги и CDN отвечают 403 на дефолтный "Python-urllib/x.y",
    # поэтому представляемся обычным браузером.
    request = Request(url, headers={"User-Agent": USER_AGENT})
    start = time.perf_counter()
    with urlopen(request) as resp:
        # Заголовки уже получены, тело ещё нет — удобная точка для отладки.
        log.debug("Ответ: HTTP %s, %s, Content-Length: %s",
                  resp.status, resp.headers.get("Content-Type"),
                  resp.headers.get("Content-Length", "не указан"))
        size = len(resp.read())  # дочитываем тело до конца, иначе замер неполный
        elapsed = time.perf_counter() - start
        content_type = resp.headers.get_content_type()
        final_url = resp.url  # urlopen сам проходит по редиректам

    if final_url != url:
        log.debug("Редирект: %s -> %s", url, final_url)

    # Страница вместо файла даст бессмысленную «скорость» — лучше упасть явно.
    if content_type == "text/html":
        raise UnexpectedContent(
            f"вместо файла пришла HTML-страница ({size} байт) с {final_url}"
        )
    return Sample(seconds=elapsed, size=size)


def measure(url: str, n: int, fetch: Fetcher = http_fetch) -> Iterator[Sample]:
    """Последовательно выполняет n запросов и отдаёт результаты по одному.

    Способ скачивания передаётся параметром, поэтому его легко подменить
    (например, заглушкой в тестах) без правки остального кода.
    """
    for i in range(1, n + 1):
        log.info("Запрос %d/%d: скачивание...", i, n)
        yield fetch(url)


# ── Статистика: чистые вычисления, без I/O ──────────────────────────

def summarize(samples: list[Sample], percentiles=PERCENTILES) -> Report:
    """Сворачивает список замеров в сводный отчёт."""
    times = [s.seconds for s in samples]
    total_size = sum(s.size for s in samples)

    # 99 границ между перцентилями; p-й перцентиль лежит по индексу p-1.
    # inclusive: интерполяция строго внутри [min, max] выборки.
    cuts = quantiles(times, n=100, method="inclusive")

    return Report(
        avg=mean(times),
        percentiles={p: cuts[p - 1] for p in percentiles},
        total_size=total_size,
        speed=total_size / sum(times),
    )


# ── Форматирование: единые правила вывода чисел ─────────────────────

def _fmt_size(size: int) -> str:
    return f"{size / MB:.2f} MB"


def _fmt_time(seconds: float) -> str:
    return f"{seconds:.2f} s"


def _fmt_speed(bps: float) -> str:
    return f"{bps / MB:.2f} MB/s ({bps * 8 / MB:.1f} Mbit/s)"


def format_sample(i: int, s: Sample) -> str:
    """Строка прогресса для одного запроса."""
    return f"#{i:<3} {_fmt_size(s.size):>10}  {_fmt_time(s.seconds):>8}  {_fmt_speed(s.speed)}"


def format_report(r: Report) -> str:
    """Итоговый блок со сводной статистикой."""
    pct = " | ".join(f"p{p} {_fmt_time(v)}" for p, v in r.percentiles.items())
    return (
        f"Время:    avg {_fmt_time(r.avg)} | {pct}\n"
        f"Скачано:  {_fmt_size(r.total_size)}\n"
        f"Скорость: {_fmt_speed(r.speed)}"
    )


# ── Точка входа: только связывает части между собой ─────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Замер скорости скачивания")
    parser.add_argument("url", help="адрес тяжёлого файла, например картинки")
    parser.add_argument("-n", type=int, default=10, help="число запросов (по умолчанию 10)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="подробные логи: заголовки ответов, редиректы")
    args = parser.parse_args()
    if args.n < 2:
        parser.error("для перцентилей нужно минимум 2 запроса")
    return args


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)

    log.info("Старт замера: %s, запросов: %d", args.url, args.n)
    started = time.perf_counter()

    samples = []
    try:
        for i, sample in enumerate(measure(args.url, args.n), start=1):
            print(format_sample(i, sample), flush=True)  # сразу, чтобы был виден прогресс
            samples.append(sample)
    except (URLError, UnexpectedContent) as e:  # HTTPError (403, 404...) — подкласс URLError
        log.error("Ошибка запроса к %s: %s", args.url, e)
        sys.exit(1)
    except KeyboardInterrupt:
        log.warning("Прервано пользователем после %d запросов", len(samples))
        sys.exit(130)

    log.info("Замер завершён за %s", _fmt_time(time.perf_counter() - started))
    print()
    print(format_report(summarize(samples)))


if __name__ == "__main__":
    main()