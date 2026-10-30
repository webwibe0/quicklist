"""Conservative constraint evidence, not person resolution or a relevance model.

No name inventory. Capitalization/syntax mark explicit proper-name spans;
regular inflections compare forms only. Missing/ambiguous evidence stays UNVERIFIED.
"""

import re
import unicodedata
from datetime import date


def words(text):
    return re.findall(r"[а-яёa-z0-9]+", unicodedata.normalize("NFC", text).casefold())


def form(token):
    """Regular Russian surface-form key, deliberately no fuzzy/alias matching.

    ponytail: this is NOT complete morphology. Irregular forms, initials and
    lowercase/foreign/ambiguous person mentions require explicit typed identity
    evidence before a production proposal; never merge people by edit distance.
    """
    token = token.casefold().replace("ё", "е")
    surname = re.fullmatch(r"(.+(?:ов|ев|ин))(?:а|у|ым|ом|е|ой|ою)?", token)
    if surname:
        return surname[1]
    for ending in ("ского", "скому", "скими", "ским", "ском", "ская", "скую", "ский"):
        if token.endswith(ending) and len(token) > len(ending) + 2:
            return token[: -len(ending)] + "ск"
    for ending in (
        "иями",
        "ией",
        "ием",
        "иям",
        "ому",
        "ого",
        "ыми",
        "ию",
        "ия",
        "ии",
        "ий",
        "ею",
        "ея",
        "еем",
        "ей",
        "ою",
        "ой",
        "ый",
        "ая",
        "ую",
        "ым",
        "им",
        "ых",
        "их",
        "ом",
        "ами",
        "ам",
        "ям",
        "ах",
        "ях",
        "у",
        "ю",
        "а",
        "я",
        "ы",
        "и",
        "е",
        "ь",
    ):
        if token.endswith(ending) and len(token) > len(ending) + 1:
            return token[: -len(ending)]
    return token


# Verb heads, not arbitrary occurrence of an action-related noun. First predicate
# owns its object and participant prepositions; a later noun cannot override it.
HEADS = [
    (r"(?:получ(?:ить|ать|и)|принять|прими|забрать|забери)", "receive"),
    (r"(?:оплат(?:ить|и)|оплачивать|заплатить)", "pay"),
    (r"(?:перевести|переведи|переводить)", "transfer"),
    (r"(?:одолжить|одолжи|одалживать|занять|займи)", "loan"),
    (r"(?:вернуть|верни|возвратить|возвращать)", "return"),
    (r"(?:отправ(?:ить|ляй|лять|ь)|переда(?:ть|й|вать)|отвезти)", "send"),
    (r"(?:назнач(?:ить|ь|ать)|запланировать|забронировать)", "schedule"),
    (r"(?:отмен(?:ить|и|ять)|отозвать|пропустить)", "cancel"),
    (r"(?:отказ(?:аться|ываться))", "refuse"),
    (r"(?:куп(?:ить|и)|покупать|заказ(?:ать|ывай|ывать)|приобрести|взять)", "purchase"),
    (
        r"(?:позвон(?:ить|и)|звонить|созвониться|написать|связаться|поговорить)",
        "contact",
    ),
    (r"(?:поздрав(?:ить|ь|лять)|пожелать)", "congratulate"),
    (r"(?:продлить|продли|возобновить)", "renew"),
    (r"(?:заменить|замени|менять|сменить|переобуть)", "change"),
    (r"(?:приготовить|приготовь|готовить|готовь)", "cook"),
    (r"(?:давать|давай|дать)", "give"),
    (r"(?:нагружать|нагружай)", "exercise"),
    (r"(?:проверить|подготовить|собрать|проверять|подготавливать)", "prepare"),
]


def head(token):
    return next((kind for pattern, kind in HEADS if re.fullmatch(pattern, token)), None)


def people(text):
    """Explicit Cyrillic proper spans, preserving excluded names and role cues."""
    tokens = list(re.finditer(r"[А-ЯЁа-яё]+", text))
    refs = []
    skip = {
        "не",
        "а",
        "и",
        "или",
        "в",
        "с",
        "со",
        "к",
        "у",
        "от",
        "для",
        "без",
        "только",
        "все",
    }
    has_contact = any(head(t[0].casefold()) == "contact" for t in tokens)
    for index, token in enumerate(tokens):
        value = token[0]
        previous = tokens[index - 1][0].casefold() if index else None
        role_text = has_contact and (
            head(previous or "") == "contact"
            or previous in {"с", "со", "к", "у", "от", "для", "не"}
            or index == 0
            and re.search(r"[уюе]$", value.casefold())
        )
        if (
            (not value[0].isupper() and not role_text)
            or value.casefold() in skip
            or head(value.casefold())
        ):
            continue
        before = text[: token.start()]
        after = text[token.end() :]
        first_name_at_start = index == 0 and (
            re.match(r"\s+[А-ЯЁ][а-яё]+", after)
            or re.match(r"\s*,\s*(?:а\s+)?не\b", after, re.IGNORECASE)
            or re.search(r"[уюе]$", value.casefold())
            and has_contact
        )
        if index == 0 and not first_name_at_start:
            continue  # Sentence-initial noun is not automatically a person.
        excluded = bool(
            re.search(r"\bне\s+(?:(?:со|с|к|у|от|для)\s+)?$", before, re.IGNORECASE)
        )
        prep = re.search(r"\b(со|с|к|у|от|для)\s+$", before, re.IGNORECASE)
        key = form(value)
        if (
            refs
            and refs[-1]["end"] < token.start()
            and text[refs[-1]["end"] : token.start()].isspace()
            and refs[-1]["excluded"] == excluded
        ):
            refs[-1]["keys"].append(key)
            refs[-1]["surface"] += " " + value
            refs[-1]["end"] = token.end()
        else:
            refs.append(
                {
                    "keys": [key],
                    "surface": value,
                    "excluded": excluded,
                    "preposition": prep[1].casefold() if prep else None,
                    "end": token.end(),
                    "confidence": "EXPLICIT_PROPER"
                    if value[0].isupper()
                    else "ROLE_TEXT",
                }
            )
    return refs


def entities(text):
    refs = people(text)
    positive = {key for r in refs if not r["excluded"] for key in r["keys"]}
    negative = {key for r in refs if r["excluded"] for key in r["keys"]}
    return (set() if "или" in words(text) else positive), negative


def frame(text):
    tokens = words(text)
    found = next(((i, head(t)) for i, t in enumerate(tokens) if head(t)), None)
    if found is None:
        return {
            "kind": None,
            "direction": None,
            "phase": None,
            "negated": None,
            "participants": people(text),
        }
    index, verb = found
    tail = tokens[index + 1 :]
    negative = any(
        t in {"не", "нельзя", "запрет", "запрещено"}
        for t in tokens[max(0, index - 2) : index]
    )
    financial_object = any(
        re.match(r"(?:ден[еь]г|оплат|плат[её]ж|средств|рубл)", t) for t in tail
    )
    kind, direction, phase = verb, None, None
    if (
        verb == "pay"
        or verb == "transfer"
        and financial_object
        or verb == "receive"
        and financial_object
    ):
        kind, direction = "finance", "in" if verb == "receive" else "out"
    elif verb in {"send", "receive"}:
        kind, direction = "dispatch", "in" if verb == "receive" else "out"
    elif verb in {"loan", "return"}:
        kind, phase = "loan", "return" if verb == "return" else "open"
    elif verb in {"schedule", "cancel"}:
        kind, phase = "appointment", "close" if verb == "cancel" else "open"
    elif verb == "refuse" and "от" in tail and any(t.startswith("покуп") for t in tail):
        kind, phase = "purchase", "close"
    elif verb == "purchase":
        phase = "open"
    participants = people(text)
    for participant in participants:
        prep = participant["preposition"]
        participant["role"] = (
            "source"
            if prep in {"у", "от"}
            else "target"
            if prep in {"к", "для"}
            else None
        )
        if (
            participant["role"] is None
            and prep is None
            and kind in {"finance", "dispatch", "loan"}
            and re.search(r"[уюе]$", participant["surface"].casefold())
        ):
            participant["role"] = "target"
    if kind == "loan":
        roles = {p["role"] for p in participants if not p["excluded"]}
        direction = "in" if "source" in roles else "out" if "target" in roles else None
    return {
        "head": verb,
        "kind": kind,
        "direction": direction,
        "phase": phase,
        "negated": negative,
        "participants": participants,
        "object_tokens": tail,
    }


def action(text):
    f = frame(text)
    return f["kind"], f["negated"]


def requested_status(text):
    if re.search(r"\b(?:включая|в том числе|а также)\b", text.casefold()):
        return None
    found = set()
    for pattern, status in [
        (r"\b(?:выполненн|завершенн|завершённ|сделанн)\w*", "completed"),
        (r"\b(?:отмененн|отменённ)\w*", "cancelled"),
        (r"\b(?:невыполненн|незавершенн|незавершённ|активн)\w*", "pending"),
    ]:
        if re.search(pattern, text.casefold()):
            found.add(status)
    return next(iter(found)) if len(found) == 1 else None


def object_form(token):
    # Noun genitive plural is not a person's surname paradigm.
    if len(token) > 4 and token.endswith(("ов", "ев")):
        return token[:-2]
    return form(token)


def phrases(text, marker):
    """Preserve qualified object phrases; split conjunction, not adjective+noun."""
    result = []
    for match in re.finditer(r"\b(?:" + marker + r")\s+([^,.;!?]+)", text.casefold()):
        tail = re.split(r"\s+(?:но|без|с|со|в|на|для|от)\s+", match[1])[0]
        for part in re.split(r"\s+и\s+", tail):
            tokens = words(part)
            if tokens:
                result.append(frozenset(object_form(t) for t in tokens))
    return result


def object_evidence(f):
    names = {key for p in f["participants"] for key in p["keys"]}
    grammar = {
        "не",
        "без",
        "с",
        "со",
        "к",
        "у",
        "от",
        "для",
        "за",
        "на",
        "в",
        "по",
        "до",
        "и",
        "или",
        "именно",
        "только",
    }
    return {
        object_form(t)
        for t in f.get("object_tokens", [])
        if t not in grammar
        and form(t) not in names
        and not t.isdigit()
        and not re.match(r"(?:ден[еь]г|средств|рубл|оплат|плат[её]ж|перевод)", t)
    }


def constraint_assessment(text, event):
    title = event["title"]
    q_refs = people(text)
    e_refs = [r for r in people(title) if not r["excluded"]]
    conflicts, unverified, notes = [], [], []
    for ref in q_refs:
        matches = [r for r in e_refs if set(ref["keys"]) <= set(r["keys"])]
        if ref["excluded"]:
            if matches:
                conflicts.append("explicitly_excluded_person")
        elif not matches:
            partial = any(set(ref["keys"]) & set(r["keys"]) for r in e_refs)

            def maybe_irregular(a, b):
                longer, shorter = sorted([a, b], key=len, reverse=True)
                return (
                    len(longer) > 2
                    and longer[-2] in "ео"
                    and longer[:-2] + longer[-1] == shorter
                )

            uncertain_forms = any(
                all(
                    any(k == other or maybe_irregular(k, other) for other in r["keys"])
                    for k in ref["keys"]
                )
                for r in e_refs
            )
            if (
                not e_refs
                or uncertain_forms
                or ref["confidence"] == "ROLE_TEXT"
                or any(r["confidence"] == "ROLE_TEXT" for r in e_refs)
                or partial
                and all(len(r["keys"]) < len(ref["keys"]) for r in e_refs)
            ):
                unverified.append("person_constraint_unverified")
            else:
                conflicts.append("explicit_name_missing_or_different")
    q, e = frame(text), frame(title)
    for participant in q["participants"]:
        role = participant.get("role")
        if role and not participant["excluded"]:
            same = [
                p
                for p in e["participants"]
                if set(participant["keys"]) <= set(p["keys"])
            ]
            if same and all(p.get("role") and p["role"] != role for p in same):
                conflicts.append("participant_role_contradiction")
            elif same and not any(p.get("role") == role for p in same):
                notes.append("participant_role_unverified")
    if (
        q["kind"] in {"finance", "dispatch"}
        and e["kind"] in {"finance", "dispatch"}
        and q["direction"] != e["direction"]
    ):
        if q["negated"] or e["negated"]:
            notes.append("direction_with_negation_unverified")
        else:
            conflicts.append("opposite_action_direction")
    if q["kind"] and e["kind"]:
        if q["kind"] == e["kind"]:
            same_direction = (
                not q["direction"]
                or not e["direction"]
                or q["direction"] == e["direction"]
            )
            if q["negated"] != e["negated"] and same_direction:
                conflicts.append("opposite_action_polarity")
            if q["direction"] and e["direction"] and q["direction"] != e["direction"]:
                if q["negated"] or e["negated"]:
                    notes.append("direction_with_negation_unverified")
                else:
                    conflicts.append("opposite_action_direction")
            if q["phase"] and e["phase"] and q["phase"] != e["phase"]:
                if q["negated"] or e["negated"]:
                    notes.append("negated_phase_requires_review")
                else:
                    conflicts.append("opposite_action_phase")
        elif q.get("head") == "cancel" or e.get("head") == "cancel":
            conflicts.append("cancellation_vs_execution")
        elif e["kind"] == "prepare" and q["kind"] != "prepare":
            notes.append("preparation_not_execution")
        else:
            notes.append("action_equivalence_unverified")
    elif q.get("head") == "cancel":
        notes.append("cancelled_status_does_not_prove_cancellation_action")
    if q["kind"] in {"finance", "dispatch", "loan", "purchase"}:
        required_object = object_evidence(q)
        if required_object and not required_object <= object_evidence(e):
            notes.append("object_equivalence_unverified")
    required_numbers = set(re.findall(r"\b\d+\b", text))
    event_numbers = set(re.findall(r"\b\d+\b", title))
    if required_numbers - event_numbers:
        (conflicts if event_numbers else unverified).append(
            "explicit_number_not_verified"
        )
    required_without, event_without = phrases(text, "без"), phrases(title, "без")
    event_with = phrases(title, "с(?:о)?")
    for value in required_without:
        if any(value <= present for present in event_with):
            conflicts.append("explicit_exclusion_contradicted")
        elif not any(value <= present for present in event_without):
            unverified.append("explicit_exclusion_unverified")
    for match in re.finditer(r"\bименно\s+([а-яё]+)", text.casefold()):
        if object_form(match[1]) not in {object_form(t) for t in words(title)}:
            unverified.append("explicit_object_unverified")
    # Retain lexical composite marker from v1, without assuming absence proves safety.
    if re.search(r"безлактоз", text.casefold()):
        if "обычн" in title.casefold() and "молок" in title.casefold():
            conflicts.append("explicit_exclusion_contradicted")
        elif not re.search(r"безлактоз|без лактоз", title.casefold()):
            unverified.append("explicit_exclusion_unverified")
    status = requested_status(text)
    if status and event["status"] != status:
        conflicts.append("explicit_lifecycle_status")
    return {
        "state": "CONTRADICTION"
        if conflicts
        else "UNVERIFIED"
        if unverified or notes
        else "PASS",
        "reasons": list(dict.fromkeys(conflicts + unverified)),
        "review_notes": notes,
        "query_frame": q,
        "event_frame": e,
    }


def constraint_reasons(text, event):
    """Legacy adapter. New caller must also inspect state/review_notes for certainty."""
    return constraint_assessment(text, event)["reasons"]


def eligible(event, filters):
    filters = dict(filters)
    supported = {"start", "end", "event_type", "status", "recurrence_required"}
    if set(filters) - supported:
        raise ValueError("Unsupported offline filter, never silently ignored")
    if (
        filters.get("recurrence_required") is not None
        and type(filters["recurrence_required"]) is not bool
    ):
        raise ValueError("recurrence_required must be boolean")
    if filters.get("status") and filters["status"] not in {
        "pending",
        "completed",
        "cancelled",
        "deleted",
    }:
        raise ValueError("Unsupported explicit lifecycle status")
    for field in ("start", "end"):
        if filters.get(field):
            filters[field] = date.fromisoformat(filters[field])
    if (
        filters.get("start")
        and filters.get("end")
        and filters["start"] > filters["end"]
    ):
        raise ValueError("Reversed period bounds")
    day = date.fromisoformat(event["event_date"]) if event["event_date"] else None
    recurring = bool(event.get("recurrence_rule") and event.get("recurrence_series_id"))
    if filters.get("recurrence_required") is not None and bool(
        event.get("recurrence_rule")
    ) != bool(event.get("recurrence_series_id")):
        return False  # Incomplete metadata proves neither recurring nor non-recurring.
    return (
        event["status"] != "deleted"
        and (not filters.get("start") or day is not None and day >= filters["start"])
        and (not filters.get("end") or day is not None and day <= filters["end"])
        and (
            not filters.get("event_type")
            or event["type"] == filters["event_type"].lower()
        )
        and (not filters.get("status") or event["status"] == filters["status"])
        and (
            filters.get("recurrence_required") is None
            or recurring is filters["recurrence_required"]
        )
    )


def guarded_rrf(text, events, lexical, semantic, groups, filters, *, raw_text=None):
    """Separate confirmed constraint matches from review; never assert full relevance."""
    from app.services.search_rank import rrf

    filters = dict(filters)
    if re.search(
        r"\b(?:только\s+повторяющ|повторяющиеся\s+(?:события|задачи|встречи|напоминания))",
        (raw_text or text).casefold(),
    ):
        if filters.get("recurrence_required") is False:
            raise ValueError("Contradictory recurrence filter")
        filters["recurrence_required"] = True
    accepted, review, rejected = set(), {}, {}
    for key in dict.fromkeys(lexical + semantic):
        event = events[key]
        if not eligible(event, filters):
            rejected[key] = "shared_filter"
            continue
        assessment = constraint_assessment(text, event)
        status = requested_status(raw_text or text)
        if status and event["status"] != status:
            rejected[key] = "explicit_lifecycle_status"
        elif assessment["state"] == "PASS":
            accepted.add(key)
        elif assessment["state"] == "UNVERIFIED":
            review[key] = assessment
        else:
            rejected[key] = assessment
    ranking = rrf(
        [key for key in lexical if key in accepted],
        [key for key in semantic if key in accepted],
        groups,
    )
    review_ranking = rrf(
        [key for key in lexical if key in review],
        [key for key in semantic if key in review],
        groups,
    )
    return {
        "ranking": ranking,
        "review_ranking": review_ranking,
        "requires_review": review,
        "rejected": rejected,
    }
