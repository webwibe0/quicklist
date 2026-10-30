import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.ai.provider import IntentParser, LLMProvider
from app.models.entities import Conversation
from app.repositories.events import exact_duplicate
from app.schemas.commands import (
    BatchCreateEventCommand,
    CreateEventCommand,
    CreateSeriesCommand,
    DeleteEventCommand,
    DeleteSelectionCommand,
    EventResponse,
    EventType,
    Intent,
    ListEventsCommand,
    MessageResponse,
    ParsedCommand,
    SearchEventsCommand,
    UpdateEventCommand,
    UpdateSeriesCommand,
    creation_commands,
)
from app.schemas.dialogue import ActionDraft, DialogueContext, PendingAction
from app.services.candidate_selection import SelectionError, candidate_indices
from app.services.hybrid_search import HybridSearch, SearchResult
from app.services.recurrence_scope import explicit_recurrence_scope
from app.services.responses import ResponseBuilder
from app.services.search_filters import filters_from_text, literal_query, normalize_filter_listing
from app.services.targets import EventTargetResolver
from app.services.validator import CommandValidator, normalize_action
from app.temporal.recurrence import RecurrenceError, UnsupportedRecurrenceError
from app.temporal.resolver import (
    FLEXIBLE_TIMES,
    TemporalAmbiguity,
    TemporalResolver,
    TemporalResult,
    clean,
    temporal_direction,
)
from app.use_cases.actions import ActionRouter, CreateEventUseCase

REFERENT = re.compile(r"\b(?:ему|ей|им|него|нее|ним|его|ее|их)\b", re.IGNORECASE)
TASK_CONVERSION = re.compile(
    r"(?:(?:тогда|лучше|ну) )?(?:создай|сделай|запиши)(?: это)? задачу(?: без времени)?"
)
NEW_REQUEST = re.compile(
    r"^(?:(?:пожалуйста|тогда|ну) )?(?:напомни|создай|поставь|назначь|запланируй|запиши|добавь|покажи|найди|глянь|"
    r"удали|убери|отмени|перенеси|измени|переименуй|отметь|пометь|заверши|что|че)\b"
)


def new_request(text):
    return bool(NEW_REQUEST.match(clean(text)) or re.search(
        r"\b(?:сделал[аи]?|выполнен[аоы]?|нужно|надо)\b", clean(text),
    ))


def explicit_event_type(text):
    text = clean(text)
    if re.match(r"^(?:пожалуйста )?напомни\b", text):
        return EventType.REMINDER
    for word, event_type in (("задачу", EventType.TASK), ("встречу", EventType.MEETING),
                             ("напоминание", EventType.REMINDER)):
        if re.match(rf"^(?:создай|поставь|запиши|добавь) {word}\b", text):
            return event_type
    return None


def is_past(command, now):
    """The same Moscow boundary applies to validated CREATE and UPDATE commands."""
    if not command.event_date or command.event_time is None:
        return False
    zone = ZoneInfo("Europe/Moscow")
    return datetime.combine(command.event_date, command.event_time, zone) < now.astimezone(zone)


def is_followup(parsed: ParsedCommand, pending: PendingAction) -> bool:
    if pending.confirmation is not None:
        return False
    previous = pending.command
    if parsed.intent != previous.intent:
        return False
    if parsed.intent == Intent.CREATE_EVENT:
        if parsed.event_type and previous.event_type:
            return False
        # A full new CREATE has a new title. A missing-title answer is a patch.
        return parsed.title is None or bool({"title", "referent"} & set(pending.missing_fields))
    return True


class DialogueService:
    def __init__(self, provider: LLMProvider, resolver: TemporalResolver,
                 router: ActionRouter, responses: ResponseBuilder):
        self.parser = IntentParser(provider)
        self.resolver = resolver
        self.router = router
        self.responses = responses
        self.validator = CommandValidator()
        # The production retrieval service owns its guard and PostgreSQL fallback.
        self.targets = EventTargetResolver(router.repository,
            router.search if isinstance(router.search, HybridSearch) else None)

    def handle(self, conversation: Conversation, text: str, now: datetime | None = None):
        now = self.resolver.local_now(now)
        pending = PendingAction.model_validate(conversation.pending_action) if conversation.pending_action else None
        context = DialogueContext.model_validate(getattr(conversation, "context", None) or {})
        context.pending_action = pending
        answer = " ".join(re.findall(r"\w+", text.casefold().replace("ё", "е")))
        if pending and answer in {"нет", "не надо", "отмена", "отмени", "не переноси",
                                  "нет не переноси", "нет спасибо", "не создавай", "нет не создавай"}:
            conversation.pending_action = None
            updating = pending.command.intent == Intent.UPDATE_EVENT
            return MessageResponse(
                conversation_id=conversation.id, action=pending.command.intent,
                message=("Перенос отменён. Событие не изменено." if updating
                         else "Создание отменено. Ничего не создано."
                         if pending.command.intent == Intent.CREATE_EVENT else "Запрос отменён."),
            )
        if (pending and pending.confirmation is None and pending.command.intent==Intent.DELETE_EVENT
                and pending.candidate_ids):
            try:
                indices=candidate_indices(text,len(pending.candidate_ids))
            except SelectionError as exc:
                return self.responses.clarification(conversation.id,Intent.DELETE_EVENT,['event_reference'],{'event_reference':str(exc)})
            if indices is not None:
                if pending.named_selected is not None:
                    if len(indices) != 1:
                        return self.responses.clarification(conversation.id, Intent.DELETE_EVENT, ["event_reference"],
                            {"event_reference": "Выберите один номер для текущего названия или отмените запрос."})
                    picked = pending.candidate_events[indices[0]]
                    if not self._selection_current(pending.model_copy(update={"candidate_events": [*pending.named_selected, picked]})):
                        conversation.pending_action = None
                        return self.responses.not_found(conversation.id, Intent.DELETE_EVENT)
                    return self._named_delete(conversation, context, pending.command, pending.temporal,
                        pending.raw_user_text, [*pending.named_selected, picked])
                return self._delete_selection(conversation,context,pending,indices,text)
        if pending and "recurrence_scope" in pending.missing_fields:
            scope = explicit_recurrence_scope(text)
            if scope:
                fetch = (self.router.repository.get_context_events if pending.command.intent == Intent.UPDATE_EVENT
                         else lambda ids: self.router.repository.get_action_events(ids, pending.command.intent))
                targets = fetch([pending.target_event_id])
                if not targets:
                    conversation.pending_action = None
                    return self.responses.not_found(conversation.id, pending.command.intent)
                parsed = pending.command.model_copy(update={"recurrence_scope": scope})
                raw = pending.raw_user_text + "\n" + text
                if parsed.intent == Intent.DELETE_EVENT:
                    return self._status_target(conversation, context, parsed, pending.temporal, raw, targets[0])
                return self._update_target(conversation, context, parsed, pending.temporal, raw, targets[0], now)
            if not new_request(text):
                return self.responses.clarification(conversation.id, pending.command.intent,
                                                    ["recurrence_scope"], {})
            pending = None
            context.pending_action = None
        if pending and pending.confirmation is None and answer in {"да", "подтверждаю"}:
            return self.responses.clarification(
                conversation.id, pending.command.intent, pending.missing_fields, pending.temporal.issues,
            )
        if (pending and pending.command.intent == Intent.CREATE_EVENT
                and not pending.actions
                and pending.command.event_type in (EventType.REMINDER, EventType.MEETING)):
            flexible = clean(text) in FLEXIBLE_TIMES
            converting = TASK_CONVERSION.fullmatch(clean(text)) is not None
            if pending.confirmation is None and "time" in pending.missing_fields and (flexible or converting):
                if pending.command.event_type == EventType.MEETING and flexible:
                    draft = pending.as_task(text)
                    draft.command = draft.command.model_copy(update={"event_type": EventType.MEETING})
                    draft.temporal.time_unspecified = True
                    command, missing = self.validator.validate(draft.command, draft.temporal, draft.raw_user_text)
                    if missing:
                        conversation.pending_action = PendingAction(
                            **draft.model_dump(), missing_fields=missing,
                        ).model_dump(mode="json")
                        return self.responses.clarification(
                            conversation.id, Intent.CREATE_EVENT, missing, draft.temporal.issues,
                        )
                    return self._create(conversation, context, draft.command, draft.temporal,
                                        draft.raw_user_text, command, now, [])
                if pending.command.event_type != EventType.REMINDER:
                    # A meeting is never silently changed to a task.
                    return self.responses.clarification(
                        conversation.id, pending.command.intent, pending.missing_fields,
                        pending.temporal.issues,
                    )
                return self._task_conversion(conversation, context, pending, text, now, confirm=flexible)
        if pending and pending.confirmation is not None:
            # Match whole replies only: a qualified or ambiguous "yes" is not consent.
            updating = isinstance(pending.confirmation, (UpdateEventCommand, UpdateSeriesCommand))
            deleting = isinstance(pending.confirmation, (DeleteEventCommand,DeleteSelectionCommand))
            accepted = ({"да", "подтверждаю", "перенеси", "все равно перенеси",
                         "да перенеси", "да все равно перенеси"} if updating else
                        {"да", "подтверждаю", "все равно", "да все равно", "создай",
                         "все равно создай", "да создай", "да все равно создай"})
            if deleting:
                accepted = {"да", "подтверждаю", "да удали"}
            if pending.confirmation_kind == "task_conversion" and TASK_CONVERSION.fullmatch(clean(text)):
                accepted.add(answer)
            tomorrow = (not updating and pending.alternative is not None and answer in {
                "завтра", "на завтра", "перенеси на завтра", "создай завтра",
            })
            if answer in accepted or tomorrow:
                command = pending.confirmation
                if deleting:
                    if isinstance(command,DeleteSelectionCommand) and not self._selection_current(pending):
                        conversation.pending_action=None
                        return MessageResponse(conversation_id=conversation.id,action=Intent.DELETE_EVENT,
                            message='Выбранные записи изменились. Ничего не удалено; запросите список заново.')
                    events = self.router.execute(command)
                    if not events:
                        conversation.pending_action = None
                        return self.responses.not_found(conversation.id, Intent.DELETE_EVENT)
                    result=self._success(conversation, context, Intent.DELETE_EVENT, events, True)
                    if isinstance(command,DeleteSelectionCommand):
                        result=result.model_copy(update={'message':f'Удалено событий: {len(events)}.'})
                    return result
                if not updating:
                    approved = [] if tomorrow else [
                        *pending.approved, pending.confirmation_kind,
                    ]
                    return self._create(
                        conversation, context, pending.command, pending.temporal,
                        pending.raw_user_text, pending.alternative if tomorrow else command,
                        now, approved,
                    )
                return self._update(
                    conversation, context, pending.command, pending.temporal, pending.raw_user_text,
                    command, now, [*pending.approved, pending.confirmation_kind],
                )
            if not new_request(text):
                return self._confirmation_response(conversation.id, pending, strict=True)
        if not context.last_created_event and conversation.last_event_id:
            previous = self.router.repository.get_context_events([conversation.last_event_id])
            if previous:
                context.last_created_event = EventResponse.model_validate(previous[0])
        parsed = None
        if (pending and pending.confirmation is None
                and pending.command.intent == Intent.UPDATE_EVENT and pending.candidate_ids
                and "event_reference" in pending.missing_fields and not new_request(text)):
            try:
                indices = candidate_indices(text, len(pending.candidate_ids))
            except SelectionError as exc:
                return self.responses.clarification(conversation.id, Intent.UPDATE_EVENT,
                    ["event_reference"], {"event_reference": str(exc)})
            if indices is not None:
                if len(indices) != 1:
                    return self.responses.clarification(conversation.id, Intent.UPDATE_EVENT,
                        ["event_reference"], {"event_reference": "Выберите один номер события."})
                selected = self.router.repository.get_context_events([pending.candidate_ids[indices[0]]])
                if not selected:
                    conversation.pending_action = None
                    return self.responses.not_found(conversation.id, Intent.UPDATE_EVENT)
                if pending.candidate_events:
                    frozen = pending.model_copy(update={"candidate_events": [pending.candidate_events[indices[0]]]})
                    if not self._selection_current(frozen):
                        conversation.pending_action = None
                        return self.responses.not_found(conversation.id, Intent.UPDATE_EVENT)
                temporal = self.resolver.resolve_update(pending.command, selected[0], now, resolved=pending.temporal)
                return self._update_target(conversation, context, pending.command, temporal,
                    pending.raw_user_text, selected[0], now)
            # A literal date (optionally after the already supplied title) filters
            # frozen candidates; it never replaces the saved destination or new title.
            reference = text.strip()
            query = pending.command.title or pending.command.search_query or ""
            if query and clean(reference).startswith(clean(query)):
                reference = reference[len(query):].lstrip(" ,:—-")
            try:
                self.resolver.resolve_period(reference, now)
                parsed = ParsedCommand(intent=Intent.UPDATE_EVENT, period_expression=reference)
            except TemporalAmbiguity:
                pass
        single_candidate_answers = {
            "этот",
            "эта",
            "это",
            "этот да",
            "эта да",
            "это да",
            "да этот",
            "да эта",
            "да это",
            "верно",
            "он",
            "она",
        }

        if (
            pending
            and pending.confirmation is None
            and pending.command.intent == Intent.UPDATE_EVENT
            and "event_reference" in pending.missing_fields
            and pending.candidate_ids
            and len(pending.candidate_ids) == 1
                and answer in single_candidate_answers and parsed is None
        ):
            parsed = ParsedCommand(
                intent=Intent.UPDATE_EVENT,
                event_reference="last_listed",
                event_index=1,
            )
        if pending and pending.confirmation is None and "date" in pending.missing_fields and not new_request(text):
            try:
                self.resolver.resolve_date(text, now)
                parsed = ParsedCommand(intent=pending.command.intent, date_expression=text)
            except TemporalAmbiguity:
                pass
        if (parsed is None and pending and pending.confirmation is None
                and 'time' in pending.missing_fields and not new_request(text)):
            # A whole temporal answer patches the frozen action. Resolve only
            # literal recognised grammar; arbitrary text still goes through NLU.
            expression = clean(text)
            qualified = re.fullmatch(r'(сегодня|завтра|послезавтра) (.+)|(.+?) (сегодня|завтра|послезавтра)', expression)
            date_expression = (qualified[1] or qualified[4]) if qualified else None
            clock_expression = (qualified[2] or qualified[3]) if qualified else expression
            try:
                self.resolver.resolve_time(clock_expression)
                parsed = ParsedCommand(intent=pending.command.intent, date_expression=date_expression,
                                       time_expression=clock_expression)
            except TemporalAmbiguity:
                try:
                    _, clock = self.resolver.resolve_date(expression, now)
                    if clock is not None:
                        parsed = ParsedCommand(intent=pending.command.intent, time_expression=expression)
                except TemporalAmbiguity:
                    pass
        if parsed is None:
            parsed = self.parser.parse(text, context.model_dump(mode="json"))
        if (parsed.intent == Intent.CREATE_EVENT and parsed.event_type == EventType.MEETING
                and not parsed.title and parsed.recipient_expression
                and not REFERENT.search(parsed.recipient_expression)):
            recipient = re.search(
                r'\bвстреч(?:у|а)\s+с\s+(' + re.escape(parsed.recipient_expression) + r')(?!\w)',
                text, re.IGNORECASE,
            )
            if recipient:
                parsed = parsed.model_copy(update={'title': 'Встреча с ' + recipient[1]})
        if parsed.intent == Intent.UNKNOWN:
            if pending and pending.confirmation is not None:
                return self._confirmation_response(conversation.id, pending, strict=True)
            return self.responses.unknown(conversation.id)
        if parsed.intent == Intent.CREATE_EVENT and parsed.title and not parsed.event_type:
            parsed = parsed.model_copy(update={"event_type": explicit_event_type(text)})
        if pending and pending.confirmation is not None:
            if not self._complete_request(parsed, text, now, context):
                return self._confirmation_response(conversation.id, pending, strict=True)
            pending = None
            context.pending_action = None
        if parsed.actions or parsed.delete_targets:
            pending = None
            context.pending_action = None
        if pending and pending.confirmation is None and new_request(text):
            standalone = self.resolver.resolve(parsed, now)
            _, missing = self.validator.validate(parsed, standalone, text)
            if not missing:
                # Complete new requests take priority over old collected entities.
                pending = None
                context.pending_action = None
        if (pending and is_followup(parsed, pending)
                and "end_time" in pending.missing_fields and "time" not in pending.missing_fields
                and parsed.time_expression and not parsed.end_time_expression):
            parsed = parsed.model_copy(update={
                "end_time_expression": parsed.time_expression, "time_expression": None,
            })
        followup = pending is not None and is_followup(parsed, pending)
        if parsed.actions or (followup and pending.actions):
            return self._multiple_creates(conversation, context, parsed, pending if followup else None, text, now)
        single_time = (parsed.event_type == EventType.REMINDER or (
            followup and pending.command.event_type == EventType.REMINDER
        ))
        time_draft = pending.time_context(self.resolver) if followup and "time" in pending.missing_fields else None
        direction_text = re.sub(re.escape(parsed.period_expression), " ", text, flags=re.I) if parsed.period_expression else text
        direction = temporal_direction(direction_text) if parsed.intent in (Intent.LIST_EVENTS, Intent.SEARCH_EVENTS) else None
        if followup:
            direction = direction or pending.temporal.search_direction
        temporal = self.resolver.resolve(parsed, now, single_time=single_time, time_draft=time_draft,
                                         direction=direction)
        raw_text = text
        if followup:
            patch = parsed.model_dump(exclude_none=True)
            if "referent" in pending.missing_fields:
                title = self._recipient_title(parsed, text, pending.command.title or "")
                if title is None:
                    patch.pop("title", None)
                else:
                    patch["title"] = title
            pending.merge(ParsedCommand.model_validate(patch), temporal, text,
                          self.resolver, single_time=single_time)
            parsed, temporal, raw_text = pending.command, pending.temporal, pending.raw_user_text
            self.resolver.resolve_recurrence_start(parsed, temporal, now)
        target = None
        if parsed.intent == Intent.CREATE_EVENT and parsed.event_type == EventType.REMINDER:
            if not parsed.title and context.last_created_event and not followup:
                candidates = self.router.repository.get_context_events([context.last_created_event.id])
                if candidates:
                    parsed = parsed.model_copy(update={"title": candidates[0].title})
        if parsed.intent == Intent.CREATE_EVENT:
            parsed = self._resolve_referent(parsed, text, context, temporal, followup)
            if followup and pending.task_conversion_requested:
                proposal = pending.model_copy(update={
                    "command": parsed, "temporal": temporal, "raw_user_text": raw_text,
                })
                return self._task_conversion(conversation, context, proposal, "", now, confirm=True)
        if parsed.intent == Intent.DELETE_EVENT and parsed.delete_targets and not temporal.issues:
            return self._named_delete(conversation, context, parsed, temporal, raw_text,
                pending.named_selected or [] if followup and pending.named_selected is not None else [],
                choice_ids=pending.candidate_ids if followup and pending.named_selected is not None else None)
        if parsed.intent in (Intent.DELETE_EVENT, Intent.COMPLETE_EVENT):
            if temporal.issues:
                missing = list(temporal.issues)
                conversation.pending_action = PendingAction(
                    command=parsed, temporal=temporal, missing_fields=missing, raw_user_text=raw_text,
                    candidate_ids=pending.candidate_ids if followup else None,
                ).model_dump(mode="json")
                return self.responses.clarification(conversation.id, parsed.intent, missing, temporal.issues)
            candidates = self.targets.resolve(
                parsed, temporal, context, text, pending.candidate_ids if followup else None,
            )
            if not candidates:
                conversation.pending_action = None
                return self.responses.not_found(conversation.id, parsed.intent)
            if len(candidates) > 1 or (
                self.targets.needs_selection(parsed, candidates[0]) and parsed.event_index is None
            ):
                visible = candidates[:self.responses.limit]
                context.last_listed_events = [EventResponse.model_validate(event) for event in visible]
                context.last_event_scope = "last_listed"
                conversation.context = context.model_dump(mode="json", exclude={"pending_action"})
                conversation.pending_action = PendingAction(
                    command=parsed, temporal=temporal, missing_fields=["event_reference"],
                    raw_user_text=raw_text, candidate_ids=[event.id for event in visible],
                    candidate_events=[EventResponse.model_validate(event) for event in visible],
                ).model_dump(mode="json")
                return self.responses.candidates(conversation.id, parsed.intent, candidates)
            return self._status_target(conversation, context, parsed, temporal, raw_text, candidates[0])
        if parsed.intent == Intent.UPDATE_EVENT:
            explicit_target = bool(parsed.search_query or parsed.title)
            independent = explicit_target and new_request(text) and not re.search(
                r'\b(?:эту|этот|это|последн\w*|из списка|перв\w*|втор\w*)\b',clean(text),
            ) and parsed.event_index is None
            reference = parsed.event_reference or (
                None if explicit_target else "last_created"
            )
            if independent:
                reference=None
            parsed = parsed.model_copy(update={"event_reference": reference})
            candidates = self.targets.resolve(
                parsed, temporal, context, text, pending.candidate_ids if followup else None,
                independent=independent and not followup,
            )
            if candidates and (len(candidates) > 1 or (
                self.targets.needs_selection(parsed, candidates[0]) and parsed.event_index is None
            )):
                visible = candidates[:self.responses.limit]
                context.last_listed_events = [EventResponse.model_validate(event) for event in visible]
                context.last_event_scope = "last_listed"
                conversation.context = context.model_dump(mode="json", exclude={"pending_action"})
                conversation.pending_action = PendingAction(
                    command=parsed, temporal=temporal, missing_fields=["event_reference"],
                    raw_user_text=raw_text, candidate_ids=[event.id for event in visible],
                    candidate_events=[EventResponse.model_validate(event) for event in visible],
                ).model_dump(mode="json")
                if (followup and pending.candidate_ids == [event.id for event in visible]):
                    return self.responses.clarification(conversation.id, parsed.intent, ["event_reference"],
                        {"event_reference": "Выберите номер из показанного списка или уточните дату события."})
                return self.responses.candidates(conversation.id, parsed.intent, visible)
            if len(candidates) != 1:
                conversation.pending_action = PendingAction(
                    command=parsed, temporal=temporal, missing_fields=["event_reference"],
                    raw_user_text=raw_text,
                ).model_dump(mode="json")
                return self.responses.clarification(
                    conversation.id, parsed.intent, ["event_reference"], {},
                )
            target = candidates[0]
            temporal = self.resolver.resolve_update(parsed, target, now, resolved=temporal)
            return self._update_target(conversation, context, parsed, temporal, raw_text, target, now)
        parsed = normalize_filter_listing(parsed, temporal, raw_text)
        parsed = normalize_action(parsed, temporal)
        business_command, missing = self.validator.validate(parsed, temporal, raw_text, target=target)
        if missing:
            conversation.pending_action = PendingAction(
                command=parsed, temporal=temporal, missing_fields=missing, raw_user_text=raw_text,
            ).model_dump(mode="json")
            return self.responses.clarification(conversation.id, parsed.intent, missing, temporal.issues)
        if isinstance(business_command, CreateEventCommand):
            if len(temporal.event_times) > 1:
                business_command = BatchCreateEventCommand(events=[
                    business_command.model_copy(update={"event_time": clock, "end_time": None})
                    for clock in temporal.event_times
                ])
            return self._create(
                conversation, context, parsed, temporal, raw_text, business_command, now, [],
                clear_pending=pending is None or followup or parsed.intent == pending.command.intent,
            )
        if isinstance(business_command, SearchEventsCommand):
            past = bool(re.search(r"\b(?:была|был|были|состоялась|состоялись|прошедшие|прошлые)\b", clean(raw_text)))
            future = bool(re.search(r"\b(?:будет|будут|предстоящие|будущие)\b", clean(raw_text)))
            if past and future and temporal.search_direction is None:
                temporal.issues["period"] = "Искать прошедшие или будущие события? Укажите период."
                conversation.pending_action = PendingAction(
                    command=parsed, temporal=temporal, missing_fields=["period"], raw_user_text=raw_text,
                ).model_dump(mode="json")
                return self.responses.clarification(conversation.id, parsed.intent, ["period"], temporal.issues)
            business_command = business_command.model_copy(update={
                "temporal_direction": temporal.search_direction or (
                    "past" if past else "future" if future else None
                ), "as_of": now,
            })
        if isinstance(business_command, ListEventsCommand):
            include_inactive = bool(re.search(
                r"\bвсе (?:(?:прошедшие|будущие|предстоящие|мои) )?(?:события|планы|задачи|встречи|напоминания)\b", clean(raw_text)))
            business_command = business_command.model_copy(update={"as_of": now,
                "include_inactive": include_inactive,
                "include_unscheduled": business_command.start == date.min and business_command.end == date.max})
        if isinstance(business_command, (SearchEventsCommand, ListEventsCommand)):
            business_command = business_command.model_copy(update=filters_from_text(raw_text))
        if isinstance(business_command, SearchEventsCommand):
            query = literal_query(raw_text, parsed)
            guard_query = query or raw_text.strip()
            grounded = (parsed.search_query or "").strip()
            if grounded and clean(grounded) in clean(raw_text):
                query = grounded
            query = query or raw_text.strip()
            business_command = business_command.model_copy(update={"query": query[:500], "raw_query": guard_query})
        events = self.router.execute(business_command)
        response = self._success(
            conversation, context, parsed.intent, events,
            pending is None or followup or parsed.intent == pending.command.intent,
        )
        result = getattr(self.router.search, "result", None)
        if isinstance(business_command, SearchEventsCommand) and isinstance(result, SearchResult):
            response.search_mode = result.mode
            response.unverified_count = result.unverified_count
            if result.unverified_count:
                response.message += " Есть кандидаты с неподтверждёнными условиями; они не включены в результат. Уточните запрос."
                if not events:
                    response.requires_input = True
                    response.missing_fields = ["search_query"]
        return response

    def _recipient_title(self, parsed, text, previous_title):
        recipient = parsed.recipient_expression or parsed.title or text
        if not re.search(r'(?<!\w)' + re.escape(recipient) + r'(?!\w)', text, re.I):
            recipient = text
        recipient = re.sub(r"^(?:позвонить|набрать|написать)\s+", "",recipient.strip(" .!?,"),flags=re.I)
        for expression in (parsed.date_expression,parsed.time_expression,parsed.end_time_expression,parsed.period_expression):
            if expression:
                recipient=re.sub(re.escape(expression),' ',recipient,flags=re.I)
        recipient=re.sub(r'\b(?:в|на|к)\b',' ',recipient,flags=re.I)
        recipient=' '.join(recipient.split())
        # A literal recipient reply, never a model-invented name or a guessed referent.
        explicit_recipient = (
            re.fullmatch(r"[А-ЯЁ][а-яё]+(?:[ -][А-ЯЁ][а-яё]+){0,2}", recipient)
            or recipient.casefold() in {"маме", "папе", "брату", "сестре", "коллеге"}
        )
        if (explicit_recipient and re.search(r'(?<!\w)'+re.escape(recipient)+r'(?!\w)',text,re.I)
                and recipient.casefold() not in {
            "да", "нет", "отмена", "отмени", "подтверждаю", "завтра", "сегодня", "вечером",
        }):
            return REFERENT.sub(lambda _: recipient, previous_title)
        else:
            return None
    def _multiple_creates(self, conversation, context, parsed, pending, text, now):
        """Collect all drafts before one existing, atomic batch confirmation."""
        if pending:
            drafts = pending.actions
            current = drafts[pending.action_index]
            if "referent" in pending.missing_fields:
                parsed = parsed.model_copy(update={"title": self._recipient_title(parsed, text, current.command.title or "")})
            temporal = self.resolver.resolve(parsed, now, single_time=True,
                time_draft=current.time_context(self.resolver))
            current.merge(parsed, temporal, text, self.resolver, single_time=True)
        else:
            drafts = []
            shared_date = parsed.date_expression
            title, event_type = parsed.title, parsed.event_type
            for item in parsed.actions:
                title, event_type = item.title or title, item.event_type or event_type
                relative = bool(re.match(r"через\b", clean(item.date_expression or item.time_expression or "")))
                if item.date_expression and not relative:
                    shared_date = item.date_expression
                item = item.model_copy(update={"title": title, "event_type": event_type,
                    "date_expression": item.date_expression or (None if relative else shared_date)})
                drafts.append(ActionDraft(command=item, temporal=TemporalResult(), raw_user_text=text))
            for index, draft in enumerate(drafts):
                draft.temporal = self.resolver.resolve(draft.command,
                    self._action_anchor(drafts, index, now), single_time=True)
        commands = []
        for index, draft in enumerate(drafts):
            if draft.command.relative_anchor_expression and index:
                draft.temporal = self.resolver.resolve(draft.command,
                    self._action_anchor(drafts, index, now), single_time=True)
            draft.command = self._resolve_referent(draft.command, draft.raw_user_text, context, draft.temporal, pending is not None)
            command, missing = self.validator.validate(draft.command, draft.temporal, draft.raw_user_text)
            if 'referent' in draft.temporal.issues:
                missing = list(dict.fromkeys([*missing, 'referent']))
            if missing:
                conversation.pending_action = PendingAction(**draft.model_dump(), missing_fields=missing,
                    actions=drafts, action_index=index).model_dump(mode="json")
                result = self.responses.clarification(conversation.id, Intent.CREATE_EVENT, missing, draft.temporal.issues)
                result.message = f"Для «{draft.command.title or 'события'}»: " + result.message
                return result
            commands.append(command)
        batch = BatchCreateEventCommand(events=commands)
        first = drafts[0]
        return self._create(conversation, context, first.command, first.temporal,
            "\n".join(dict.fromkeys(draft.raw_user_text for draft in drafts)), batch, now, [])

    def _action_anchor(self, drafts, index, now):
        if not index or not drafts[index].command.relative_anchor_expression:
            return now
        connector = clean(drafts[index].command.relative_anchor_expression)
        if not re.fullmatch(r"(?:(?:а )?потом(?: еще)?|(?:а )?затем(?: еще)?|после (?:первого|предыдущего) напоминания)", connector):
            drafts[index].command = drafts[index].command.model_copy(update={"date_expression": connector})
            return now
        previous = drafts[0 if connector == "после первого напоминания" else index - 1].temporal
        if previous.event_date and previous.event_time and not previous.issues:
            return datetime.combine(previous.event_date, previous.event_time, self.resolver.zone)
        return now

    def _ask_scope(self, conversation, parsed, temporal, raw_text, target):
        conversation.pending_action = PendingAction(
            command=parsed, temporal=temporal, raw_user_text=raw_text,
            missing_fields=["recurrence_scope"], target_event_id=target.id,
        ).model_dump(mode="json")
        return self.responses.clarification(conversation.id, parsed.intent, ["recurrence_scope"], {})

    def _named_delete(self, conversation, context, parsed, temporal, raw_text, selected, *, choice_ids=None):
        # Resolve every literal target before creating a single finite-ID confirmation.
        for index in range(len(selected), len(parsed.delete_targets)):
            name = parsed.delete_targets[index]
            query = parsed.search_query if choice_ids and parsed.search_query else name
            request = parsed.model_copy(update={"delete_targets": None, "search_query": query, "title": None,
                "event_reference": None, "event_index": None, "recurrence_scope": None})
            candidates = self.targets.resolve(request, temporal, context, raw_text, choice_ids)
            choice_ids = None
            if not candidates:
                conversation.pending_action = None
                return self.responses.not_found(conversation.id, Intent.DELETE_EVENT)
            if len(candidates) != 1 or self.targets.needs_selection(request, candidates[0]):
                visible = candidates[:self.responses.limit]
                snapshots = [EventResponse.model_validate(event) for event in visible]
                context.last_listed_events = snapshots
                context.last_event_scope = "last_listed"
                conversation.context = context.model_dump(mode="json", exclude={"pending_action"})
                conversation.pending_action = PendingAction(command=parsed, temporal=temporal, raw_user_text=raw_text,
                    missing_fields=["event_reference"], candidate_ids=[event.id for event in visible],
                    candidate_events=snapshots, named_selected=selected).model_dump(mode="json")
                result = self.responses.candidates(conversation.id, Intent.DELETE_EVENT, visible)
                result.message = f"Для «{name}»: " + result.message
                return result
            selected.append(EventResponse.model_validate(candidates[0]))
        if parsed.recurrence_scope == "series" and any(event.recurrence_series_id for event in selected):
            conversation.pending_action = None
            return MessageResponse(conversation_id=conversation.id, action=Intent.DELETE_EVENT,
                message="Удаляйте каждую серию отдельным запросом, чтобы подтвердить её область целиком.")
        unique = list({event.id: event for event in selected}.values())
        frozen = PendingAction(command=parsed, temporal=temporal, raw_user_text=raw_text,
            missing_fields=["event_reference"], candidate_ids=[event.id for event in unique], candidate_events=unique)
        return self._delete_selection(conversation, context, frozen, list(range(len(unique))), raw_text)

    def _selection_current(self,pending):
        expected=pending.candidate_events or []
        events=self.router.repository.get_action_events([event.id for event in expected],Intent.DELETE_EVENT)
        actual={event.id:EventResponse.model_validate(event) for event in events}
        return bool(expected) and len(actual)==len(expected) and all(actual.get(event.id)==event for event in expected)

    def _delete_selection(self,conversation,context,pending,indices,text):
        ids=[pending.candidate_ids[index] for index in indices]
        known={event.id:event for event in pending.candidate_events or context.last_listed_events}
        selected=[known[event_id] for event_id in ids if event_id in known]
        frozen=pending.model_copy(update={'candidate_events':selected})
        if len(selected)!=len(ids) or not self._selection_current(frozen):
            conversation.pending_action=None
            return MessageResponse(conversation_id=conversation.id,action=Intent.DELETE_EVENT,
                message='Выбранные записи изменились. Ничего не удалено; запросите список заново.')
        if len(ids)==1 and text.strip().isdigit():
            targets=self.router.repository.get_action_events(ids,Intent.DELETE_EVENT)
            return self._status_target(conversation,context,pending.command,pending.temporal,pending.raw_user_text,targets[0])
        frozen=frozen.model_copy(update={'confirmation':DeleteSelectionCommand(event_ids=ids),
            'confirmation_kind':'delete','missing_fields':['confirmation'],'candidate_ids':ids})
        conversation.pending_action=frozen.model_dump(mode='json')
        return self._confirmation_response(conversation.id,frozen)

    def _recurrence_clarification(self, conversation, parsed, temporal, raw_text, error):
        temporal.issues["recurrence"] = str(error)
        conversation.pending_action = PendingAction(
            command=parsed, temporal=temporal, raw_user_text=raw_text, missing_fields=["recurrence"],
        ).model_dump(mode="json")
        return self.responses.clarification(conversation.id, parsed.intent, ["recurrence"], temporal.issues)

    def _update_target(self, conversation, context, parsed, temporal, raw_text, target, now):
        if parsed.recurrence_expression or re.search(
            r"\b(?:теперь|вместо)\b.*\b(?:каждый|каждую|каждое|по будням)\b", clean(raw_text),
        ):
            return self._recurrence_clarification(
                conversation, parsed, temporal, raw_text,
                UnsupportedRecurrenceError("Изменение правила повторения пока не поддерживается. "
                                           "Можно изменить название, дату или время серии."),
            )
        if target.recurrence_series_id and not parsed.recurrence_scope:
            if temporal.period_start == temporal.period_end == target.event_date and target.event_date is not None:
                parsed = parsed.model_copy(update={"recurrence_scope": "occurrence"})
            else:
                return self._ask_scope(conversation, parsed, temporal, raw_text, target)
        command, missing = self.validator.validate(parsed, temporal, raw_text, target=target)
        if missing:
            conversation.pending_action = PendingAction(
                command=parsed, temporal=temporal, raw_user_text=raw_text,
                missing_fields=missing, candidate_ids=[target.id], target_event_id=target.id,
            ).model_dump(mode="json")
            return self.responses.clarification(conversation.id, Intent.UPDATE_EVENT, missing, temporal.issues)
        if target.recurrence_series_id and parsed.recurrence_scope == "series":
            try:
                command = self.router.update_series.expand(
                    command, target, change_time=bool(parsed.time_expression or parsed.shift_expression
                                                     or command.event_time != target.event_time),
                    explicit_end=bool(parsed.end_time_expression),
                    relative_shift=bool(parsed.shift_expression or re.search(
                        r"вперед|назад|позже|раньше", parsed.time_expression or "",
                    )),
                )
            except RecurrenceError as exc:
                return self._recurrence_clarification(conversation, parsed, temporal, raw_text, exc)
        return self._update(conversation, context, parsed, temporal, raw_text, command, now, [])

    def _update(self, conversation, context, parsed, temporal, raw_text, command, now, approved):
        updates = command.updates if isinstance(command, UpdateSeriesCommand) else [command]
        ids = [item.event_id for item in updates]
        candidates = self.router.repository.get_context_events(ids)
        if len(candidates) != len(ids) or (isinstance(command, UpdateSeriesCommand) and any(
            event.recurrence_series_id != command.recurrence_series_id for event in candidates
        )):
            conversation.pending_action = None
            return self.responses.not_found(conversation.id, Intent.UPDATE_EVENT)
        indexed = {event.id: event for event in candidates}
        schedule_change = any((parsed.date_expression, parsed.time_expression,
                               parsed.end_time_expression, parsed.shift_expression))
        kind, conflict_event = None, None
        if "past" not in approved and schedule_change and any(is_past(item, now) for item in updates):
            kind = "past"
        if kind is None and "conflict" not in approved and schedule_change:
            for item in updates:
                conflicts = self.router.repository.find_conflicts(
                    item, event_type=indexed[item.event_id].type, exclude_event_ids=ids,
                )
                if conflicts:
                    kind, conflict_event = "conflict", EventResponse.model_validate(conflicts[0])
                    break
        if kind:
            pending = PendingAction(
                command=parsed, temporal=temporal, raw_user_text=raw_text,
                missing_fields=["confirmation"], confirmation=command, confirmation_kind=kind,
                confirmation_event=conflict_event, approved=approved,
            )
            conversation.pending_action = pending.model_dump(mode="json")
            return self._confirmation_response(conversation.id, pending)
        events = self.router.execute(command)
        return self._success(conversation, context, Intent.UPDATE_EVENT, events, True)

    def _task_conversion(self, conversation, context, pending, text, now, *, confirm):
        draft = pending.as_task(text)
        command, missing = self.validator.validate(draft.command, draft.temporal, draft.raw_user_text)
        if confirm and draft.temporal.event_date is None and "date" in pending.missing_fields:
            missing = list(dict.fromkeys([*missing, "date"]))
        if missing:
            if confirm:
                conversation.pending_action = PendingAction(
                    command=pending.command, temporal=draft.temporal, raw_user_text=draft.raw_user_text,
                    missing_fields=missing, task_conversion_requested=True,
                ).model_dump(mode="json")
                return self.responses.clarification(
                    conversation.id, Intent.CREATE_EVENT, missing, draft.temporal.issues,
                )
            conversation.pending_action = PendingAction(
                **draft.model_dump(), missing_fields=missing,
            ).model_dump(mode="json")
            return self.responses.clarification(
                conversation.id, Intent.CREATE_EVENT, missing, draft.temporal.issues,
            )
        if confirm:
            proposal = pending.model_copy(update={
                "confirmation": command, "confirmation_kind": "task_conversion",
                "missing_fields": ["confirmation"], "raw_user_text": draft.raw_user_text,
            })
            conversation.pending_action = proposal.model_dump(mode="json")
            return self._confirmation_response(conversation.id, proposal)
        return self._create(conversation, context, draft.command, draft.temporal,
                            draft.raw_user_text, command, now, [])

    def _complete_request(self, parsed, text, now, context):
        if parsed.actions:
            return True
        if not new_request(text) or parsed.intent == Intent.UNKNOWN:
            return False
        temporal = self.resolver.resolve(parsed, now)
        if parsed.intent in (Intent.DELETE_EVENT, Intent.COMPLETE_EVENT):
            return not temporal.issues
        target = None
        if parsed.intent == Intent.UPDATE_EVENT:
            candidates = self.targets.resolve(parsed, temporal, context, text)
            if len(candidates) != 1:
                return False
            target = candidates[0]
            temporal = self.resolver.resolve_update(parsed, target, now, resolved=temporal)
        _, missing = self.validator.validate(parsed, temporal, text, target=target)
        return not missing

    def handle_event_action(self, conversation, event_id, intent):
        context = DialogueContext.model_validate(getattr(conversation, "context", None) or {})
        events = self.router.repository.get_action_events([event_id], intent)
        if not events:
            return self.responses.not_found(conversation.id, intent)
        return self._status_target(conversation, context, ParsedCommand(intent=intent),
                                   TemporalResult(), "Действие с карточки", events[0])

    def _status_target(self, conversation, context, parsed, temporal, raw_text, target):
        if parsed.intent == Intent.DELETE_EVENT and target.recurrence_series_id and not parsed.recurrence_scope:
            return self._ask_scope(conversation, parsed, temporal, raw_text, target)
        command, missing = self.validator.validate(parsed, temporal, raw_text, target=target)
        if missing:
            return self.responses.clarification(conversation.id, parsed.intent, missing, temporal.issues)
        if isinstance(command, DeleteEventCommand):
            if parsed.recurrence_scope == "series" and target.recurrence_series_id:
                command = command.model_copy(update={"recurrence_series_id": target.recurrence_series_id})
            pending = PendingAction(
                command=parsed, temporal=temporal, raw_user_text=raw_text,
                missing_fields=["confirmation"], confirmation=command, confirmation_kind="delete",
                confirmation_event=EventResponse.model_validate(target),
            )
            conversation.pending_action = pending.model_dump(mode="json")
            return self._confirmation_response(conversation.id, pending)
        events = self.router.execute(command)
        if not events:
            conversation.pending_action = None
            return self.responses.not_found(conversation.id, parsed.intent)
        return self._success(conversation, context, parsed.intent, events, True)

    def _confirmation_response(self, conversation_id, pending, *, strict=False):
        if isinstance(pending.confirmation,DeleteSelectionCommand):
            return MessageResponse(conversation_id=conversation_id,action=Intent.DELETE_EVENT,
                message=f'Выбрано {len(pending.confirmation.event_ids)} событий из показанного списка. Действительно удалить все выбранные? Ответьте «да» или «нет».',
                requires_input=True,missing_fields=['confirmation'],events=pending.candidate_events or [])
        if isinstance(pending.confirmation, (UpdateEventCommand, UpdateSeriesCommand)):
            if pending.confirmation_kind == "conflict":
                result = self.responses.update_conflict_confirmation(
                    conversation_id,
                    pending.confirmation_event,
                    series=isinstance(pending.confirmation, UpdateSeriesCommand),
                )
            else:
                result = self.responses.past_update_confirmation(
                    conversation_id,
                    pending.confirmation,
                )
        elif isinstance(pending.confirmation, DeleteEventCommand):
            result = self.responses.delete_confirmation(
                conversation_id, pending.confirmation_event, series=pending.confirmation.recurrence_series_id is not None,
            )
        else:
            result = self.responses.create_confirmation(conversation_id, pending)
        if strict:
            result.message = ("Нужно выбрать: выполнить действие или отменить его. "
                              "Ответьте «да» или «нет». " + result.message)
        return result

    def _create(self, conversation, context, parsed, temporal, raw_text, command, now,
                approved, *, clear_pending=True):
        try:
            command = CreateEventUseCase.prepare(command)
        except RecurrenceError as exc:
            return self._recurrence_clarification(conversation, parsed, temporal, raw_text, exc)
        commands = creation_commands(command)
        kind, alternative, conflict_event = None, None, None
        if "past" not in approved and any(is_past(item, now) for item in commands):
            kind = "past"
            day = now.astimezone(ZoneInfo("Europe/Moscow")).date() + timedelta(days=1)
            shift = day - commands[0].event_date
            alternatives = [item.model_copy(update={
                "event_date": item.event_date + shift if isinstance(command, CreateSeriesCommand) else day,
            }) for item in commands]
            alternative = (CreateSeriesCommand(occurrences=alternatives) if isinstance(command, CreateSeriesCommand)
                           else BatchCreateEventCommand(events=alternatives)
                           if isinstance(command, BatchCreateEventCommand) else alternatives[0])
        elif "duplicate" not in approved and any(
            self.router.repository.find_duplicate(item) is not None for item in commands
        ):
            kind = "duplicate"
        if kind is None and "conflict" not in approved:
            for item in commands:
                conflict = next((event for event in self.router.repository.find_conflicts(item)
                                 if not exact_duplicate(item, event)), None)

                if conflict is not None:
                    kind = "conflict"
                    conflict_event = EventResponse.model_validate(conflict)
                    break

        if kind is None and "batch" not in approved and isinstance(command, BatchCreateEventCommand):
            kind = "batch"

        if (
            kind is None
            and "unscheduled_meeting" not in approved
            and temporal.time_unspecified
            and parsed.event_type == EventType.MEETING
        ):
            kind = "unscheduled_meeting"
        if kind:
            pending = PendingAction(
                command=parsed, temporal=temporal, raw_user_text=raw_text,
                missing_fields=["confirmation"], confirmation=command,
                confirmation_kind=kind,
                confirmation_event=conflict_event,
                alternative=alternative,
                approved=approved,
            )
            conversation.pending_action = pending.model_dump(mode="json")
            return self._confirmation_response(conversation.id, pending)
        events = self.router.execute(command)
        return self._success(conversation, context, Intent.CREATE_EVENT, events, clear_pending)

    def _resolve_referent(self, parsed, text, context, temporal, followup):
        title = parsed.title or ""
        # Do not silently accept an LLM that dropped the explicitly spoken pronoun.
        raw_reference = re.search(r"\b(?:позвонить|набрать|написать)\s+(?:ему|ей|им)\b", text, re.I)
        if raw_reference and not followup and not REFERENT.search(title):
            title = raw_reference[0].capitalize()
        if not REFERENT.search(title):
            return parsed.model_copy(update={"title": title or None})
        snapshots = [*context.last_listed_events]
        if context.last_created_event:
            snapshots.append(context.last_created_event)
        candidates = self.router.repository.get_context_events([event.id for event in snapshots])
        recipients = {}
        for event in candidates:
            # Only one explicitly named participant in an active contextual event is safe.
            person = re.fullmatch(
                r"(?i:встреча|созвон|план[её]рка|звонок|обед|ужин)\s+с "
                r"([А-ЯЁ][а-яё]+(?: [А-ЯЁ][а-яё]+){0,2})", event.title,
            )
            if person and not REFERENT.search(event.title):
                recipients[person[1].casefold()] = event
        if len(recipients) == 1:
            event = next(iter(recipients.values()))
            title = REFERENT.sub(lambda _: f"участнику события «{event.title}»", title)
        else:
            temporal.issues["referent"] = "Кому именно позвонить?"
        return parsed.model_copy(update={"title": title})

    def _success(self, conversation, context, intent, events, clear_pending):
        if not events and intent in (Intent.CREATE_EVENT, Intent.UPDATE_EVENT, Intent.DELETE_EVENT, Intent.COMPLETE_EVENT):
            if clear_pending:
                conversation.pending_action = None
            return self.responses.not_found(conversation.id, intent)
        if intent == Intent.CREATE_EVENT:
            conversation.last_event_id = events[0].id
            context.last_created_event = EventResponse.model_validate(events[0])
            context.last_event_scope = "last_created"
            if len(events) > 1:
                # A created batch/series is also a visible ordered list. Explicit
                # ordinals must address the same cards the user just received.
                context.last_listed_events = [
                    EventResponse.model_validate(event) for event in events[:self.responses.limit]
                ]
        elif intent in (Intent.LIST_EVENTS, Intent.SEARCH_EVENTS):
            context.last_listed_events = [
                EventResponse.model_validate(event) for event in events[:self.responses.limit]
            ]
            context.last_event_scope = "last_listed"
        elif intent == Intent.DELETE_EVENT:
            deleted_ids = {event.id for event in events}
            if context.last_created_event and context.last_created_event.id in deleted_ids:
                context.last_created_event = None
            if conversation.last_event_id in deleted_ids:
                conversation.last_event_id = None
            context.last_listed_events = [event for event in context.last_listed_events if event.id not in deleted_ids]
        elif intent in (Intent.UPDATE_EVENT, Intent.COMPLETE_EVENT):
            updated = {event.id: EventResponse.model_validate(event) for event in events}
            if context.last_created_event and context.last_created_event.id in updated:
                context.last_created_event = updated[context.last_created_event.id]
            context.last_listed_events = [
                updated.get(event.id, event) for event in context.last_listed_events
            ]
            if (intent == Intent.COMPLETE_EVENT and not context.last_created_event
                    and not context.last_listed_events):
                context.last_listed_events = list(updated.values())
                context.last_event_scope = "last_listed"
        conversation.context = context.model_dump(mode="json", exclude={"pending_action"})
        if clear_pending:
            conversation.pending_action = None
        return self.responses.success(conversation.id, intent, events)
