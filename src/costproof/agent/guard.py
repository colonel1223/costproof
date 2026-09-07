"""Check a model-written narrative against the tool output it was given.

Why this exists
---------------
The system prompt tells the model that every figure it states must appear verbatim in
the tool output. A prompt is a request, not a guarantee. This module is the guarantee:
it extracts every number the narrative contains and refuses the narrative if any of
them cannot be found in the evidence the model was shown.

It also refuses an empty narrative. The first live watsonx run returned zero characters
-- a chat model called through the legacy completion endpoint emits end-of-sequence
immediately -- and the report silently omitted the narrative section while its header
still credited the model. A governance layer that can be blank without anyone noticing
is not a governance layer.

What "grounded" means here
--------------------------
A number in the narrative is grounded if the same number, after normalisation, occurs
anywhere in the evidence text: dollar signs and thousands separators removed, trailing
zeros after a decimal point dropped, percentages compared as written. "$970,158" matches
"970158.0"; "10.6%" matches "10.6%"; "$1.3M" does not match anything and is refused,
because the model was told not to round.

Small integers (under 100) are ignored: "4 findings", "3 requiring approval", "60 days"
are structural, not financial, and the false-positive rate on them would make the check
useless.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Matches $970,158  1,435,302  0.65  10.6%  212576.4  -- but not bare small integers.
_NUMBER = re.compile(r"(?<![\w.])\$?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s?(%?)")

#: Below this, a bare integer is treated as a count, not a figure.
_MIN_FIGURE = 100


@dataclass
class GuardResult:
    ok: bool
    problems: list[str] = field(default_factory=list)
    figures_checked: int = 0
    figures_ungrounded: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return f"passed: {self.figures_checked} figure(s) traced to tool output"
        return "; ".join(self.problems)


def _normalise(int_part: str, frac: str | None, pct: str) -> str:
    """Canonical form of a number token: no separators, no trailing decimal zeros."""
    digits = int_part.replace(",", "")
    if frac:
        frac = frac.rstrip("0")
        if frac == ".":
            frac = ""
    else:
        frac = ""
    return f"{digits}{frac}{pct}"


def extract_numbers(text: str) -> list[tuple[str, str]]:
    """Return (as_written, normalised) for every figure-sized number in ``text``."""
    out: list[tuple[str, str]] = []
    for m in _NUMBER.finditer(text):
        int_part, frac, pct = m.group(1), m.group(2), m.group(3)
        norm = _normalise(int_part, frac, pct)
        is_percent = pct == "%"
        has_frac = bool(frac)
        value = float(int_part.replace(",", "") + (frac or ""))
        # Skip counts: small bare integers with no decimal, no % and no $.
        if not is_percent and not has_frac and value < _MIN_FIGURE and "$" not in m.group(0):
            continue
        out.append((m.group(0).strip(), norm))
    return out


def _norm_str(number_text: str) -> str:
    """Normalise a plain numeric string like '10.60%' or '970158.0' the same way the
    narrative side is normalised, so the two sides compare like for like."""
    m = _NUMBER.fullmatch(number_text)
    if not m:
        return number_text
    return _normalise(m.group(1), m.group(2), m.group(3))


def evidence_tokens(evidence_text: str) -> set[str]:
    """Every normalised number present in the evidence, plus the forms prose may use.

    The evidence is JSON: floats arrive as 970158.37. The narrative may legitimately
    write $970,158 (the report itself renders impacts with no decimals), 0.65 for
    0.648, or 10.6% for 0.106. Those are display precision and units. It may NOT write
    $1.3M or $970,000: thousands- and millions-rounding is arithmetic the model was told
    not to perform, and neither form is generated here.
    """
    tokens: set[str] = set()
    for _, norm in extract_numbers(evidence_text):
        tokens.add(norm)
        if "%" in norm:
            continue
        value = float(norm)
        # display precision: whole number, one and two decimals
        tokens.add(str(round(value)))
        tokens.add(_norm_str(f"{round(value, 1):.1f}"))
        tokens.add(_norm_str(f"{round(value, 2):.2f}"))
        # unit: a fraction written as a percentage, at zero, one and two decimals
        pct = value * 100
        for digits in (0, 1, 2):
            tokens.add(_norm_str(f"{round(pct, digits):.{digits}f}%"))
    return tokens


def check_narrative(narrative: str, evidence_text: str, min_chars: int = 40) -> GuardResult:
    """Refuse an empty narrative or one containing a figure the evidence does not."""
    text = (narrative or "").strip()
    if len(text) < min_chars:
        return GuardResult(ok=False, problems=[
            f"model returned {len(text)} character(s); a narrative needs at least {min_chars}"
        ])
    allowed = evidence_tokens(evidence_text)
    found = extract_numbers(text)
    ungrounded = [written for written, norm in found if norm not in allowed]
    if ungrounded:
        return GuardResult(
            ok=False,
            problems=[f"{len(ungrounded)} figure(s) not present in tool output: "
                      + ", ".join(dict.fromkeys(ungrounded))],
            figures_checked=len(found),
            figures_ungrounded=list(dict.fromkeys(ungrounded)),
        )
    return GuardResult(ok=True, figures_checked=len(found))
