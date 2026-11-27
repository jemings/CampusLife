"""Compare Korean full-text search analyzers on a synthetic corpus this script writes itself.

    uv run python tools/search_bench.py [--blocks 100000] [--dir DIR]

The indexes go to a temporary directory removed at the end; with --dir they go to a new
directory under DIR and stay there.

Every number it prints comes from local synthetic text (lecture notes, notices and spoken
transcripts made from a fixed word list and seed), never from real data. Checking the choice
against a copy of a real database is #67.

Variants:
  unicode61         FTS5 unicode61, each query term a quoted phrase
  unicode61-prefix  the same with every term a prefix query ("term"*)
  trigram           FTS5 trigram; a query with a term under 3 characters scans with LIKE
  bigram            hangul-bigram@1 (campuslife_hub.search.analyzer): analyzed text in FTS5
                    with the original (through fold_original) in a plain table for scans
  bigram-contentless  the same with a contentless FTS5 table, so the original is stored once

A block is relevant when its NFC text holds every query term as a case-folded substring.
recall@10 = |top 10 ∩ relevant| / min(10, |relevant|); recall = the same without a limit;
FP counts returned blocks that are not relevant. Latency is plan + query + fetch of the top 10
on a warm page cache, split into queries the index answers (MATCH) and queries that scan.
"""

from __future__ import annotations

import argparse
import os
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from campuslife_hub.search.analyzer import HANGUL_BIGRAM, LIKE_ESCAPE, SearchPlan, plan_query

SEED = 11
REPEATS = 5

NOUNS = (
    "자료구조 알고리즘 이진탐색트리 연결리스트 해시테이블 그래프 최단경로 동적계획법 정렬 병합정렬 퀵정렬 힙 큐 스택 "
    "트리 노드 간선 정점 재귀 시간복잡도 공간복잡도 운영체제 프로세스 스레드 교착상태 스케줄링 메모리 가상메모리 페이지 "
    "캐시 데이터베이스 트랜잭션 정규화 인덱스 네트워크 라우팅 프로토콜 컴파일러 파서 문법 선형대수 행렬 고유값 미분 적분 "
    "확률 통계 분산 평균 회귀 과제 시험 중간고사 기말고사 마감 제출 강의 교수 조교 학기 학점 성적 출석 공지 장학금 동아리 "
    "축제 세미나 수강신청 휴강 보강 실습 보고서 발표 팀플 도서관 기숙사 식당 등록금 졸업 인턴십 공모전 해커톤 면접 이력서"
).split()
PARTICLES = "은 는 이 가 을 를 의 에 에서 으로 로 와 과 도 만 부터 까지 에게 보다 처럼".split()
ENDINGS = (
    "입니다 합니다 했습니다 됩니다 있습니다 없습니다 해야 합니다 하세요 이라고 하면 하는데 이고 이며 하기 때문에 "
    "중요합니다 설명했습니다 다룹니다 확인하세요 제출하세요 공부합니다"
).split()
ADVERBS = "그리고 그래서 하지만 또한 특히 먼저 다음으로 마지막으로 예를 들어 즉 따라서 반드시 대략 보통".split()
FILLERS = "음 어 자 그러니까 이제 뭐 네 그죠".split()
ENGLISH = "BST Python Java heap Dijkstra O(n) O(log n) LMS PDF API SQL B-tree hash quicksort mergesort CPU GPU TCP/IP Git".split()

QUERIES = {
    "1syl": ["힙", "큐", "값", "뷁"],
    "2syl": ["과제", "시험", "마감", "제출", "트리", "정렬", "학점", "공지", "강의", "재귀", "벚꽃"],
    "particle": ["자료구조", "알고리즘", "운영체제", "데이터베이스", "장학금", "기말고사"],
    "infix": ["탐색트리", "탐색", "리스트", "메모리", "경로", "고사"],
    "mixed": ["Python 과제", "BST 노드", "heap 정렬", "Dijkstra 최단경로", "SQL 트랜잭션"],
    "english": ["heap", "Dijkstra", "quicksort", "B-tree"],
}


def _word(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.55:
        word = rng.choice(NOUNS)
        return word + rng.choice(PARTICLES) if rng.random() < 0.7 else word
    if r < 0.65:
        word = rng.choice(ENGLISH)
        return word + rng.choice(PARTICLES) if rng.random() < 0.3 else word
    return rng.choice(ADVERBS) if r < 0.8 else rng.choice(ENDINGS)


def _sentence(rng: random.Random) -> str:
    return " ".join(_word(rng) for _ in range(rng.randint(4, 10))) + rng.choice(".?!.")


def _note(rng: random.Random) -> str:
    return "\n".join(f"- {_sentence(rng)}" for _ in range(rng.randint(2, 6)))


def _notice(rng: random.Random) -> str:
    head = f"[공지] {rng.randint(1, 12)}월 {rng.randint(1, 28)}일 {rng.choice(NOUNS)} 안내"
    return head + "\n" + " ".join(_sentence(rng) for _ in range(rng.randint(2, 5)))


def _transcript(rng: random.Random) -> str:
    return " ".join(f"{rng.choice(FILLERS)} {_sentence(rng)}" for _ in range(rng.randint(3, 8)))


def corpus(blocks: int) -> list[str]:
    """Synthetic blocks shaped like lecture notes, notices and spoken transcripts."""
    rng = random.Random(SEED)
    shapes = (_note, _notice, _transcript)
    return [rng.choice(shapes)(rng) for _ in range(blocks)]


def _terms(query: str) -> list[str]:
    return unicodedata.normalize("NFC", query).split()


def _quoted(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _like(term: str) -> str:
    return "%" + term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2).replace("%", LIKE_ESCAPE + "%").replace("_", LIKE_ESCAPE + "_") + "%"


@dataclass(frozen=True)
class Variant:
    name: str
    create: str
    row: Callable[[int, str], tuple]
    insert: str
    plan: Callable[[str], SearchPlan | None]
    holds_original: bool


def _unicode61_plan(prefix: bool) -> Callable[[str], SearchPlan]:
    return lambda query: SearchPlan(" AND ".join(_quoted(t) + ("*" if prefix else "") for t in _terms(query)), ())


def _trigram_plan(query: str) -> SearchPlan:
    terms = _terms(query)
    if any(len(t) < 3 for t in terms):
        return SearchPlan(None, tuple(_like(t) for t in terms))
    return SearchPlan(" AND ".join(_quoted(t) for t in terms), ())


def _bigram_plan(query: str) -> SearchPlan | None:
    return plan_query(query, HANGUL_BIGRAM)


_ORIGINAL = "CREATE TABLE original(id INTEGER PRIMARY KEY, text TEXT NOT NULL);"

VARIANTS = (
    Variant("unicode61", "CREATE VIRTUAL TABLE fts USING fts5(text, tokenize='unicode61');",
            lambda i, t: (i, t), "INSERT INTO fts(rowid, text) VALUES (?, ?)", _unicode61_plan(False), True),
    Variant("unicode61-prefix", "CREATE VIRTUAL TABLE fts USING fts5(text, tokenize='unicode61');",
            lambda i, t: (i, t), "INSERT INTO fts(rowid, text) VALUES (?, ?)", _unicode61_plan(True), True),
    Variant("trigram", "CREATE VIRTUAL TABLE fts USING fts5(text, tokenize='trigram');",
            lambda i, t: (i, t), "INSERT INTO fts(rowid, text) VALUES (?, ?)", _trigram_plan, True),
    Variant("bigram", f"CREATE VIRTUAL TABLE fts USING fts5(body, tokenize='{HANGUL_BIGRAM.fts5_tokenize}');" + _ORIGINAL,
            lambda i, t: (i, HANGUL_BIGRAM.analyze(t)), "INSERT INTO fts(rowid, body) VALUES (?, ?)", _bigram_plan, False),
    Variant("bigram-contentless",
            f"CREATE VIRTUAL TABLE fts USING fts5(body, content='', contentless_delete=1, tokenize='{HANGUL_BIGRAM.fts5_tokenize}');" + _ORIGINAL,
            lambda i, t: (i, HANGUL_BIGRAM.analyze(t)), "INSERT INTO fts(rowid, body) VALUES (?, ?)", _bigram_plan, False),
)


def _search(con: sqlite3.Connection, variant: Variant, query: str, limit: int | None) -> tuple[list[int], bool]:
    """The ids found, best first, and whether the query had to scan text."""
    plan = variant.plan(query)
    if plan is None:
        return [], False
    text = "fts.text" if variant.holds_original else "original.text"
    scans = "".join(f" AND {text} LIKE ? ESCAPE '{LIKE_ESCAPE}'" for _ in plan.like)
    tail = "" if limit is None else f" LIMIT {limit}"
    source = "fts" if variant.holds_original else "fts JOIN original ON original.id = fts.rowid"
    if plan.match is None:
        table = "fts" if variant.holds_original else "original"
        rows = con.execute(f"SELECT {table}.rowid FROM {table} WHERE 1{scans}{tail}", plan.like)
    else:
        rows = con.execute(f"SELECT fts.rowid FROM {source} WHERE fts MATCH ?{scans} ORDER BY rank{tail}", (plan.match, *plan.like))
    return [row[0] for row in rows], bool(plan.like)


@dataclass
class Result:
    analyze_s: float
    build_s: float
    size_bytes: int
    match_ms: list[float]
    scan_ms: list[float]
    per_category: dict[str, tuple[float, float, int]]


def measure(variant: Variant, texts: list[str], folded: list[str], directory: Path) -> Result:
    path = directory / f"{variant.name}.sqlite3"
    con = sqlite3.connect(path)
    con.executescript(variant.create)
    started = time.perf_counter()
    rows = [variant.row(i, t) for i, t in enumerate(texts)]
    analyzed = time.perf_counter()
    with con:
        con.executemany(variant.insert, rows)
        if not variant.holds_original:
            con.executemany("INSERT INTO original(id, text) VALUES (?, ?)", enumerate(HANGUL_BIGRAM.fold_original(t) for t in texts))
        con.execute("INSERT INTO fts(fts) VALUES ('optimize')")
    built = time.perf_counter()
    con.execute("VACUUM")
    con.close()
    size = os.path.getsize(path)

    con = sqlite3.connect(path)
    match_ms: list[float] = []
    scan_ms: list[float] = []
    per_category = {}
    for category, queries in QUERIES.items():
        at_10, at_all, false_positives = [], [], 0
        for query in queries:
            needles = [t.casefold() for t in _terms(query)]
            relevant = {i for i, text in enumerate(folded) if all(n in text for n in needles)}
            for _ in range(REPEATS):
                t0 = time.perf_counter()
                top, scanned = _search(con, variant, query, 10)
                (scan_ms if scanned else match_ms).append((time.perf_counter() - t0) * 1000)
            found = set(_search(con, variant, query, None)[0])
            if relevant:
                at_10.append(len(set(top) & relevant) / min(10, len(relevant)))
                at_all.append(len(found & relevant) / len(relevant))
            false_positives += len(found - relevant)
        per_category[category] = (
            statistics.mean(at_10) if at_10 else float("nan"),
            statistics.mean(at_all) if at_all else float("nan"),
            false_positives,
        )
    con.close()
    return Result(analyzed - started, built - started, size, match_ms, scan_ms, per_category)


def _p(values: list[float], q: float) -> str:
    if not values:
        return "-"
    ordered = sorted(values)
    return f"{ordered[round(q * (len(ordered) - 1))]:.2f}"


def main(argv: list[str] | None = None) -> dict[str, Result]:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--blocks", type=int, default=100_000)
    parser.add_argument(
        "--dir", type=Path, help="build the indexes in a new directory under DIR and keep them (default: a temporary directory, removed)"
    )
    args = parser.parse_args(argv)

    texts = corpus(args.blocks)
    folded = [unicodedata.normalize("NFC", t).casefold() for t in texts]
    print(
        f"출처: 로컬 합성 (seed {SEED}, blocks {len(texts)}, chars {sum(map(len, texts))}) · "
        f"Python {sys.version.split()[0]} · SQLite {sqlite3.sqlite_version} · {platform.system()} {platform.machine()} · warm page cache"
    )
    if args.dir is None:
        with tempfile.TemporaryDirectory() as directory:
            results = {variant.name: measure(variant, texts, folded, Path(directory)) for variant in VARIANTS}
    else:
        directory = Path(tempfile.mkdtemp(dir=args.dir))
        results = {variant.name: measure(variant, texts, folded, directory) for variant in VARIANTS}

    print()
    print("| variant | analyze s | build s | size MB | MATCH p50/p95/max ms | scan p50/p95/max ms | all p95 ms |")
    print("|---|---|---|---|---|---|---|")
    for name, r in results.items():
        print(
            f"| {name} | {r.analyze_s:.2f} | {r.build_s:.2f} | {r.size_bytes / 1e6:.1f} "
            f"| {_p(r.match_ms, 0.5)}/{_p(r.match_ms, 0.95)}/{_p(r.match_ms, 1)} "
            f"| {_p(r.scan_ms, 0.5)}/{_p(r.scan_ms, 0.95)}/{_p(r.scan_ms, 1)} | {_p(r.match_ms + r.scan_ms, 0.95)} |"
        )
    print()
    print("| variant | " + " | ".join(f"{c} r@10/recall/FP" for c in QUERIES) + " |")
    print("|---|" + "---|" * len(QUERIES))
    for name, r in results.items():
        cells = " | ".join(f"{a:.2f}/{b:.2f}/{fp}" for a, b, fp in r.per_category.values())
        print(f"| {name} | {cells} |")
    return results


if __name__ == "__main__":
    main()
