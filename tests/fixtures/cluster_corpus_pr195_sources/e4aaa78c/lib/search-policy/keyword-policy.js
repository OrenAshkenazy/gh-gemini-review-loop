import { technologyFamily } from "../jd-extraction/tech-families.js";

export const DOMAIN_SIGNALS = [
  { pattern: /\badtech\b/i, value: "AdTech" },
  { pattern: /mobile monetization/i, value: "mobile monetization" },
  { pattern: /ad mediation|mediation framework|mediation layer/i, value: "ad mediation" },
  { pattern: /\bunity\b/i, value: "Unity" },
  { pattern: /\bpayments?\b/i, value: "Payments" },
  { pattern: /\bcyber\b|cybersecurity/i, value: "Cyber Security" },
];

export const KEYWORD_SIGNALS = [
  // Mobile
  { pattern: /\bios\b/i, value: "iOS" },
  { pattern: /\bandroid\b/i, value: "Android" },
  { pattern: /mobile SDK|SDKs/i, value: "mobile SDK" },
  { pattern: /\bswift\b/i, value: "Swift" },
  { pattern: /\bkotlin\b/i, value: "Kotlin" },
  { pattern: /OS internals/i, value: "OS internals" },
  { pattern: /memory management/i, value: "memory management" },
  { pattern: /multi-threaded|multithreaded/i, value: "multi-threaded" },
  // Languages (Java guarded so it never matches "JavaScript")
  { pattern: /\bnode\.?js\b/i, value: "Node.js" },
  { pattern: /\btypescript\b/i, value: "TypeScript" },
  { pattern: /\bjavascript\b/i, value: "JavaScript" },
  { pattern: /\bjava\b(?!script)/i, value: "Java" },
  { pattern: /\bpython\b/i, value: "Python" },
  { pattern: /\bgolang\b|\bgo\b(?=\s+(developer|engineer|programming|language))/i, value: "Go" },
  { pattern: /(?:^|[^a-z0-9_])c\+\+(?=$|[^a-z0-9_])/i, value: "C++" },
  { pattern: /\b(c#|\.net)\b/i, value: ".NET" },
  { pattern: /\bruby\b/i, value: "Ruby" },
  { pattern: /\brust\b/i, value: "Rust" },
  { pattern: /\bscala\b/i, value: "Scala" },
  // Frameworks / frontend
  { pattern: /\breact\b/i, value: "React" },
  { pattern: /\bangular\b/i, value: "Angular" },
  { pattern: /\bvue(\.js)?\b/i, value: "Vue" },
  { pattern: /\bsvelte(kit)?\b/i, value: "Svelte" },
  { pattern: /\bfigma\b/i, value: "Figma" },
  // Data / infra
  { pattern: /\bpostgres(ql)?\b/i, value: "PostgreSQL" },
  { pattern: /\bmysql\b/i, value: "MySQL" },
  { pattern: /\bmongodb\b/i, value: "MongoDB" },
  { pattern: /\bredis\b/i, value: "Redis" },
  { pattern: /\bkafka\b/i, value: "Kafka" },
  { pattern: /\bkubernetes\b|\bk8s\b/i, value: "Kubernetes" },
  { pattern: /\bdocker\b/i, value: "Docker" },
  { pattern: /\baws\b|amazon web services/i, value: "AWS" },
  { pattern: /\bgcp\b|google cloud/i, value: "GCP" },
  { pattern: /\bazure\b/i, value: "Azure" },
  // Security — the differentiators that separate a security-owning DevSecOps
  // engineer from a generic DevOps engineer.
  { pattern: /\bdev[\s_/-]?sec[\s_/-]?ops\b/i, value: "DevSecOps" },
  { pattern: /cloud security/i, value: "cloud security" },
  { pattern: /\biam\b|identity and access management/i, value: "IAM" },
  { pattern: /\bsoc\s?-?\s?2\b/i, value: "SOC2" },
  { pattern: /\biso\s?-?\s?27001\b/i, value: "ISO 27001" },
  { pattern: /(?:vulnerability|vuln) (testing|scanning|assessment|management)/i, value: "vulnerability testing" },
  { pattern: /security assessment/i, value: "security assessment" },
  { pattern: /application security|\bappsec\b/i, value: "application security" },
  { pattern: /network security|\bnetsec\b/i, value: "network security" },
  { pattern: /security incident|security breach/i, value: "security incident" },
  { pattern: /incident response/i, value: "incident response" },
  { pattern: /risk analys(is|es)|risk assessment/i, value: "risk analysis" },
  { pattern: /security controls/i, value: "security controls" },
  { pattern: /security polic(y|ies)/i, value: "security policies" },
  { pattern: /security posture/i, value: "security posture" },
  { pattern: /\bencryption\b/i, value: "encryption" },
  { pattern: /\bcompliance\b/i, value: "compliance" },
  { pattern: /secure ci\/cd|security in ci\/cd/i, value: "secure CI/CD" },
  { pattern: /security automation/i, value: "security automation" },
  { pattern: /kubernetes security|k8s security/i, value: "Kubernetes security" },
  // Security certifications (optional boosters)
  { pattern: /aws certified security(?:\s+[-–]\s+)?(?:specialty|speciality)?|security specialty/i, value: "AWS Security Specialty" },
  { pattern: /certified kubernetes administrator|\bcka\b/i, value: "CKA" },
  { pattern: /comptia security\+?|\bsecurity\+/i, value: "CompTIA Security+" },
  // Machine learning / AI / data science
  { pattern: /machine learning|\bml\b/i, value: "Machine Learning" },
  { pattern: /deep learning/i, value: "deep learning" },
  { pattern: /\bnlp\b|natural language processing/i, value: "NLP" },
  { pattern: /\bpytorch\b/i, value: "PyTorch" },
  { pattern: /\btensorflow\b/i, value: "TensorFlow" },
  { pattern: /\bkeras\b/i, value: "Keras" },
  { pattern: /scikit-?learn|\bsklearn\b/i, value: "scikit-learn" },
  { pattern: /hugging\s?face/i, value: "Hugging Face" },
  { pattern: /\bllms?\b|large language models?/i, value: "LLM" },
  { pattern: /\btransformers?\b/i, value: "Transformers" },
  { pattern: /computer vision/i, value: "Computer Vision" },
  { pattern: /\bmlops\b/i, value: "MLOps" },
  { pattern: /\bspark\b|pyspark/i, value: "Spark" },
  { pattern: /\bpandas\b/i, value: "pandas" },
  { pattern: /data science|data scientist/i, value: "data science" },
  { pattern: /big[\s-]data/i, value: "big data" },
  { pattern: /data pipelines?/i, value: "data pipelines" },
  { pattern: /\bsql\b/i, value: "SQL" },
  { pattern: /\bairflow\b/i, value: "Airflow" },
  { pattern: /\bdbt\b/i, value: "dbt" },
  { pattern: /\bsnowflake\b/i, value: "Snowflake" },
  { pattern: /\bbigquery\b/i, value: "BigQuery" },
  { pattern: /\betl\b/i, value: "ETL" },
  { pattern: /\brag\b|retrieval-augmented/i, value: "RAG" },
  { pattern: /prompt engineering/i, value: "prompt engineering" },
  { pattern: /agentic|\bai agents?\b/i, value: "AI agents" },
  // Test automation tooling
  { pattern: /\bplaywright\b/i, value: "Playwright" },
  { pattern: /\bcypress\b/i, value: "Cypress" },
  { pattern: /\bselenium\b/i, value: "Selenium" },
  { pattern: /\bpytest\b/i, value: "Pytest" },
  { pattern: /\bappium\b/i, value: "Appium" },
  { pattern: /\bjest\b/i, value: "Jest" },
  { pattern: /\bjenkins\b/i, value: "Jenkins" },
  { pattern: /gitlab[\s-]ci/i, value: "GitLab CI" },
  { pattern: /github actions/i, value: "GitHub Actions" },
  { pattern: /test automation|automated testing|automation testing/i, value: "test automation" },
  { pattern: /end-to-end testing|e2e testing/i, value: "E2E testing" },
  // Platform / operations
  { pattern: /\bterraform\b/i, value: "Terraform" },
  { pattern: /\bhelm\b/i, value: "Helm" },
  { pattern: /\bprometheus\b/i, value: "Prometheus" },
  { pattern: /\bgrafana\b/i, value: "Grafana" },
  { pattern: /\bansible\b/i, value: "Ansible" },
  { pattern: /\bobservability\b/i, value: "observability" },
  // Embedded / firmware
  { pattern: /(?:^|[^+#.])\bC\b(?![+#])/m, value: "C" },
  { pattern: /\blinux\b/i, value: "Linux" },
  { pattern: /\brtos\b|real[ -]time operating system/i, value: "RTOS" },
  { pattern: /\barm\b/i, value: "ARM" },
  { pattern: /\bfirmware\b/i, value: "firmware" },
  { pattern: /\bmicrocontrollers?\b|\bmcu\b/i, value: "microcontrollers" },
  { pattern: /bare[ -]metal/i, value: "bare metal" },
  // Architecture
  { pattern: /microservices?/i, value: "microservices" },
  { pattern: /distributed systems/i, value: "distributed systems" },
  { pattern: /\bgraphql\b/i, value: "GraphQL" },
  { pattern: /\brest(ful)?\s+api|\brest\s+api/i, value: "REST API" },
  // Practices
  { pattern: /debugging/i, value: "debugging" },
  { pattern: /CI\/CD/i, value: "CI/CD" },
  { pattern: /performance optimization|performance optimizations/i, value: "performance optimization" },
  { pattern: /code review|code reviews/i, value: "code review" },
];

// Security vocabulary remains useful for recognizing and canonicalizing
// security terms. It must not, however, decide a position's must-haves: those
// come from the JD's explicitly-required source facts below.
export const SECURITY_GROUP_DEFS = [
  ["DevSecOps", "cloud security", "security into the DevOps lifecycle"],
  ["SOC2", "ISO 27001", "compliance", "security assessment", "vulnerability testing"],
  ["IAM", "application security", "network security", "security incident", "security controls"],
];

export const SECURITY_BOOSTER_VALUES = new Set([
  "risk analysis", "encryption", "incident response", "security posture", "secure CI/CD",
  "security automation", "security policies", "Kubernetes security",
  "AWS Security Specialty", "CKA", "CompTIA Security+",
]);

export const SECURITY_VOCAB = new Set([...SECURITY_GROUP_DEFS.flat(), ...SECURITY_BOOSTER_VALUES]);

// A position can name dozens of tools. The first few explicit, evidenceable
// requirement groups are the practical search contract; the rest still widen
// retrieval and rank candidates, but cannot turn into an accidental hard gate.
// An alternatives group counts once because it represents one source need.
export const MAX_SOURCE_MUST_HAVE_GROUPS = 5;

// Extracted keywords carry the JD's own casing (e.g. lowercase "devsecops",
// "iam"), while the security vocabulary is canonical-cased ("DevSecOps", "IAM").
// Membership tests must be case-insensitive so a differently-cased source term
// still activates its group and is consumed, instead of leaking as a duplicate
// flat keyword. Output criteria keep the canonical/source casing untouched.
const SECURITY_VOCAB_LOWER = new Set([...SECURITY_VOCAB].map((value) => value.toLowerCase()));

// Term-level canonical projection. Maps a promoted fact's own value (an alias
// or source spelling) to the product's canonical criterion. Keyed on the whole
// trimmed value, so open products (NovaDB, Apache Iceberg, AWS Glue) that are
// not aliases pass through unchanged and are never collapsed to a substring.
const TECH_TERM_ALIASES = new Map(
  [
    ["netsec", "network security"],
    ["vue.js", "Vue"],
    ["vuejs", "Vue"],
    ["nodejs", "Node.js"],
    ["node js", "Node.js"],
    ["k8s", "Kubernetes"],
    ["rest apis", "REST API"],
    ["restful apis", "REST API"],
    ["rest api", "REST API"],
    ["multi-threaded architectures", "multi-threaded"],
    ["multithreaded architectures", "multi-threaded"],
    ["sdk", "mobile SDK"],
    ["sdks", "mobile SDK"],
    ["mobile sdks", "mobile SDK"],
    ["ml", "Machine Learning"],
    ["transformers", "Transformers"],
    ["large language models", "LLM"],
    ["aws certified security specialty", "AWS Security Specialty"],
    ["aws certified security speciality", "AWS Security Specialty"],
    ["certified kubernetes administrator", "CKA"],
  ].map(([alias, canonical]) => [alias.toLowerCase(), canonical]),
);

export function projectTechnicalTerm(value) {
  return TECH_TERM_ALIASES.get(value.trim().toLowerCase()) ?? value;
}

// Concept/practice phrases that rank but never gate (issue #130): candidates
// do not carry these literal JD phrases as profile evidence — 0/11 real
// profiles satisfied any of the first four in the Mobile Team Lead acceptance
// run — so a required gate on them can only empty the shortlist.
export const NON_EVIDENCEABLE_KEYWORD_VALUES = [
  "OS internals",
  "memory management",
  "multi-threaded",
  "debugging",
  "performance optimization",
  "code review",
];

const NON_EVIDENCEABLE_LOWER = new Set(
  NON_EVIDENCEABLE_KEYWORD_VALUES.map((value) => value.toLowerCase()),
);

const KNOWN_KEYWORD_VALUES_LOWER = new Set(KEYWORD_SIGNALS.map((signal) => signal.value.toLowerCase()));

// An open-vocabulary term earns gate-eligibility by looking like a nameable
// product or technology — something a candidate could list as a skill —
// rather than a prose token the entity miner let through ("breaches",
// "Respond", "techniques").
function looksLikeProductTerm(value) {
  if (/[0-9#+./]/.test(value)) return true; // C++, Node.js, C#, k6
  if (/^[A-Z]{2,6}$/.test(value)) return true; // acronyms: DLP, SIEM, QA
  if (/[a-z][A-Z]/.test(value)) return true; // internal capitals: NovaDB, PostgreSQL
  const words = value.split(/\s+/);
  return words.length >= 2 && words.every((word) => /^[A-Z]/.test(word)); // Apache Iceberg
}

// Whether a keyword may DEFAULT to required at extraction time. This only
// shapes extraction defaults — an explicit recruiter toggle on the position
// is applied downstream and always wins.
export function isGateEligibleKeywordTerm(term) {
  const projected = projectTechnicalTerm(String(term ?? "").trim());
  const lower = projected.toLowerCase();
  if (NON_EVIDENCEABLE_LOWER.has(lower)) return false;
  if (KNOWN_KEYWORD_VALUES_LOWER.has(lower) || SECURITY_VOCAB_LOWER.has(lower)) return true;
  return looksLikeProductTerm(projected);
}

export function collectSignals(text, signals) {
  const values = [];
  for (const signal of signals) {
    if (signal.pattern.test(text) && !values.includes(signal.value)) {
      values.push(signal.value);
    }
  }
  return values;
}

// A role whose deliverable is code. The floor below only applies here: an
// operations, security or QA brief legitimately gates on tooling rather than on
// a programming language, and forcing one there invents a requirement the JD
// never stated. Platform, data and ML roles DO belong: they are hired to write
// Python, Scala or Java, and a data search with no language gate matches anyone
// whose profile lists a warehouse product.
// A bare "Lead" paired with a software discipline is hands-on and in scope —
// target-title expansion emits "Mobile SDK Lead" itself, so rejecting it left
// exactly the positions this repo generates without a language gate.
const SOFTWARE_ROLE_NOUN_PATTERN =
  /\b(engineer|engineering|architect|scientist|lead|leader)\b/i;
const SOFTWARE_DISCIPLINE_PATTERN =
  /\b(software|backend|back[\s-]end|frontend|front[\s-]end|full[\s-]?stack|web|mobile|android|ios|embedded|firmware|game|application|algorithms?|sdk|r&d|platform|data|analytics|machine learning|ml|ai)\b/i;
// "Developer" and "programmer" name the discipline on their own.
const INHERENTLY_SOFTWARE_PATTERN = /\b(developer|programmer|software engineer)\b/i;
// A qualifier that moves the role off code delivery even when the rest of the
// title looks like it ("QA Automation Engineer", "Network Engineer"). Checked
// first, so a compound title resolves to the operations reading: a "Data
// Infrastructure Engineer" is an infrastructure engineer working on data.
const NON_SOFTWARE_DISCIPLINE_PATTERN =
  /\b(devops|dev[\s_/-]?sec[\s_/-]?ops|sre|site reliability|infrastructure|network|security|cyber|qa|quality assurance|automation test|support|helpdesk|product manager|designer|sales|marketing)\b/i;
// A management headline whose deliverable is the organization, not the code.
// "Director of Software Engineering" satisfies both patterns above, and gating
// it on a language the JD listed as optional would reject exactly the leaders
// it is looking for. A team/tech/group lead is hands-on and stays in scope.
const MANAGEMENT_TITLE_PATTERN =
  /\b(manager|management|director|vp|vice president|head of|chief|cto|officer)\b/i;

export function isSoftwareEngineeringRole(title) {
  const text = String(title ?? "");
  if (NON_SOFTWARE_DISCIPLINE_PATTERN.test(text)) return false;
  if (MANAGEMENT_TITLE_PATTERN.test(text)) return false;
  if (INHERENTLY_SOFTWARE_PATTERN.test(text)) return true;
  return SOFTWARE_ROLE_NOUN_PATTERN.test(text) && SOFTWARE_DISCIPLINE_PATTERN.test(text);
}

// The families that answer "can this person build software": a programming
// language, or the frontend framework a UI role is actually hired to write.
const DEVELOPMENT_FAMILIES = new Set(["language", "frontend-framework"]);

// EVERY satisfying alternative must carry development evidence, not just one.
// A group is satisfied by any single member, so "React OR Figma" — grouped
// because Figma has no known family to split it on — would count as the gate
// while a candidate who only matches Figma has shown no code at all.
function namesDevelopmentLanguage(criterion) {
  const members = [criterion.term, ...(criterion.anyOf ?? [])];
  return members.every((member) => DEVELOPMENT_FAMILIES.has(technologyFamily(member) ?? ""));
}

// Guarantee a software-engineering brief gates on at least one development
// language. Without it a backend search matches anyone whose profile names the
// surrounding tooling — Kafka, microservices, AWS — while never having shipped
// code, because extraction leaves the language optional whenever the JD states
// it in an advantage clause or past the must-have cap.
//
// The floor promotes; it never invents. A brief that names no language at all
// is returned untouched, and the first language the JD stated (extraction keeps
// JD order, so the most prominent one) is the one that becomes the gate.
export function ensureRequiredDevelopmentKeyword(criteria, title) {
  if (!isSoftwareEngineeringRole(title)) return criteria;
  const skillCriteria = criteria.filter((criterion) => !criterion.requiredRoleTitle);
  // An ALREADY-required language still has to be marked. Enforcement and cap
  // reservation both key off developmentLanguageGate, so returning here unmarked
  // left the gate as an ordinary threshold criterion: a brief requiring Java,
  // Kafka and AWS sets minRequiredMatches to 2 and a candidate with only Kafka
  // and AWS passes without the language.
  const alreadyRequired = skillCriteria.find(
    (criterion) => criterion.required && namesDevelopmentLanguage(criterion),
  );
  if (alreadyRequired) {
    return criteria.map((criterion) =>
      criterion === alreadyRequired ? { ...criterion, developmentLanguageGate: true } : criterion,
    );
  }
  // Membership in a development family IS the eligibility test. Routing the
  // term through isGateEligibleKeywordTerm as well rejected languages this
  // module itself recognizes — Elixir and Perl are `language` but appear in
  // neither KEYWORD_SIGNALS nor looksLikeProductTerm — leaving an Elixir brief
  // with no gate at all.
  const gate = skillCriteria.find((criterion) => namesDevelopmentLanguage(criterion));
  if (!gate) return criteria;
  return criteria.map((criterion) =>
    criterion === gate ? { ...criterion, required: true, developmentLanguageGate: true } : criterion,
  );
}

// The index of the criterion a required-keyword cap must never demote. Both caps
// below demote by position, so a language gate stated late in the JD would be
// the first thing dropped — the exact keyword the floor exists to protect.
// Reserving its slot keeps the cap's own count exact: the gate is kept INSTEAD
// of the last criterion that would otherwise have fit, not in addition to it.
//
// `capacity` is the cap doing the demoting. A cap of zero is a deliberate
// setting the flag parser accepts, and it means zero required skill keywords —
// reserving a slot inside it would hand back one anyway.
function reservedGateIndex(criteria, capacity) {
  if (capacity < 1) return -1;
  return criteria.findIndex((criterion) => criterion.required && criterion.developmentLanguageGate);
}

export function isDevSecOpsRole(title) {
  return /dev[\s\-_/]?sec[\s\-_/]?ops/i.test(title);
}

export function isSecurityRole(title, detectedKeywords) {
  if (/dev[\s_/-]?sec[\s_/-]?ops|\bsecurity\b|\bcyber\b/i.test(title)) return true;
  return detectedKeywords.filter((term) => SECURITY_VOCAB_LOWER.has(term.toLowerCase())).length >= 2;
}

function normalizeSourceKeywordCriteria(sourceKeywords) {
  const criteriaByKey = new Map();
  for (const sourceKeyword of sourceKeywords) {
    const criterion = typeof sourceKeyword === "string"
      ? { term: sourceKeyword, required: false }
      : {
          term: sourceKeyword.term,
          required: sourceKeyword.required === true,
          ...(sourceKeyword.alternativesGroupId
            ? { alternativesGroupId: sourceKeyword.alternativesGroupId }
            : {}),
        };
    const term = typeof criterion.term === "string" ? criterion.term.trim() : "";
    if (!term) continue;
    const key = term.toLowerCase();
    const existing = criteriaByKey.get(key);
    if (!existing) {
      criteriaByKey.set(key, { ...criterion, term });
    } else if (criterion.required && !existing.required) {
      criteriaByKey.set(key, { ...existing, required: true });
    }
  }
  return { criteria: [...criteriaByKey.values()] };
}

// Collapses criteria that share an alternativesGroupId (siblings from one
// "X, Y, or similar" span) into a single any-of criterion carried by the first
// member; the group is required when any member is. A group with one surviving
// member stays a plain criterion. The provenance key itself is extraction
// plumbing and never appears on built criteria.
function groupAlternativeCriteria(criteria) {
  const grouped = [];
  const carriers = new Map();
  const memberTerms = new Map();
  const memberRequired = new Map();
  for (const { alternativesGroupId, ...criterion } of criteria) {
    if (!alternativesGroupId) {
      grouped.push(criterion);
      continue;
    }
    if (!carriers.has(alternativesGroupId)) {
      // The carrier holds the group's shared shape; its term/anyOf/required are
      // finalized in the second pass once every member is known.
      const carrier = { ...criterion };
      carriers.set(alternativesGroupId, carrier);
      memberTerms.set(alternativesGroupId, [criterion.term]);
      memberRequired.set(alternativesGroupId, criterion.required === true);
      grouped.push(carrier);
    } else {
      memberTerms.get(alternativesGroupId).push(criterion.term);
      memberRequired.set(
        alternativesGroupId,
        memberRequired.get(alternativesGroupId) || criterion.required === true,
      );
    }
  }
  const demotedMembers = [];
  for (const [id, carrier] of carriers) {
    const required = memberRequired.get(id);
    // A required group gates, and matchesTerm matches whole words, so a noisy
    // open-vocab member ("is", "frameworks") extracted from the alternatives
    // span would let ordinary profile prose satisfy the gate. Keep only
    // gate-eligible members as required alternatives; the rest are demoted to
    // optional ranking signals — never dropped (issue #130) — the same contract
    // flat keywords get. An optional group never gates, so it keeps every
    // extracted alternative.
    const members = [...new Set(memberTerms.get(id))];
    const gating = required ? members.filter((term) => isGateEligibleKeywordTerm(term)) : members;
    if (required) {
      for (const term of members) {
        if (!isGateEligibleKeywordTerm(term)) demotedMembers.push({ term, required: false });
      }
    }
    carrier.term = gating[0];
    carrier.required = required;
    if (gating.length > 1) {
      carrier.anyOf = gating.slice(1);
      // alternativesGroup distinguishes a source-derived group (every member
      // extracted from the JD) from a policy-synthesized one (security
      // OR-groups, canonical-expanded) — the extraction corpus audits the
      // former. A group that collapses to a single surviving member is a plain
      // criterion and carries no marker.
      carrier.alternativesGroup = true;
    } else {
      delete carrier.anyOf;
    }
  }
  return [...grouped, ...demotedMembers];
}

export function buildKeywordCriteria(sourceKeywords, title, targetTitles) {
  const { criteria } = normalizeSourceKeywordCriteria(sourceKeywords);
  const groupedSourceCriteria = groupAlternativeCriteria(
    criteria.map((criterion) =>
      criterion.required && !isGateEligibleKeywordTerm(criterion.term)
        ? { ...criterion, required: false }
        : criterion,
    ),
  );
  // The floor runs before the cap so a language stated late in the JD can still
  // become the gate; the cap then reserves its slot rather than demoting it.
  const flooredSourceCriteria = ensureRequiredDevelopmentKeyword(groupedSourceCriteria, title);
  const sourceMustHaveCriteria = limitRequiredSourceGroups(flooredSourceCriteria);

  const roleTitleCriterion = buildRequiredRoleTitleCriterion(title, targetTitles);

  return [...roleTitleCriterion, ...sourceMustHaveCriteria];
}

function buildRequiredRoleTitleCriterion(title, targetTitles) {
  // The adapter supplies the reviewed target-title family. The exported policy
  // helper also serves focused keyword-only callers, which intentionally omit
  // title criteria by not passing this third argument.
  if (!Array.isArray(targetTitles)) return [];
  // A present-but-empty list is a stated selection of no titles. Falling back to
  // the position title would reinstate a gate nobody asked for; at extraction
  // time the list is only empty when no title was extracted, and `title` is then
  // the "Open Position" placeholder the guard below already rejects.
  const terms = [...new Set(targetTitles.map((candidate) => String(candidate ?? "").trim()).filter(Boolean))];
  if (terms.length === 0 || terms[0] === "Open Position") return [];
  return [{
    term: terms[0],
    ...(terms.length > 1 ? { anyOf: terms.slice(1) } : {}),
    required: true,
    requiredRoleTitle: true,
    // Existing DevSecOps target-title expansion deliberately includes broad
    // operational titles. A management/architecture headline must not satisfy
    // that operational gate; other title families (e.g. Cloud Architect) may.
    // The selected family decides — not the position title it was derived from.
    //
    // Suppression is family-wide but the exemption is not: an explicitly
    // targeted leadership title ("DevSecOps Manager", "Head of DevSecOps") is
    // the wanted evidence, and evaluateKeyword grants the allowance to the
    // alternative that actually matched. Lifting it here for the whole family
    // would let an untargeted "DevOps Director" ride in on the broad "DevOps"
    // member.
    ...(terms.some(isDevSecOpsRole) ? {} : { allowLeadershipTitle: true }),
  }];
}

function limitRequiredSourceGroups(criteria) {
  const gateIndex = reservedGateIndex(criteria, MAX_SOURCE_MUST_HAVE_GROUPS);
  let requiredGroups = gateIndex >= 0 ? 1 : 0;
  return criteria.map((criterion, index) => {
    if (!criterion.required) return criterion;
    if (index === gateIndex) return criterion;
    requiredGroups += 1;
    return requiredGroups <= MAX_SOURCE_MUST_HAVE_GROUPS
      ? criterion
      : { ...criterion, required: false };
  });
}

// Relieve-keywords policy: keep only the first maxRequired required criteria
// required and demote the rest to optional. The mandatory role-title criterion
// Identity of the REQUIRED skill contract: the set of required non-title
// groups with their members. The source-derived evidence floor is calibrated
// against exactly this set, so an edit that leaves the signature unchanged
// (adding, removing, or retyping an OPTIONAL keyword) must keep the floor —
// clearing it there silently converts a 2-of-5 contract into 5-of-5 and
// drains the shortlist into reserve.
export function requiredGroupSignature(criteria) {
  return (criteria ?? [])
    .filter((criterion) => criterion.required && !criterion.requiredRoleTitle)
    .map((criterion) => [criterion.term, ...(criterion.anyOf ?? [])]
      .map((member) => String(member ?? "").trim().toLocaleLowerCase())
      .join("|"))
    .toSorted()
    .join("::");
}

// is preserved without consuming the skill-group cap. Group structure (anyOf,
// requiredRoleTitle) survives demotion.
export function relieveKeywordCriteria(criteria, maxRequired) {
  const gateIndex = reservedGateIndex(criteria, maxRequired);
  let requiredKept = gateIndex >= 0 ? 1 : 0;
  return criteria.map((criterion, index) => {
    if (!criterion.required) return criterion;
    if (criterion.requiredRoleTitle) return criterion;
    if (index === gateIndex) return criterion;
    requiredKept += 1;
    return requiredKept <= maxRequired ? criterion : { ...criterion, required: false };
  });
}

export function requirementKind(label) {
  if (/\b(years?|experience)\b/i.test(label)) {
    return /\b(lead|manager|management|mentor|people)\b/i.test(label) ? "leadership" : "experience";
  }
  if (/\b(degree|bsc|msc|phd|bachelor|master)\b/i.test(label)) return "education";
  if (collectSignals(label, KEYWORD_SIGNALS).length > 0) return "technical";
  if (collectSignals(label, DOMAIN_SIGNALS).length > 0) return "domain";
  return "general";
}
