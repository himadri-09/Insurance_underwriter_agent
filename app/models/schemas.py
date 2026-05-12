"""
Canonical data models for TriagePilot — Commercial Insurance.
"""

from __future__ import annotations
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from enum import Enum
from datetime import datetime
from uuid import uuid4


# ── Enums ────────────────────────────────────────────

class DocType(str, Enum):
    ACORD = "acord"
    BROKER_SUBMISSION = "broker_submission"
    LOSS_RUN = "loss_run"
    PROPERTY_SCHEDULE = "property_schedule"
    LEGAL_DOCS = "legal_docs"
    FIRE_NOC = "fire_noc"
    INCIDENT_PHOTO = "incident_photo"
    PRIOR_INSURANCE = "prior_insurance"
    FINANCIAL = "financial"
    UNKNOWN = "unknown"


class LineOfBusiness(str, Enum):
    GL = "general_liability"
    PROPERTY = "property"
    WC = "workers_comp"
    COMMERCIAL_AUTO = "commercial_auto"
    UMBRELLA = "umbrella"
    CYBER = "cyber"
    BOP = "business_owners_policy"
    EPLI = "epli"
    DNO = "directors_officers"
    MULTI = "multi_line"
    OTHER = "other"


class AppetiteStatus(str, Enum):
    ACCEPT = "accept"
    REVIEW = "review"
    DECLINE = "decline"
    REFER = "refer"


class SubmissionStatus(str, Enum):
    UPLOADED = "uploaded"
    CLASSIFYING = "classifying"
    EXTRACTING = "extracting"
    RETRIEVING = "retrieving"
    SCORING = "scoring"
    GENERATING = "generating"
    COMPLETE = "complete"
    ERROR = "error"


# ── Company ──────────────────────────────────────────

class CompanyInfo(BaseModel):
    name: str = ""
    dba: str = ""
    registration_number: str = ""
    entity_type: str = ""
    description: str = ""
    naics_code: str = ""
    sic_code: str = ""
    industry: str = ""
    website: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    phone: str = ""
    email: str = ""
    year_established: Optional[int] = None
    years_in_business: Optional[int] = None
    funding_stage: str = ""
    annual_revenue: Optional[float] = None
    headcount: Optional[int] = None
    annual_payroll: Optional[float] = None


# ── Location ─────────────────────────────────────────

class LocationInfo(BaseModel):
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    building_value: Optional[float] = None
    contents_value: Optional[float] = None
    bi_value: Optional[float] = None
    construction_type: str = ""
    year_built: Optional[int] = None
    stories: Optional[int] = None
    square_footage: Optional[int] = None
    sprinklered: Optional[bool] = None
    alarm_system: Optional[bool] = None
    occupancy: str = ""
    protection_class: str = ""
    roof_type: str = ""
    roof_age: Optional[int] = None
    flood_zone: str = ""


# ── Loss ─────────────────────────────────────────────

class LossRecord(BaseModel):
    policy_year: Optional[str] = None
    carrier: Optional[str] = None
    lob: Optional[str] = None
    claim_number: Optional[str] = None
    date_of_loss: Optional[str] = None
    type: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    claims_count: int = 0
    amount_paid: float = 0.0
    amount_reserved: float = 0.0
    incurred: float = 0.0
    subrogation: Optional[str] = None


# ── Coverage ─────────────────────────────────────────

class CoverageRequest(BaseModel):
    lob: str = ""
    coverage_type: str = ""
    limit: Optional[float] = None
    deductible: Optional[float] = None
    retention: Optional[float] = None
    aggregate_limit: Optional[float] = None
    per_occurrence_limit: Optional[float] = None
    prior_carrier: str = ""
    prior_premium: Optional[float] = None
    expiring_date: str = ""
    effective_date: str = ""


# ── Prior Insurance ──────────────────────────────────

class PriorInsurance(BaseModel):
    carrier: str = ""
    policy_number: str = ""
    lob: str = ""
    effective_date: str = ""
    expiration_date: str = ""
    limits: str = ""
    premium: Optional[float] = None
    lapse_in_coverage: bool = False
    lapse_days: Optional[int] = None
    reason_for_change: str = ""
    cancelled_by_carrier: bool = False


# ── Incident ─────────────────────────────────────────

class IncidentDetail(BaseModel):
    date: str = ""
    time: str = ""
    location: str = ""
    description: str = ""
    injuries: bool = False
    property_damage: bool = False
    estimated_loss: Optional[float] = None
    police_report: bool = False
    witnesses: str = ""


# ── Legal ────────────────────────────────────────────

class LegalInfo(BaseModel):
    pending_lawsuits: bool = False
    lawsuit_details: str = ""
    regulatory_actions: bool = False
    regulatory_details: str = ""
    fire_noc_status: str = ""
    fire_noc_expiry: str = ""
    compliance_notes: str = ""


# ── Extracted Field ──────────────────────────────────

class ExtractedField(BaseModel):
    field_name: str = ""
    value: Any = None
    confidence: float = 0.0
    source_doc_id: str = ""
    source_page: int = 0
    source_location: str = ""


# ── Extraction Result ────────────────────────────────

class ExtractionResult(BaseModel):
    company: CompanyInfo = Field(default_factory=CompanyInfo)
    locations: List[LocationInfo] = []
    loss_history: List[LossRecord] = []
    coverages: List[CoverageRequest] = []
    prior_insurance: List[PriorInsurance] = []
    incidents: List[IncidentDetail] = []
    legal: LegalInfo = Field(default_factory=LegalInfo)
    broker_notes: str = ""
    business_description: str = ""
    property_description: str = ""
    requested_effective_date: str = ""
    stated_total_premium: float = 0.0   # from loss run summary — authoritative
    stated_total_incurred: float = 0.0  # from loss run summary — authoritative
    raw_fields: List[ExtractedField] = []
    missing_fields: List[str] = []


# ── Retrieval ────────────────────────────────────────

class RetrievedChunk(BaseModel):
    chunk_id: str
    text: str
    source_doc: str = ""
    section: str = ""
    page: Optional[int] = None
    score: float = 0.0
    match_type: str = ""


# ── Rules & Appetite ─────────────────────────────────

class RuleResult(BaseModel):
    rule_id: str
    rule_name: str
    passed: bool
    reason: str
    severity: str = "info"


class AppetiteAssessment(BaseModel):
    score: int = Field(ge=1, le=5, default=3)
    status: AppetiteStatus = AppetiteStatus.REVIEW
    reasons: List[str] = []
    rule_results: List[RuleResult] = []
    retrieved_evidence: List[RetrievedChunk] = []


# ── Scoring ──────────────────────────────────────────

class SubmissionScoring(BaseModel):
    winnability_score: float = Field(ge=0.0, le=1.0, default=0.5)
    priority_score: float = Field(ge=0.0, le=1.0, default=0.5)
    confidence_score: float = Field(ge=0.0, le=1.0, default=0.5)
    recommended_queue: str = "general"
    referral_required: bool = False
    referral_reasons: List[str] = []
    broker_questions: List[str] = []


# ── Citation ─────────────────────────────────────────

class Citation(BaseModel):
    claim: str = ""
    source_doc: str = ""
    page: Optional[int] = None
    section: str = ""
    quote: str = ""


# ── Upload Document ──────────────────────────────────

class UploadedDocument(BaseModel):
    doc_id: str = Field(default_factory=lambda: str(uuid4()))
    filename: str = ""
    file_type: str = ""
    storage_path: str = ""
    page_count: int = 0
    doc_class: DocType = DocType.UNKNOWN
    classification_confidence: float = 0.0


# ── Final Output ─────────────────────────────────────

class SubmissionOutput(BaseModel):
    submission_id: str = ""
    status: SubmissionStatus = SubmissionStatus.COMPLETE
    line_of_business: str = "other"
    created_at: datetime = Field(default_factory=datetime.utcnow)

    company: CompanyInfo = Field(default_factory=CompanyInfo)
    extracted_facts: List[ExtractedField] = []
    missing_information: List[str] = []

    appetite_assessment: AppetiteAssessment = Field(default_factory=AppetiteAssessment)
    winnability_score: float = 0.5
    priority_score: float = 0.5
    referral_required: bool = False
    referral_reasons: List[str] = []
    recommended_queue: str = "general"
    broker_questions: List[str] = []

    risk_brief_markdown: str = ""
    referral_note: str = ""
    citations: List[Citation] = []
    retrieved_evidence: List[RetrievedChunk] = []

    documents: List[UploadedDocument] = []
    processing_time_seconds: float = 0.0
    model_versions: Dict[str, str] = {}
    errors: List[str] = []


# ── Pipeline State ───────────────────────────────────

class PipelineState(BaseModel):
    submission_id: str = Field(default_factory=lambda: str(uuid4()))
    status: SubmissionStatus = SubmissionStatus.UPLOADED
    started_at: datetime = Field(default_factory=datetime.utcnow)

    documents: List[UploadedDocument] = []
    extraction: ExtractionResult = Field(default_factory=ExtractionResult)
    retrieved_chunks: List[RetrievedChunk] = []
    appetite: AppetiteAssessment = Field(default_factory=AppetiteAssessment)
    scoring: SubmissionScoring = Field(default_factory=SubmissionScoring)
    risk_brief: str = ""
    referral_note: str = ""
    citations: List[Citation] = []
    lob: str = "other"

    form_data: Dict[str, Any] = {}
    errors: List[str] = []
    current_step: str = ""