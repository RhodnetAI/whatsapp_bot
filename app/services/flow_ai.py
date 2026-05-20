"""
Flow AI service for managing conversation flows defined in the flow builder.
Routes users through a series of questions and collects responses.
"""
import datetime
import logging
from typing import Any, Optional

logger = logging.getLogger("whatsapp")

DEFAULT_GREETING = "Hi! Before we chat, I'd love to ask you a couple of quick questions."
DEFAULT_COMPLETION = "Thanks! Here's what I've got — confirm to continue."
DEFAULT_THANK_YOU = "Thanks for sharing that! I'm all set — feel free to ask me anything now."

LEAD_LABEL_GENERAL = "general"
LEAD_LABEL_HIGH_INTENT = "high intent"
LEAD_LABEL_HOT_LEAD = "hot lead"


def get_flow_state(conversation_data: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Extract flow state from conversation metadata.
    Flow state is stored in the last message's flow_state field if it exists.
    """
    if not conversation_data:
        return {
            "started": False,
            "current_question_index": 0,
            "answers": {},
            "completed": False,
        }

    last_entry = conversation_data[-1] if conversation_data else {}
    return last_entry.get("flow_state", {
        "started": False,
        "current_question_index": 0,
        "answers": {},
        "completed": False,
    })


def set_flow_state(conversation_data: list[dict[str, Any]], flow_state: dict[str, Any]) -> None:
    """Update flow state in the last conversation entry."""
    if not conversation_data:
        return
    conversation_data[-1]["flow_state"] = flow_state


def extract_text_nodes(
    flow_state: dict[str, Any],
    flow_builder: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """
    Extract question nodes from the flow builder state.
    Returns a list of text nodes with their questions and options.
    """
    if isinstance(flow_builder, dict):
        state = flow_builder.get("state")
        if isinstance(state, dict):
            nodes = state.get("nodes")
            if isinstance(nodes, list):
                return [node for node in nodes if isinstance(node, dict) and node.get("type") == "text"]

    nodes = flow_state.get("nodes")
    if not isinstance(nodes, list):
        return []
    
    text_nodes = [node for node in nodes if isinstance(node, dict) and node.get("type") == "text"]
    return text_nodes


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
    
    # Start flow if not started
    if not flow_state.get("started"):
        flow_state["started"] = True
        flow_state["current_question_index"] = 0
        flow_state["answers"] = {}
        flow_state["completed"] = False
        set_flow_state(conversation_data, flow_state)

        # Prompt with the greeting and first question together at flow start
        text_nodes = extract_text_nodes(flow_state, flow_builder)
        if text_nodes:
            return f"{DEFAULT_GREETING}\n\n{_render_question_text(text_nodes[0])}", flow_state

        return DEFAULT_GREETING, flow_state
    
    # Get text nodes (questions) from flow
    text_nodes = extract_text_nodes(flow_state, flow_builder)
    
    if not text_nodes:
        # No questions in flow, end the flow
        flow_state["completed"] = True
        set_flow_state(conversation_data, flow_state)
        return DEFAULT_THANK_YOU, flow_state
    
    current_idx = flow_state.get("current_question_index", 0)
    
    # Flow already completed - shouldn't reach here, but handle gracefully
    if flow_state.get("completed"):
        return "", flow_state
    
    # Check if user is confirming completion
    if current_idx >= len(text_nodes):
        if user_input.lower().strip() in ["confirm", "yes", "ok"]:
            flow_state["completed"] = True
            set_flow_state(conversation_data, flow_state)
            return DEFAULT_THANK_YOU, flow_state
        else:
            # Waiting for confirmation
            completion_message = _build_completion_message(
                text_nodes,
                flow_state.get("answers", {})
            )
            return completion_message, flow_state
    
    # Store user's answer to current question
    current_node = text_nodes[current_idx]
    current_node_id = current_node.get("id")
    if not isinstance(current_node_id, str):
        current_node_id = f"question_{current_idx + 1}"
    current_question = current_node.get("question", f"Question {current_idx + 1}")
    
    flow_state["answers"][current_node_id] = user_input
    
    # Move to next question
    flow_state["current_question_index"] = current_idx + 1
    set_flow_state(conversation_data, flow_state)
    
    # Check if we have more questions
    if current_idx + 1 < len(text_nodes):
        next_node = text_nodes[current_idx + 1]
        acknowledgment = f"Got it, thanks for that. "
        return f"{acknowledgment}\n\n{_render_question_text(next_node)}", flow_state
    else:
        # All questions answered, show completion
        completion_message = _build_completion_message(
            text_nodes,
            flow_state.get("answers", {})
        )
        return completion_message, flow_state


def _render_question_text(node: dict[str, Any]) -> str:
    """Render a single question text, including options if present."""
    question = node.get("question") or node.get("label") or "Question"
    raw_options = node.get("options") or node.get("choices")
    if isinstance(raw_options, list) and raw_options:
        option_lines: list[str] = []
        for option in raw_options:
            if isinstance(option, dict):
                label = option.get("label") or option.get("value") or str(option)
            else:
                label = str(option)
            option_lines.append(f"- {label}")

        return f"{question}\n{chr(10).join(option_lines)}"
    return str(question)


def _build_completion_message(text_nodes: list[dict[str, Any]], answers: dict[str, str]) -> str:
    """Build a summary message of collected answers."""
    if not text_nodes:
        return DEFAULT_COMPLETION
    
    summary_lines = [DEFAULT_COMPLETION, ""]
    
    for node in text_nodes:
        node_id = node.get("id")
        if not isinstance(node_id, str):
            node_id = ""
        question = node.get("question", "Question")
        answer = answers.get(node_id, "—")
        summary_lines.append(f"• {question}\n  {answer}")
    
    summary_lines.extend(["", "Type *confirm* to proceed."])
    return "\n".join(summary_lines)


def get_flow_lead_label(
    flow_state: dict[str, Any],
    flow_builder: Optional[dict[str, Any]],
) -> str:
    text_nodes = extract_text_nodes(flow_state, flow_builder)
    total_questions = len(text_nodes)
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
    text_nodes = extract_text_nodes(flow_state, flow_builder)
    answers = flow_state.get("answers", {})

    confirmed_questions: list[dict[str, str]] = []
    for node in text_nodes:
        node_id = node.get("id")
        if not isinstance(node_id, str):
            continue
        question = node.get("question", "Question")
        confirmed_questions.append(
            {
                "node_id": node_id,
                "question": question,
                "answer": answers.get(node_id, ""),
            }
        )

    return {
        "answers": answers,
        "questions": confirmed_questions,
        "completed_at": datetime.datetime.utcnow().isoformat() if isinstance(datetime, type) else "",
    }


def should_use_flow(flow_builder: Optional[dict[str, Any]]) -> bool:
    """Check if flow builder is enabled and has valid state."""
    if not isinstance(flow_builder, dict):
        return False
    
    if not flow_builder.get("enabled"):
        return False
    
    state = flow_builder.get("state")
    if not isinstance(state, dict):
        return False
    
    nodes = state.get("nodes")
    if not isinstance(nodes, list):
        return False
    
    # Flow must have at least one text node (question)
    text_nodes = [n for n in nodes if isinstance(n, dict) and n.get("type") == "text"]
    return len(text_nodes) > 0
