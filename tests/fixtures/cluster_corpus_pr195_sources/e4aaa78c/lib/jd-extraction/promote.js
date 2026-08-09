import { isTechnicalListSpan } from "./facts/entities.js";
import { locatorInPreferredClause, splitClauses } from "./advantage-cue.js";
import { partitionByFamily } from "./tech-families.js";

const MODAL_CUE_PATTERN = /\b(must have|must|required|requires|mandatory)\b/i;

// A clause phrased as alternatives ("Node.js, Java, or similar", "such as X, Y",
// "one of the following") states ONE need satisfiable by any listed member;
// promoting each member as an independent requirement multiplies that one need
// into N hard requirements. A per-clause group key lets search policy collapse
// the siblings into a single any-of criterion.
//
// A cue governs only its own syntactic list, not the whole clause: in
// "Proficiency with querying (e.g., SQL) and comfort across distributed
// systems", the e.g. governs SQL alone — sweeping the clause would fold the
// independent distributed-systems requirement into the SQL alternatives.
// Leading cues govern the term run that FOLLOWS them; trailing cues ("or
// similar") govern the run that PRECEDES them.
const LEADING_ALTERNATIVES_CUE_PATTERN = new RegExp(
  [
    String.raw`\bone or more of\b`,
    String.raw`\bone of\b`,
    String.raw`\bsuch as\b`,
    String.raw`\be\.g\b`,
  ].join("|"),
  "gi",
);
const TRAILING_ALTERNATIVES_CUE_PATTERN = new RegExp(
  String.raw`\bor (?:a |an )?(?:similar|comparable|equivalent)\b`,
  "i",
);

// Assign a clause-scoped alternatives group to every promoted term whose clause
// states alternatives. A clause qualifies when a cue frames it (above) or when
// two or more of its own technical terms are joined by a bare disjunctive "or"
// ("React or Vue is required"). Membership is the terms of THAT clause only, so a
// sibling requirement in another clause of the same span ("...; Kubernetes is
// required") never joins the group. The group key includes the clause index so
// two alternatives clauses in one span stay distinct.
function assignAlternativesGroups(promoted, spanById) {
  const termsBySpan = new Map();
  for (const term of promoted) {
    if (!termsBySpan.has(term.owningSpanId)) termsBySpan.set(term.owningSpanId, []);
    termsBySpan.get(term.owningSpanId).push(term);
  }
  const groupIdByTerm = new Map();
  for (const [spanId, terms] of termsBySpan) {
    const span = spanById.get(spanId);
    if (!span) continue;
    splitClauses(span.normalizedText).forEach((clause, clauseIndex) => {
      const lowerClause = clause.toLowerCase();
      // Terms located in this clause, in order of appearance.
      const located = terms
        .map((term) => ({ term, start: lowerClause.indexOf(String(term.value).toLowerCase()) }))
        .filter((entry) => entry.start >= 0)
        .map((entry) => ({ ...entry, end: entry.start + String(entry.term.value).length }))
        .sort((a, b) => a.start - b.start);
      if (located.length === 0) return;
      const groupId = `${spanId}#${clauseIndex}`;
      // A framing cue makes the terms of ITS list alternatives — only the run
      // the cue governs, never independent requirements elsewhere in the
      // clause. A clause can carry several lists ("build tools (e.g., Bazel,
      // SWC) and CI workflows (e.g., GitHub Actions, GitLab CI)"), so every
      // cue governs its own run; distinct runs get distinct group keys.
      const leadingCues = [...lowerClause.matchAll(LEADING_ALTERNATIVES_CUE_PATTERN)];
      const trailingCue = TRAILING_ALTERNATIVES_CUE_PATTERN.exec(lowerClause);
      // A run states alternatives only for the members that answer the same
      // need: cross-family members ("Kubernetes or Terraform") are independent
      // requirements and drop out of the group before it is assigned.
      const assignRun = (members, runGroupId) => {
        const { kept } = partitionByFamily(members, (member) => member.term.value);
        if (kept.length < 2) return;
        for (const member of kept) groupIdByTerm.set(member.term, runGroupId);
      };
      if (leadingCues.length > 0 || trailingCue) {
        let cueRunIndex = 0;
        const assignCueRun = (members) => {
          if (members.length < 2) return;
          assignRun(members, `${groupId}.${cueRunIndex++}`);
        };
        for (const leadingCue of leadingCues) {
          assignCueRun(cueGovernedRun(lowerClause, located, leadingCue, null));
        }
        if (trailingCue) assignCueRun(cueGovernedRun(lowerClause, located, null, trailingCue));
        return;
      }
      // Otherwise group only maximal runs of terms DIRECTLY connected by a
      // disjunctive "or" — a comma-separated list whose connector is "or". The
      // separator must be exactly a list connector, so a stray "or" elsewhere in
      // the clause ("Native or near-native English") never grabs the terms.
      // Each run is an independent requirement ("Frontend React or Vue,
      // backend Java or Python"), so runs get distinct suffixed keys exactly
      // like cue-governed runs above — one shared clause-level ID would
      // collapse them into a single anyOf.
      let runIndex = 0;
      for (const run of disjunctiveRuns(lowerClause, located)) {
        if (run.length < 2) continue;
        assignRun(run, `${groupId}.${runIndex++}`);
      }
    });
  }
  return promoted.map((term) =>
    groupIdByTerm.has(term) ? { ...term, alternativesGroupId: groupIdByTerm.get(term) } : term,
  );
}

// The run of located terms a framing cue governs. A leading cue ("such as",
// "e.g.", "one of") governs the comma/or-separated run immediately after it —
// bounded by the enclosing parenthetical when the cue sits inside one; a
// trailing cue ("or similar") governs the run immediately before it. An
// "and"-classified gap ends the list either way: "and" joins independent
// requirements, not alternatives.
function cueGovernedRun(lowerClause, located, leadingCue, trailingCue) {
  if (leadingCue) {
    const cueEnd = leadingCue.index + leadingCue[0].length;
    const openParen = lowerClause.lastIndexOf("(", leadingCue.index);
    const closeParen = lowerClause.indexOf(")", cueEnd);
    const bound = openParen >= 0 && closeParen > cueEnd ? closeParen : lowerClause.length;
    const startIndex = located.findIndex((entry) => entry.start >= cueEnd && entry.end <= bound);
    if (startIndex < 0) return [];
    const run = [located[startIndex]];
    for (let i = startIndex + 1; i < located.length; i++) {
      if (located[i].end > bound) break;
      const separator = classifySeparator(lowerClause.slice(located[i - 1].end, located[i].start));
      if (separator !== "comma" && separator !== "or") break;
      run.push(located[i]);
    }
    return run;
  }
  const cueStart = trailingCue.index;
  const cueEnd = cueStart + trailingCue[0].length;
  // A trailing cue inside a parenthetical qualifies only what the parenthetical
  // hangs off — never the comma run that precedes it. "Kubernetes, Terraform (or
  // equivalent IaC)" states two independent needs (orchestration AND IaC); the
  // unbounded backward walk made Kubernetes an alternative to Terraform, so a
  // candidate carrying only one of the two satisfied both.
  const openParen = lowerClause.lastIndexOf("(", cueStart);
  const closeParen = lowerClause.indexOf(")", cueStart);
  const inParenthetical = openParen >= 0 && closeParen > cueStart;
  const backwardBound = inParenthetical ? openParen : -1;

  let endIndex = -1;
  for (let i = located.length - 1; i >= 0; i--) {
    if (located[i].end <= cueStart && located[i].start > backwardBound) {
      endIndex = i;
      break;
    }
  }
  let run;
  if (endIndex >= 0) {
    run = [located[endIndex]];
    for (let i = endIndex - 1; i >= 0; i--) {
      if (located[i].start <= backwardBound) break;
      const separator = classifySeparator(lowerClause.slice(located[i].end, located[i + 1].start));
      if (separator !== "comma" && separator !== "or") break;
      run.unshift(located[i]);
    }
  } else if (inParenthetical) {
    // The parenthetical names no member before the cue ("Terraform (or
    // equivalent IaC)"), so it qualifies the single term it directly follows.
    const anchorIndex = located.findLastIndex((entry) => entry.end <= openParen);
    if (anchorIndex < 0) return [];
    run = [located[anchorIndex]];
  } else {
    return [];
  }
  // Terms stated after the cue but still inside the parenthetical are members
  // of the same list ("or equivalent IaC" makes IaC an alternative to Terraform).
  if (inParenthetical) {
    for (const entry of located) {
      if (entry.start < cueEnd || entry.end > closeParen) continue;
      const previous = run[run.length - 1];
      if (previous.end > cueStart) {
        const separator = classifySeparator(lowerClause.slice(previous.end, entry.start));
        if (separator !== "comma" && separator !== "or") break;
      }
      run.push(entry);
    }
  }
  return run;
}

// Break the located terms into maximal runs whose consecutive members are joined
// only by list separators (comma, "or"), keeping a run only if it actually
// contains an "or" connector (a bare comma list is not a disjunction) AND the
// run is not "and"-linked at either boundary. An and-linked boundary means the
// disjunction has compound branches ("React and TypeScript, or Vue and
// JavaScript" is (React AND TS) OR (Vue AND JS)); a flat anyOf cannot represent
// that, and grouping just the or-adjacent pair would wrongly weaken both
// branches — so compound disjuncts stay ungrouped (conservatively ANDed).
function disjunctiveRuns(lowerClause, located) {
  const runs = [];
  let run = [located[0]];
  let sawOr = false;
  let leftBoundAnd = false;
  const flush = (rightBoundAnd) => {
    if (sawOr && !leftBoundAnd && !rightBoundAnd) runs.push(run);
  };
  for (let i = 1; i < located.length; i++) {
    const separator = classifySeparator(lowerClause.slice(located[i - 1].end, located[i].start));
    if (separator === "or") {
      run.push(located[i]);
      sawOr = true;
    } else if (separator === "comma") {
      run.push(located[i]);
    } else {
      flush(separator === "and");
      run = [located[i]];
      sawOr = false;
      leftBoundAnd = separator === "and";
    }
  }
  flush(false);
  return runs;
}

// Classify the gap text between two consecutive terms:
//   "or":        a disjunctive connector — "or", ", or", "or a" (alternatives)
//   "comma":     a bare list comma — continues a run, not itself a disjunction
//   "and":       a bare conjunction gluing the two terms into one compound
//                branch ("React and TypeScript")
//   "comma_and": ", and" — closes a list and continues to an INDEPENDENT
//                requirement ("React or Vue, and Kubernetes"); ends the run
//                without making its neighbor compound
//   "none":      anything else — ends the run
function classifySeparator(gap) {
  const trimmed = gap.trim();
  if (/^,?\s*or(?:\s+an?)?$/.test(trimmed)) return "or";
  // Slash notation states alternatives: "JavaScript/TypeScript", "React /Vue".
  if (trimmed === "/") return "or";
  if (trimmed === "," || trimmed === "") return "comma";
  if (/^,\s*(?:and|&)$/.test(trimmed)) return "comma_and";
  if (/^(?:and|&)$/.test(trimmed)) return "and";
  // A single comma-delimited token between two located terms is a list member
  // the extractor did not promote in this span ("Node.js, TypeScript, Java" —
  // TypeScript may be owned by another span). It continues the list rather
  // than ending it, or the located siblings around it would fall out of the
  // group. One token only, and never a discourse word — a prose gap still
  // breaks the run.
  if (
    /^,\s*[\w.+#/&-]+\s*,$/.test(trimmed) &&
    !/\b(?:however|ideally|preferably|especially|including|etc)\b/.test(trimmed)
  ) {
    return "comma";
  }
  return "none";
}

// Curated qualification spans (requirement/preferred) are trusted verbatim: any
// concrete technical term stated there is a legitimate keyword, open-vocabulary
// included. Prose spans (responsibility bullets, unstructured content) are noisy
// in real JDs, so an open-vocabulary term is only promoted from them when the
// document has no curated qualifications at all (a flat one-line JD) or the term
// is a recognized technology; otherwise marketing prose leaks as keywords.
const CURATED_ITEM_CLASSES = new Set(["requirement", "preferred"]);
const PROSE_ITEM_CLASSES = new Set(["responsibility", "unclassified"]);

function classificationForSpan(classifications, spanId) {
  return classifications.find((c) => c.spanId === spanId);
}

function supportingSpanIds(fact) {
  return [...new Set([
    fact.owningSpanId,
    ...fact.provenance.filter((ref) => ref.source === "span-text" && ref.spanId).map((ref) => ref.spanId),
  ])];
}

function technicalRequiredness(fact, classificationsBySpan, spanById) {
  let sawPreferred = false;
  for (const spanId of supportingSpanIds(fact)) {
    const itemClass = classificationsBySpan.get(spanId)?.itemClass;
    if (!itemClass) continue;
    // In a mixed requirement/advantage bullet the span stays a requirement, but
    // a term sitting in its advantage clause is a preference, not a requirement.
    const span = spanById.get(spanId);
    const inAdvantageClause = span
      ? locatorInPreferredClause(span.normalizedText, fact.value)
      : false;
    if (itemClass === "requirement") {
      if (inAdvantageClause) sawPreferred = true;
      else return "required";
    } else if (itemClass === "preferred") {
      sawPreferred = true;
    }
  }
  return sawPreferred ? "preferred" : "unknown";
}

function toFact(span, classification, requiredness) {
  return {
    value: span.normalizedText,
    requiredness,
    confidence: classification.confidence,
    owningSpanId: span.id,
    provenance: [
      {
        source: "span-text",
        spanId: span.id,
        evidenceText: span.normalizedText,
        ruleId: `promoted-${classification.itemClass}`,
        ruleVersion: 1,
      },
    ],
  };
}

export function promote(spans, classifications, experienceFacts, technicalTerms) {
  const spanById = new Map(spans.map((s) => [s.id, s]));
  const classificationsBySpan = new Map(classifications.map((classification) => [classification.spanId, classification]));
  const responsibilities = [];
  const requirements = [];
  const preferred = [];

  for (const span of spans) {
    const classification = classificationForSpan(classifications, span.id);
    if (!classification) continue;
    if (classification.itemClass === "responsibility") responsibilities.push(toFact(span, classification, "unknown"));
    if (classification.itemClass === "requirement") requirements.push(toFact(span, classification, "required"));
    if (classification.itemClass === "preferred") preferred.push(toFact(span, classification, "preferred"));
  }

  const experience = experienceFacts.map((fact) => {
    const classification = classificationForSpan(classifications, fact.owningSpanId);
    if (!classification) return fact;
    // In a mixed bullet the span stays a requirement; demote only this fact if
    // its own evidence clause is the advantage one (so a requirement clause's
    // "N+ years" is not demoted by an advantage cue on a sibling clause).
    const span = spanById.get(fact.owningSpanId);
    const evidenceText = fact.provenance.find((ref) => ref.source === "span-text")?.evidenceText;
    const inAdvantageClause =
      span && evidenceText ? locatorInPreferredClause(span.normalizedText, evidenceText) : false;
    if (classification.itemClass === "requirement") {
      return { ...fact, requiredness: inAdvantageClause ? "preferred" : "required" };
    }
    if (classification.itemClass === "preferred") return { ...fact, requiredness: "preferred" };
    return fact;
  });

  const technologies = technicalTerms.map((term) => ({
    ...term,
    requiredness: technicalRequiredness(term, classificationsBySpan, spanById),
  }));

  const hasCuratedQualifications = classifications.some((c) => CURATED_ITEM_CLASSES.has(c.itemClass));

  const eligibleTechnologies = technologies.filter((term) => {
    return supportingSpanIds(term).some((spanId) => {
      const classification = classificationsBySpan.get(spanId);
      const itemClass = classification?.itemClass;
      if (itemClass && CURATED_ITEM_CLASSES.has(itemClass)) return true;
      const span = spanById.get(spanId);
      if (span && MODAL_CUE_PATTERN.test(span.normalizedText)) return true;
      // A delimited tech-stack list is curated content even when the classifier
      // left it unclassified for lack of a heading.
      if (span && isTechnicalListSpan(span.normalizedText)) return true;
      if (itemClass && PROSE_ITEM_CLASSES.has(itemClass)) {
        return !hasCuratedQualifications || term.knownVocabularyBoost === true;
      }
      return false;
    });
  });
  const promotedTechnologies = assignAlternativesGroups(eligibleTechnologies, spanById);

  return { responsibilities, requirements, preferred, experience, technologies, promotedTechnologies };
}
