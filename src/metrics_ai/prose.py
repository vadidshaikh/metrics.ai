"""XYZ assembly, gerund normalization and the deterministic prose/grammar linter."""

from __future__ import annotations

import re

# Preposition/gerund collisions that make a bullet read badly ("by doing implementing"). The spec's
# pattern, with the full "by doing <gerund>" form tried first so overlapping hits are reported whole.
PROSE_DEFECT_RE = re.compile(r"\b(by doing \w+ing|by doing|by through|by via|by by|doing \w+ing)\b",
                             re.IGNORECASE)
Z_PREFIXES = ("by doing ", "by ", "through ", "via ")
X_PREFIXES = ("accomplished ",)
Y_PREFIXES = ("as measured by ", "measured by ")
CONJUNCTION_RE = re.compile(r"\b(and|while)\b", re.IGNORECASE)
# Finite verbs that turn the [Y] noun phrase into a clause ("as measured by latency fell ...").
Y_FINITE_VERB_RE = re.compile(r"\b(fell|dropped|decreased|declined|went|was|were|rose|improved|"
                              r"reduced|shrank|grew|is|are)\b", re.IGNORECASE)
CODE_PUNCT_RE = re.compile(r"(?<=\w)\(\)|@(?=\w)")
# Words ending in "ed" that are not past-tense verbs, so they may open the [X] noun phrase.
NOT_PAST_TENSE = {"speed", "need", "seed", "feed", "embed", "shred", "bed", "red"}


def _strip_prefixes(text: str, prefixes: tuple[str, ...]) -> str:
    text = text.strip()
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if text.lower().startswith(prefix):
                text = text[len(prefix):].lstrip()
                changed = True
    return text


def _trim(text: str) -> str:
    return text.strip().rstrip(".,;: ")


def _lower_first(text: str) -> str:
    # Keep acronyms/identifiers such as "SQLite" or "AST" intact; lowercase ordinary words only.
    first = text.split(" ", 1)[0]
    if first[:1].isupper() and first[1:2].islower():
        return text[:1].lower() + text[1:]
    return text


def normalize_x(x: str) -> str:
    return _lower_first(_trim(_strip_prefixes(x, X_PREFIXES)))


def normalize_y(y: str) -> str:
    return _trim(_strip_prefixes(y, Y_PREFIXES))


def normalize_z(z: str) -> str:
    """Strip leading "by doing "/"by "/"through "/"via " so prepending "by " is always clean."""
    return _lower_first(_trim(_strip_prefixes(z, Z_PREFIXES)))


def plain_identifiers(text: str) -> str:
    """`executemany()` -> `executemany`, `@lru_cache` -> `lru_cache`: code punctuation reads badly in prose."""
    return CODE_PUNCT_RE.sub("", text)


def assemble_xyz(x: str, y: str, z: str) -> str:
    return plain_identifiers(f"Accomplished {normalize_x(x)} as measured by {normalize_y(y)}, by {normalize_z(z)}.")


def prose_defects(text: str) -> list[str]:
    return [m.group(0) for m in PROSE_DEFECT_RE.finditer(text)]


def first_word(text: str) -> str:
    match = re.match(r"\s*([A-Za-z]+)", text)
    return match.group(1).lower() if match else ""


def starts_with_gerund(z: str) -> bool:
    word = first_word(z)
    return len(word) > 4 and word.endswith("ing")


def x_is_noun_phrase(x: str) -> bool:
    word = first_word(x)
    return not (word.endswith("ed") and len(word) > 3 and word not in NOT_PAST_TENSE)


def has_conjunction(z: str) -> bool:
    return bool(CONJUNCTION_RE.search(z))


def mentions_identifier(text: str, identifiers: set[str]) -> bool:
    lowered = text.lower()
    return any(ident.lower() in lowered for ident in identifiers)


def mechanism_tokens(mechanism: str) -> set[str]:
    """Code identifiers inside one mechanism description (`executemany()`, `@lru_cache`, ...)."""
    pairs = re.findall(r"@?(\w+)\(\)|@(\w+)", mechanism)
    return {name.lower() for pair in pairs for name in pair if name}


def mechanisms_referenced(z: str, mechanisms: list[str]) -> int:
    z_low = z.lower()
    return sum(1 for m in mechanisms if any(tok in z_low for tok in mechanism_tokens(m)))


NEGATED_REDUCTION_RE = re.compile(
    r"\b(reduc\w*|decreas\w*|drop\w*|cut\w*|fell|lower\w*|shrank|declin\w*)\s+(?:of|by)\s+-\s?\d", re.IGNORECASE)


def negated_reductions(text: str) -> list[str]:
    """'a reduction of -95.7%' is a double negative; reductions are stated as positive magnitudes."""
    return [m.group(0) for m in NEGATED_REDUCTION_RE.finditer(text)]


def unmeasured_claims(text: str, unmeasured: list[str]) -> list[str]:
    """Metric words (e.g. "throughput") the bullet uses although that metric was never measured."""
    lowered = text.lower()
    return [term for term in unmeasured if re.search(rf"\b{re.escape(term)}\b", lowered)]


def lint_xyz(x: str, z: str, combined: str, identifiers: set[str] | None,
             mechanisms: list[str], y: str = "", unmeasured: list[str] | None = None) -> list[str]:
    """Return human-readable defects; an empty list means the bullet is accepted."""
    problems = []
    negated = negated_reductions(combined)
    if negated:
        problems.append(f"double negative {negated}; state reductions as positive magnitudes")
    claimed = unmeasured_claims(combined, unmeasured or [])
    if claimed:
        problems.append(f"bullet mentions {claimed} but that metric was not measured; describe only measured "
                        "metrics (a speedup here is a latency speedup)")
    defects = prose_defects(combined)
    if defects:
        problems.append(f"awkward preposition/gerund collision {defects}; write 'by <gerund>' exactly once")
    if not x_is_noun_phrase(x):
        problems.append(f"[X] must be a noun phrase (e.g. 'a 95.3% latency reduction'), not start with "
                        f"the verb '{first_word(x)}'")
    if not starts_with_gerund(z):
        problems.append(f"[Z] must start with a lowercase gerund verb (e.g. 'batching ...'), got '{first_word(z)}'")
    if Y_FINITE_VERB_RE.search(y):
        problems.append(f"[Y] must be a noun phrase (e.g. 'mean latency dropping from A ms to B ms'), not a "
                        f"clause with the verb '{Y_FINITE_VERB_RE.search(y).group(0)}'")
    if mechanisms_referenced(z, mechanisms) >= 2 and not has_conjunction(z):
        problems.append("[Z] combines several mechanisms; join parallel gerund clauses with 'and' or 'while'")
    if identifiers and not mentions_identifier(combined, identifiers):
        problems.append("bullet must name at least one concrete identifier from AST_DIFF or FILES, e.g. "
                        + ", ".join(sorted(identifiers)[:6]))
    return problems
