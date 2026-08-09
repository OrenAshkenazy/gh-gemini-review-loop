// Technology families: the substitutability classes an "or" list may span.
//
// A JD writes "Kubernetes or Terraform" and "Terraform or Pulumi" with the same
// connector, but they do not mean the same thing. The second names one need with
// two acceptable answers; the first names two different aspects of a platform
// stack — orchestration and infrastructure-as-code — and a candidate who has one
// has not covered the other. Collapsing the first into an anyOf halves the bar
// silently, which is exactly the failure a recruiter reads as "this shortlist is
// full of people missing half the stack".
//
// A family is therefore a substitutability class, not a topic: two terms share a
// family when a team that needs one would accept the other. Membership is
// deliberately partial. An unlisted term has NO family and is never split off,
// because the cost of a wrong split (one need becomes N hard requirements, and
// the shortlist empties) is far higher than the cost of a missed one. Only add a
// term here when its family is unambiguous.
const FAMILY_MEMBERS = {
  "cloud-provider": [
    "AWS", "Amazon Web Services", "Azure", "GCP", "Google Cloud", "Google Cloud Platform",
    "OCI", "Oracle Cloud", "Alibaba Cloud", "IBM Cloud",
  ],
  // Provisioning and configuration management read as one need in practice: JDs
  // list Terraform alongside Ansible and CloudFormation as acceptable answers to
  // "can you codify infrastructure".
  iac: [
    "Terraform", "OpenTofu", "Pulumi", "CloudFormation", "CDK", "AWS CDK",
    "Ansible", "Chef", "Puppet", "SaltStack", "IaC", "Infrastructure as Code",
  ],
  orchestration: [
    "Kubernetes", "K8s", "OpenShift", "EKS", "GKE", "AKS", "ECS", "Nomad", "Docker Swarm",
  ],
  containerization: ["Docker", "Podman", "containerd"],
  "ci-cd": [
    "Jenkins", "GitLab CI", "GitHub Actions", "CircleCI", "Travis CI", "TeamCity",
    "Bamboo", "ArgoCD", "Argo CD", "Spinnaker", "Drone",
  ],
  observability: [
    "Prometheus", "Grafana", "Datadog", "New Relic", "Dynatrace", "OpenTelemetry",
    "Jaeger", "Zipkin", "ELK", "Elastic Stack",
  ],
  language: [
    "Java", "Python", "TypeScript", "JavaScript", "Node.js", "Go", "Golang", "C++", "C#",
    ".NET", "Ruby", "Rust", "Scala", "Kotlin", "Swift", "PHP", "Elixir", "Perl", "C",
  ],
  "frontend-framework": ["React", "Angular", "Vue", "Svelte", "SvelteKit", "Next.js", "Ember"],
  // Relational and document stores are one family, not two. A JD that writes
  // "PostgreSQL or MongoDB" is naming one database-experience need with two
  // acceptable answers; splitting on the storage model would turn that explicit
  // disjunction into two hard requirements and empty the shortlist.
  database: [
    "PostgreSQL", "Postgres", "MySQL", "MariaDB", "SQL Server", "MSSQL", "Oracle Database",
    "MongoDB", "DynamoDB", "Cassandra", "Couchbase", "CosmosDB",
  ],
  cache: ["Redis", "Memcached", "Valkey"],
  "message-broker": ["Kafka", "RabbitMQ", "Pulsar", "Kinesis", "SQS", "NATS", "ActiveMQ"],
  "compliance-framework": [
    "SOC 2", "SOC2", "ISO 27001", "ISO27001", "NIST", "PCI DSS", "PCI-DSS", "HIPAA",
    "GDPR", "FedRAMP",
  ],
};

const FAMILY_BY_TERM = new Map(
  Object.entries(FAMILY_MEMBERS).flatMap(([family, members]) =>
    members.map((member) => [member.toLowerCase(), family]),
  ),
);

// The family a term belongs to, or null when the term is open-vocabulary or its
// family is genuinely ambiguous (SIEM, EDR and IDS are distinct tool classes, but
// JDs enumerate them as one "security tooling" need — splitting them would invent
// hard requirements the JD never stated).
export function technologyFamily(value) {
  if (typeof value !== "string") return null;
  return FAMILY_BY_TERM.get(value.trim().toLowerCase()) ?? null;
}

// Partition an ordered alternatives run into the members that keep the group and
// the members that must stand alone.
//
// The group keeps its dominant family — the one most of its family-bearing
// members share, first-mentioned winning a tie — and every member of another
// known family splits off as an independent requirement. Members with no known
// family stay with the group: the guard narrows a group only on evidence, never
// on the absence of it.
export function partitionByFamily(members, valueOf = (member) => member) {
  const families = members.map((member) => technologyFamily(valueOf(member)));
  const known = families.filter(Boolean);
  if (new Set(known).size < 2) return { kept: members, split: [] };

  const counts = new Map();
  for (const family of known) counts.set(family, (counts.get(family) ?? 0) + 1);
  let dominant = known[0];
  for (const family of known) {
    if (counts.get(family) > counts.get(dominant)) dominant = family;
  }

  const kept = [];
  const split = [];
  members.forEach((member, index) => {
    const family = families[index];
    if (family === null || family === dominant) kept.push(member);
    else split.push(member);
  });
  return { kept, split };
}
