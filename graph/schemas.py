"""Pydantic schemas for every `.with_structured_output()` call."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Label = Literal["interview_invite", "scheduling_request", "interview_confirmation", "reschedule", "next_round",
                "recruiter_outreach", "rejection_or_offer", "not_relevant"]
Round = Literal["coding", "ai_assisted_coding", "system_design", "low_level_design", "behavioral"]
Difficulty = Literal["easy", "medium", "hard"]
Level = Literal["junior", "mid", "senior", "staff", "principal"]
Conf = Literal["high", "medium", "low"]


class TriageDecision(BaseModel):
    label: Label
    confidence: Conf
    reason: str = Field(description="One line explaining the classification.")
    reschedule_scope: Optional[Literal["date_only", "round_changed"]] = Field(
        None, description="Only for reschedule: did only the date/time change, or did the round details change too?")


class FieldValue(BaseModel):
    value: Optional[str] = None
    source: Optional[str] = Field(None, description="Where it came from, e.g. 'email body', 'calendar invite', 'thread message 1', 'application confirmation email', 'job post', 'company research', 'inferred from role title'.")


class ExtractionResult(BaseModel):
    company: FieldValue = FieldValue()
    role: FieldValue = FieldValue()
    round_types: list[Round] = []
    round_types_source: Optional[str] = None
    round_types_stated: bool = Field(True, description="False when round types come from company research, not the emails.")
    level: FieldValue = FieldValue()
    interview_datetime: FieldValue = Field(FieldValue(), description="ISO 8601 with offset, e.g. 2026-11-12T10:00:00-08:00.")
    format: FieldValue = FieldValue()
    job_post: FieldValue = FieldValue()
    job_post_summary: Optional[str] = None
    recruiter_name: FieldValue = FieldValue()
    industry: FieldValue = FieldValue()
    focus_topics: list[str] = []
    focus_topics_source: Optional[str] = Field(None, description="e.g. 'attachment: Prep Guide.pdf'")
    notes: Optional[str] = None
    conflicts: list[str] = Field([], description="Specific conflicts between sources, each naming both sources.")
    decision: Literal["complete", "wait", "handoff"] = "complete"
    decision_reason: str = ""
    handoff_question: Optional[str] = Field(None, description="One specific question for the user, only when decision=handoff.")


class JobPostInfo(BaseModel):
    role: Optional[str] = None
    level: Optional[Level] = None
    industry: Optional[str] = None
    stack: list[str] = []
    responsibilities: list[str] = []
    likely_round_types: list[Round] = []
    summary: str = ""


class CompanyProcess(BaseModel):
    rounds: list[Round] = []
    notes: str = ""
    sources: list[str] = []
    confidence: Conf = "low"
    confidence_reason: str = ""


class ReportedQuestion(BaseModel):
    title: str
    prompt: str
    round_type: Round
    topic: str
    url: Optional[str] = None
    audience_role: str = Field("software_engineer", description="Which job the source targets (software_engineer, product_manager, data_scientist, ...).")


class ReportedQuestions(BaseModel):
    questions: list[ReportedQuestion] = []


class GeneratedQuestion(BaseModel):
    title: str
    prompt: str = Field(description="3-4 sentences, one paragraph, no worked examples. Coding: state input, output, and constraints. AI-assisted: a build/extend task where the candidate drives an AI assistant, then inspects, runs, verifies and corrects its output.")
    topic: str
    competency: Optional[str] = None


class DifficultyCalibration(BaseModel):
    difficulty: Difficulty
    reason: str = Field(description="One line, for the target level.")


class Gap(BaseModel):
    round: str
    difficulty: Difficulty
    filled: int
    target: int


class CoverageReport(BaseModel):
    meets_goal: bool
    gaps: list[Gap] = []
    quality_issues: list[str] = []
    summary: str = ""


class FeedbackScope(BaseModel):
    level: Literal["question", "round", "doc"] = "doc"
    rounds: list[Round] = []
    difficulties: list[Difficulty] = []
    question_ids: list[str] = []


class FeedbackDecision(BaseModel):
    classification: Literal["revision_request", "question", "count_change", "note", "unclear"]
    scope: FeedbackScope = FeedbackScope()
    directive: str = Field("", description="What to change, e.g. 'harder: replace easy questions', 'more payments-relevant', 'replace question X'.")
    count_override: Optional[Gap] = Field(None, description="For count_change: round, difficulty and the new target (filled is ignored).")
    answer: Optional[str] = Field(None, description="For question: the answer, grounded only in the stored question details.")
    clarifying_question: Optional[str] = None


class BaselineQuestion(BaseModel):
    round_type: str
    difficulty: str
    title: str
    url: Optional[str] = None


class BaselineResult(BaseModel):
    label: Label
    company: Optional[str] = None
    role: Optional[str] = None
    level: Optional[str] = None
    round_types: list[str] = []
    interview_datetime: Optional[str] = None
    questions: list[BaselineQuestion] = []
