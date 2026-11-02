from __future__ import annotations

import json
from datetime import datetime, timedelta


def format_countdown(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def group_times(entries: list[dict], threshold_seconds: float) -> list[list[dict]]:
    ordered = sorted(entries, key=lambda item: item["seconds"])
    clusters: list[list[dict]] = []
    for entry in ordered:
        if clusters and entry["seconds"] - clusters[-1][0]["seconds"] <= threshold_seconds:
            clusters[-1].append(entry)
        else:
            clusters.append([entry])
    return clusters


def compute_schedule(
    captured_at: datetime,
    timers: dict[str, dict],
    threshold_minutes: float = 10,
    buffer_seconds: int = 60,
) -> list[dict]:
    entries = [
        {"station": station, "seconds": data["seconds"]}
        for station, data in timers.items()
    ]
    clusters = group_times(entries, threshold_minutes * 60)
    points: list[dict] = []
    for cluster in clusters:
        anchor = max(cluster, key=lambda item: item["seconds"])
        fire_at = captured_at + timedelta(
            seconds=anchor["seconds"] + buffer_seconds
        )
        points.append(
            {
                "at": fire_at.isoformat(timespec="seconds"),
                "anchor": anchor["station"],
                "countdown": format_countdown(anchor["seconds"]),
                "stations": [item["station"] for item in cluster],
            }
        )
    points.sort(key=lambda item: item["at"])
    return points


def self_test() -> None:
    captured = datetime.fromisoformat("2026-11-01T14:15:21")
    timers = {
        "技术中心": {"seconds": 3 * 3600 + 1 * 60 + 57},
        "工作台": {"seconds": 5 * 3600 + 1 * 60 + 41},
        "制药台": {"seconds": 5 * 3600 + 1 * 60 + 52},
        "防具台": {"seconds": 5 * 3600 + 1 * 60 + 46},
    }
    points = compute_schedule(captured, timers)
    print(json.dumps(points, ensure_ascii=False, indent=2))
    assert len(points) == 2, points
    assert points[0]["at"] == "2026-11-01T17:18:18", points[0]
    assert points[0]["stations"] == ["技术中心"], points[0]
    assert points[1]["at"] == "2026-11-01T19:18:13", points[1]
    assert set(points[1]["stations"]) == {"工作台", "制药台", "防具台"}, points[1]


if __name__ == "__main__":
    self_test()
    print("planner self-test passed")
