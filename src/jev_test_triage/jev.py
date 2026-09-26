"""Thin Jev client: disk cache, spend ledger with a hard budget, parallel requests.

Answers come back as plain dicts so composition code does not depend on SDK types:
  noul   -> {"noul": p}
  score  -> {"score": s, "confidence": c, "probabilities": {...}}
  choice -> {"choice": k, "confidence": c, "probabilities": {...}}
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from typesafe_sdk import (
    TypeSafeAuthenticationError,
    TypeSafeClient,
    TypeSafeError,
    TypeSafePermissionDeniedError,
)

MODEL = os.environ.get("JTT_MODEL", "jev-1.13.0")  # pinned: thresholds are tuned per version
USD_PER_INPUT_TOKEN = 0.042 / 1_000_000


class BudgetExceeded(RuntimeError):
    """The ledger reached its spend limit; no further requests are sent."""


class JevFatal(RuntimeError):
    """A non-transient API failure (auth, permission, billing): stop and ask a human."""


class Ledger:
    """Cumulative input tokens across runs, persisted to a JSON file."""

    def __init__(self, path: Path | None, budget_usd: float | None):
        self.path, self.budget = path, budget_usd
        self.lock = threading.Lock()
        self.tokens = 0
        if path and path.exists():
            self.tokens = json.loads(path.read_text()).get("input_tokens", 0)

    @property
    def usd(self) -> float:
        return self.tokens * USD_PER_INPUT_TOKEN

    def check(self) -> None:
        if self.budget is not None and self.usd >= self.budget:
            raise BudgetExceeded(f"Jev spend ${self.usd:.4f} reached the ${self.budget:.2f} budget")

    def add(self, tokens: int) -> None:
        with self.lock:
            self.tokens += tokens
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"input_tokens": self.tokens, "usd": round(self.usd, 6)}))


def _plain(answer) -> dict:
    d = answer.model_dump(mode="json", exclude_none=True)
    d.pop("legend", None)
    return d


def dump_questions(questions: Mapping) -> dict:
    return {k: (q.model_dump(mode="json", exclude_none=True) if hasattr(q, "model_dump") else q)
            for k, q in questions.items()}


class Jev:
    def __init__(self, cache_dir: Path | None = None, ledger: Path | None = None,
                 budget_usd: float | None = None, model: str = MODEL, workers: int = 8,
                 offline: bool = False):
        self.cache_dir = cache_dir
        self.ledger = Ledger(ledger, budget_usd)
        self.model = model
        self.workers = workers
        self.offline = offline  # replay the cache only; a miss raises
        self._client: TypeSafeClient | None = None
        self.calls = self.hits = 0

    def _key(self, state, qs: dict) -> str:
        blob = json.dumps({"m": self.model, "s": state, "q": qs}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def ask(self, state, questions: Mapping) -> dict:
        """One System One request. Returns {"answers": {...}, "input_tokens": n, "cached": bool}."""
        qs = dump_questions(questions)
        key = self._key(state, qs)
        path = self.cache_dir / key[:2] / f"{key}.json" if self.cache_dir else None
        if path and path.exists():
            self.hits += 1
            return {**json.loads(path.read_text()), "cached": True}
        if self.offline:
            raise KeyError(f"cache miss in offline mode ({key[:12]})")
        self.ledger.check()
        if self._client is None:
            self._client = TypeSafeClient(model=self.model, timeout=120.0)
        try:
            r = self._client.system_one(state=state, questions=questions)
        except (TypeSafeAuthenticationError, TypeSafePermissionDeniedError) as e:
            raise JevFatal(f"{type(e).__name__}: check TYPESAFE_API_KEY and account status") from e
        except TypeSafeError as e:
            status = getattr(e, "status_code", None)
            if status in (401, 402, 403):
                raise JevFatal(f"{type(e).__name__} ({status}): check account and billing") from e
            raise
        self.calls += 1
        self.ledger.add(r.usage.input_tokens)
        out = {"model": r.model, "input_tokens": r.usage.input_tokens,
               "answers": {k: _plain(a) for k, a in r.answers.items()}}
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(out))
        return {**out, "cached": False}

    def ask_many(self, requests: list[tuple[object, Mapping]]) -> list[dict]:
        with ThreadPoolExecutor(self.workers) as ex:
            return list(ex.map(lambda sq: self.ask(*sq), requests))

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
