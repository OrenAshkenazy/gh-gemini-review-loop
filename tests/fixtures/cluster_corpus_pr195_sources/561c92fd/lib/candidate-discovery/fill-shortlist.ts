import { evaluateKeyword, type KeywordMatch } from "../keywords/evaluate-keyword";
import { LEADERSHIP_TITLE_PATTERN } from "../../src/leadership-titles.js";
import type { EnrichmentProvider, EnrichedProfile } from "./enrichment-types";
import type { KeywordCriterion } from "../types";
import {
  evaluateTargetCompanyStages,
  type CompanyStage,
  type CompanyStageEvaluation,
  type CompanyStageTreatment,
} from "../../src/company-stage.js";
import {
  DEFAULT_SHORT_CURRENT_COMPANY_TENURE_MONTHS,
  buildCurrentCompanyTenureExclusionRecord,
  evaluateCurrentCompanyTenure,
  evaluateShortCurrentCompanyTenure,
  type CurrentCompanyTenureEvaluation,
  type CurrentCompanyTenureExclusionRecord,
  type ShortCurrentCompanyTenureEvaluation,
} from "../../src/current-company-tenure.js";
import {
  DEFAULT_MAX_EMPLOYMENT_GAP_TREATMENT,
  buildEmploymentGapExclusionRecord,
  evaluateEmploymentGap,
  type EmploymentGapEvaluation,
  type EmploymentGapExclusionRecord,
} from "../../src/employment-gap.js";
import {
  evaluateManagementExperience,
  evaluateProfessionalExperience,
  EXPERIENCE_NOT_MET_PENALTY,
  unverifiedRelevantExperiencePenalty,
  EXPERIENCE_OVER_CAP_PENALTY,
  NON_PROFESSIONAL_ROLE_PATTERNS,
  type ExperienceEvaluation,
} from "../../src/experience-evaluation.js";
import type { ExperienceCeilingTreatment } from "../types";
import { rankedTotal } from "./ranked-total";
import {
  evaluateCurrentEmploymentEvidence,
  type CurrentEmploymentEvidenceEvaluation,
} from "../../src/current-employment-evidence.js";
import {
  evaluateCompanyRecognition,
  type CompanyRecognitionEvaluation,
} from "../../src/company-recognition.js";
import { compareShortlistOrder, pendingRequiredCount, preferredStageRank } from "./shortlist-order";
import { isEnrichmentQuotaRefusal, sanitizeVendorMessage } from "../../src/enrichment/quota-refusal.js";
import { GATE_DECISION_VERSION, isCurrentTerminalGateFailure } from "../../src/candidate-discovery/gate-decision.js";
import {
  matchesOpenToWorkFilter,
  type OpenToWorkFilter,
} from "../../src/candidate-discovery/open-to-work-filter.js";

export { GATE_DECISION_VERSION };

// An experience evaluation with its enforcement stamped on: how the minimum
// and the value ceiling are each treated, and the summed ranking penalty a
// preferred-mode definite not_met and a rank_down material overshoot carry.
export type TreatedExperienceEvaluation = ExperienceEvaluation & {
  treatment: "required" | "preferred";
  ceilingTreatment: ExperienceCeilingTreatment;
  penaltyPoints: number;
};

export interface RankedRef {
  id: string;
  normalizedUrl: string;
  title?: string;
  snippet?: string;
  strictCriteriaEligible?: boolean;
  // Stamped when an enriched evaluation definitely failed a hard gate. The
  // candidate stays in reserve as the audited failure it is, but a later
  // enrichment pass (enrich-more resubmits the whole reserve) skips it without
  // spending budget — otherwise a failed prefix as long as maxEnrichments
  // would be re-evaluated on every click and the window would never reach the
  // unprocessed tail. Terminal is per-criteria: editing criteria starts a new
  // discovery run, which rebuilds the reserve without markers.
  terminalGateFailure?: boolean;
  // The gate semantics version that stamped terminalGateFailure. A marker
  // whose version does not match the current GATE_DECISION_VERSION is stale
  // and falls through to re-evaluation instead of being trusted.
  gateDecisionVersion?: number;
  // Transient provenance: stamped "snippet_only" on the tail a quota refusal
  // cut off, and cleared the moment a candidate actually reaches the vendor so
  // a later successful pass cannot leave a stale marker behind.
  enrichmentStatus?: "enriched" | "partial" | "snippet_only";
  // Carried from pre-enrichment ranking; the preference-pool selection ranks by
  // rankedTotal (base score + corroboration − penalties) so the visible slice
  // matches enrich-and-rank's final order.
  score?: { total?: number };
  corroborationScore?: number;
  // Availability from the enriched profile; rankedTotal boosts it (#182).
  openToWork?: boolean;
}

export interface CompanyStageCriterion {
  targetStages: CompanyStage[];
  treatment: CompanyStageTreatment;
}

export interface VisibleEnriched<T extends RankedRef> {
  candidate: T;
  enrichedProfile: EnrichedProfile;
  keywordMatches: KeywordMatch[];
  // True when every required criterion is evidenced. With snippet-level
  // enrichment a candidate may be visible with requiredPassed=false; the
  // unconfirmed criteria are listed in pendingRequiredVerification.
  requiredPassed: boolean;
  pendingRequiredVerification?: string[];
  companyStageEvaluation?: CompanyStageEvaluation;
  currentCompanyTenureEvaluation?: CurrentCompanyTenureEvaluation;
  shortCurrentCompanyTenureEvaluation?: ShortCurrentCompanyTenureEvaluation;
  employmentGapEvaluation?: EmploymentGapEvaluation;
  professionalExperienceEvaluation?: TreatedExperienceEvaluation;
  managementExperienceEvaluation?: TreatedExperienceEvaluation;
  currentEmploymentEvidenceEvaluation?: CurrentEmploymentEvidenceEvaluation;
  companyRecognitionEvaluation?: CompanyRecognitionEvaluation;
}

export interface QuotaRefusal {
  provider: string;
  vendorMessage: string;
  /** Vendor-side correlation: which Apify actor/run refused (when known). */
  actorId?: string;
  runId?: string;
  // Positions worked through before the stop, not profiles fetched: the loop
  // also advances past pre-enrichment-ineligible and terminally-failed
  // candidates without calling the vendor.
  processedCount: number;
  unevaluatedCount: number;
  inputCount: number;
  /** Successful enrichments (vendor or cache) before the stop — 0 means the
   * refusal left the run with no profile evidence at all. */
  enrichedCount: number;
}

export interface FillResult<T extends RankedRef> {
  visible: VisibleEnriched<T>[];
  reserve: Array<T & {
    companyStageEvaluation?: CompanyStageEvaluation;
    currentCompanyTenureEvaluation?: CurrentCompanyTenureEvaluation;
    employmentGapEvaluation?: EmploymentGapEvaluation;
    professionalExperienceEvaluation?: TreatedExperienceEvaluation;
    managementExperienceEvaluation?: TreatedExperienceEvaluation;
    currentEmploymentEvidenceEvaluation?: CurrentEmploymentEvidenceEvaluation;
    companyRecognitionEvaluation?: CompanyRecognitionEvaluation;
  }>;
  enrichmentCount: number;
  exclusionAudit: Array<CurrentCompanyTenureExclusionRecord | EmploymentGapExclusionRecord>;
  quotaRefusal?: QuotaRefusal;
}

export async function fillShortlist<T extends RankedRef>(args: {
  ranked: T[];
  provider: EnrichmentProvider;
  keywords: KeywordCriterion[];
  recencyYears: number;
  visibleTarget?: number;
  maxEnrichments?: number;
  openToWorkFilter?: OpenToWorkFilter;
  minRequiredMatches?: number;
  companyStageCriterion?: CompanyStageCriterion;
  currentCompanyTenureCriterion?: { maxYears: number; treatment?: "required" | "preferred" };
  minimumCurrentCompanyTenureMonths?: number | null;
  maxEmploymentGapMonths?: number | null;
  maxEmploymentGapTreatment?: "required" | "preferred";
  minimumProfessionalExperienceYears?: number | null;
  minimumProfessionalExperienceTreatment?: "required" | "preferred";
  minimumManagementExperienceYears?: number | null;
  minimumManagementExperienceTreatment?: "required" | "preferred";
  maxValuedProfessionalExperienceYears?: number | null;
  maxValuedProfessionalExperienceTreatment?: ExperienceCeilingTreatment;
  maxValuedManagementExperienceYears?: number | null;
  maxValuedManagementExperienceTreatment?: ExperienceCeilingTreatment;
  experienceAsOfDate?: string;
  hiringCompany?: string;
}): Promise<FillResult<T>> {
  const {
    ranked,
    provider,
    keywords,
    recencyYears,
    visibleTarget = 5,
    maxEnrichments = 15,
    openToWorkFilter = "all",
    minRequiredMatches,
    companyStageCriterion,
    currentCompanyTenureCriterion,
    minimumCurrentCompanyTenureMonths = DEFAULT_SHORT_CURRENT_COMPANY_TENURE_MONTHS,
    maxEmploymentGapMonths = null,
    maxEmploymentGapTreatment = DEFAULT_MAX_EMPLOYMENT_GAP_TREATMENT,
    minimumProfessionalExperienceYears = null,
    minimumProfessionalExperienceTreatment = "required",
    minimumManagementExperienceYears = null,
    minimumManagementExperienceTreatment = "required",
    maxValuedProfessionalExperienceYears = null,
    maxValuedProfessionalExperienceTreatment = "clamp_only",
    maxValuedManagementExperienceYears = null,
    maxValuedManagementExperienceTreatment = "clamp_only",
    experienceAsOfDate,
    hiringCompany,
  } = args;
  const eligible: VisibleEnriched<T>[] = [];
  // Snippet-level providers synthesize profiles from ~160 chars of SERP text: a
  // missing skill term there is not evidence the candidate lacks the skill, so
  // required skill criteria rank and flag instead of gating (role titles, which
  // SERP titles do prove, still gate). See passesRequiredGate.
  const snippetEvidence = provider.evidenceLevel === "snippet";
  // The approved role family for experience relevance is the required role-title
  // criterion (its term plus anyOf members) — the same terms the role-title gate
  // matches. Absent one, experience falls back to counting all non-denylisted
  // roles.
  const roleTitleCriterion = keywords.find((k) => k.requiredRoleTitle);
  const relevantRoleTitleTerms = roleTitleCriterion
    ? [roleTitleCriterion.term, ...(roleTitleCriterion.anyOf ?? [])]
    : [];
  // Candidates that were enriched but did not pass the required-keyword gate are
  // held in reserve rather than dropped, so a strict gate can't silently zero
  // out a shortlist.
  const gateFailed: Array<T & {
    companyStageEvaluation?: CompanyStageEvaluation;
    currentCompanyTenureEvaluation?: CurrentCompanyTenureEvaluation;
  }> = [];
  const exclusionAudit: Array<
    CurrentCompanyTenureExclusionRecord | EmploymentGapExclusionRecord
  > = [];
  let enrichmentCount = 0;
  let enrichedProfileCount = 0;
  // Confirmed passes (no adjacent, provisional role-title match) toward the
  // visible target. A candidate with no role-title criterion at all counts as
  // confirmed. Pending/adjacent passes fill `eligible` but do not by
  // themselves justify stopping the loop, or an early provisional match could
  // fill the target and starve a later confirmed match of evaluation.
  let confirmedCount = 0;
  let refusal:
    | Pick<QuotaRefusal, "provider" | "vendorMessage" | "actorId" | "runId">
    | undefined;
  // Candidates the quota refusal skipped. They are kept apart from `gateFailed`
  // because no gate ever ran on them.
  const quotaSkipped: T[] = [];
  let i = 0;
  // Any preferred ranking-only criterion — company stage, tenure cap, or an
  // experience minimum — keeps a shortfall candidate eligible and sinks it in
  // ranking instead of reserving it. The visible slice must therefore be drawn
  // from the whole evaluated pool, not truncated at the first `visibleTarget`
  // eligible candidates, or a penalized early candidate would hold a slot a
  // later unpenalized (higher adjusted-score) candidate deserves.
  const mustEvaluateBeyondVisibleTarget =
    // Full-profile enrichment now contributes always-on ranking signals
    // (current-employment evidence, company recognition, and availability).
    // They can reorder an otherwise criterion-free shortlist, so stopping at
    // the first visible prefix would make the result depend on input order.
    openToWorkFilter !== "all" ||
    !snippetEvidence ||
    companyStageCriterion?.treatment === "preferred" ||
    currentCompanyTenureCriterion?.treatment === "preferred" ||
    (maxEmploymentGapMonths != null && maxEmploymentGapTreatment === "preferred") ||
    (minimumProfessionalExperienceYears != null &&
      minimumProfessionalExperienceTreatment === "preferred") ||
    (minimumManagementExperienceYears != null &&
      minimumManagementExperienceTreatment === "preferred") ||
    // A rank_down ceiling is ranking-only too: the materially over-cap
    // candidate must be displaceable by an unpenalized later one.
    (maxValuedProfessionalExperienceYears != null &&
      maxValuedProfessionalExperienceTreatment === "rank_down") ||
    (maxValuedManagementExperienceYears != null &&
      maxValuedManagementExperienceTreatment === "rank_down") ||
    // ADR 0002: required skill criteria rank instead of gating, so a
    // supported later candidate must be able to displace an unverified
    // earlier one — which requires evaluating the whole pool.
    keywords.some((keyword) => keyword.required && !keyword.requiredRoleTitle);

  for (; i < ranked.length; i++) {
    if (!mustEvaluateBeyondVisibleTarget && confirmedCount >= visibleTarget) break;
    if (enrichmentCount >= maxEnrichments) break;

    let candidate = ranked[i];
    if (candidate.strictCriteriaEligible === false) {
      gateFailed.push(candidate);
      continue;
    }
    // Already terminally evaluated in a previous pass over this reserve: keep
    // the audited failure without re-enriching, so the budget reaches
    // candidates that still have an open outcome. The check is version-aware:
    // a marker stamped by an older gate is stale and falls through to
    // re-evaluation instead of being trusted (see GATE_DECISION_VERSION).
    if (isCurrentTerminalGateFailure(candidate)) {
      gateFailed.push(candidate);
      continue;
    }
    if (candidate.terminalGateFailure) {
      candidate = clearGateDecisionMarker(candidate);
    }
    let profile: EnrichedProfile | null;
    try {
      profile = await provider.enrich({
        normalizedUrl: candidate.normalizedUrl,
        title: candidate.title,
        snippet: candidate.snippet,
      });
    } catch (error) {
      // A quota refusal is not this candidate's verdict, it is the vendor
      // budget running out. `isEnrichmentQuotaRefusal` is declared as a type
      // predicate, so this guard both filters out non-quota errors and narrows
      // `error` for the reads below.
      if (!isEnrichmentQuotaRefusal(error)) throw error;
      // The caching provider serves a cache hit before it rethrows the
      // remembered refusal, so a later candidate may still be enrichable with
      // no vendor call at all. Skip only this candidate and keep scanning:
      // breaking here would mislabel already-cached profiles as unprocessed and
      // hand back an undersized shortlist while their evidence sat in the cache.
      if (!refusal) {
        const ids = error as { actorId?: unknown; runId?: unknown };
        refusal = {
          provider: error.provider,
          vendorMessage: sanitizeVendorMessage(error.vendorMessage),
          ...(typeof ids.actorId === "string" ? { actorId: ids.actorId } : {}),
          ...(typeof ids.runId === "string" ? { runId: ids.runId } : {}),
        };
      }
      quotaSkipped.push(candidate);
      continue;
    }
    enrichmentCount++;
    // enrichmentCount is the BUDGET counter: a null (unfetchable) profile was
    // still a billed attempt. Profile evidence obtained is tracked separately —
    // quota-refusal severity must not read attempts as successes.
    if (profile) enrichedProfileCount++;
    // This candidate reached the vendor, so a marker inherited from an earlier
    // quota-stopped pass is now stale.
    candidate = clearQuotaMarker(candidate);
    if (!profile) {
      const currentCompanyTenureEvaluation = currentCompanyTenureCriterion
        ? evaluateCurrentCompanyTenure({
            positions: [],
            maxYears: currentCompanyTenureCriterion.maxYears,
            treatment: currentCompanyTenureCriterion.treatment,
          })
        : undefined;
      gateFailed.push(currentCompanyTenureEvaluation
        ? { ...candidate, currentCompanyTenureEvaluation }
        : candidate);
      continue;
    }

    const companyStageEvaluation = companyStageCriterion
      ? evaluateTargetCompanyStages({
          positions: profile.positions,
          targetStages: companyStageCriterion.targetStages,
          treatment: companyStageCriterion.treatment,
        })
      : undefined;
    const currentCompanyTenureEvaluation = currentCompanyTenureCriterion
      ? evaluateCurrentCompanyTenure({
          positions: profile.positions,
          maxYears: currentCompanyTenureCriterion.maxYears,
          treatment: currentCompanyTenureCriterion.treatment,
        })
      : undefined;
    const shortCurrentCompanyTenureEvaluation = evaluateShortCurrentCompanyTenure({
      positions: profile.positions,
      minMonths: minimumCurrentCompanyTenureMonths,
    });
    // Issue #178: a current role at freelance/self-employment or with no
    // company name is legitimate employment nothing external can corroborate —
    // a ranking demotion, never a gate. Needs real profile evidence; a
    // snippet-synthesized profile fabricates its employer.
    const currentEmploymentEvidenceEvaluation = !snippetEvidence
      ? evaluateCurrentEmploymentEvidence({ positions: profile.positions })
      : undefined;
    // Issue #179: a multi-employer history resolving to zero recognized
    // companies (top1000 catalog) demotes and flags for verification — never
    // gates. Snippet-synthesized histories prove nothing about employers.
    const companyRecognitionEvaluation = !snippetEvidence
      ? evaluateCompanyRecognition({ positions: profile.positions })
      : undefined;
    // The gap is measured from dated history, so it needs the same real
    // profile evidence the experience minimums do: a snippet-synthesized
    // profile proves nothing about when a role ended.
    const employmentGapEvaluation =
      !snippetEvidence && maxEmploymentGapMonths != null && experienceAsOfDate
        ? evaluateEmploymentGap({
            positions: profile.positions,
            maxGapMonths: maxEmploymentGapMonths,
            asOfDate: experienceAsOfDate,
            openToWork: profile.openToWork === true,
            treatment: maxEmploymentGapTreatment,
          })
        : undefined;
    // Experience gating needs real dated history: a profile-level provider
    // returns the full experience section, so coverage is complete and a
    // definite not_met is trustworthy. Snippet-level profiles prove nothing
    // about durations and are never gated on experience.
    const baseProfessionalExperienceEvaluation =
      !snippetEvidence &&
      (minimumProfessionalExperienceYears != null ||
        maxValuedProfessionalExperienceYears != null) &&
      experienceAsOfDate
        ? evaluateProfessionalExperience({
            positions: profile.positions,
            minimumYears: minimumProfessionalExperienceYears,
            // Value ceiling (issue #157): years past it stop counting, so a
            // profile at the ceiling and one far past it rank the same.
            maxValuedYears: maxValuedProfessionalExperienceYears,
            historyCoverage: "complete",
            asOfDate: experienceAsOfDate,
            // Help-desk/support/intern roles are not the professional
            // engineering experience the threshold measures; excluding them
            // stops career-changer padding from clearing the bar.
            excludedRoleTitlePatterns: NON_PROFESSIONAL_ROLE_PATTERNS,
            // A dated role counts toward the years bar only if its title is in
            // the approved role family; unrelated roles (Product Manager,
            // Recruiter, unrelated engineering) cannot manufacture a "met".
            relevantRoleTitleTerms,
          })
        : undefined;
    const professionalExperienceEvaluation = withExperienceTreatment(
      baseProfessionalExperienceEvaluation,
      minimumProfessionalExperienceTreatment,
      maxValuedProfessionalExperienceTreatment,
    );
    // Management years count only in people-management titled roles (Team Lead
    // yes, Tech Lead no — issue #136); same profile-evidence-only rule as the
    // professional gate. The approved role family scopes broad seniority
    // titles (Head of, VP, Director) to the discipline the minimum measures.
    const managementExperienceEvaluation = withExperienceTreatment(
      !snippetEvidence &&
      (minimumManagementExperienceYears != null ||
        maxValuedManagementExperienceYears != null) &&
      experienceAsOfDate
        ? evaluateManagementExperience({
            positions: profile.positions,
            minimumYears: minimumManagementExperienceYears,
            maxValuedYears: maxValuedManagementExperienceYears,
            historyCoverage: "complete",
            asOfDate: experienceAsOfDate,
            relevantRoleTitleTerms,
          })
        : undefined,
      minimumManagementExperienceTreatment,
      maxValuedManagementExperienceTreatment,
    );
    const availabilityStampedCandidate = stampLatestOpenToWork(candidate, profile.openToWork);
    const evaluatedCandidate = companyStageEvaluation || currentCompanyTenureEvaluation || shortCurrentCompanyTenureEvaluation || employmentGapEvaluation || professionalExperienceEvaluation || managementExperienceEvaluation || currentEmploymentEvidenceEvaluation || companyRecognitionEvaluation || profile.openToWork === true
      ? {
          ...availabilityStampedCandidate,
          ...(companyStageEvaluation ? { companyStageEvaluation } : {}),
          ...(currentCompanyTenureEvaluation ? { currentCompanyTenureEvaluation } : {}),
          ...(employmentGapEvaluation ? { employmentGapEvaluation } : {}),
          ...(professionalExperienceEvaluation ? { professionalExperienceEvaluation } : {}),
          ...(managementExperienceEvaluation ? { managementExperienceEvaluation } : {}),
          ...(currentEmploymentEvidenceEvaluation ? { currentEmploymentEvidenceEvaluation } : {}),
          ...(companyRecognitionEvaluation ? { companyRecognitionEvaluation } : {}),
          // Availability travels with the candidate: rankedTotal boosts it
          // (#182) and the UI badges it.
          shortCurrentCompanyTenureEvaluation,
        }
      : availabilityStampedCandidate;
    if (hasCurrentHiringCompany(profile.positions, hiringCompany)) continue;
    if (
      companyStageEvaluation?.treatment === "required" &&
      companyStageEvaluation.outcome !== "match"
    ) {
      continue;
    }
    // Required treatment: a verified over-limit tenure is an audited exclusion.
    // Preferred treatment keeps the candidate eligible (outcome stays
    // "excluded" as the verified fact) and rankedTotal applies penaltyPoints.
    if (currentCompanyTenureEvaluation && !currentCompanyTenureEvaluation.eligible) {
      exclusionAudit.push(
        buildCurrentCompanyTenureExclusionRecord(candidate.id, currentCompanyTenureEvaluation),
      );
      continue;
    }
    // Required treatment: a measured gap past the maximum is an audited
    // exclusion. Preferred keeps the candidate eligible and rankedTotal applies
    // penaltyPoints — an unknown or unmeasurable gap never reaches either.
    if (employmentGapEvaluation && !employmentGapEvaluation.eligible) {
      exclusionAudit.push(
        buildEmploymentGapExclusionRecord(candidate.id, employmentGapEvaluation),
      );
      continue;
    }
    // A verified engagement younger than the recruiter's minimum is a hard
    // criterion (short-tenure rule v2), not a ranking nudge: a penalty alone
    // keeps the candidate visible whenever shown < visibleTarget. Definite
    // "short" goes to reserve; unknown never gates.
    if (shortCurrentCompanyTenureEvaluation?.outcome === "short") {
      gateFailed.push({ ...evaluatedCandidate, terminalGateFailure: true, gateDecisionVersion: GATE_DECISION_VERSION });
      continue;
    }
    const matches = keywords.map((k) => evaluateKeyword(k, profile, recencyYears));
    // ADR 0002: the skill gate is always skipped — a profile skills section is
    // a capped inventory, not an exhaustive one, so absence is not disproof
    // even from a complete-profile provider. Unsatisfied required skills rank
    // and surface as pendingRequiredVerification. Only the role-title
    // criterion gates here, and that failure is a structural contradiction —
    // full-profile title evidence — so it stays terminal, stamped with the
    // gate version that decided it.
    if (!passesRequiredGate(matches, minRequiredMatches, { skipSkillGate: true })) {
      gateFailed.push({ ...evaluatedCandidate, terminalGateFailure: true, gateDecisionVersion: GATE_DECISION_VERSION });
      continue;
    }
    if (companyStageEvaluation && !companyStageEvaluation.eligible) {
      gateFailed.push({ ...evaluatedCandidate, terminalGateFailure: true, gateDecisionVersion: GATE_DECISION_VERSION });
      continue;
    }
    // PRD section 10: a definite not_met is an audited exclusion held in
    // reserve; met passes; unknown is never a hard rejection.
    if (
      professionalExperienceEvaluation?.outcome === "not_met" &&
      professionalExperienceEvaluation.treatment !== "preferred"
    ) {
      gateFailed.push({ ...evaluatedCandidate, terminalGateFailure: true, gateDecisionVersion: GATE_DECISION_VERSION });
      continue;
    }
    if (
      managementExperienceEvaluation?.outcome === "not_met" &&
      managementExperienceEvaluation.treatment !== "preferred"
    ) {
      gateFailed.push({ ...evaluatedCandidate, terminalGateFailure: true, gateDecisionVersion: GATE_DECISION_VERSION });
      continue;
    }

    // Two disjoint sources, unioned: unverifiedRequiredCriteria covers required
    // NON-title criteria (ADR 0002 — they rank instead of gating, so they are
    // always pending, not only on incomplete skill evidence), and
    // adjacentRoleTitleTerm covers the required ROLE-TITLE criterion when the
    // classifier returned "adjacent". Their filters are mutually exclusive
    // (!requiredRoleTitle vs requiredRoleTitle), so no term can appear twice.
    const roleTerm = adjacentRoleTitleTerm(matches);
    const pendingRequiredVerification = [
      ...unverifiedRequiredCriteria(matches),
      ...(roleTerm ? [roleTerm] : []),
    ];
    eligible.push({
      candidate: evaluatedCandidate,
      enrichedProfile: profile,
      keywordMatches: matches,
      requiredPassed: pendingRequiredVerification.length === 0,
      ...(pendingRequiredVerification.length > 0 ? { pendingRequiredVerification } : {}),
      ...(companyStageEvaluation ? { companyStageEvaluation } : {}),
      ...(currentCompanyTenureEvaluation ? { currentCompanyTenureEvaluation } : {}),
      ...(employmentGapEvaluation ? { employmentGapEvaluation } : {}),
      ...(professionalExperienceEvaluation ? { professionalExperienceEvaluation } : {}),
      ...(managementExperienceEvaluation ? { managementExperienceEvaluation } : {}),
      ...(currentEmploymentEvidenceEvaluation ? { currentEmploymentEvidenceEvaluation } : {}),
      ...(companyRecognitionEvaluation ? { companyRecognitionEvaluation } : {}),
      shortCurrentCompanyTenureEvaluation,
    });
    // A candidate with no role-title criterion has no adjacent term either,
    // so it counts as confirmed too.
    if (!roleTerm) confirmedCount++;
  }

  // The confirmed-first guarantee must hold in every mode: whenever a pending
  // candidate is in the pool, or preference-pool evaluation ran (which can
  // leave a penalized confirmed candidate ahead of an unpenalized one),
  // re-sort so pending never displaces a confirmed candidate from the visible
  // slice.
  const prioritized =
    mustEvaluateBeyondVisibleTarget ||
    eligible.some((v) => (v.pendingRequiredVerification?.length ?? 0) > 0) ||
    // The always-on weak-current-employment penalty (#178) must be able to
    // reorder the evaluated pool even when no configured criterion forced
    // whole-pool evaluation.
    eligible.some((v) => (v.currentEmploymentEvidenceEvaluation?.penaltyPoints ?? 0) > 0) ||
    eligible.some((v) => (v.companyRecognitionEvaluation?.penaltyPoints ?? 0) > 0) ||
    // The open-to-work boost (#182) reorders too.
    eligible.some((v) => v.candidate.openToWork === true)
      ? prioritizePreferencePool(eligible)
      : eligible;
  const visible = prioritized
    .filter((item) => matchesOpenToWorkFilter(item.candidate, openToWorkFilter))
    .slice(0, visibleTarget);
  const visibleIds = new Set(visible.map((item) => item.candidate.id));
  const eligibleOverflow = prioritized
    .filter((item) => !visibleIds.has(item.candidate.id))
    .map((item) => item.candidate);
  // Preferred changes ordering, never eligibility. Passing candidates outside
  // the visible target remain auditable in reserve.
  const unprocessed = refusal
    ? ranked.slice(i).map((candidate) => ({ ...candidate, enrichmentStatus: "snippet_only" as const }))
    : ranked.slice(i);
  // Everything the loop never determined an outcome for: the candidates a
  // refusal skipped, plus any tail left when the loop stopped early. Counting
  // both keeps processedCount + unevaluatedCount === inputCount.
  const unevaluatedCount = quotaSkipped.length + unprocessed.length;
  const quotaRefusal: QuotaRefusal | undefined = refusal
    ? {
        ...refusal,
        processedCount: ranked.length - unevaluatedCount,
        unevaluatedCount,
        inputCount: ranked.length,
        // processedCount includes candidates the gates settled without a
        // vendor call, and enrichmentCount includes billed attempts that
        // returned no profile. Severity keys on profiles actually obtained.
        enrichedCount: enrichedProfileCount,
      }
    : undefined;
  const reserve = [
    ...gateFailed,
    ...eligibleOverflow,
    ...quotaSkipped.map((candidate) => ({ ...candidate, enrichmentStatus: "snippet_only" as const })),
    ...unprocessed,
  ];
  return {
    visible, reserve, enrichmentCount, exclusionAudit,
    ...(quotaRefusal ? { quotaRefusal } : {}),
  };
}

// Only the transient quota marker is cleared; "enriched" and "partial" are
// real provenance and stay.
function clearQuotaMarker<T extends RankedRef>(candidate: T): T {
  if (candidate.enrichmentStatus !== "snippet_only") return candidate;
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const { enrichmentStatus: _dropped, ...rest } = candidate;
  return rest as T;
}

// A stale-version terminalGateFailure marker is being re-evaluated from
// scratch, so it (and the version it was stamped with) must not survive onto
// the re-evaluated copy — otherwise a candidate that now passes would still
// carry a marker claiming it terminally failed.
function clearGateDecisionMarker<T extends RankedRef>(candidate: T): T {
  if (!candidate.terminalGateFailure) return candidate;
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const { terminalGateFailure: _t, gateDecisionVersion: _v, ...rest } = candidate;
  return rest as T;
}

// Availability is refreshed profile state, not an accumulated signal. Remove
// an older true value when the latest enrichment no longer reports the banner.
function stampLatestOpenToWork<T extends RankedRef>(candidate: T, openToWork?: boolean): T {
  const withoutAvailability = { ...candidate };
  delete withoutAvailability.openToWork;
  return (openToWork === true
    ? { ...withoutAvailability, openToWork: true }
    : withoutAvailability) as T;
}

function hasCurrentHiringCompany(
  positions: EnrichedProfile["positions"],
  hiringCompany?: string,
): boolean {
  const normalizedHiringCompany = normalizeCompanyName(hiringCompany);
  if (!normalizedHiringCompany) return false;
  return positions.some(
    (position) =>
      position.endDate == null &&
      normalizeCompanyName(position.company) === normalizedHiringCompany,
  );
}

function normalizeCompanyName(value?: string | null): string {
  return String(value ?? "").trim().toLocaleLowerCase();
}

// Order the evaluated pool the way the visible list will ultimately be ranked
// (shortlist-order holds the shared keys), so the top `visibleTarget` slice is
// the strongest candidates: confirmed (no pending required verification) first
// — a pending/adjacent provisional pass must never displace a confirmed
// candidate — then a preferred company-stage match, then higher adjusted
// score, original order breaking ties. This is what lets an unpenalized later
// candidate displace a penalized earlier one from the visible slice.
function prioritizePreferencePool<T extends RankedRef>(
  candidates: VisibleEnriched<T>[],
): VisibleEnriched<T>[] {
  return candidates
    .map((entry, index) => ({
      entry,
      key: {
        pendingRequiredCount: pendingRequiredCount(entry),
        preferredStageRank: preferredStageRank(entry.companyStageEvaluation),
        score: rankedTotal(entry.candidate),
        index,
      },
    }))
    .sort((left, right) => compareShortlistOrder(left.key, right.key))
    .map(({ entry }) => entry);
}

// A mandatory role-title criterion must pass independently. The remaining
// required criteria use the configured threshold, so operational-role evidence
// never substitutes for the required security depth.
//
// With options.skipSkillGate only the role-title criterion gates. This holds
// whenever skill evidence is incomplete: snippet-level providers (a ~160-char
// SERP snippet omitting "IAM" is not evidence the candidate lacks IAM) and
// profile providers with an incomplete skills section (apimaestro). Threshold
// criteria then rank and surface as pendingRequiredVerification instead of
// rejecting, until a provider with complete skill coverage supplies real
// evidence. The role-title criterion still gates in every case.
export function passesRequiredGate(
  matches: KeywordMatch[],
  minRequiredMatches?: number,
  { skipSkillGate = false }: { skipSkillGate?: boolean } = {},
): boolean {
  const required = matches.filter((m) => m.required);
  const roleTitleMatch = required.find((m) => m.requiredRoleTitle);
  if (roleTitleMatch && !roleTitleMatch.satisfied && roleTitleMatch.roleTitleVerdict !== "adjacent") return false;
  if (
    roleTitleMatch && !roleTitleMatch.allowLeadershipTitle &&
    (hasLeadershipRoleEvidence(roleTitleMatch.evidence) || roleTitleMatch.currentTitleIsLeadership === true)
  ) return false;
  if (skipSkillGate) return true;
  // The development-language gate is a floor, not a vote. Left in the ordinary
  // N-of-M threshold it is bypassable: a brief with three required groups sets
  // minRequiredMatches to 2, so a candidate could satisfy two tooling criteria,
  // miss the language entirely, and still pass — which is precisely the outcome
  // the gate exists to prevent. It sits after the skipSkillGate escape hatch on
  // purpose: a provider with an incomplete skills section still yields pending
  // verification rather than a rejection.
  const developmentGate = required.find((m) => m.developmentLanguageGate);
  if (developmentGate && !developmentGate.satisfied) return false;
  const thresholdCriteria = required.filter((m) => !m.requiredRoleTitle);
  const satisfied = thresholdCriteria.filter((m) => m.satisfied).length;
  const threshold = minRequiredMatches != null
    ? Math.min(minRequiredMatches, thresholdCriteria.length)
    : thresholdCriteria.length;
  return satisfied >= threshold;
}

// Required non-role criteria the snippet could not confirm; carried on the
// shortlist entry so the head hunter verifies them on the full profile.
export function unverifiedRequiredCriteria(matches: KeywordMatch[]): string[] {
  return matches
    .filter((m) => m.required && !m.requiredRoleTitle && !m.satisfied)
    .map((m) => m.term);
}

// The required role-title criterion's term when its verdict is a provisional
// "adjacent" pass (compatible-but-not-identical discipline) rather than a
// confirmed "match" — carried on the shortlist entry alongside
// unverifiedRequiredCriteria so the head hunter verifies the title on the
// full profile. A "miss" never reaches here: passesRequiredGate rejects it
// before pending verification is computed.
export function adjacentRoleTitleTerm(matches: KeywordMatch[]): string | null {
  const m = matches.find((x) => x.required && x.requiredRoleTitle && !x.satisfied && x.roleTitleVerdict === "adjacent");
  return m ? m.term : null;
}

function hasLeadershipRoleEvidence(evidence: string | null): boolean {
  const titlePart = (evidence ?? "").split(/\s+(?:@|·)\s+/)[0];
  return LEADERSHIP_TITLE_PATTERN.test(titlePart);
}

// Enforcement-layer augmentation (issue #135 follow-up): the evaluators stay
// pure versioned rules; treatment and the preferred-mode penalty are stamped
// here, where enforcement policy lives.
// The two shortfalls are independent and both sink in ranking, so they sum: a
// candidate can be below the minimum AND materially past the ceiling only when
// the recruiter set contradictory bounds, but nothing here needs to assume they
// didn't. Neither ceiling mode ever excludes — that is the whole reason the
// ceiling does not use the required/preferred vocabulary.
function withExperienceTreatment(
  evaluation: ExperienceEvaluation | undefined,
  treatment: "required" | "preferred",
  ceilingTreatment: ExperienceCeilingTreatment,
): TreatedExperienceEvaluation | undefined {
  if (!evaluation) return undefined;
  const minimumShortfallPenalty =
    treatment === "preferred" && evaluation.outcome === "not_met" ? EXPERIENCE_NOT_MET_PENALTY : 0;
  const overCeilingPenalty =
    ceilingTreatment === "rank_down" && evaluation.capOutcome === "materially_over_cap"
      ? EXPERIENCE_OVER_CAP_PENALTY
      : 0;
  // Issue #176: unknown-with-unestablished-relevance is weak evidence, not
  // neutral. Scaled by the proven shortfall so a candidate with 4 relevant
  // years against a 6-year bar sinks less than one with zero. Applies under
  // both treatments — unknown never gates, so ranking is the only lever.
  const unverifiedRelevancePenalty = unverifiedRelevantExperiencePenalty(evaluation);
  return {
    ...evaluation,
    treatment,
    ceilingTreatment,
    penaltyPoints: minimumShortfallPenalty + overCeilingPenalty + unverifiedRelevancePenalty,
  };
}
