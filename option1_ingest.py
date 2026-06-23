#!/usr/bin/env python3
"""
Option 1 — Webinar lead ingestion, hygiene & routing pipeline
Dot Compliance GTM Engineer take-home.

This is the DECISION-LOGIC layer only: brains in code, the
orchestration/triggers/alerts live in a no-code layer — Make/n8n — in production.

It runs on the real 83-row export and emits:
  - option1_output.csv            one annotated row per input — every routing
                                  decision, fully auditable (the proof artifact)
  - option1_sample_payloads.json  example upsert payloads (Lead / CampaignMember /
                                  AccountTask) shaped to option1_schema.json

There is no live Salesforce or enrichment API in a take-home, so the two external
lookups (CRM account-match, ZoomInfo/Lusha enrichment) are INTEGRATION STUBS that
return nothing — they never fabricate data. The account-match / open-opp branches
below are real code that lights up against a live org; on this standalone file they
correctly find no CRM, so every business-domain row routes net-new. In production
you swap the two stub bodies for API clients — nothing else changes.

Pipeline stages, in order:
  - routing waterfall, cheap+terminal gates before any paid enrichment
  - data contract: upsert-on-key + idempotency (the two-class field-MERGE
    policy — fill-blank vs max-date — is a contract enforced at the upsert/CRM
    layer, NOT in this decision-logic script; this script emits the payload)
  - object model (Lead / Campaign Member / Account task); maps the lead /
    campaign / intent-relevant columns (5 low-routing-value cols deliberately
    dropped — see deliverable.md §1.5)
  - two-stage ICP-gated enrichment
  - suppression/special-routing stage + intent->priority escalation
  - task-firing seam (situation stamped here; SF flow fires on assignment)

Pure standard library — runs anywhere with `python3 option1_ingest.py`.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The script may sit beside the data (flat public repo) or one dir up from a
# `source/` folder (local layout). Try each in order, fall back to the first.
_SOURCE_CANDIDATES = [
    HERE / "attendee-data.csv",
    HERE / "source" / "attendee-data.csv",
    HERE.parent / "source" / "attendee-data.csv",
]
SOURCE = next((p for p in _SOURCE_CANDIDATES if p.exists()), _SOURCE_CANDIDATES[0])
OUT_CSV = HERE / "option1_output.csv"
OUT_JSON = HERE / "option1_sample_payloads.json"

# --------------------------------------------------------------------------- #
# Reference data (the only things that need maintaining as policy changes)
# --------------------------------------------------------------------------- #

# Consumer / free email providers. NEVER account-match on these — the domain is
# shared by millions, so domain-matching would collapse every signup into one
# fake account. Strict free-provider list;
# a one-company vanity domain (e.g. rusticbakery.com) is NOT on it.
CONSUMER_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.co.uk", "outlook.com",
    "live.com", "msn.com", "aol.com", "yahoo.com", "yahoo.ca", "yahoo.co.in",
    "yahoo.co.uk", "ymail.com", "icloud.com", "me.com", "mac.com", "proton.me",
    "protonmail.com", "gmx.de", "gmx.com", "web.de", "free.fr", "orange.fr",
    "laposte.net", "bigpond.com", "qq.com", "163.com", "126.com",
}

# The brief's High-Intent Content Trap. The brief names FOUR example terms —
# "validate AI QMS / Veeva / MasterControl / replace" — as the keywords that bump a
# match to Priority Tier 1. We match exactly those four (substring, same mechanism for
# each) and elevate any hit. On THIS data the trap fires once — OUR measured count on
# the file (1 of 83), not a number the brief states — and that one hit is the EVALUATION
# term ("validate AI QMS"); the competitor/displacement subset scores 0. That ~1/83 base
# rate is the empirical spine of the thesis: self-report intent is noise. We keep the
# competitor-vs-evaluation TYPE (it drives different plays — a displacement battlecard vs
# an SE/AE technical follow-up) via the matched-term list and the alert note, NOT as a
# separate intent level. We never claim "zero hits" — the brief lists "validate AI QMS" as
# a trap term and it hits once. We do not add terms or fuzzy-match to
# inflate the count; one is the honest number.
COMPETITOR_KEYWORDS = ["veeva", "mastercontrol", "replace"]   # the brief's competitor/displacement terms
EVALUATION_KEYWORDS = ["validate ai qms"]                     # the brief's product-evaluation trap term
INTENT_KEYWORDS = COMPETITOR_KEYWORDS + EVALUATION_KEYWORDS

# Dot Compliance ICP = regulated life sciences.
# ICP fit is read for FREE from the source `Company type` column (100% populated
# here: Pharmaceutical/Medical Device/Biotechnology/CRO/Cosmetics = ICP;
# Academia/Other/blank = not ICP). We do NOT enrich industry — we already have it.
# (Enrichment is reserved for what's genuinely absent: company SIZE, and resolving
# a personal-email registrant to a company.) Cheap-before-paid.
ICP_TYPE_TOKENS = {
    "pharmaceutical", "biotechnology", "medical device",
    "contract research organization", "cosmetics", "diagnostics",
}


def is_icp(company_type: str) -> bool:
    ct = (company_type or "").lower()
    return any(tok in ct for tok in ICP_TYPE_TOKENS)

# Legal suffixes / generic words stripped before comparing a company name to a
# domain — so "Clario Inc." matches "clario.com" and a bare "Pharma"/"Company"
# can't spuriously corroborate anything (used only by company_conflict()).
ORG_STOPWORDS = {
    "inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
    "gmbh", "ag", "sa", "sas", "bv", "plc", "group", "holding", "holdings",
    "international", "global", "the", "and", "of", "solutions", "technologies",
    "labs", "laboratories", "pharma", "pharmaceuticals", "biopharma",
}


def _company_tokens(s: str) -> set[str]:
    """Lowercased alphanumeric tokens of a company string, minus legal suffixes and
    generic words. Used ONLY to test agreement between an email domain and the
    Organization field — never to resolve the true company (the rigged data
    destroyed that)."""
    toks = re.findall(r"[a-z0-9]+", (s or "").lower())
    # Drop all-digit tokens: a pure number (e.g. "8123255522", a phone sitting in the
    # org field, row 81) is not a company name -> it must never corroborate or contradict
    # a domain. The bright line is str.isdigit(); judging whether a *name* string is
    # "real" would be the resolver this build refuses. org_junk flags WHY.
    return {t for t in toks if t not in ORG_STOPWORDS and len(t) > 1 and not t.isdigit()}


def company_conflict(domain: str | None, organization: str | None) -> bool | None:
    """Cheap DETECTOR (not a resolver) for the rigged Email-domain <-> Organization
    derangement (of the 23 corporate bare rows, 21 name a DIFFERENT company
    than the email domain -> this detector; 1 is junk -- row 81's phone-in-org -> org_junk;
    1 corroborates -- lifesync.com, the fixed point). Returns:
      None  -> not applicable (no corporate domain, or Organization blank/all-junk)
      False -> domain root and Organization share a token -> they corroborate
               (e.g. clario.com / 'Clario', broadspectrumgxp.com / 'Broad Spectrum GXP')
      True  -> no shared token -> distrust BOTH, send to human review

    Deliberately biased to OVER-flag: a false positive costs one human glance on a
    row already bound for the manual/staging queue; a false negative silently trusts
    the poison column. We do NOT fuzzy-match typos or resolve acronym domains
    (tapi->Teva) — that is the confidence-scored resolver this build refuses to build.
    route() scopes the call to contact-less corporate-bare rows, where
    Organization is the poison column and this dumb check is near-exact; named rows
    keep email-as-employer (Class E, 2 rows, defensible) and are not flagged."""
    if not domain:
        return None
    org_toks = _company_tokens(organization or "")
    if not org_toks:                       # Organization blank or all stopwords/junk
        return None
    root = domain.split(".")[0]            # 'clario' from 'clario.com'
    # Agreement: exact token match, or (for non-trivial roots) substring either way
    # — the latter catches concatenated domains like broadspectrumgxp <-> "Broad".
    agree = any(root == t or (len(root) >= 4 and (root in t or t in root))
                for t in org_toks)
    return not agree

# The free-text columns where self-reported intent could appear. The brief names ONE
# column ("Questions & Comments"); we scan all THREE free-text fields defensively — the
# single real hit (row 81) sits in the brief's named column either way, so 1/83 is robust
# to a one-column scan.
FREE_TEXT_COLS = [
    "Questions & Comments",
    "What topics and/or speakers would you like to see in future webinars hosted by Dot Compliance?",
    "Questions and/or comments",
]

# Minimal ISO country -> E.164 calling code map (only the regions in this file +
# common ones). A production build uses the `phonenumbers` library; this is the
# defensive stdlib version (decision: never infer geography from the email TLD —
# we use the clean Country/Region ISO column as the region hint for the phone).
CALLING_CODE = {
    "US": "1", "CA": "1", "GB": "44", "UK": "44", "IL": "972", "AU": "61",
    "DK": "45", "CH": "41", "FR": "33", "DE": "49", "IN": "91", "ES": "34",
    "IT": "39", "NL": "31", "IE": "353", "DO": "1", "BR": "55", "JP": "81",
    "GR": "30", "PT": "351", "SE": "46",   # the file's missing codes
}


# Country -> ISO alias map. On THIS file the `Country/Region` ISO column is already
# clean, canonical and uppercase (all 83 rows), so normalize_country() is a no-op
# pass-through here. It is in the code on purpose: the moment a second source sends
# a free-text country ("United States", "USA", "U.S.") instead of an ISO code, that
# value must collapse to the SAME code or geo routing fragments silently. Same
# decision as the phone path — normalize at the edge, never trust raw free text.
COUNTRY_ALIAS = {
    "united states": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB",
    "germany": "DE", "deutschland": "DE",
    "france": "FR", "canada": "CA", "australia": "AU", "israel": "IL",
    # extend as new sources introduce free-text country values
}


def normalize_country(value: str | None, iso_value: str | None = None) -> str | None:
    """Return a 2-letter ISO country code, or None if nothing usable / no recognized
    mapping is present.

    Prefers the clean ISO column when it carries a value (canonical on this data,
    so this branch is a no-op pass-through that only normalizes case). Falls back to
    alias-normalizing a free-text country NAME for any future source that sends one.
    Decision: geography is read from the country field, NEVER inferred from the email
    TLD (see normalize_phone) — this function is the single place that hardening lives.
    """
    iso = (iso_value or "").strip()
    if iso:
        return iso.upper()
    name = (value or "").strip()
    if not name:
        return None
    # An unmapped free-text name is NOT an ISO code, so we never emit it into
    # country_iso (that would fragment geo routing — the exact failure this guards).
    # Known aliases collapse to canonical ISO; anything else returns None and the row
    # rides manual/enrichment review until the alias map is extended.
    return COUNTRY_ALIAS.get(name.lower())


# --------------------------------------------------------------------------- #
# EXTERNAL INTEGRATION BOUNDARIES — swap these stub bodies for live API clients
# in production. They return NOTHING here (no live org/API) and NEVER fabricate
# data: seeding fake accounts on real domains and presenting them in the audit
# trail as real routing is exactly the dishonesty this build refuses to ship.
# --------------------------------------------------------------------------- #

def crm_lookup_account(domain: str | None) -> dict | None:
    """Integration boundary: look up an existing Salesforce Account by corporate
    domain. There is NO live Salesforce in a take-home, so this returns None —
    nothing is fabricated. The account-match / open-opp / account-customer branches
    in route() are real code that lights up against a live org; on this standalone
    file they correctly find no CRM, so every business-domain row routes net-new.
    In prod: SF query by domain -> {account_id, owner, is_customer, open_opp} | None."""
    return None


# Owners already on a record. This is NOT external CRM state — it is read from the
# source file's own `Assigned To` column (populated on exactly one real row: Ryan
# Daley). So the owner-exists rung fires on real data, never on a fixture.
EXISTING_OWNERS: dict[str, str] = {}   # email -> owner; filled from the file in main()


def crm_lookup_owner(email: str | None) -> str | None:
    """Integration boundary: the exact-email CRM contact lookup — is there already an
    OWNED record for this email? This is the free, deterministic gate that runs before
    any paid enrichment (rung 1; it is also the whole of rung 5's "squeeze the free
    signal first" for personal-email rows). Keyed on the email ONLY — never a fuzzy
    name+title+org match: a wrong fuzzy match routes a lead to the wrong rep (worse
    than no match), and it would match on `Organization`, the rigged file's poison
    column. A personal-email row that misses here has no free signal left, so
    it goes to enrichment, not to a guess.

    In production this is a live Salesforce query by email; here it is backed by the
    file's OWN `Assigned To` column (loaded into EXISTING_OWNERS in main()), so unlike
    crm_lookup_account it returns REAL data (Ryan Daley), never a fabricated owner.
    In prod: SF query Lead/Contact WHERE email = :email -> OwnerId | None."""
    return EXISTING_OWNERS.get(normalize_email(email or ""))


# Enrichment provider waterfall: ZoomInfo primary, Lusha fallback. Chosen by
# coverage (ZoomInfo stronger US/enterprise; Lusha often better EU/mobile). We try
# the primary, fall back only on a miss/low-confidence — not both always (cost).
ENRICH_PROVIDERS = ["ZoomInfo", "Lusha"]


def _enrich_call(provider: str, domain: str | None, email: str | None) -> dict | None:
    """Stub for a real ZoomInfo/Lusha client. Returns None here — there is NO live
    API in a take-home. In prod: HTTP call, parse firmographics (SIZE / identity)
    + the provider's own confidence score. Swap this body for the real client."""
    return None


def enrich_stub(domain: str | None, email: str | None) -> dict:
    """The enrichment waterfall: try ZoomInfo (primary), fall back to Lusha on a
    miss. NEVER fabricates firmographics (that's what produced the Academia->Pharma
    bug). With no live API every call misses, so this returns `pending` plus the
    provider order it WOULD use and what it WOULD fetch (size, or identity+size for
    a personal-email row). Industry/ICP are NOT enriched — they come from the file."""
    needs = "size" if domain else "identity+size"
    for provider in ENRICH_PROVIDERS:                  # primary -> fallback
        hit = _enrich_call(provider, domain, email)
        if hit:                                        # in prod: stop on first good hit
            return {"status": "success", "provider": provider, **hit}
    return {"status": "pending", "needs": needs, "provider_order": list(ENRICH_PROVIDERS)}


# --------------------------------------------------------------------------- #
# Sanitization (decision: fill-blank policy applies later; this just cleans)
# --------------------------------------------------------------------------- #

def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower().replace("mailto:", "")


def extract_domain(email: str) -> str | None:
    """Corporate domain from EITHER a full email or a bare domain.
    `john@clario.com` and `clario.com` both yield `clario.com`."""
    e = normalize_email(email)
    if not e:
        return None
    dom = e.split("@")[-1] if "@" in e else e
    # a domain must have a dot and a plausible TLD (any TLD, not just .com)
    return dom if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", dom) else None


def is_consumer(domain: str | None) -> bool:
    return bool(domain) and domain in CONSUMER_DOMAINS


def stable_row_hash(row: dict) -> str:
    """Deterministic 12-hex hash of the whole raw row, stable across runs and
    machines. Uses hashlib, NOT the builtin hash() (which is salted per process and
    would silently break idempotency on re-run). Byte-identical rows hash the same
    (correct dedup); any differing field separates them."""
    items = sorted((str(k), "" if v is None else str(v)) for k, v in row.items())
    return hashlib.sha1(json.dumps(items, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def surrogate_key(row: dict, domain: str | None) -> str:
    """Dedup key for a row with NO '@' email (a bare domain, or nothing). A bare
    domain (e.g. vitalant.org) must NEVER be the key on its own: two unrelated
    registrants can share it, and an upsert on that shared key destructively merges
    them — the exact bug that erased the dataset's only buying signal.
    So we key on a per-REGISTRATION surrogate (corporate-domain anchor + stable row
    hash): unique per registrant, yet identical on re-run (idempotent). The bare
    domain is kept separately as `account_domain` — a non-destructive account hint,
    never a merge key."""
    anchor = domain if (domain and not is_consumer(domain)) else "staging"
    return f"{anchor}|{stable_row_hash(row)}"


def normalize_phone(raw: str, iso_country: str) -> tuple[str | None, str]:
    """Return (E.164-ish phone or None, quality_flag).

    quality_flag is one of: ok | repaired | no_country_code | junk | too_short |
    blank. Only `ok`/`repaired` are trustworthy E.164 — build_payload writes those
    to Salesforce and nulls the rest (the raw value is always retained in the audit
    record, so nothing is destroyed; an absent dial on an ICP row rides the
    enrichment loop). Uses the clean Country ISO column as the region hint — NEVER
    the email TLD. A bad phone never triggers manual qualification (routing runs on
    identity, not phone).
    """
    if not raw or not raw.strip():
        return None, "blank"
    s = raw.strip().lstrip("'")                       # Excel apostrophe artifact
    # Deterministic trunk-prefix repair: a leading 0 INSIDE
    # parentheses is a formatting/trunk artifact, e.g. AU '+61 (02) 9658 3412' ->
    # the area code is '2', the '0' is the national trunk prefix. The parentheses
    # are the machine-readable signal that this 0 is not subscriber data, so
    # stripping just it is DETERMINISTIC — not the unsafe general "strip any leading
    # 0" rule (wrong for IT: +39 06... keeps its 0). General trunk handling is the
    # job of the `phonenumbers` library in prod; we only take the unambiguous case.
    repaired = re.sub(r"\(\s*0", "(", s)
    was_repaired = repaired != s
    s = repaired
    digits = re.sub(r"[^\d+]", "", s)                 # keep digits and +
    bare = digits.lstrip("+")
    # Exact-match known placeholder junk, NOT a substring scan: the old `"1234567890" in
    # bare` over-flagged any number that merely CONTAINS the run (a guess; 0 hits here).
    # The literal placeholder (row 14: 123-456-7890) is caught by the exact set; judging
    # whether an arbitrary number is fake is the `phonenumbers` library's job in prod.
    if bare in {"1234567890", "123456789", "0000000000"}:
        return None, "junk"
    if len(bare) < 7:
        return None, "too_short"
    good = "repaired" if was_repaired else "ok"
    if digits.startswith("+"):
        return digits, good
    code = CALLING_CODE.get((iso_country or "").upper().strip())
    if not code:
        return digits, "no_country_code"               # keep national, flag it (-> null in SF)
    if bare.startswith(code):
        return "+" + bare, good
    return "+" + code + bare.lstrip("0"), good


def normalize_name(raw: str) -> str:
    s = (raw or "").strip()
    # fix ALL-CAPS / all-lower; leave mixed case alone
    return s.title() if s and (s.isupper() or s.islower()) else s


REG_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S",
    "%m/%d/%y %H:%M", "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M:%S",
]


def parse_registration_time(raw: str) -> str | None:
    """Multi-format parser + Excel serial-float defense.
    Returns ISO-8601 or None. This file is clean datetime; the serial-float
    case only appears on a real Excel->CSV export, handled defensively."""
    s = (raw or "").strip()
    if not s or s == "--":
        return None
    # Excel serial float (days since 1899-12-30) — the CSV-export artifact
    try:
        if re.match(r"^\d{4,6}(\.\d+)?$", s):
            base = datetime(1899, 12, 30)
            return (base + timedelta(days=float(s))).isoformat()
    except (ValueError, OverflowError):
        pass
    for fmt in REG_FORMATS:
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Classification + intent
# --------------------------------------------------------------------------- #

def classify_identifier(email_raw: str) -> str:
    """One classification, up front: bare_domain / business_email /
    personal_email / none. Drives which branch of the waterfall the row takes."""
    e = normalize_email(email_raw)
    if not e:
        return "none"
    if "@" not in e:
        return "bare_domain" if extract_domain(e) else "none"
    dom = extract_domain(e)
    if not dom:
        return "none"
    return "personal_email" if is_consumer(dom) else "business_email"


def intent_scan(row: dict) -> dict:
    """The brief's content trap: scan the free-text for the four trap terms;
    any hit elevates the record to Priority Tier 1 (in _apply_intent). On this data
    exactly 1 of 83 hits — the EVALUATION term 'validate AI QMS'; the competitor/
    displacement terms score 0. That 1/83 base rate is the thesis spine: self-
    report intent is noise. The competitor-vs-evaluation TYPE is preserved in the
    matched-term list (intent_keywords), not as a separate level — a competitor mention
    and a genuine evaluation question are BOTH Tier-1 trap hits per the brief, but route
    to different plays. Topical AI interest *without* a trap term is NOT escalated.
    (Even classifying the one hit is a judgment call — itself the point: at ~1/83,
    self-report is too noisy to route on. Bridge to Option 2.)"""
    blob = " ".join(row.get(c, "") or "" for c in FREE_TEXT_COLS).lower()
    matched = [k for k in INTENT_KEYWORDS if k in blob]
    level = "keyword" if matched else "none"
    # intent_text uses the same _clean() placeholder filter as every other field
    # (strips '', '--', 'N/A', 'n/a') so junk like "N/A | N/A" never ships in a
    # payload. We do NOT scrub real-but-empty prose ("No comments") or a
    # field-swap ("Compliance Consultant" in an answer column) — that's a classifier
    # / Part-2 narrative, not a sanitizer's job.
    return {"intent_level": level, "intent_keywords": matched,
            "intent_text": " | ".join(v for c in FREE_TEXT_COLS
                                       if (v := _clean(row.get(c)))) or None}


# --------------------------------------------------------------------------- #
# The waterfall + suppression/special-routing + enrichment
# --------------------------------------------------------------------------- #

def route(row: dict) -> dict:
    """Run one row through the full model. Returns the annotated decision.

    Order = cheap+terminal gates first, paid enrichment last:
      stage 0  suppression / special-routing (customer, open-opp, DNC)
      rung 1   owner-exists                 (free, terminal)
      rung 2   account-match on domain      (free, terminal)
      rung 3   ICP gate (FREE, from Company type) -> enrichment (PAID) for residue
               non-ICP: nurture; 'Other': light review; ICP net-new: enrich size
      rung 4   manual queue (no usable identity)
    """
    lifecycle_file = (row.get("Lead/MQL") or "").strip() or None
    iso = normalize_country(row.get("Country/Region Name"), iso_value=row.get("Country/Region"))
    # Phone: normalize once here. Salesforce gets a dial ONLY when it's
    # trustworthy (ok/repaired); everything else writes null so a rep never dials a
    # bad number. The raw + quality flag are retained in the audit record (phone_raw
    # / phone_quality) — nulling the CRM field is not the same as discarding data.
    phone_e164, phone_quality = normalize_phone(row.get("Phone", ""), iso)
    if phone_quality not in {"ok", "repaired"}:
        phone_e164 = None
    email = normalize_email(row.get("Email", ""))
    domain = extract_domain(email)
    id_class = classify_identifier(row.get("Email", ""))
    has_person = bool((row.get("First Name") or "").strip() or (row.get("Last Name") or "").strip())
    intent = intent_scan(row)
    company_type = (row.get("Company type") or "").strip()   # ICP read for FREE

    # company_conflict (detector): does the email domain
    # name a DIFFERENT company than Organization? Scoped to CONTACT-LESS corporate-
    # bare rows — there Organization is the rigged file's POISON column and this
    # dumb token check is near-exact (flags the 21 deranged rows; row 81's junk org goes
    # to org_junk, not here; clears the lone fixed point lifesync.com). Named rows keep
    # email-as-employer (Class E) and are
    # NOT flagged: the check false-positives on acronym domains (tapi->Teva) there.
    # None = not applicable. Detector only — the confidence downgrade + note are
    # applied as a post-pass in main() (mirrors shared_domain_collision).
    conflict = (company_conflict(domain, row.get("Organization"))
                if (not has_person and domain and not is_consumer(domain)) else None)
    # org_junk (B-flag): the Organization is non-blank but contains NO letters -> it can't
    # be a company name (e.g. row 81's "8123255522", a phone in the org field). Bright line
    # = no alphabetic char (str.isdigit spirit, robust to "812-325-5522"). Deliberately NOT
    # "tokenizes to nothing": that also fires on generic words (Pharma) and short real names
    # (Q and E / qande.dk) -- weak-but-real, not junk. We ignore it for matching AND record
    # it was junk, not merely absent; gates it out of enrichment as a company hint.
    org_for_junk = (row.get("Organization") or "").strip()
    org_junk = bool(org_for_junk) and not any(c.isalpha() for c in org_for_junk)

    d = {  # the decision record (subset becomes the audit CSV)
        "email": email or None,
        "dedup_key": email if "@" in (email or "") else surrogate_key(row, domain),
        "account_domain": domain if (domain and not is_consumer(domain)) else None,
        "shared_domain_collision": False,   # set by the batch post-pass in main()
        "company_conflict": conflict,       # downgrade applied in main()
        "org_junk": org_junk,               # non-blank Organization, no usable token (B-flag)
        "id_class": id_class,
        "has_person": has_person,
        "lifecycle_file": lifecycle_file,
        "lifecycle_resolved": lifecycle_file or "Lead",   # both-blank default = Lead
        "account_id": None,
        "match_status": None,
        "object_written": None,
        "destination_type": None,
        "destination_owner": None,
        "routing_situation": None,
        "enrichment_status": "not_needed",
        "industry": company_type or None,        # from the file, never fabricated
        "icp_fit": is_icp(company_type),          # cheap gate, read for free
        "intent_level": intent["intent_level"],
        "intent_keywords": ",".join(intent["intent_keywords"]),
        "intent_text": intent["intent_text"],   # cleaned once here; reused in build_payload (kills #10's double scan)
        "priority": "low",
        "ae_loop_in": False,
        "consent_status": "unknown",   # data lives on the form/CRM, not this CSV
        "phone_e164": phone_e164,      # trustworthy dial only; null otherwise
        "phone_quality": phone_quality,
        "phone_raw": (row.get("Phone") or "").strip() or None,   # raw retained -> nothing destroyed
        "confidence": "low",
        "notes": "",
    }

    crm_account = crm_lookup_account(domain) if domain and not is_consumer(domain) else None

    # ----- stage 0: suppression / special-routing -----------------------
    # object_written follows has_person in EVERY branch. Task(Account) is an
    # Account-level task that carries NO contact, so it is emitted only for a contact-
    # less row; a NAMED special-routing row updates the existing person + logs the
    # attendance (Lead(update)+CampaignMember) and routes to the right team; a row with
    # no live account match is suppressed-but-identified (object 'none', person kept for
    # the human queue + audit). This makes the "Task(Account) with a populated lead"
    # contradiction structurally impossible (also guarded by an assert in build_payload).
    # All Task(Account) sites now require not-has_person; crm_account is always None on
    # this file, so the change is a pure no-op on the real data (byte-identical), the
    # same pure-refactor proof used throughout.
    is_customer = (lifecycle_file == "Customer") or (crm_account and crm_account["is_customer"])
    if is_customer:
        if crm_account:
            cust_obj = "Lead(update)+CampaignMember" if has_person else "Task(Account)"
        else:
            cust_obj = "none"   # no live account to attach -> suppressed-but-identified
        d.update(match_status="customer_suppress", object_written=cust_obj,
                 destination_type="CS_AM", account_id=crm_account["account_id"] if crm_account else None,
                 destination_owner=crm_account["owner"] if crm_account else "CS/AM queue",
                 confidence="high", notes="Customer -> CS/AM expansion; suppressed from BDR outbound")
        return _apply_intent(d, intent, crm_account)
    if crm_account and crm_account["open_opp"]:
        oo_obj = "Lead(update)+CampaignMember" if has_person else "Task(Account)"
        d.update(match_status="open_opp", object_written=oo_obj, account_id=crm_account["account_id"],
                 destination_type="AE", destination_owner=crm_account["owner"], ae_loop_in=True,
                 confidence="high", notes="Open opportunity -> route to AE, suppress BDR outbound")
        return _apply_intent(d, intent, crm_account)

    # ----- rung 1: owner-exists (free, terminal) -----------------------------
    # Exact-email CRM lookup (the free, deterministic gate — see crm_lookup_owner).
    # This IS rung 5's free pre-enrichment squeeze for personal-email rows: keyed on
    # email only, never a fuzzy name/org match. Miss here -> no free signal left.
    owner = crm_lookup_owner(email)
    if owner:
        d.update(match_status="owner_exists", object_written="Lead(update)+CampaignMember",
                 destination_type="existing_owner", destination_owner=owner, confidence="high",
                 notes="Already owned -> no reroute (idempotent no-op)")
        return _apply_intent(d, intent, crm_account)

    # ----- rung 2: account-match on corporate domain (free, terminal) --------
    if crm_account:
        if has_person:
            d.update(match_status="account_matched", account_id=crm_account["account_id"],
                     object_written="Lead+CampaignMember", destination_type="account_owner",
                     destination_owner=crm_account["owner"], confidence="high",
                     notes="Matched corporate domain -> account owner; contact attached")
        else:  # the 31 bare-domain rows: account-level, no person
            d.update(match_status="account_matched_contactless", account_id=crm_account["account_id"],
                     object_written="Task(Account)", destination_type="account_owner",
                     destination_owner=crm_account["owner"], routing_situation="contact_less_inspect",
                     confidence="medium", notes="Corporate domain, no person -> Account task, no Lead")
        return _apply_intent(d, intent, crm_account)

    # ----- rung 3: ICP gate (FREE, from Company type) then enrichment --------
    # ICP fit is already known from the file (d["icp_fit"]); no enrichment needed
    # to decide it. Two non-ICP outcomes: a clear non-fit (Academia,
    # blank) -> nurture; the ambiguous 'Other' -> LIGHT human review, because it
    # can hide a relevant CRO/supplier and we don't want to silently nurture it.
    if not d["icp_fit"]:
        if company_type.strip().lower() == "other":
            d.update(match_status="other_review", object_written="Lead(review)" if has_person else "staging",
                     destination_type="review_queue", destination_owner="Ops light-review queue",
                     confidence="low",
                     notes="Company type 'Other' -> light human review (may be a relevant CRO/supplier)")
        else:
            d.update(match_status="non_icp", object_written="Lead(nurture)" if has_person else "none",
                     destination_type="nurture", destination_owner="Marketing nurture",
                     confidence="medium",
                     notes=f"Non-ICP per Company type ({company_type or 'blank'}) -> nurture, no BDR")
        return _apply_intent(d, intent, crm_account)

    # ICP, net-new. Enrichment is reserved for what's genuinely missing: company
    # SIZE (pod tier) and, for personal-email rows, the company identity. No live
    # API here, so we mark `pending` and route on what's free (geo + ICP); we do
    # NOT fabricate size/industry. Bare consumer / no-identity ICP rows have
    # nothing to enrich ON -> manual.
    has_identity = bool(domain and not is_consumer(domain)) or id_class == "personal_email"
    if has_person and has_identity:
        enr = enrich_stub(domain if not is_consumer(domain or "") else None, email)
        order = "->".join(enr.get("provider_order", []))
        d.update(match_status="icp_netnew", enrichment_status=enr["status"],
                 object_written="Lead+CampaignMember", destination_type="bdr_pod",
                 destination_owner=f"BDR pod [{iso or '??'} / size-pending]",
                 routing_situation="net_new_enriched", confidence="medium",
                 notes=f"Net-new ICP ({company_type}) -> BDR pod by geo; enrich {enr['needs']} "
                       f"via {order} in prod; SF round-robin assigns the rep")
    elif domain and not is_consumer(domain):     # ICP corporate domain, no person
        d.update(match_status="icp_contactless", enrichment_status="pending",
                 object_written="staging", destination_type="manual_queue",
                 destination_owner="BDR review queue", routing_situation="needs_manual_qual",
                 confidence="low",
                 notes=f"Net-new ICP domain ({company_type}), no person -> manual persona qualification")
    else:                                          # ICP but consumer/blank, no identity to enrich
        d.update(match_status="no_match", object_written="staging", destination_type="manual_queue",
                 destination_owner="BDR review queue", routing_situation="needs_manual_qual",
                 confidence="low",
                 notes=f"ICP per Company type ({company_type}) but no usable identity "
                       "(consumer/blank) -> manual qualification")
    return _apply_intent(d, intent, crm_account)


def _apply_intent(d: dict, intent: dict, crm_account: dict | None) -> dict:
    """Intent -> priority escalation, tiered by intent x account-value.
    Never reassigns an owned account (integrity holds); raises priority and,
    for ICP, loops in the AE. Customers/nurture never get BDR escalation."""
    if d["match_status"] in {"customer_suppress", "non_icp"}:
        return d
    if intent["intent_level"] == "keyword":
        d["priority"] = "high"
        kws = intent["intent_keywords"]
        # Same Tier-1 elevation for any trap hit; the TYPE only shapes the alert text and
        # the downstream play — competitor/displacement -> battlecard; evaluation question
        # -> SE/AE technical follow-up. On this data the one hit is the evaluation term.
        is_competitor = any(k in COMPETITOR_KEYWORDS for k in kws)
        tag = "competitor/displacement keyword" if is_competitor else "evaluation question (human review)"
        d["notes"] += f" | HIGH-INTENT [{tag}]: immediate alert (Priority Tier 1)"
        icp = bool(d.get("icp_fit")) or (crm_account is not None)
        if icp:
            d["ae_loop_in"] = True
            if d["destination_type"] == "bdr_pod":
                d["destination_owner"] = "Senior BDR queue (fast-track)"
                d["routing_situation"] = "high_intent_fast_track"
            d["notes"] += "; AE looped in (ICP)"
    return d


# --------------------------------------------------------------------------- #
# Build sample upsert payloads (shaped to option1_schema.json)
# --------------------------------------------------------------------------- #

def _clean(v: str | None) -> str | None:
    """Empty / placeholder ('--') -> None, so junk never reaches the CRM."""
    s = (v or "").strip()
    return None if s in {"", "--", "N/A", "n/a"} else s


def build_payload(row: dict, d: dict) -> dict:
    """The JSON we'd upsert downstream. Upsert-on-key = idempotent."""
    lead = None if not d["has_person"] else {
        "email": d["email"],
        "first_name": normalize_name(row.get("First Name", "")) or None,
        "last_name": normalize_name(row.get("Last Name", "")) or None,
        "company": (row.get("Organization") or "").strip() or None,
        "title": (row.get("Job Title") or "").strip() or None,
        "phone_e164": d["phone_e164"],   # trustworthy dial only (else null); computed once in route()
        "country_iso": normalize_country(row.get("Country/Region Name"), iso_value=row.get("Country/Region")),
        "lifecycle": d["lifecycle_resolved"],
        "owner": d["destination_owner"],
        "confidence_flag": d["confidence"],
        "routing_situation": d["routing_situation"],
    }
    # Invariant: object and lead-presence must agree. An Account-level task carries
    # no contact, so Task(Account) must NEVER ship a lead block; a lead block requires a
    # person-object. route() already guarantees this (Task(Account) only when not
    # has_person); the assert documents + enforces the contract, so any FUTURE branch
    # that violates it fails loudly in testing instead of writing a contradictory payload.
    assert not (d["object_written"] == "Task(Account)" and lead is not None), (
        f"contract violation: Task(Account) with a populated lead for {d['email']!r}")
    return {
        "dedup_key": d["dedup_key"],
        "object": d["object_written"],
        "lead": lead,
        "campaign_member": {
            "webinar_id": "webinar-2026-05-gxp-ai",
            "registration_time": parse_registration_time(row.get("Registration Time", "")),
            "attended": _clean(row.get("Attended")),
            "time_in_session_min": _clean(row.get("Time in Session (minutes)")),
            "source_event": _clean(row.get("Source Name")),
        },
        "intent": {"level": d["intent_level"], "keywords": d["intent_keywords"] or None,
                   "text": d["intent_text"]},
        "_meta": {"match_status": d["match_status"], "enrichment_status": d["enrichment_status"],
                  "destination_type": d["destination_type"], "consent_status": d["consent_status"],
                  "account_domain": d["account_domain"],
                  "shared_domain_collision": d["shared_domain_collision"],
                  "company_conflict": d["company_conflict"],
                  "org_junk": d["org_junk"],
                  "phone_quality": d["phone_quality"], "phone_raw": d["phone_raw"]},
    }


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    if not SOURCE.exists():
        print(f"source not found: {SOURCE}", file=sys.stderr)
        return 1
    rows = list(csv.DictReader(SOURCE.open(encoding="utf-8-sig")))

    # Owner-exists signal comes from the file's own `Assigned To` column (real data:
    # exactly one row, Ryan Daley). Not a mock — a real ownership signal already in
    # the export, which the pipeline must honor (never reroute an owned record).
    for r in rows:
        if (r.get("Assigned To") or "").strip():
            EXISTING_OWNERS[normalize_email(r["Email"])] = r["Assigned To"].strip()

    # Within-batch dedup: collapse rows that share a dedup_key. Person rows key
    # on the '@' email; contact-less rows key on a per-registration SURROGATE
    # (corporate-domain + stable row hash), so two unrelated registrants who share a
    # bare domain (the two "vitalant.org" rows, one holding the only buying signal)
    # get DIFFERENT keys and are never merged. Only byte-identical rows collapse here
    # — which is correct dedup. The surrogate makes
    # collapse-on-key safe for bare-domain rows, where keying on the raw domain was not.
    seen, deduped, dupes = set(), [], 0
    for r in rows:
        e = normalize_email(r["Email"])
        key = e if "@" in e else surrogate_key(r, extract_domain(e))
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        deduped.append(r)

    decisions = [route(r) for r in deduped]

    # Shared-domain collision flag: when >=2 CONTACT-LESS rows (no '@'
    # person key) share the same corporate domain, they are distinct registrants who
    # must not be merged into one account. Flag them so the human reviewer sees the
    # collision — and, for vitalant.org, that one of the two carries the only buying
    # signal. (The surrogate key already prevents the merge; this surfaces it.)
    contactless_domains = Counter(
        d["account_domain"] for d in decisions
        if d["account_domain"] and "@" not in (d["email"] or "")
    )
    collided = {dom for dom, n in contactless_domains.items() if n >= 2}
    for d in decisions:
        if "@" not in (d["email"] or "") and d["account_domain"] in collided:
            d["shared_domain_collision"] = True
            d["notes"] += (f" | SHARED-DOMAIN COLLISION ({d['account_domain']}): "
                           "distinct registrant, not merged -> human review")

    # company_conflict downgrade: a contact-less row whose corporate
    # domain and Organization name DIFFERENT companies — the rigged file's poison
    # column. We do NOT resolve the true employer (the shuffle destroyed it);
    # we mark confidence low and surface it for the human in the staging/manual queue.
    # Annotate-only — no reroute: the row was already bound for that queue.
    for r, d in zip(deduped, decisions):
        if d["company_conflict"] is True:
            d["confidence"] = "low"
            org = (r.get("Organization") or "").strip()
            d["notes"] += (f" | COMPANY CONFLICT: email-domain '{d['account_domain']}' "
                           f"names a different company than Organization '{org}' "
                           "-> distrust both, human review (rigged-data Class A)")
        elif d["org_junk"]:
            # Junk org is an ABSENCE of usable data, not a competing claim -> no
            # confidence downgrade (same posture as a blank org); we record the fact only.
            org = (r.get("Organization") or "").strip()
            d["notes"] += (f" | ORG JUNK: Organization '{org}' is not a company name "
                           "(all-digits/non-name) -> ignored, not used for match or enrichment")

    payloads = [build_payload(r, d) for r, d in zip(deduped, decisions)]

    # ---- annotated audit CSV (the proof artifact) ----
    cols = ["email", "dedup_key", "account_domain", "shared_domain_collision",
            "company_conflict", "org_junk", "id_class", "has_person", "lifecycle_file",
            "lifecycle_resolved", "match_status", "object_written", "destination_type",
            "destination_owner", "routing_situation", "enrichment_status", "industry",
            "icp_fit", "intent_level", "intent_keywords", "priority", "ae_loop_in",
            "consent_status", "phone_quality", "confidence", "notes"]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for d in decisions:
            w.writerow({k: d.get(k) for k in cols})

    # ---- a few representative sample payloads (one per major branch) ----
    samples, want = [], {"account_matched", "account_matched_contactless", "customer_suppress",
                         "owner_exists", "icp_netnew", "icp_contactless", "non_icp", "no_match",
                         "open_opp", "other_review"}
    for p, d in zip(payloads, decisions):
        if d["match_status"] in want:
            samples.append(p)
            want.discard(d["match_status"])
    OUT_JSON.write_text(json.dumps(samples, indent=2, ensure_ascii=False))

    # ---- run summary (observability: what the pipeline did) ----
    ms = Counter(d["match_status"] for d in decisions)
    print("=" * 64)
    print(f"INGESTED {len(rows)} rows  ->  {len(deduped)} after within-batch dedup ({dupes} dupes)")
    print("-" * 64)
    print("Routing outcomes (match_status):")
    for k, v in ms.most_common():
        print(f"  {v:>3}  {k}")
    print("-" * 64)
    trap_hits = sum(1 for d in decisions if d['intent_level'] == 'keyword')
    comp_hits = sum(1 for d in decisions if d['intent_level'] == 'keyword'
                    and any(k in d['intent_keywords'] for k in COMPETITOR_KEYWORDS))
    print(f"  Brief's content-trap hits (4 terms) : "
          f"{trap_hits}  <- 1/83, our measured count on the data")
    print(f"    competitor/displacement mentions  : "
          f"{comp_hits}  <- 0 (Veeva/MasterControl/replace)")
    print(f"    genuine evaluation questions      : "
          f"{trap_hits - comp_hits}  <- 1 ('validate AI QMS'), the one real signal")
    print(f"  ICP fit read free from Company type : "
          f"{sum(1 for d in decisions if d['icp_fit'])} ICP / "
          f"{sum(1 for d in decisions if not d['icp_fit'])} non-ICP  (0 industry enrichment)")
    print(f"  Would enrich (size/identity) in prod: "
          f"{sum(1 for d in decisions if d['enrichment_status'] == 'pending')}")
    print(f"  AE looped in                        : {sum(1 for d in decisions if d['ae_loop_in'])}")
    print(f"  Shared-domain collisions flagged    : "
          f"{sum(1 for d in decisions if d['shared_domain_collision'])}  <- vitalant.org (incl. the buying signal)")
    print(f"  Company conflicts flagged (Class A) : "
          f"{sum(1 for d in decisions if d['company_conflict'] is True)}  <- email-domain != Organization (poison column)")
    print(f"  Org-field junk flagged (non-name)   : "
          f"{sum(1 for d in decisions if d['org_junk'])}  <- all-digit Organization: ignored + flagged (row 81)")
    pq = Counter(d["phone_quality"] for d in decisions)
    written = sum(1 for d in decisions if d["phone_e164"])
    print(f"  Phones -> SF (ok/repaired only)     : "
          f"{written} written  ["
          f"{pq.get('ok',0)} ok, {pq.get('repaired',0)} repaired, "
          f"{pq.get('no_country_code',0)} no_cc, {pq.get('junk',0)} junk, "
          f"{pq.get('too_short',0)} short, {pq.get('blank',0)} blank]  <- raw kept in audit")
    print("=" * 64)
    print(f"wrote {OUT_CSV.name} and {OUT_JSON.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
