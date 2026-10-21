"""Knesset seat allocation (Bader-Ofer / D'Hondt with surplus-vote agreements)."""

from __future__ import annotations

SEATS = 120
THRESHOLD = 0.0325


def bader_ofer(votes: dict[str, float], agreements: list[list[str]] | None = None,
               seats: int = SEATS, threshold: float = THRESHOLD) -> dict:
    """Allocate seats.

    * Lists below ``threshold`` of all valid votes are eliminated.
    * Quota = votes of passing lists / seats; each list gets floor(votes / quota).
    * Remaining seats go one at a time to the unit with the highest votes/(seats+1).
      Two lists with a surplus agreement form one unit (only if both pass).
    * Seats won by a pair are split between them with the same D'Hondt rule.
    """
    valid = sum(votes.values())
    result = {l: 0 for l in votes}
    if valid <= 0:
        return {"seats": result, "threshold_votes": 0, "passing": [], "pairs": [], "void_pairs": []}
    min_votes = valid * threshold
    passing = [l for l, v in votes.items() if v > 0 and v >= min_votes]
    vp = sum(votes[l] for l in passing)
    for l in passing:
        result[l] = int(votes[l] / vp * seats)
    used: set[str] = set()
    units: list[list[str]] = []
    pairs, void_pairs = [], []
    for pair in agreements or []:
        a, b = pair[0], pair[1]
        if a in passing and b in passing and a not in used and b not in used:
            units.append([a, b])
            pairs.append([a, b])
            used.update((a, b))
        elif a in votes or b in votes:
            void_pairs.append([a, b])
    units += [[l] for l in passing if l not in used]
    unit_votes = [sum(votes[l] for l in u) for u in units]
    unit_seats = [sum(result[l] for l in u) for u in units]
    left = seats - sum(unit_seats)
    while left > 0 and units:
        best = max(range(len(units)), key=lambda i: unit_votes[i] / (unit_seats[i] + 1))
        unit_seats[best] += 1
        left -= 1
    for u, s in zip(units, unit_seats):
        if len(u) == 1:
            result[u[0]] = s
            continue
        a, b = u
        r = s - result[a] - result[b]
        while r > 0:
            if votes[a] / (result[a] + 1) >= votes[b] / (result[b] + 1):
                result[a] += 1
            else:
                result[b] += 1
            r -= 1
    return {"seats": result, "threshold_votes": min_votes, "passing": passing,
            "pairs": pairs, "void_pairs": void_pairs}
