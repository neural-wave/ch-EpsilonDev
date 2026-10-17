// Instant hint while typing: does the wording look like a forbidden request?
//
// Mirrors the team backend's keyword checker, src/nl2sparql/agents/intent_heuristic.py
// (EpsilonDev26/ep-rep), and the guard's fixed hints in src/nl2sparql/guard/decision.py.
// Keep the patterns in sync with that file, or replace this with a backend endpoint.
//
// This only reads the wording. The real verdict also checks the generated query, so the
// hint can only warn ("may be refused"), never say a question is allowed.
window.SQ = window.SQ || {};

(function () {
  const PATTERNS = {
    asks_for_identifiers: /\b(which (ones|patients|cases)|who (are|is|were) (they|the)|identifiers?|\bids?\b|pseudo.?ids?|names? of (the )?patients|list (the |all )?patients)\b/i,
    single_patient: /\b(patient\s+(number\s+|no\.?\s*|#\s*)?[A-Za-z_-]*\d+|this patient|that patient|a specific patient|one patient)\b/i,
    individual_rows: /\b(show me (each|every|all)|list (each|every|all)|each (case|record|row)|one row per|row by row|for each patient)\b/i,
    whole_graph: /\b(how much data|all (the )?data|everything|entire (database|graph)|whole (database|graph)|all triples|dump)\b/i,
    not_a_select: /\b(delete|remove|insert|update|drop|modify|as a graph|construct|take away|export)\b/i,
  };
  // Frontend-only additions: everyday phrasings the backend's keyword list misses.
  // They only make the typing hint more helpful; the backend verdict is unchanged.
  const EXTRA = {
    asks_for_identifiers: /(^\s*who\b|\bwhich (people|persons|individuals|ones)\b|\bnames? of (the )?(patients?|people|persons|individuals)\b|\b(patients?|people)('s|s')? names?\b|\bpatient (list|names?|ids?|identifiers?|numbers?)\b|\b(list|show|give|get|tell|display)( me)?( (all|the|every))* (patients|people|persons|individuals)\b|\btheir (names|ids|identifiers|pseudo.?ids?)\b)/i,
    individual_rows: /(\b(records|rows|entries|details) (of|for) (the )?(patients|each|every|all)\b|\braw (data|records|rows)\b|\b(each|every) patient\b|\bline by line\b|\bindividual (records|patients|cases|rows)\b|\ball (the )?(records|rows|entries)\b)/i,
    // Title + capitalised name stays case-sensitive so "dr" in ordinary words does not match.
    single_patient: /(\b(M(r|rs|s)|Dr)\.?\s+[A-Z][a-z]+|\b[Pp]atient (with )?(the )?([Ii][Dd]|[Nn]umber|[Nn]o\.?)\b)/,
    whole_graph: /\b(full|complete|whole|entire) (database|dataset|data ?set|graph|data)\b/i,
    not_a_select: /(\bdownload\b|\badd (a|an|new)\b|\bcreate\b|\boverwrite\b)/i,
  };
  const REASONS = {
    asks_for_identifiers: "it asks which patients or records are involved",
    single_patient: "it is about one specific patient",
    individual_rows: "it asks for individual records instead of a summary",
    whole_graph: "it asks for the whole database without restriction",
    not_a_select: "it asks to change or export data",
  };
  // The guard's fixed hint for each rule (guard/decision.py, RULE_HINTS).
  const HINTS = {
    single_patient: "Ask about all patients, or about a group defined by a condition.",
    individual_rows: "Ask for a count, an average or a distribution instead of a list of records.",
    whole_graph: "Narrow the question to a concept, such as a diagnosis, a lab test or a drug.",
    not_a_select: "Ask a question that reads data; changes and exports are not possible.",
    asks_for_identifiers: "Ask how many patients, not who they are.",
  };
  const IDENTIFIER_TAIL = /,?\s*(and\s+)?(which ones|who) (are|were) (they|them)\??\s*$/i;

  SQ.precheck = function precheck(text) {
    const t = (text || "").trim();
    if (t.length < 8) return null;
    const raised = Object.keys(PATTERNS).filter((name) => PATTERNS[name].test(t) || EXTRA[name].test(t));
    if (!raised.length) return null;
    const first = raised[0];
    let rephrase = null;
    if (raised.includes("asks_for_identifiers") && IDENTIFIER_TAIL.test(t)) {
      rephrase = t.replace(IDENTIFIER_TAIL, "").replace(/[\s,?]+$/, "") + "?";
    }
    return { flags: raised, reason: REASONS[first], hint: HINTS[first], rephrase };
  };
})();
