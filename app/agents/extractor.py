"""
Agent: Document Extractor — Commercial Insurance

All PDFs parsed in parallel via LlamaParse (batch).
Classification + extraction done in ONE LLM call per document.
All images processed in parallel via asyncio.gather.

Key: captures stated_total_premium from loss run summaries
     and cancelled_by_carrier from prior insurance / broker notes.
"""

import asyncio
import structlog
from app.models.schemas import (
    PipelineState, DocType, ExtractionResult,
    CompanyInfo, LocationInfo, LossRecord, CoverageRequest,
    PriorInsurance, IncidentDetail, LegalInfo, ExtractedField
)
from app.services.llm_service import LLMService
from app.services.parse_service import ParseService
from app.utils.document_processor import DocumentProcessor
from app.agents.prompts import (
    EXTRACT_SUBMISSION, EXTRACT_LOSS_RUN,
    EXTRACT_LEGAL, EXTRACT_PRIOR_INSURANCE
)

log = structlog.get_logger()


CLASSIFY_AND_EXTRACT_PROMPT = """You are an expert US commercial insurance document processor.

STEP 1: Classify this document into one of these types:
- acord, broker_submission, loss_run, property_schedule, legal_docs, fire_noc, prior_insurance, financial, unknown

STEP 2: Extract ALL structured data based on the document type.

Return JSON with this structure:
{
  "classification": {
    "doc_type": "<type>",
    "confidence": <0.0-1.0>
  },
  "extracted_data": { ... all fields ... }
}

For ALL document types, extract into this schema where applicable:

"company": { "name", "dba", "registration_number", "entity_type", "description", "naics_code", "sic_code", "industry", "website", "address", "city", "state", "zip_code", "phone", "email", "year_established", "years_in_business", "funding_stage", "annual_revenue", "headcount", "annual_payroll" },
"locations": [{ "address", "city", "state", "zip_code", "building_value", "contents_value", "bi_value", "construction_type", "year_built", "stories", "square_footage", "sprinklered", "alarm_system", "occupancy", "protection_class", "roof_type", "roof_age", "flood_zone" }],
"coverages": [{ "lob", "coverage_type", "limit", "deductible", "aggregate_limit", "per_occurrence_limit", "prior_carrier", "prior_premium", "expiring_date", "effective_date" }],
"prior_insurance": [{ "carrier", "policy_number", "lob", "effective_date", "expiration_date", "limits", "premium", "lapse_in_coverage", "cancelled_by_carrier", "reason_for_change" }],
"loss_history": [{ "claim_number", "date_of_loss", "type", "lob", "description", "status", "amount_paid", "amount_reserved", "incurred", "subrogation", "carrier" }],
"legal": { "pending_lawsuits", "lawsuit_details", "regulatory_actions", "regulatory_details", "fire_noc_status", "fire_noc_expiry", "compliance_notes" },
"broker_notes": "",
"requested_effective_date": "",

CRITICAL FOR LOSS RUNS: Also extract the SUMMARY section at the bottom:
"summary": {
  "total_claims": 0,
  "total_incurred": 0,
  "total_paid": 0,
  "total_premium": 0,
  "years_covered": "",
  "loss_ratio": ""
}

CRITICAL FOR PRIOR INSURANCE / DEC PAGES:
- If ANY mention of "non-renewal", "non-renewed", "will not renew", "cancelled by carrier":
  set cancelled_by_carrier: true and include carrier name.
- Extract the TOTAL ANNUAL PREMIUM if shown.
- Extract ALL endorsements listed.

CRITICAL FOR BROKER SUBMISSIONS:
- Extract the REQUESTED effective date (not current policy date).
- If broker mentions any carrier non-renewal, capture it.

Rules:
- Extract exact values. Do not infer or calculate.
- Dollar amounts as numbers without $ or commas.
- Use null for missing values, not guesses.
""" + """
When extracting from ACORD forms, know that:

ACORD 125 (Commercial Applicant):
- Applicant name, DBA, address, FEIN (→ registration_number), entity type
- SIC/NAICS codes, nature of business, years in business
- Prior insurance: carrier, policy number, premium, dates
- "Any policy cancelled/declined/non-renewed?" — if YES, set cancelled_by_carrier: true

ACORD 126 (GL Section):
- Classification codes, exposure basis, limits, deductible
- Occurrence vs claims-made, hazard questions

ACORD 130 (Workers Compensation):
- State(s), classification codes, payroll per class, experience mod rate
- Prior WC carrier and premium

ACORD 140 (Property Section):
- Construction type, year built, stories, sq ft, sprinklered, alarm
- Protection class, occupancy, roof type/age
- Values: building, contents, business income
- Cause of loss form, coinsurance, valuation method
"""


IMAGE_PROMPT = """Describe this image for an insurance underwriter. Note:
- Type of property/premises/equipment visible
- Any damage visible and severity
- Safety concerns or hazards
- Condition assessment
Return JSON: {"description": "", "damage_visible": false, "damage_severity": "", "property_type": "", "safety_concerns": [], "condition_assessment": "", "relevant_details": []}"""


class ExtractorAgent:
    def __init__(self):
        self.llm = LLMService()
        self.parser = ParseService()
        self.processor = DocumentProcessor()

    async def _extract_single_pdf(self, filename: str, markdown: str) -> dict:
        log.info("llm_extraction_starting", filename=filename, markdown_chars=len(markdown))

        # Step 1: Quick classify from first 1500 chars
        preview = markdown[:1500]
        classify_result = await self.llm.reason(
            system_prompt="Classify this document. Return JSON only: {\"doc_type\": \"acord|broker_submission|loss_run|legal_docs|fire_noc|prior_insurance|financial|unknown\", \"confidence\": 0.0-1.0}",
            user_prompt=preview,
            response_format="json",
        )
        doc_type = classify_result.get("doc_type", "unknown") if isinstance(classify_result, dict) else "unknown"
        confidence = classify_result.get("confidence", 0.0) if isinstance(classify_result, dict) else 0.0
        log.info("quick_classify", filename=filename, doc_type=doc_type, confidence=confidence)

        # Step 2: Use focused prompt based on doc type
        if doc_type == "loss_run":
            extraction_prompt = EXTRACT_LOSS_RUN
        elif doc_type in ("legal_docs", "fire_noc"):
            extraction_prompt = EXTRACT_LEGAL
        elif doc_type == "prior_insurance":
            extraction_prompt = EXTRACT_PRIOR_INSURANCE
        else:
            extraction_prompt = EXTRACT_SUBMISSION

        result = await self.llm.reason(
            system_prompt="You are an expert US commercial insurance data extractor. Return JSON only.",
            user_prompt=f"{extraction_prompt}\n\n---\n\nDOCUMENT ({filename}):\n\n{markdown}",
            response_format="json",
        )

        if isinstance(result, str):
            result = {"raw_text": result}

        # Log the raw extraction for debugging
        log.info(
            "llm_extraction_raw",
            filename=filename,
            doc_type=doc_type,
            result_type=type(result).__name__,
            result_keys=list(result.keys()) if isinstance(result, dict) else [],
            result_json=result if isinstance(result, dict) else None,
        )

        result["_source_doc"] = filename
        result["_doc_type"] = doc_type
        result["_confidence"] = confidence
        
        # Log critical fields
        loss_history = result.get("loss_history", [])
        prior_insurance = result.get("prior_insurance", [])
        summary = result.get("summary", {})
        
        log.info(
            "llm_extraction_done",
            filename=filename,
            doc_type=doc_type,
            keys=list(result.keys()),
            loss_history_count=len(loss_history) if isinstance(loss_history, list) else 0,
            loss_history_type=type(loss_history).__name__,
            prior_insurance_count=len(prior_insurance) if isinstance(prior_insurance, list) else 0,
            prior_insurance_type=type(prior_insurance).__name__,
            has_summary=bool(summary),
            summary_total_premium=summary.get("total_premium") if isinstance(summary, dict) else None,
        )
        
        return result

    async def _extract_single_image(self, filename: str, file_bytes: bytes, file_type: str) -> dict:
        log.info("image_extraction_starting", filename=filename, model=self.llm.settings.extraction_model)
        img_b64 = self.processor.image_to_base64(file_bytes)
        result = await self.llm.extract_from_image(image_base64=img_b64, prompt=IMAGE_PROMPT, media_type=f"image/{file_type}")
        result["_source_doc"] = filename
        result["_doc_type"] = "incident_photo"
        log.info("image_extraction_done", filename=filename)
        return result

    def _merge_extractions(self, extractions: list) -> dict:
        merged = {
            "company": {},
            "locations": [],
            "coverages": [],
            "prior_insurance": [],
            "loss_history": [],
            "legal": {},
            "broker_notes": "",
            "other_fields": [],
            "image_descriptions": [],
            "summary": {},  # loss run summary with stated premium
            "requested_effective_date": "",
        }

        # Log all extractions before merge
        log.info(
            "merge_starting",
            total_extractions=len(extractions),
            extraction_sources=[ext.get("_source_doc", "unknown") for ext in extractions if isinstance(ext, dict)],
        )
        
        for idx, ext in enumerate(extractions):
            if not isinstance(ext, dict):
                continue
            
            # LOG THE ACTUAL KEYS and full JSON so we can debug
            log.info(
                "merging_extraction",
                index=idx,
                source=ext.get("_source_doc", "unknown"),
                doc_type=ext.get("_doc_type", "unknown"),
                keys=list(ext.keys()),
                loss_history_in_ext=len(ext.get("loss_history", [])) if isinstance(ext.get("loss_history"), list) else 0,
                prior_insurance_in_ext=len(ext.get("prior_insurance", [])) if isinstance(ext.get("prior_insurance"), list) else 0,
                full_json=ext,
            )
            
            # LOG THE ACTUAL KEYS so we can debug
            log.info("merging_extraction", source=ext.get("_source_doc", "unknown"), keys=list(ext.keys()))

            # Company
            if "company" in ext and isinstance(ext["company"], dict):
                for k, v in ext["company"].items():
                    if v and not merged["company"].get(k):
                        merged["company"][k] = v

            # Lists
            for key in ("locations", "coverages", "other_fields"):
                if key in ext and isinstance(ext[key], list):
                    merged[key].extend(ext[key])

            # Prior insurance — handle list or dict
            if "prior_insurance" in ext:
                pi = ext["prior_insurance"]
                if isinstance(pi, list):
                    merged["prior_insurance"].extend(pi)
                elif isinstance(pi, dict):
                    merged["prior_insurance"].append(pi)
            if "policies" in ext and isinstance(ext["policies"], list):
                merged["prior_insurance"].extend(ext["policies"])

            # DEEP SCAN for prior insurance / policies under any key
            for key, val in ext.items():
                if key.startswith("_") or key in ("company", "locations", "coverages",
                    "loss_history", "records", "legal", "summary", "broker_notes",
                    "other_fields", "prior_insurance", "policies"):
                    continue
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    if any(k in val[0] for k in ("policy_number", "expiration_date", "cancelled_by_carrier")):
                        merged["prior_insurance"].extend(val)
                        log.info("prior_insurance_found_via_deep_scan", key=key, count=len(val))
                elif isinstance(val, dict) and any(k in val for k in ("policy_number", "carrier", "expiration_date")):
                    if key not in ("company", "legal", "summary"):
                        merged["prior_insurance"].append(val)
                        log.info("prior_insurance_found_via_deep_scan_dict", key=key)

            # Loss history
            if "loss_history" in ext and isinstance(ext["loss_history"], list):
                merged["loss_history"].extend(ext["loss_history"])
            if "records" in ext and isinstance(ext["records"], list):
                merged["loss_history"].extend(ext["records"])

            # DEEP SCAN — catch claims under any key name
            for key, val in ext.items():
                if key.startswith("_") or key in ("company", "locations", "coverages",
                    "prior_insurance", "legal", "summary", "broker_notes",
                    "other_fields", "image_descriptions", "classification",
                    "loss_history", "records", "requested_effective_date"):
                    continue
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    if any(k in val[0] for k in ("date_of_loss", "claim_number", "amount_paid", "incurred", "total_incurred")):
                        merged["loss_history"].extend(val)
                        log.info("loss_history_found_via_deep_scan", key=key, count=len(val))

            # Summary from loss runs — CAPTURE STATED PREMIUM
            if "summary" in ext and isinstance(ext["summary"], dict):
                for k, v in ext["summary"].items():
                    if v and not merged["summary"].get(k):
                        merged["summary"][k] = v

            # Legal
            if "legal" in ext and isinstance(ext["legal"], dict):
                for k, v in ext["legal"].items():
                    if v and not merged["legal"].get(k):
                        merged["legal"][k] = v
            for field in ("pending_lawsuits", "lawsuit_details", "regulatory_actions",
                          "regulatory_details", "fire_noc_status", "fire_noc_expiry", "compliance_notes"):
                if field in ext and ext[field]:
                    merged["legal"][field] = ext[field]

            # Broker notes
            if ext.get("broker_notes"):
                merged["broker_notes"] += "\n" + str(ext["broker_notes"])

            # Requested effective date
            if ext.get("requested_effective_date") and not merged["requested_effective_date"]:
                merged["requested_effective_date"] = str(ext["requested_effective_date"])

            # Image descriptions
            if ext.get("_doc_type") == "incident_photo":
                merged["image_descriptions"].append({
                    "filename": ext.get("_source_doc", ""),
                    "description": ext.get("description", ""),
                    "damage_visible": ext.get("damage_visible", False),
                })

            # Detect non-renewal from any document
            notes = str(ext.get("broker_notes", "")).lower()
            if any(kw in notes for kw in ["non-renew", "nonrenew", "will not renew", "non-renewal"]):
                # Mark in prior_insurance if we can find the carrier
                for pi in merged["prior_insurance"]:
                    if isinstance(pi, dict) and not pi.get("cancelled_by_carrier"):
                        pi["cancelled_by_carrier"] = True

        # ══════════════════════════════════════════════════
        # POST-PROCESSING: Auto-calculate missing summary totals
        # ══════════════════════════════════════════════════
        # If summary exists but key totals are missing/zero, calculate from loss_history records
        if merged["loss_history"] and merged["summary"]:
            summary = merged["summary"]
            
            # Calculate total_incurred if missing or zero
            if not summary.get("total_incurred") or summary.get("total_incurred") == 0:
                total_inc = sum(
                    (r.get("total_incurred") or (r.get("amount_paid", 0) + r.get("amount_reserved", 0)))
                    for r in merged["loss_history"]
                    if isinstance(r, dict)
                )
                if total_inc > 0:
                    summary["total_incurred"] = total_inc
            
            # Calculate total_paid if missing or zero
            if not summary.get("total_paid") or summary.get("total_paid") == 0:
                total_paid = sum(
                    r.get("amount_paid", 0)
                    for r in merged["loss_history"]
                    if isinstance(r, dict)
                )
                if total_paid > 0:
                    summary["total_paid"] = total_paid
            
            # Calculate total_claims if missing or zero
            if not summary.get("total_claims") or summary.get("total_claims") == 0:
                summary["total_claims"] = len(merged["loss_history"])
            
            # Calculate open_claims if missing
            if not summary.get("open_claims"):
                open_count = sum(
                    1 for r in merged["loss_history"]
                    if isinstance(r, dict) and r.get("status", "").lower() in ("open", "reserved")
                )
                if open_count > 0:
                    summary["open_claims"] = open_count

        # Log final merged state with comprehensive details
        log.info(
            "merge_complete",
            total_loss_history=len(merged["loss_history"]),
            total_prior_insurance=len(merged["prior_insurance"]),
            total_locations=len(merged["locations"]),
            total_coverages=len(merged["coverages"]),
            has_summary=bool(merged["summary"]),
            summary_total_premium=merged["summary"].get("total_premium") if merged["summary"] else None,
            summary_total_claims=merged["summary"].get("total_claims") if merged["summary"] else None,
            summary_total_incurred=merged["summary"].get("total_incurred") if merged["summary"] else None,
            merged_state=merged,
        )

        return merged

    def _build_extraction_result(self, raw: dict, doc_id: str) -> ExtractionResult:
        result = ExtractionResult()

        # Company
        if "company" in raw and isinstance(raw["company"], dict):
            try:
                result.company = CompanyInfo(**{
                    k: v for k, v in raw["company"].items()
                    if k in CompanyInfo.model_fields and v is not None
                })
            except Exception:
                pass

        # Locations
        for loc in raw.get("locations", []):
            if isinstance(loc, dict):
                try:
                    result.locations.append(LocationInfo(**{
                        k: v for k, v in loc.items() if k in LocationInfo.model_fields
                    }))
                except Exception:
                    pass

        # Loss history
        for loss in raw.get("loss_history", []):
            if isinstance(loss, dict):
                try:
                    result.loss_history.append(LossRecord(**{
                        k: v for k, v in loss.items() if k in LossRecord.model_fields
                    }))
                except Exception:
                    pass

        # Coverages
        for cov in raw.get("coverages", []):
            if isinstance(cov, dict):
                try:
                    result.coverages.append(CoverageRequest(**{
                        k: v for k, v in cov.items() if k in CoverageRequest.model_fields
                    }))
                except Exception:
                    pass

        # Prior insurance
        for pi in raw.get("prior_insurance", []):
            if isinstance(pi, dict):
                try:
                    result.prior_insurance.append(PriorInsurance(**{
                        k: v for k, v in pi.items() if k in PriorInsurance.model_fields
                    }))
                except Exception:
                    pass

        # Legal
        if "legal" in raw and isinstance(raw["legal"], dict):
            try:
                result.legal = LegalInfo(**{
                    k: v for k, v in raw["legal"].items() if k in LegalInfo.model_fields
                })
            except Exception:
                pass

        # Broker notes
        result.broker_notes = raw.get("broker_notes", "").strip()

        # Requested effective date
        result.requested_effective_date = raw.get("requested_effective_date", "")

        # STATED PREMIUM & INCURRED from loss run summary — AUTHORITATIVE
        summary = raw.get("summary", {})
        if isinstance(summary, dict):
            tp = summary.get("total_premium") or summary.get("total_premium_paid") or 0
            if isinstance(tp, (int, float)) and tp > 0:
                result.stated_total_premium = float(tp)
            ti = summary.get("total_incurred") or 0
            if isinstance(ti, (int, float)) and ti > 0:
                result.stated_total_incurred = float(ti)
        
        # FALLBACK: Calculate incurred from records if summary not provided
        if result.stated_total_incurred == 0.0 and result.loss_history:
            total_inc = sum(
                (r.incurred or (r.amount_paid + r.amount_reserved))
                for r in result.loss_history
                if r.incurred or (r.amount_paid + r.amount_reserved)
            )
            if total_inc > 0:
                result.stated_total_incurred = total_inc

        # Raw fields for audit
        result.raw_fields = [
            ExtractedField(field_name="full_extraction", value=raw, confidence=1.0, source_doc_id=doc_id)
        ]

        # Missing fields
        missing = []
        if not result.company.name:
            missing.append("company_name")
        if not result.company.address:
            missing.append("company_address")
        if not result.company.naics_code and not result.company.sic_code:
            missing.append("industry_classification")
        if not result.company.annual_revenue:
            missing.append("annual_revenue")
        if not result.coverages:
            missing.append("coverage_details")
        if not result.loss_history:
            missing.append("loss_history")
        if not result.prior_insurance:
            missing.append("prior_insurance")
        if not result.company.description:
            missing.append("business_description")
        result.missing_fields = missing

        return result

    def _merge_form_data(self, extraction: ExtractionResult, form_data: dict):
        company_fields = form_data.get("company", {})
        if isinstance(company_fields, dict):
            for k, v in company_fields.items():
                if v and hasattr(extraction.company, k) and not getattr(extraction.company, k, None):
                    setattr(extraction.company, k, v)

        if form_data.get("business_description"):
            extraction.business_description = str(form_data["business_description"])
            if not extraction.company.description:
                extraction.company.description = extraction.business_description

        if form_data.get("property_description"):
            extraction.property_description = str(form_data["property_description"])

        incidents = form_data.get("incidents", [])
        if isinstance(incidents, list):
            for inc in incidents:
                if isinstance(inc, dict):
                    try:
                        extraction.incidents.append(IncidentDetail(**{
                            k: v for k, v in inc.items() if k in IncidentDetail.model_fields
                        }))
                    except Exception:
                        pass

    async def run(self, state: PipelineState, files: dict) -> PipelineState:
        state.status = "extracting"
        state.current_step = "extraction"

        # Step 1: Parse all PDFs in parallel
        log.info("batch_extraction_starting", total_files=len(files))
        parsed = await self.parser.parse_batch(files)
        log.info("batch_parse_complete", pdfs_parsed=len(parsed))

        # Step 2: Build extraction tasks
        pdf_tasks = []
        image_tasks = []

        for doc in state.documents:
            if doc.filename not in files:
                continue
            if doc.filename in parsed:
                pdf_tasks.append(self._extract_single_pdf(doc.filename, parsed[doc.filename]))
            elif doc.file_type in ("png", "jpg", "jpeg", "tiff"):
                image_tasks.append(self._extract_single_image(doc.filename, files[doc.filename], doc.file_type))

        # Step 3: Run ALL extractions in parallel
        log.info("parallel_extraction_starting", pdf_count=len(pdf_tasks), image_count=len(image_tasks))
        all_results = await asyncio.gather(*pdf_tasks, *image_tasks, return_exceptions=True)

        all_raw = []
        for result in all_results:
            if isinstance(result, Exception):
                log.error("extraction_failed_parallel", error=str(result))
                state.errors.append(f"Extraction failed: {str(result)}")
            elif isinstance(result, dict):
                doc_type = result.get("_doc_type", "unknown")
                for doc in state.documents:
                    if doc.filename == result.get("_source_doc"):
                        try:
                            doc.doc_class = DocType(doc_type)
                        except ValueError:
                            doc.doc_class = DocType.UNKNOWN
                        doc.classification_confidence = result.get("_confidence", 0.0)
                all_raw.append(result)

        log.info("parallel_extraction_done", successful=len(all_raw), errors=len(state.errors))

        # Step 4: Merge
        combined = self._merge_extractions(all_raw)
        state.extraction = self._build_extraction_result(combined, state.submission_id)

        # Step 5: Merge form data
        if state.form_data:
            self._merge_form_data(state.extraction, state.form_data)

        # Step 6: Detect LOB
        if state.extraction.coverages:
            lob_str = (state.extraction.coverages[0].coverage_type or "").lower()
            lob_map = {
                "gl": "general_liability", "general liability": "general_liability",
                "property": "property", "wc": "workers_comp", "workers comp": "workers_comp",
                "auto": "commercial_auto", "umbrella": "umbrella", "cyber": "cyber",
                "bop": "business_owners_policy",
            }
            for key, val in lob_map.items():
                if key in lob_str:
                    state.lob = val
                    break
            if len(state.extraction.coverages) > 1:
                state.lob = "multi_line"

        log.info("extraction_complete",
            company=state.extraction.company.name,
            locations=len(state.extraction.locations),
            losses=len(state.extraction.loss_history),
            coverages=len(state.extraction.coverages),
            stated_premium=state.extraction.stated_total_premium,
            missing=state.extraction.missing_fields,
        )
        return state