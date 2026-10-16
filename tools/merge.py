#!/usr/bin/env python3
"""Доливает в репозиторий доменные группы с другого роутера Keenetic.

`build.py` пересобирает репозиторий с нуля из одного роутера. Этот скрипт
ничего не удаляет: он берёт дамп `show running-config` второго роутера и

* дописывает в существующие `lists/<имя>.txt` домены, которых там не было;
* заводит новые списки для групп, которых в репозитории нет;
* пересобирает `routes.json` и `routes-inline.json` по всем спискам.

Keenetic сопоставляет домен с записью группы по окончанию строки: запись
`example.com` ловит и `www.example.com`, и вообще любое имя, которое на неё
заканчивается. Поэтому повторы убираются так:

* в списке не остаётся домена, у которого в этом же списке есть родитель
  (`www.viber.com` при `viber.com`) — Keenetic поймает его и так;
* с другого роутера не добавляется домен, уже покрытый любым списком.

Покрытие проверяется по границе домена (`.example.com`), а не по строке: так
`facebook.com` не считается покрытым опечаткой `acebook.com`.

Группа сопоставляется со списком сначала по имени (с учётом копий, которые
awg-manager создаёт с суффиксом `_p1`), затем по пересечению доменов: если
не меньше половины доменов группы уже лежат в одном списке, это тот же сервис.

    python3 tools/merge.py --config .local/upstroy-config.txt --label Upstroy \
        --base-url https://raw.githubusercontent.com/<user>/<repo>/main
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

import build

AWG_SUFFIX = "_p1"
MIN_OVERLAP = 0.5

# Клоны: опечатки и подделки под бренды (faceboook.com, achat-followers-instagram.com).
# Для работы сервисов они не нужны, а из-за сравнения по окончанию строки ещё и
# затягивают чужие домены. Оставляются только домены, без которых сервис не работает.
CLONE_BRANDS = ("facebook", "instagram")
CLONE_KEEP = {"cdninstagram.com", "instagramstatic.com", "facebookmail.com"}

# Списки, собранные вручную: группы роутеров сводятся к ним по имени, но домены
# в них не доливаются (старые instagram-1/2 и meta-1/2 были раздуты сотнями
# лишних доменов, а Keenetic и так ловит поддомены корневых записей).
CURATED = {"instagram", "facebook", "meta"}
ALIASES = {
    "instagram-1": "facebook", "meta-1": "facebook",
    "instagram-2": "instagram", "meta-2": "meta", "threads": "meta",
}


class ListFile:
    """Список домена в репозитории: заголовок-комментарии и множество доменов."""

    def __init__(self, path: Path, header: list[str], domains: set[str]) -> None:
        self.path = path
        self.header = header
        self.domains = domains

    @classmethod
    def read(cls, path: Path) -> "ListFile":
        header, domains = [], set()
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                header.append(line)
            elif line.strip():
                domains.add(line.strip())
        return cls(path, header, domains)

    def write(self) -> None:
        body = self.header + [""] + sorted(self.domains)
        self.path.write_text("\n".join(body) + "\n", encoding="utf-8")

    @property
    def title(self) -> str:
        return self.header[0][1:].strip() if self.header else self.path.stem

    @property
    def routed(self) -> bool:
        for line in self.header:
            if line.startswith("# маршрут:"):
                return "не назначен" not in line
        return False


def base_name(title: str) -> str:
    """Имя сервиса без суффикса копии awg-manager."""
    return title[: -len(AWG_SUFFIX)] if title.endswith(AWG_SUFFIX) else title


def name_candidates(title: str) -> list[str]:
    """Возможные имена файла: awg-manager меняет `-` и `.` на `_`."""
    base = base_name(title)
    variants = {base, base.replace("_", "-"), base.replace("_", ".")}
    if "_" in base:
        head, tail = base.rsplit("_", 1)
        variants.add(f"{head.replace('_', '-')}.{tail}")
    return [build.slugify(v, base) for v in variants]


def levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def is_clone(entry: str) -> bool:
    """Опечатка или подделка под бренд; также записи без зоны, кроме зон брендов."""
    if is_cidr(entry) or entry in CLONE_KEEP:
        return False
    if "." not in entry:
        return "-" in entry  # instagram-ads, whatsapp-ads; cisco/dell/intel — зоны
    label = entry.split(".")[-2]
    for brand in CLONE_BRANDS:
        if label == brand:
            return False
        if len(label) >= 5 and levenshtein(label, brand) <= 2:
            return True
        if brand in label.replace("-", ""):
            return True
    return False


def load_excluded(path: Path) -> set[str]:
    if not path.exists():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    return {l.strip() for l in lines if l.strip() and not l.startswith("#")}


def is_cidr(entry: str) -> bool:
    return "/" in entry or entry.replace(".", "").isdigit() or ":" in entry


def covered(entry: str, pool: set[str]) -> bool:
    """Домен уже ловится записью из `pool` (сам или по родителю)."""
    if is_cidr(entry):
        return entry in pool
    labels = entry.split(".")
    return any(".".join(labels[i:]) in pool for i in range(len(labels)))


def prune(domains: set[str]) -> set[str]:
    """Убирает домены, родитель которых есть в этом же наборе."""
    keep = set()
    for entry in domains:
        if is_cidr(entry):
            keep.add(entry)
            continue
        labels = entry.split(".")
        if not any(".".join(labels[i:]) in domains for i in range(1, len(labels))):
            keep.add(entry)
    return keep


def best_overlap(domains: set[str], lists: dict[str, ListFile]) -> str | None:
    best, score = None, 0.0
    for slug, item in lists.items():
        if not domains:
            break
        share = len(domains & item.domains) / len(domains)
        if share > score:
            best, score = slug, share
    return best if score >= MIN_OVERLAP else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--label", required=True, help="имя роутера для заголовков")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out", type=Path, default=Path.cwd())
    args = parser.parse_args()

    groups, routes = build.parse_config(args.config.read_text(encoding="utf-8"))
    lists_dir = args.out / "lists"
    lists = {p.stem: ListFile.read(p) for p in sorted(lists_dir.glob("*.txt"))}
    stamp = date.today().isoformat()
    excluded = load_excluded(args.out / "exclude.txt")

    def unwanted(entry: str) -> bool:
        return covered(entry, excluded) or is_clone(entry)

    # Копии awg-manager первыми: у них есть маршрут, а одноимённые старые группы
    # без маршрута потом сливаются в тот же список по пересечению доменов.
    order = sorted(groups.values(), key=lambda g: (not g.title.endswith(AWG_SUFFIX), g.title))
    added, created = {}, []
    for group in order:
        domains = set(group.domains)
        if not domains:
            continue
        names = [ALIASES.get(c, c) for c in name_candidates(group.title)]
        slug = next((c for c in names if c in lists), None)
        if slug in CURATED:
            continue
        slug = slug or best_overlap(domains, lists)
        if slug is None:
            title = base_name(group.title).replace("_", "-")
            slug = build.slugify(title, group.ident)
            interface = routes.get(group.ident)
            header = [
                f"# {title}",
                f"# группа Keenetic: {group.ident} ({args.label})",
                f"# маршрут: {interface or 'не назначен'}",
                f"# выгружено: {stamp}",
            ]
            lists[slug] = ListFile(lists_dir / f"{slug}.txt", header, set())
            created.append(slug)
        everywhere = set().union(*(item.domains for item in lists.values()))
        fresh = {d for d in domains if not covered(d, everywhere) and not unwanted(d)}
        if fresh:
            lists[slug].domains |= fresh
            added[slug] = added.get(slug, 0) + len(fresh)

    pruned = {}
    for slug, item in lists.items():
        before = len(item.domains)
        item.domains = prune({d for d in item.domains if not unwanted(d)})
        if len(item.domains) < before:
            pruned[slug] = before - len(item.domains)

    for slug in [s for s in created if not lists[s].domains]:
        del lists[slug]
        created.remove(slug)
        added.pop(slug, None)

    for slug in set(added) | set(pruned):
        lists[slug].write()

    base = args.base_url.rstrip("/")
    portable, inline = [], []
    for slug, item in sorted(lists.items()):
        portable.append({
            "name": item.title,
            "enabled": item.routed,
            "subscriptions": [{"url": f"{base}/lists/{slug}.txt", "name": item.title}],
        })
        inline.append({"name": item.title, "enabled": item.routed,
                       "manualDomains": sorted(item.domains)})
    for name, payload in (("routes.json", portable), ("routes-inline.json", inline)):
        (args.out / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for slug in sorted(added):
        mark = "новый" if slug in created else "дополнен"
        print(f"{slug:<28} +{added[slug]:>4}   {mark}")
    for slug in sorted(pruned):
        print(f"{slug:<28} -{pruned[slug]:>4}   повторы, клоны, исключения")
    print(f"\nновых списков: {len(created)}, дополнено: {len(set(added) - set(created))}, "
          f"доменов добавлено: {sum(added.values())}, убрано повторов: {sum(pruned.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
