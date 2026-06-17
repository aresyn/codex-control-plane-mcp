from __future__ import annotations


OPERATION_STARTABLE_STATUSES = {
    "queued",
    "starting_app_server",
    "starting_thread",
    "starting_turn",
}

TURN_ACTIVE_STATUSES = {
    "accepted",
    "started",
    "running",
    "first_message_received",
    "waiting_for_approval",
    "waiting_for_user_input",
}

TURN_COMPLETION_OBSERVED_STATUSES = {
    "completed",
    "failed",
    "aborted",
    "cancelled",
    "canceled",
    "interrupted",
}

TURN_TERMINAL_STATUSES = TURN_COMPLETION_OBSERVED_STATUSES | {
    "unknown_after_app_server_exit",
}

TURN_SUCCESS_STATUSES = {"completed"}

OPERATION_ACTIVE_STATUSES = OPERATION_STARTABLE_STATUSES | TURN_ACTIVE_STATUSES

OPERATION_TERMINAL_STATUSES = {
    "completed",
    "failed",
    "aborted",
    "cancelled",
    "canceled",
    "interrupted",
    "orphaned",
    "unknown_after_app_server_exit",
}

OPERATION_SUCCESS_STATUSES = {"completed"}

PROMPT_SUBMISSION_CLEANUP_STATUSES = OPERATION_TERMINAL_STATUSES | {
    "orphaned_after_app_server_exit",
}

PENDING_INTERACTION_ACTIVE_STATUSES = {"pending"}

PENDING_INTERACTION_TERMINAL_STATUSES = {
    "answered",
    "auto_declined",
    "expired",
    "failed",
    "orphaned_after_app_server_exit",
}

PENDING_INTERACTION_STATUSES = PENDING_INTERACTION_ACTIVE_STATUSES | PENDING_INTERACTION_TERMINAL_STATUSES


def is_operation_startable(status: str) -> bool:
    return status in OPERATION_STARTABLE_STATUSES


def is_operation_terminal(status: str) -> bool:
    return status in OPERATION_TERMINAL_STATUSES


def is_turn_terminal(status: str) -> bool:
    return status in TURN_TERMINAL_STATUSES


def is_pending_interaction_terminal(status: str) -> bool:
    return status in PENDING_INTERACTION_TERMINAL_STATUSES
