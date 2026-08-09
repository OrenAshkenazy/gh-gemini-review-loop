// The single source of truth for the provider search query.
//
// Both the discovery engine (server-side, when calling SearXNG/Serper) and the
// Search-strategy preview (client-side, so the head hunter sees exactly what
// will be searched before running) build the query through this function.

export interface ProviderQueryInput {
  geoTerms: string[];
  targetTitles: string[];
  keywords: { term: string; required?: boolean; anyOf?: string[]; requiredRoleTitle?: boolean }[];
  // The evidence-floor threshold the post-enrichment gate will apply. The
  // query must relax with it: a query that ANDs all N required criteria
  // excludes exactly the candidates a floor of k < N exists to keep.
  minRequiredMatches?: number | null;
  minimumProfessionalExperienceYears?: number | null;
  minimumManagementExperienceYears?: number | null;
  targetFields: string[];
  targetFieldMode?: "required" | "preferred" | "verify_later";
  targetCompanies: string[];
  targetCompanyMode?: "required" | "preferred";
  targetCompanyMatch?: "all" | "any";
  exclusionTerms: string[];
  includeDevSignals?: boolean;
}

const PUBLIC_DEVELOPER_SIGNAL_TERMS = ['"github.com"', '"stackoverflow.com"'];

export function buildProviderQuery(input: ProviderQueryInput): string {
  const titles = normalizeSignals(input.targetTitles);
  const isIsraelScoped = hasIsraelScope(input.geoTerms);
  const geoTerms = isIsraelScoped ? israelScopedGeoTerms(input.geoTerms) : input.geoTerms;
  const parts = [
    isIsraelScoped ? "site:il.linkedin.com/in" : "site:linkedin.com/in",
    ...geoQuerySignals(geoTerms),
    ...companyQuerySignals(input),
    ...targetFieldQuerySignals(input),
  ];

  // Require the candidate to hold one of the target roles, as a quoted OR group,
  // so the provider returns people in the role instead of a loose bag of words.
  // A required role-title criterion IS that gate (built from the same target
  // titles); emitting the raw targetTitles group beside it duplicates the
  // clause in every outgoing query.
  const hasRoleTitleCriterion = input.keywords.some(
    (keyword) => keyword.required && keyword.requiredRoleTitle,
  );
  if (titles.length > 0 && !hasRoleTitleCriterion) {
    parts.push(formatOrGroup(titles));
  }

  parts.push(...keywordQuerySignals(input.keywords, input.minRequiredMatches));
  if (input.includeDevSignals === true) {
    parts.push(formatOrGroup(PUBLIC_DEVELOPER_SIGNAL_TERMS));
  }
  parts.push(...exclusionQuerySignals(input.exclusionTerms));

  return parts.filter(Boolean).join(" ");
}

function keywordQuerySignals(
  keywords: {
    term: string;
    required?: boolean;
    anyOf?: string[];
    requiredRoleTitle?: boolean;
    developmentLanguageGate?: boolean;
  }[],
  minRequiredMatches?: number | null,
): string[] {
  const requiredCriteria = keywords.filter((k) => k.required);
  // A required role-title criterion always gates and is always ANDed; the
  // evidence floor applies to the threshold criteria only, mirroring
  // passesRequiredGate.
  //
  // The development-language gate is ANDed for the same reason: passesRequiredGate
  // enforces it independently of the floor, so it is NOT order-blind and leaving
  // it in the relaxed OR retrieves profiles that are guaranteed to fail the gate.
  // Provider results are capped, so those profiles consume the raw pool and
  // starve the candidates who actually carry the language.
  const roleTitleCriteria = requiredCriteria.filter((k) => k.requiredRoleTitle);
  const developmentGateCriteria = requiredCriteria.filter(
    (k) => !k.requiredRoleTitle && k.developmentLanguageGate,
  );
  const thresholdCriteria = requiredCriteria.filter(
    (k) => !k.requiredRoleTitle && !k.developmentLanguageGate,
  );
  // Relax retrieval with the gate: the gate is order-blind — any `floor` of
  // the N threshold criteria pass — so pinning any criterion as its own AND
  // part would exclude candidates who satisfy the gate via a subset that
  // skips it. The strongest query every qualifying combination satisfies
  // (for any floor >= 1) is a single OR over all threshold members; the
  // post-retrieval gate restores the full any-`floor` precision. A floor of
  // 0 gates nothing, so every threshold criterion joins the optional pool
  // as pure retrieval bias.
  const floor =
    minRequiredMatches != null && minRequiredMatches < thresholdCriteria.length
      ? minRequiredMatches
      : thresholdCriteria.length;
  const floorRelaxes = floor < thresholdCriteria.length;
  const andedCriteria = floorRelaxes
    ? [...roleTitleCriteria, ...developmentGateCriteria]
    : [...roleTitleCriteria, ...developmentGateCriteria, ...thresholdCriteria];
  const broadenedSlot = floorRelaxes && floor > 0 ? thresholdCriteria : [];
  const relaxedCriteria = floorRelaxes && floor === 0 ? thresholdCriteria : [];
  // Optional criteria all collapse into one broadening OR group, so an optional
  // alternatives group contributes every member (term + anyOf), not just its
  // primary term — otherwise a candidate matching only a sibling alternative is
  // never retrieved. Under a floor of 0, threshold criteria join the same pool.
  const optional = [
    ...new Set(
      normalizeSignals(
        [...keywords.filter((k) => !k.required), ...relaxedCriteria].flatMap((k) => [
          k.term,
          ...(k.anyOf ?? []),
        ]),
      ),
    ),
  ];

  const parts: string[] = [];
  // Each ANDed criterion: a grouped criterion (anyOf) becomes a single OR
  // group — "(DevSecOps OR "cloud security")" — so the gate's "at least one of
  // these" survives into the provider query.
  for (const criterion of andedCriteria) {
    const members = [...new Set(normalizeSignals([criterion.term, ...(criterion.anyOf ?? [])]))];
    if (members.length === 0) continue;
    parts.push(members.length === 1 ? formatSearchTerm(members[0]) : formatOrGroup(members));
  }
  // The broadened floor-th slot: one OR over every member (term + anyOf) of
  // the remaining criteria — the query's "one more match from any of these".
  // Optional terms (advantages and demoted tail groups) fold into the SAME OR
  // clause instead of forming their own space-joined part: a separate part is
  // ANDed by the provider, which would turn every demoted/optional term back
  // into a retrieval requirement and exclude candidates who satisfy the
  // advertised floor-of-threshold contract with none of the optional terms.
  // Inside the shared clause they only widen retrieval; the post-enrichment
  // gate restores the threshold precision.
  if (broadenedSlot.length > 0) {
    const members = [
      ...new Set([
        ...normalizeSignals(broadenedSlot.flatMap((k) => [k.term, ...(k.anyOf ?? [])])),
        ...optional,
      ]),
    ];
    if (members.length === 1) parts.push(formatSearchTerm(members[0]));
    else if (members.length > 1) parts.push(formatOrGroup(members));
    return parts;
  }
  // With no relaxed slot there is nothing left to broaden: every threshold
  // criterion is already its own ANDed part. A separate optional part would not
  // widen retrieval — the provider ANDs space-joined parts — it would narrow it,
  // turning advantages and cap-demoted tail groups back into requirements and
  // excluding candidates who satisfy the entire required contract without them.
  // Optional terms are retrieval bias, so they are emitted only when they are
  // the query's sole content and dropping them would leave nothing to search.
  if (parts.length > 0) return parts;
  if (optional.length === 1) parts.push(formatSearchTerm(optional[0]));
  else if (optional.length > 1) parts.push(formatOrGroup(optional));
  return parts;
}

function targetFieldQuerySignals(input: ProviderQueryInput): string[] {
  if ((input.targetFieldMode ?? "required") !== "required") {
    return [];
  }
  const fields = normalizeSignals(input.targetFields);
  if (fields.length === 0) return [];
  return [fields.length === 1 ? formatSearchTerm(fields[0]) : formatOrGroup(fields)];
}

function geoQuerySignals(geoTerms: string[]): string[] {
  const geo = normalizeSignals(geoTerms);
  if (geo.length === 0) return [];
  return [geo.length === 1 ? formatSearchTerm(geo[0]) : formatOrGroup(geo)];
}

function hasIsraelScope(geoTerms: string[]): boolean {
  const normalized = normalizeSignals(geoTerms);
  const israelTerms = new Set([
    "israel",
    "ישראל",
    "tel aviv",
    "tel-aviv",
    "haifa",
    "jerusalem",
  ]);
  return (
    normalized.length > 0 &&
    normalized.every((term) => israelTerms.has(term.toLowerCase()))
  );
}

// site:il.linkedin.com/in already hard-scopes results to Israel, so a metro-only
// OR group (e.g. "Tel Aviv" OR Haifa) silently drops every other Israeli city.
// Keep English "Israel" as a broadening OR member whenever a city is present, so
// the geo group biases toward the metro without excluding the rest of the country.
// The Hebrew alias is always dropped (provider queries stay English), and a lone
// "Israel" is dropped as redundant with the country host.
function israelScopedGeoTerms(geoTerms: string[]): string[] {
  const terms = normalizeSignals(geoTerms).filter((term) => term !== "ישראל");
  const hasCity = terms.some((term) => term.toLowerCase() !== "israel");
  return hasCity ? terms : [];
}

function companyQuerySignals(input: ProviderQueryInput): string[] {
  const companies = normalizeSignals(input.targetCompanies);
  if (companies.length === 0) return [];
  if (
    (input.targetCompanyMode ?? "required") === "preferred" ||
    input.targetCompanyMatch === "any"
  ) {
    return [formatOrGroup(companies)];
  }
  return companies.map(formatSearchTerm);
}

function exclusionQuerySignals(exclusionTerms: string[]): string[] {
  return normalizeSignals(exclusionTerms).map((term) => `-${formatSearchTerm(term)}`);
}

function formatOrGroup(values: string[]): string {
  return `(${values.map(formatSearchTerm).join(" OR ")})`;
}

function formatSearchTerm(value: string): string {
  return value.includes(" ") ? `"${value}"` : value;
}

export function normalizeSignals(values: string[] = []): string[] {
  return values
    .flatMap((v) => String(v).split(","))
    .map((v) => v.trim())
    .filter(Boolean);
}
