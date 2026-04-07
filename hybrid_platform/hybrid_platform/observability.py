from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class MetricsRecorder:
    counters: Dict[str, int] = field(default_factory=dict)
    timings_ms: Dict[str, list[float]] = field(default_factory=dict)

    def inc(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def observe_ms(self, name: str, value: float) -> None:
        self.timings_ms.setdefault(name, []).append(value)

    def timer(self, name: str):
        return _Timer(self, name)

    def snapshot(self) -> Dict[str, object]:
        p95 = {}
        for key, values in self.timings_ms.items():
            if not values:
                continue
            vals = sorted(values)
            idx = int(0.95 * (len(vals) - 1))
            p95[key] = vals[idx]
        return {"counters": self.counters, "p95_ms": p95}

    def to_json(self) -> str:
        return json.dumps(self.snapshot(), ensure_ascii=False, indent=2)


class _Timer:
    def __init__(self, recorder: MetricsRecorder, name: str) -> None:
        self.recorder = recorder
        self.name = name
        self.start = 0.0

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed_ms = (time.perf_counter() - self.start) * 1000
        self.recorder.observe_ms(self.name, elapsed_ms)
