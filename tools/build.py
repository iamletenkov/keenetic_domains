#!/usr/bin/env python3
"""Собирает доменные списки Keenetic в формат, который понимает awg-manager.

На вход — дамп `ndmc -c 'show running-config'`, на выход:

* `lists/<имя>.txt` — простой текст, один домен в строке, комментарии через `#`;
  именно такой контент ожидает awg-manager по ссылке подписки;
* `routes.json` — массив PortableDnsRoute, где каждый маршрут ссылается на свой
  список в репозитории через `subscriptions[].url`;
* `routes-inline.json` — то же самое, но с доменами внутри (`manualDomains`),
  на случай если подписки по ссылке в вашей сборке не заработают.

Имя маршрута берётся из `description` группы, то есть человекочитаемые названия
сохраняются. Имя файла — транслитерация этого же описания.

    python3 tools/build.py --config .local/running-config.txt \
        --base-url https://raw.githubusercontent.com/<user>/<repo>/main
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

GROUP_RE = re.compile(r"^object-group fqdn (\S+)\s*$")
DESCRIPTION_RE = re.compile(r"^\s+description (.+?)\s*$")
INCLUDE_RE = re.compile(r"^\s+include (\S+)\s*$")
ROUTE_RE = re.compile(r"^\s+route object-group (\S+) (\S+)")
BLOCK_END_RE = re.compile(r"^!\s*$")

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


class Group:
    """Одна группа `object-group fqdn` из конфигурации роутера."""

    def __init__(self, ident: str) -> None:
        self.ident = ident
        self.description = ""
        self.domains: list[str] = []

    @property
    def title(self) -> str:
        return self.description or self.ident


def slugify(text: str, fallback: str) -> str:
    """Имя файла: транслитерация, нижний регистр, точки и дефисы сохраняются."""
    lowered = text.strip().lower()
    out = "".join(TRANSLIT.get(char, char) for char in lowered)
    out = re.sub(r"[^a-z0-9._-]+", "-", out)
    out = re.sub(r"-{2,}", "-", out).strip("-._")
    return out or fallback


def parse_config(text: str) -> tuple[dict[str, Group], dict[str, str]]:
    """Возвращает группы по идентификатору и карту «группа -> интерфейс»."""
    groups: dict[str, Group] = {}
    routes: dict[str, str] = {}
    current: Group | None = None
    in_dns_proxy = False

    for line in text.splitlines():
        if BLOCK_END_RE.match(line):
            current = None
            in_dns_proxy = False
            continue

        group_match = GROUP_RE.match(line)
        if group_match:
            current = Group(group_match.group(1))
            groups[current.ident] = current
            continue

        if line.rstrip() == "dns-proxy":
            in_dns_proxy = True
            continue

        if current is not None:
            description_match = DESCRIPTION_RE.match(line)
            if description_match:
                current.description = description_match.group(1)
                continue
            include_match = INCLUDE_RE.match(line)
            if include_match:
                current.domains.append(include_match.group(1))
            continue

        if in_dns_proxy:
            route_match = ROUTE_RE.match(line)
            if route_match:
                routes[route_match.group(1)] = route_match.group(2)

    return groups, routes


def assign_slugs(groups: dict[str, Group]) -> dict[str, str]:
    """Имя файла на группу; коллизии разводятся идентификатором группы."""
    taken: set[str] = set()
    slugs: dict[str, str] = {}
    for ident, group in groups.items():
        slug = slugify(group.title, ident)
        if slug in taken:
            slug = f"{slug}-{ident}"
        taken.add(slug)
        slugs[ident] = slug
    return slugs


def write_list(path: Path, group: Group, interface: str | None, stamp: str) -> int:
    """Пишет текстовый список. Домены сортируются — так диффы в git читаемы."""
    domains = sorted(set(group.domains))
    header = [
        f"# {group.title}",
        f"# группа Keenetic: {group.ident}",
        f"# маршрут: {interface or 'не назначен'}",
        f"# выгружено: {stamp}",
        "",
    ]
    path.write_text("\n".join(header + domains) + "\n", encoding="utf-8")
    return len(domains)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--base-url",
        required=True,
        help="префикс сырых файлов репозитория, без завершающего слеша",
    )
    parser.add_argument("--out", type=Path, default=Path.cwd())
    args = parser.parse_args()

    groups, routes = parse_config(args.config.read_text(encoding="utf-8"))
    if not groups:
        print("в конфиге не найдено ни одной object-group fqdn", file=sys.stderr)
        return 1

    lists_dir = args.out / "lists"
    lists_dir.mkdir(parents=True, exist_ok=True)
    for stale in lists_dir.glob("*.txt"):
        stale.unlink()

    slugs = assign_slugs(groups)
    stamp = date.today().isoformat()
    base = args.base_url.rstrip("/")

    portable: list[dict] = []
    inline: list[dict] = []
    total = 0

    for ident, group in sorted(groups.items(), key=lambda item: slugs[item[0]]):
        interface = routes.get(ident)
        slug = slugs[ident]
        count = write_list(lists_dir / f"{slug}.txt", group, interface, stamp)
        total += count
        domains = sorted(set(group.domains))
        portable.append(
            {
                "name": group.title,
                "enabled": interface is not None,
                "subscriptions": [
                    {"url": f"{base}/lists/{slug}.txt", "name": group.title}
                ],
            }
        )
        inline.append(
            {
                "name": group.title,
                "enabled": interface is not None,
                "manualDomains": domains,
            }
        )
        print(f"{slug:<28} {count:>5} доменов   {interface or '—'}")

    for name, payload in (("routes.json", portable), ("routes-inline.json", inline)):
        (args.out / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    routed = sum(1 for ident in groups if ident in routes)
    print(
        f"\nгрупп: {len(groups)} (с маршрутом: {routed}), доменов всего: {total}",
        f"\nсписки: {lists_dir}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
