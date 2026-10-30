from collections import defaultdict


def rrf(lexical, semantic, groups, *, k=60):
    """Equal weights; collapse each branch BEFORE fusion to prevent duplicate votes."""
    if k <= 0:
        raise ValueError("RRF k must be positive")
    scores, representatives = defaultdict(float), {}
    for branch in [lexical, semantic]:
        seen = set()
        for event_id in branch:
            unit = groups[event_id]
            if unit in seen:
                continue
            seen.add(unit)
            scores[unit] += 1 / (k + len(seen))
            representatives.setdefault(unit, event_id)
    ordered = sorted(scores, key=lambda unit: (-scores[unit], unit))
    return [representatives[unit] for unit in ordered]
