"""Asynchronous OpenAI Responses API orchestration for one customer turn."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final, cast

from openai import APIError, AsyncOpenAI
from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseInputItemParam,
    ResponseOutputItem,
    ToolParam,
)
from pydantic import BaseModel

from app.agent.prompt import RECENT_MESSAGE_LIMIT, build_agent_prompt
from app.agent.tool_definitions import TOOL_DEFINITIONS
from app.agent.tool_executor import PreparedPreviewMetadata, ToolExecutor
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    TECHNICAL_FALLBACK_MESSAGE,
    AgentExecutionError,
    ApplicationError,
)
from app.schemas.agent import AgentResult, PreviewDeliveryMetadata
from app.schemas.quotation import GeneratedQuotation
from app.schemas.session import ConversationMessage, ConversationSession
from app.services.quotation_service import CurrentTurnContext

MAX_TOOL_ITERATIONS: Final = 8


class AIAgent:
    """Run a bounded Responses API tool loop against the allowlisted executor."""

    __slots__ = (
        "_client",
        "_max_tool_iterations",
        "_model",
        "_recent_message_limit",
        "_tool_executor",
    )

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: AsyncOpenAI | None = None,
        tool_executor: ToolExecutor | None = None,
    ) -> None:
        resolved_settings = settings or get_settings()
        model = resolved_settings.openai_model
        if model is None or not model.strip():
            raise AgentExecutionError("OPENAI_MODEL is required for agent execution")

        if client is None:
            configured_key = resolved_settings.openai_api_key
            if configured_key is None or not configured_key.get_secret_value().strip():
                raise AgentExecutionError("OPENAI_API_KEY is required for agent execution")
            client = AsyncOpenAI(
                api_key=configured_key.get_secret_value(),
                timeout=resolved_settings.openai_timeout_seconds,
            )

        self._client = client
        self._model = model
        self._tool_executor = tool_executor if tool_executor is not None else ToolExecutor()
        self._max_tool_iterations = min(
            resolved_settings.max_tool_iterations,
            MAX_TOOL_ITERATIONS,
        )
        self._recent_message_limit = min(
            resolved_settings.recent_message_limit,
            RECENT_MESSAGE_LIMIT,
        )

    async def handle_message(
        self,
        session: ConversationSession,
        user_message: str,
        turn_context: CurrentTurnContext,
    ) -> AgentResult:
        """Handle one customer message with bounded sequential tool execution."""
        instructions = build_agent_prompt(
            session,
            recent_message_limit=self._recent_message_limit,
        )
        generated_quote: GeneratedQuotation | None = None
        prepared_preview: PreparedPreviewMetadata | None = None

        try:
            _validate_turn(session, user_message, turn_context)
        except AgentExecutionError:
            return self._finish_turn(
                session,
                TECHNICAL_FALLBACK_MESSAGE,
                generated_quote=None,
                prepared_preview=None,
                append_user=user_message if isinstance(user_message, str) else None,
            )

        session.recent_messages.append(ConversationMessage(role="user", content=user_message))
        input_items: list[ResponseInputItemParam] = [
            cast(ResponseInputItemParam, {"role": "user", "content": user_message})
        ]

        try:
            for _ in range(self._max_tool_iterations):
                response = await self._client.responses.create(
                    model=self._model,
                    instructions=instructions,
                    input=list(input_items),
                    tools=cast(Iterable[ToolParam], TOOL_DEFINITIONS),
                    parallel_tool_calls=False,
                    stream=False,
                )
                if not isinstance(response, Response):
                    raise AgentExecutionError("Responses API returned an unexpected object")
                _validate_response(response)

                response_items = [_response_item_as_input(item) for item in response.output]
                function_calls = _function_calls(response)
                if not function_calls:
                    response_text = _final_response_text(response)
                    return self._finish_turn(
                        session,
                        response_text,
                        generated_quote=generated_quote,
                        prepared_preview=prepared_preview,
                    )

                input_items.extend(response_items)
                for function_call in function_calls:
                    tool_result = await self._tool_executor.execute(
                        function_call.name,
                        function_call.arguments,
                        session,
                        turn_context,
                    )
                    if tool_result.generated_quote is not None:
                        generated_quote = tool_result.generated_quote
                    if tool_result.prepared_preview is not None:
                        prepared_preview = tool_result.prepared_preview
                        preview_text = await self._prepared_preview_text(
                            session,
                            turn_context,
                        )
                        return self._finish_turn(
                            session,
                            preview_text,
                            generated_quote=generated_quote,
                            prepared_preview=prepared_preview,
                        )
                    input_items.append(
                        cast(
                            ResponseInputItemParam,
                            {
                                "type": "function_call_output",
                                "call_id": function_call.call_id,
                                "output": tool_result.model_output_json(),
                            },
                        )
                    )

            raise AgentExecutionError("Maximum tool iterations exceeded")
        except (APIError, ApplicationError, TimeoutError, TypeError, ValueError):
            # Diagnostics remain on the internal exception/logging side. The customer
            # receives one stable fallback and any already-created artifact survives.
            return self._finish_turn(
                session,
                TECHNICAL_FALLBACK_MESSAGE,
                generated_quote=generated_quote,
                prepared_preview=prepared_preview,
            )

    async def _prepared_preview_text(
        self,
        session: ConversationSession,
        turn_context: CurrentTurnContext,
    ) -> str:
        """Retrieve the service-owned deterministic preview text through the executor."""
        summary = await self._tool_executor.execute(
            "get_quote_summary",
            "{}",
            session,
            turn_context,
        )
        summary_text = summary.model_output.get("summary")
        if not isinstance(summary_text, str) or not summary_text.strip():
            raise AgentExecutionError("Quotation preview did not produce deterministic text")
        return summary_text

    def _finish_turn(
        self,
        session: ConversationSession,
        response_text: str,
        *,
        generated_quote: GeneratedQuotation | None,
        prepared_preview: PreparedPreviewMetadata | None,
        append_user: str | None = None,
    ) -> AgentResult:
        """Append exactly one assistant response and enforce the history bound."""
        if append_user is not None:
            session.recent_messages.append(ConversationMessage(role="user", content=append_user))
        session.recent_messages.append(ConversationMessage(role="assistant", content=response_text))
        session.recent_messages = session.recent_messages[-self._recent_message_limit :]
        preview = (
            PreviewDeliveryMetadata(
                fingerprint=prepared_preview.fingerprint,
                originating_turn_id=prepared_preview.originating_turn_id,
            )
            if prepared_preview is not None
            else None
        )
        return AgentResult(
            response_text=response_text,
            updated_session=session,
            generated_quote=generated_quote,
            prepared_preview=preview,
        )


def _validate_turn(
    session: ConversationSession,
    user_message: str,
    turn_context: CurrentTurnContext,
) -> None:
    if not isinstance(session, ConversationSession):
        raise AgentExecutionError("session must be backend-owned conversation state")
    if not isinstance(user_message, str) or not user_message.strip():
        raise AgentExecutionError("user_message must be a nonblank string")
    if not isinstance(turn_context, CurrentTurnContext):
        raise AgentExecutionError("turn_context must be backend-owned")
    if turn_context.session_id != session.session_id:
        raise AgentExecutionError("turn context belongs to another session")
    if turn_context.customer_message != user_message:
        raise AgentExecutionError("turn context message does not match user_message")
    if not isinstance(turn_context.turn_id, str) or not turn_context.turn_id.strip():
        raise AgentExecutionError("turn context ID must be nonblank")


def _validate_response(response: Response) -> None:
    if response.status != "completed" or response.error is not None:
        raise AgentExecutionError(f"Responses API returned status {response.status!r}")
    if not response.output:
        raise AgentExecutionError("Responses API returned no output items")


def _function_calls(response: Response) -> list[ResponseFunctionToolCall]:
    calls = [item for item in response.output if isinstance(item, ResponseFunctionToolCall)]
    call_ids: set[str] = set()
    for call in calls:
        if (
            not call.name.strip()
            or not call.arguments.strip()
            or not call.call_id.strip()
            or call.call_id in call_ids
        ):
            raise AgentExecutionError("Responses API returned a malformed function call")
        call_ids.add(call.call_id)
    return calls


def _response_item_as_input(item: ResponseOutputItem) -> ResponseInputItemParam:
    if not isinstance(item, BaseModel):
        raise AgentExecutionError("Response output item cannot be continued")
    serialized = item.model_dump(mode="json", exclude_none=True, by_alias=True)
    if not isinstance(serialized, Mapping) or "type" not in serialized:
        raise AgentExecutionError("Response output item has no valid type")
    return cast(ResponseInputItemParam, dict(serialized))


def _final_response_text(response: Response) -> str:
    output_text = response.output_text
    if not isinstance(output_text, str) or not output_text.strip():
        raise AgentExecutionError("Responses API returned no final assistant text")
    return output_text.strip()


_default_agent: AIAgent | None = None
_default_agent_signature: tuple[str | None, float, int, int] | None = None


def _get_default_agent() -> AIAgent:
    """Return a lazily configured process agent matching current settings."""
    global _default_agent, _default_agent_signature

    settings = get_settings()
    signature = (
        settings.openai_model,
        settings.openai_timeout_seconds,
        settings.max_tool_iterations,
        settings.recent_message_limit,
    )
    if _default_agent is None or _default_agent_signature != signature:
        _default_agent = AIAgent(settings=settings)
        _default_agent_signature = signature
    return _default_agent


async def handle_message(
    session: ConversationSession,
    user_message: str,
    turn_context: CurrentTurnContext,
) -> AgentResult:
    """Handle a turn through the lazily configured default agent."""
    return await _get_default_agent().handle_message(session, user_message, turn_context)


Agent = AIAgent

__all__ = [
    "MAX_TOOL_ITERATIONS",
    "AIAgent",
    "Agent",
    "handle_message",
]
