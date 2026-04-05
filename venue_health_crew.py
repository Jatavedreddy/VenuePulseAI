import json
import os
from datetime import datetime
from typing import Any

from crewai import Agent, Crew, Process, Task, LLM
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from agent_tools import resolve_helpdesk_ticket, update_event_staffing


def _as_json(value: Any) -> str:
    """Safely serialize any input payload for agent context blocks."""
    try:
        return json.dumps(value, default=str, separators=(",", ":"))
    except TypeError:
        return str(value)


def _shorten(value: Any, limit: int = 1200) -> str:
    """Trim large serialized payloads to keep token usage predictable."""
    payload = _as_json(value)
    if len(payload) <= limit:
        return payload
    return payload[:limit] + "... [truncated]"


def _extract_text(result: Any) -> str:
    """Normalize CrewAI result objects into plain text."""
    if hasattr(result, "raw") and result.raw:
        return str(result.raw)
    return str(result)


def _run_guardrail_actions(event_data: Any, financial_data: Any, open_tickets: Any) -> dict[str, list[str]]:
    """Run deterministic tool actions so DB updates happen even if model skips tool calls."""
    notes: dict[str, list[str]] = {"staffing": [], "helpdesk": []}

    event = event_data if isinstance(event_data, dict) else {}
    finance = financial_data if isinstance(financial_data, dict) else {}

    event_id = int(event.get("id", 0) or 0)
    if event_id > 0:
        expected_attendance = float(finance.get("expected_attendance_count", 0) or 0)
        if expected_attendance <= 0:
            capacity = float(event.get("capacity", 0) or 0)
            expected_pct = float(finance.get("expected_capacity_percentage", 0) or 0)
            expected_attendance = capacity * (expected_pct / 100.0)

        recommended_bartenders = max(1, int(round(expected_attendance / 120)))
        recommended_security = max(1, int(round(expected_attendance / 180)))
        staffing_result = update_event_staffing.run(
            event_id=event_id,
            recommended_bartenders=recommended_bartenders,
            recommended_security=recommended_security,
        )
        notes["staffing"].append(staffing_result)

    if isinstance(open_tickets, list):
        event_name = event.get("name", "the event")
        for ticket in open_tickets[:5]:
            if not isinstance(ticket, dict):
                continue
            ticket_id = ticket.get("id")
            if ticket_id is None:
                continue

            email_response = (
                f"Hello,\n\n"
                f"Thanks for contacting us about {event_name}. "
                "We have reviewed your request and resolved your support ticket. "
                "If you still need help, reply to this message and our team will assist promptly.\n\n"
                "Best regards,\nVenuePulse Support"
            )
            helpdesk_result = resolve_helpdesk_ticket.run(
                ticket_id=int(ticket_id),
                email_response=email_response,
            )
            notes["helpdesk"].append(helpdesk_result)

    return notes


def _run_event_guardrail_actions(event_data: Any, financial_data: Any) -> list[str]:
    """Run deterministic staffing action for the event-health crew."""
    notes = _run_guardrail_actions(event_data=event_data, financial_data=financial_data, open_tickets=[])
    return notes.get("staffing", [])


def _run_support_guardrail_actions(open_tickets: Any, event_name: str = "the event") -> list[str]:
    """Run deterministic helpdesk actions for the support-triage crew."""
    notes = _run_guardrail_actions(
        event_data={"name": event_name},
        financial_data={},
        open_tickets=open_tickets,
    )
    return notes.get("helpdesk", [])


def _build_llm_and_model() -> tuple[LLM, str]:
    """Create CrewAI LLM and return model display name."""
    load_dotenv()

    model_name = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
    crew_model = os.getenv("CREW_GROQ_MODEL", "llama-3.1-8b-instant")
    groq_api_key = os.getenv("GROQ_API_KEY")
    crew_max_tokens = int(os.getenv("CREW_MAX_TOKENS", "1200"))

    ChatGroq(
        model=model_name,
        groq_api_key=groq_api_key,
        temperature=0.2,
    )

    normalized_model = crew_model
    if not normalized_model.startswith("groq/"):
        normalized_model = f"groq/{normalized_model}"

    llm = LLM(
        model=normalized_model,
        api_key=groq_api_key,
        temperature=0.2,
        max_tokens=crew_max_tokens,
    )
    return llm, crew_model


def _kickoff_with_fallback(crew: Crew) -> tuple[Any | None, str | None]:
    """Run crew kickoff and capture failures as string messages."""
    kickoff_result = None
    kickoff_error = None
    try:
        kickoff_result = crew.kickoff()
    except Exception as exc:
        kickoff_error = str(exc)

    if kickoff_result is not None and not _extract_text(kickoff_result).strip():
        kickoff_error = "Crew analysis failed: model returned empty response."
        kickoff_result = None

    return kickoff_result, kickoff_error


def _collect_task_sections(kickoff_result: Any | None) -> list[str]:
    """Collect per-task output sections from CrewAI results."""
    task_sections: list[str] = []
    if kickoff_result is not None and hasattr(kickoff_result, "tasks_output") and kickoff_result.tasks_output:
        for idx, task_output in enumerate(kickoff_result.tasks_output, start=1):
            task_sections.append(f"## Task {idx} Output\n\n{_extract_text(task_output)}")
    return task_sections


def _build_markdown_report(
    title: str,
    active_model: str,
    final_output: str,
    kickoff_error: str | None,
    task_sections: list[str],
    guardrail_heading: str,
    guardrail_lines: list[str],
) -> str:
    """Build a markdown report for a crew run."""
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    markdown_report = (
        f"# {title}\n\n"
        f"- Model: `{active_model}`\n"
        f"- Generated: `{generated_at}`\n\n"
        "## Final Crew Output\n\n"
        f"{final_output}\n"
    )

    if kickoff_error:
        markdown_report += (
            "\n## Crew Execution Note\n\n"
            f"- Encountered model/tool-call issue: `{kickoff_error}`\n"
            "- Returned report includes deterministic actions and their outcomes.\n"
        )

    markdown_report += "\n## Guardrail Actions\n\n"
    markdown_report += "These deterministic actions were executed to ensure DB updates are applied.\n\n"
    markdown_report += f"### {guardrail_heading}\n"

    if guardrail_lines:
        markdown_report += "\n".join([f"- {line}" for line in guardrail_lines]) + "\n"
    else:
        markdown_report += f"- No {guardrail_heading.lower()} action executed.\n"

    if task_sections:
        markdown_report += "\n---\n\n" + "\n\n---\n\n".join(task_sections)

    return markdown_report


def run_event_health_crew(event_data: Any, financial_data: Any) -> str:
    """Run event-health crew with Financial, Marketing, and Operations agents."""
    llm, active_model = _build_llm_and_model()

    financial_analyst = Agent(
        role="Financial Analyst",
        goal=(
            "Assess event profitability based on budget position, break-even analysis, "
            "ticket economics, and projected capacity utilization."
        ),
        backstory=(
            "You are a venue finance specialist who has audited dozens of live events. "
            "You focus on hard numbers, downside risk, and actionable fixes to protect margins."
        ),
        llm=llm,
        allow_delegation=False,
    )

    marketing_strategist = Agent(
        role="Marketing Strategist",
        goal=(
            "Write conversion-focused promotional copy or discount email messaging "
            "based on financial urgency and demand signals."
        ),
        backstory=(
            "You are a growth marketer for entertainment brands with expertise in urgency, "
            "positioning, and retention campaigns for underperforming ticket sales."
        ),
        llm=llm,
        allow_delegation=False,
    )

    operations_manager = Agent(
        role="Operations Manager",
        goal=(
            "Calculate staffing and inventory needs using expected attendance, "
            "capacity pressure, and event type constraints."
        ),
        backstory=(
            "You have run high-volume venue operations and specialize in staffing plans, "
            "service-level reliability, and avoiding stockouts during peak crowd windows."
        ),
        llm=llm,
        tools=[update_event_staffing],
        allow_delegation=False,
    )

    event_context = _shorten(event_data, 600)
    financial_context = _shorten(financial_data, 600)

    financial_task = Task(
        description=(
            "Analyze event and financial context to assess profitability and financial risk.\n\n"
            f"Event Data:\n{event_context}\n\n"
            f"Financial Data:\n{financial_context}\n\n"
            "Provide:\n"
            "1) Profitability status (healthy/warning/critical).\n"
            "2) Break-even outlook and assumptions.\n"
            "3) Top 3 financial risks.\n"
            "4) Top 3 concrete corrective actions."
        ),
        expected_output=(
            "A concise financial assessment with risk rating, break-even interpretation, "
            "and prioritized recommendations."
        ),
        agent=financial_analyst,
    )

    marketing_task = Task(
        description=(
            "Use the same context to draft promotional messaging aligned to financial urgency.\n\n"
            f"Event Data:\n{event_context}\n\n"
            f"Financial Data:\n{financial_context}\n\n"
            "Create:\n"
            "1) A short promotional social/media ad copy.\n"
            "2) A discount email draft with subject line and CTA.\n"
            "3) Audience segment suggestions and urgency rationale."
        ),
        expected_output=(
            "Campaign-ready promotional copy and discount email tailored to the event's "
            "financial urgency."
        ),
        agent=marketing_strategist,
    )

    operations_task = Task(
        description=(
            "You are in strict tool-calling mode.\n\n"
            f"Event Data:\n{event_context}\n\n"
            f"Financial Data:\n{financial_context}\n\n"
            "Required steps:\n"
            "1) Compute integer staffing values for bartenders and security from expected attendance.\n"
            "2) Immediately call `update_event_staffing` exactly once with integer args:\n"
            "   - event_id\n"
            "   - recommended_bartenders\n"
            "   - recommended_security\n"
            "   Use numeric values (not quoted strings).\n"
            "3) After the tool call, return one short plain-text sentence only.\n\n"
            "Do NOT include equations, markdown headings, bullet lists, or explanatory paragraphs before the tool call."
        ),
        expected_output=(
            "A short plain-text confirmation that update_event_staffing was executed successfully."
        ),
        agent=operations_manager,
    )

    crew = Crew(
        agents=[financial_analyst, marketing_strategist, operations_manager],
        tasks=[financial_task, marketing_task, operations_task],
        process=Process.sequential,
        verbose=False,
    )

    kickoff_result, kickoff_error = _kickoff_with_fallback(crew)
    task_sections = _collect_task_sections(kickoff_result)

    final_output = (
        _extract_text(kickoff_result)
        if kickoff_result is not None
        else "Crew run did not return a valid tool-call response from the model. Deterministic staffing actions were executed."
    )

    staffing_lines = _run_event_guardrail_actions(event_data=event_data, financial_data=financial_data)

    return _build_markdown_report(
        title="Event Health Crew Report",
        active_model=active_model,
        final_output=final_output,
        kickoff_error=kickoff_error,
        task_sections=task_sections,
        guardrail_heading="Staffing",
        guardrail_lines=staffing_lines,
    )


def run_support_triage_crew(open_tickets: Any) -> str:
    """Run support-triage crew with only the Helpdesk Triage agent."""
    llm, active_model = _build_llm_and_model()

    helpdesk_triage = Agent(
        role="Helpdesk Triage",
        goal=(
            "Draft polite, concise, and solution-oriented email replies for open support tickets."
        ),
        backstory=(
            "You are a senior customer support lead known for empathetic communication, "
            "clear next steps, and fast de-escalation in event support workflows."
        ),
        llm=llm,
        tools=[resolve_helpdesk_ticket],
        allow_delegation=False,
    )

    tickets_context = _shorten(open_tickets, 900)

    helpdesk_task = Task(
        description=(
            "You are in strict tool-calling mode.\n\n"
            f"Open Tickets:\n{tickets_context}\n\n"
            "For each listed open ticket, call `resolve_helpdesk_ticket` with:\n"
            "- ticket_id (int)\n"
            "- email_response (str, polite and concise)\n\n"
            "After all tool calls, return one short plain-text summary with resolved ticket IDs.\n"
            "Do NOT include markdown headings, bullet lists, or explanatory paragraphs before tool calls."
        ),
        expected_output=(
            "A short plain-text summary listing ticket IDs resolved via resolve_helpdesk_ticket."
        ),
        agent=helpdesk_triage,
    )

    crew = Crew(
        agents=[helpdesk_triage],
        tasks=[helpdesk_task],
        process=Process.sequential,
        verbose=False,
    )

    kickoff_result, kickoff_error = _kickoff_with_fallback(crew)
    task_sections = _collect_task_sections(kickoff_result)

    final_output = (
        _extract_text(kickoff_result)
        if kickoff_result is not None
        else "Crew run did not return a valid tool-call response from the model. Deterministic support actions were executed."
    )

    helpdesk_lines = _run_support_guardrail_actions(open_tickets=open_tickets)

    return _build_markdown_report(
        title="Support Triage Crew Report",
        active_model=active_model,
        final_output=final_output,
        kickoff_error=kickoff_error,
        task_sections=task_sections,
        guardrail_heading="Helpdesk",
        guardrail_lines=helpdesk_lines,
    )


def run_venue_health_check(event_data: Any, financial_data: Any, open_tickets: Any) -> str:
    """Backward-compatible wrapper that runs both crews and combines reports."""
    event_report = run_event_health_crew(
        event_data=event_data,
        financial_data=financial_data,
    )
    support_report = run_support_triage_crew(open_tickets=open_tickets)
    return event_report + "\n\n---\n\n" + support_report
