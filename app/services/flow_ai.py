"""
Flow AI service for managing conversation flows defined in the flow builder.

The flow builder (frontend/src/components/Projects/BotConfiguration/Flow) saves
its configuration as a `PipelineConfig` JSON object on
`information_bot.flow_builder` (see frontend/src/types/Flow.ts):

    {
      "questions": [
        {
          "id": "q-...",
          "text": "...",
          "type": "text" | "radio" | "select" | "checkbox",
          "required": bool,
          "options": [{"id": "...", "label": "..."}],
          "context": "...",
          "rules": [{"op": "...", "value": ..., "next": "<question id>|end"}],
          "default_next": "<question id>|end"
        },
        ...
      ],
      "greeting_message": "...",
      "completion_message": "...",
      "thank_you_message": "..."
    }

This module routes a user through that pipeline, evaluating per-question
routing rules to pick the next question, and collects their answers.
"""
import datetime
import logging
import re
from typing import Any, Optional

logger = logging.getLogger("whatsapp")

DEFAULT_GREETING = "Hi! Before we chat, I'd love to ask you a couple of quick questions."
DEFAULT_COMPLETION = "Thanks! Here's what I've got — confirm to continue."
DEFAULT_THANK_YOU = "Thanks for sharing that! I'm all set — feel free to ask me anything now."

LEAD_LABEL_GENERAL = "general"
LEAD_LABEL_HIGH_INTENT = "high intent"
LEAD_LABEL_HOT_LEAD = "hot lead"

# Matches the "end" node id used by the flow builder canvas (flowUtils.END_ID).
END_ID = "end"

CONFIRM_WORDS = {"confirm", "yes", "ok", "okay", "y"}

# Global Settings "When does this run?" option (flow_builder.run_mode).
RUN_MODE_EVERY_SESSION = "every_session"
RUN_MODE_ONCE_PER_USER = "once_per_user"
DEFAULT_RUN_MODE = RUN_MODE_EVERY_SESSION


def get_flow_state(conversation_data: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Extract flow state from conversation metadata.
    Flow state is stored in the last message's flow_state field if it exists.
    """
    default_state = {
        "started": False,
        "current_question_id": None,
        "answers": {},
        "completed": False,
    }

    if not conversation_data:
        return default_state

    last_entry = conversation_data[-1] if conversation_data else {}
    return last_entry.get("flow_state", default_state)


def set_flow_state(conversation_data: list[dict[str, Any]], flow_state: dict[str, Any]) -> None:
    """Update flow state in the last conversation entry."""
    if not conversation_data:
        return
    conversation_data[-1]["flow_state"] = flow_state


def get_run_mode(flow_builder: Optional[dict[str, Any]]) -> str:
    """Read the "When does this run?" Global Setting from flow_builder.

    "every_session" (default): the flow restarts whenever a new session
    begins, even if a previous session already completed it.
    "once_per_user": the flow runs at most once ever for a given sender.
    """
    if isinstance(flow_builder, dict):
        mode = flow_builder.get("run_mode")
        if mode in (RUN_MODE_EVERY_SESSION, RUN_MODE_ONCE_PER_USER):
            return mode
    return DEFAULT_RUN_MODE


def fresh_flow_state() -> dict[str, Any]:
    """Return a brand-new, not-yet-started flow state."""
    return {
        "started": False,
        "current_question_id": None,
        "answers": {},
        "completed": False,
    }


def _get_questions(flow_builder: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the `questions` list from a PipelineConfig flow_builder dict."""
    if not isinstance(flow_builder, dict):
        return []
    questions = flow_builder.get("questions")
    if not isinstance(questions, list):
        return []
    return [q for q in questions if isinstance(q, dict)]


def _find_question(questions: list[dict[str, Any]], question_id: Any) -> Optional[dict[str, Any]]:
    for question in questions:
        if question.get("id") == question_id:
            return question
    return None


def _render_question_text(question: dict[str, Any]) -> str:
    """Render a single question's text, including numbered options if present."""
    text = question.get("text") or "Question"
    options = question.get("options")
    if isinstance(options, list) and options:
        lines = [text, ""]
        for idx, option in enumerate(options, start=1):
            label = option.get("label") if isinstance(option, dict) else str(option)
            lines.append(f"{idx}. {label}")
        if question.get("type") == "checkbox":
            lines.append("")
            lines.append("(Reply with the numbers or labels, separated by commas)")
        return "\n".join(lines)
    return text


def _match_single_option(options: list[dict[str, Any]], token: str) -> Optional[dict[str, Any]]:
    token = token.strip()
    if not token:
        return None

    # Match by 1-based index
    if token.isdigit():
        idx = int(token) - 1
        if 0 <= idx < len(options):
            return options[idx]

    # Match by option id or label (case-insensitive)
    token_lower = token.lower()
    for option in options:
        if option.get("id") == token:
            return option
        label = str(option.get("label") or "").strip()
        if label.lower() == token_lower:
            return option
    return None


def _match_options(question: dict[str, Any], user_input: str) -> list[dict[str, Any]]:
    """Match the user's reply against a question's options.

    Returns a list of matched option dicts (0 or 1 for radio/select, 0+ for
    checkbox). Returns an empty list for "text" questions or unmatched input.
    """
    options = question.get("options")
    if not isinstance(options, list) or not options:
        return []

    if question.get("type") == "checkbox":
        tokens = [t for t in re.split(r"[,\n]", user_input) if t.strip()]
    else:
        tokens = [user_input]

    matched: list[dict[str, Any]] = []
    for token in tokens:
        match = _match_single_option(options, token)
        if match is not None and match not in matched:
            matched.append(match)
    return matched


def _format_answer(user_input: str, matched_options: list[dict[str, Any]]) -> str:
    if matched_options:
        return ", ".join(str(option.get("label") or "") for option in matched_options)
    return user_input.strip()


def _rule_matches(
    rule: dict[str, Any],
    question: dict[str, Any],
    user_input: str,
    matched_options: list[dict[str, Any]],
) -> bool:
    op = rule.get("op")
    value = rule.get("value")

    if question.get("type") == "text":
        haystack = user_input.strip()
        if op == "equals":
            return isinstance(value, str) and haystack.lower() == value.strip().lower()
        if op == "not_equals":
            return isinstance(value, str) and haystack.lower() != value.strip().lower()
        if op == "contains":
            return isinstance(value, str) and value.strip().lower() in haystack.lower()
        if op == "regex":
            try:
                return isinstance(value, str) and bool(re.search(value, haystack))
            except re.error:
                return False
        if op == "any_of":
            values = value if isinstance(value, list) else [value]
            return any(
                isinstance(v, str) and v.strip().lower() == haystack.lower()
                for v in values
            )
        return False

    # radio / select / checkbox — compare against the matched option(s)
    selected_ids = {o.get("id") for o in matched_options if o.get("id")}
    selected_labels = {str(o.get("label") or "").strip().lower() for o in matched_options}

    if op == "any_of":
        values = value if isinstance(value, list) else [value]
        value_ids = {v for v in values if isinstance(v, str)}
        value_labels = {v.strip().lower() for v in values if isinstance(v, str)}
        return bool(selected_ids & value_ids) or bool(selected_labels & value_labels)
    if op == "equals":
        return isinstance(value, str) and (value in selected_ids or value.strip().lower() in selected_labels)
    if op == "not_equals":
        return isinstance(value, str) and not (value in selected_ids or value.strip().lower() in selected_labels)
    if op == "contains":
        return isinstance(value, str) and any(value.strip().lower() in label for label in selected_labels)
    if op == "regex":
        try:
            return isinstance(value, str) and any(re.search(value, label) for label in selected_labels)
        except re.error:
            return False
    return False


def _resolve_next_question_id(
    questions: list[dict[str, Any]],
    question: dict[str, Any],
    user_input: str,
    matched_options: list[dict[str, Any]],
) -> str:
    """Determine the next question id (or END_ID) after answering `question`."""
    rules = question.get("rules")
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if _rule_matches(rule, question, user_input, matched_options):
                next_target = rule.get("next")
                if isinstance(next_target, str) and next_target:
                    return next_target

    default_next = question.get("default_next")
    if isinstance(default_next, str) and default_next:
        return default_next

    # Sequential fallback: move to the next question in the list
    ids = [q.get("id") for q in questions]
    try:
        idx = ids.index(question.get("id"))
    except ValueError:
        return END_ID
    if idx + 1 < len(ids):
        next_id = ids[idx + 1]
        if isinstance(next_id, str):
            return next_id
    return END_ID


def _build_completion_message(
    questions: list[dict[str, Any]], answers: dict[str, str], completion_message: str
) -> str:
    """Build a summary message of collected answers."""
    if not questions:
        return completion_message

    summary_lines = [completion_message, ""]

    for question in questions:
        question_id = question.get("id")
        if not isinstance(question_id, str):
            continue
        text = question.get("text") or "Question"
        answer = answers.get(question_id, "—")
        summary_lines.append(f"• {text}\n  {answer}")

    summary_lines.extend(["", "Type *confirm* to proceed."])
    return "\n".join(summary_lines)


def process_flow_message(
    user_input: str,
    flow_state: dict[str, Any],
    conversation_data: list[dict[str, Any]],
    flow_builder: Optional[dict[str, Any]] = None,
) -> tuple[str, dict[str, Any]]:
    """
    Process a user message within a flow.
    Returns: (response_message, updated_flow_state)
    """
    config = flow_builder if isinstance(flow_builder, dict) else {}
    questions = _get_questions(config)
    greeting_message = config.get("greeting_message") or DEFAULT_GREETING
    completion_message = config.get("completion_message") or DEFAULT_COMPLETION
    thank_you_message = config.get("thank_you_message") or DEFAULT_THANK_YOU

    # Start flow if not started
    if not flow_state.get("started"):
        flow_state["started"] = True
        flow_state["answers"] = {}
        flow_state["completed"] = False

        if not questions:
            flow_state["current_question_id"] = END_ID
            flow_state["completed"] = True
            set_flow_state(conversation_data, flow_state)
            return thank_you_message, flow_state

        first_question = questions[0]
        flow_state["current_question_id"] = first_question.get("id")
        set_flow_state(conversation_data, flow_state)

        return f"{greeting_message}\n\n{_render_question_text(first_question)}", flow_state

    # Flow already completed - shouldn't reach here, but handle gracefully
    if flow_state.get("completed"):
        return "", flow_state

    if not questions:
        flow_state["completed"] = True
        set_flow_state(conversation_data, flow_state)
        return thank_you_message, flow_state

    current_id = flow_state.get("current_question_id")

    # Awaiting confirmation after the last question was answered
    if current_id is None or current_id == END_ID:
        if user_input.strip().lower() in CONFIRM_WORDS:
            flow_state["completed"] = True
            set_flow_state(conversation_data, flow_state)
            return thank_you_message, flow_state

        return _build_completion_message(questions, flow_state.get("answers", {}), completion_message), flow_state

    current_question = _find_question(questions, current_id)
    if current_question is None:
        # Defensive: unknown question id, end the flow gracefully
        flow_state["current_question_id"] = END_ID
        flow_state["completed"] = True
        set_flow_state(conversation_data, flow_state)
        return thank_you_message, flow_state

    # Validate the answer to the current question
    matched_options = _match_options(current_question, user_input)
    is_choice_question = current_question.get("type") != "text"

    if current_question.get("required"):
        if is_choice_question and not matched_options:
            return f"Sorry, I didn't catch that.\n\n{_render_question_text(current_question)}", flow_state
        if not is_choice_question and not user_input.strip():
            return f"This question is required.\n\n{_render_question_text(current_question)}", flow_state

    # Store the answer
    flow_state.setdefault("answers", {})[current_id] = _format_answer(user_input, matched_options)

    # Determine the next question
    next_id = _resolve_next_question_id(questions, current_question, user_input, matched_options)
    flow_state["current_question_id"] = next_id
    set_flow_state(conversation_data, flow_state)

    if next_id == END_ID:
        return _build_completion_message(questions, flow_state["answers"], completion_message), flow_state

    next_question = _find_question(questions, next_id)
    if next_question is None:
        flow_state["current_question_id"] = END_ID
        set_flow_state(conversation_data, flow_state)
        return _build_completion_message(questions, flow_state["answers"], completion_message), flow_state

    return f"Got it, thanks for that.\n\n{_render_question_text(next_question)}", flow_state


def get_flow_lead_label(
    flow_state: dict[str, Any],
    flow_builder: Optional[dict[str, Any]],
) -> str:
    questions = _get_questions(flow_builder)
    total_questions = len(questions)
    if total_questions == 0:
        return LEAD_LABEL_GENERAL

    answers = flow_state.get("answers")
    if not isinstance(answers, dict):
        return LEAD_LABEL_GENERAL

    completed_answers = sum(
        1
        for answer in answers.values()
        if isinstance(answer, str) and answer.strip() != ""
    )

    if completed_answers == 0:
        return LEAD_LABEL_GENERAL
    if completed_answers >= total_questions:
        return LEAD_LABEL_HOT_LEAD
    return LEAD_LABEL_HIGH_INTENT


def build_flow_confirmation_details(
    flow_builder: Optional[dict[str, Any]],
    flow_state: dict[str, Any],
) -> dict[str, Any]:
    """Build the JSON payload saved when the flow is confirmed."""
    questions = _get_questions(flow_builder)
    answers = flow_state.get("answers", {})

    confirmed_questions: list[dict[str, str]] = []
    for question in questions:
        question_id = question.get("id")
        if not isinstance(question_id, str):
            continue
        confirmed_questions.append(
            {
                "node_id": question_id,
                "question": question.get("text") or "Question",
                "answer": answers.get(question_id, ""),
            }
        )

    return {
        "answers": answers,
        "questions": confirmed_questions,
        "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def should_use_flow(flow_builder: Optional[dict[str, Any]], flow_creation_enabled: bool = False) -> bool:
    """Check if Flow Creation is enabled and the flow has at least one question."""
    if not flow_creation_enabled:
        return False

    return len(_get_questions(flow_builder)) > 0
