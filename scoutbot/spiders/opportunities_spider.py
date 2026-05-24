"""
ScoutBot — opportunities_spider.py  (SWE-List Edition)
=======================================================

Scrapes DIRECT sources only — no aggregator roundups.
Every URL points to an official programme, company careers page,
foundation portal, or government scholarship body.

Coverage:
  • Nigeria-national opportunities
  • Pan-African opportunities (must be open to Nigerians)
  • Global / international — Africa, South America, Asia, Europe, West
  • Categories: Scholarships · Fellowships · Internships · Bootcamps ·
    Grants · Accelerators · Incubators · Pitch Competitions · VC Funding

Nigeria filter:
  Any opportunity that explicitly restricts eligibility to non-Nigerian
  countries or regions is dropped. Opportunities with no geographic
  restriction pass through (they are open to all).

SWE-List quality rules:
  • application_link always points directly to the apply / programme page
  • summary is a concise one-liner, not a scraped paragraph blob
  • stale / past-deadline entries are dropped at parse time
"""

import re
from datetime import date, datetime, timezone
from urllib.parse import urlparse

import scrapy
from scoutbot.items import OpportunityItem

try:
    from dateutil.parser import parse as dateutil_parse
    HAS_DATEUTIL = True
except ImportError:
    HAS_DATEUTIL = False


# ── Keyword banks ────────────────────────────────────────────────────────────

INDUSTRY_KEYWORDS = {
    "Startup": [
        "startup", "start-up", "founder", "entrepreneur", "early-stage",
        "pre-seed", "seed round", "series a", "incubator", "accelerator",
        "venture capital", "vc fund", "angel investor", "scale-up", "scaleup",
        "innovation hub", "pitch competition", "hackathon",
    ],
    "Tech": [
        "tech", "software", "coding", "developer", "data", "ai", "digital",
        "fintech", "ict", "computer", "stem", "cyber", "programming",
        "machine learning", "saas", "deeptech", "web3", "blockchain",
        "open source", "cloud", "devops",
    ],
    "Engineering": [
        "engineer", "mechanical", "civil", "electrical", "petroleum",
        "chemical", "structural", "architecture", "aerospace",
    ],
    "Law": [
        "law", "legal", "justice", "llb", "llm", "barrister", "solicitor",
        "rights", "policy", "governance",
    ],
    "Finance": [
        "finance", "fintech", "accounting", "economics", "business",
        "commerce", "banking", "investment", "microfinance", "treasury",
    ],
    "Medicine": [
        "medicine", "health", "medical", "nursing", "pharma",
        "biology", "public health", "research", "clinical", "global health",
    ],
}

CATEGORY_MAP = [
    ("venture capital",      "VC Funding"),
    ("vc fund",              "VC Funding"),
    ("seed round",           "VC Funding"),
    ("series a",             "VC Funding"),
    ("pre-seed",             "VC Funding"),
    ("angel invest",         "VC Funding"),
    ("equity investment",    "VC Funding"),
    ("incubator",            "Incubator"),
    ("accelerator",          "Accelerator"),
    ("pitch competition",    "Pitch Competition"),
    ("pitch contest",        "Pitch Competition"),
    ("startup competition",  "Pitch Competition"),
    ("startup challenge",    "Pitch Competition"),
    ("hackathon",            "Pitch Competition"),
    ("scholarship",          "Scholarship"),
    ("fellowships",          "Fellowship"),
    ("fellowship",           "Fellowship"),
    ("internship",           "Internship"),
    ("industrial training",  "Internship"),
    ("bootcamp",             "Bootcamp"),
    ("boot-camp",            "Bootcamp"),
    ("coding camp",          "Bootcamp"),
    ("apprentice",           "Apprenticeship"),
    ("conference",           "Conference"),
    ("summit",               "Conference"),
    ("grant",                "Grant"),
    ("award",                "Award"),
    ("competition",          "Competition"),
    ("programme",            "Fellowship"),
    ("program",              "Fellowship"),
    ("training",             "Internship"),
    ("funding",              "Grant"),
]

EDU_KEYWORDS = {
    "PhD":      ["phd", "doctorate", "doctoral", "post-doctoral", "postdoctoral"],
    "Masters":  ["masters", "master's", "msc", "mba", "postgraduate", "graduate"],
    "HND/OND":  ["hnd", "ond", "polytechnic", "national diploma"],
    "Bachelor": ["bachelor", "undergraduate", "bsc", "beng", "llb", "first degree"],
    "Any":      ["any level", "all levels", "open to all", "any background"],
}

# Regions used for the Range column
INTL_KEYWORDS = [
    "international", "global", "worldwide", "overseas", "study abroad",
    "uk ", " usa", "united states", "europe", "canada", "australia",
    "fully funded", "full scholarship",
    "china", "japan", "korea", "india", "asia", "singapore", "malaysia",
    "indonesia", "thailand", "taiwan", "hong kong", "vietnam",
    "brazil", "south america", "latin america",
    "germany", "france", "netherlands", "sweden", "norway",
    "commonwealth", "fulbright", "daad", "mext", "kgsp", "csc scholarship",
    "erasmus", "chevening", "gates cambridge", "rhodes",
]

PAST_YEAR_RE = re.compile(r"\b(202[0-4])\b")

# Words that flag an opportunity as explicitly excluding Nigerians / West Africa
EXCLUSION_SIGNALS = [
    "east africa only", "east african only",
    "southern africa only", "southern african only",
    "north africa only", "francophone africa",
    "lusophone", "portuguese-speaking africa",
    "us citizens only", "uk citizens only",
    "eu citizens only", "european citizens only",
    "domestic students only", "us residents only",
    "must be a us citizen", "must be a uk citizen",
]

# Category-URL fragments that signal a listing page, not a detail page
CATEGORY_URL_PATTERNS = [
    "/category/", "/tag/", "/page/", "?page=", "#comments",
    "/author/", "facebook.com/groups", "linkedin.com/company",
    "twitter.com", "/feed/", ".rss",
]


# ── Helper functions ─────────────────────────────────────────────────────────

def is_category_url(url: str) -> bool:
    u = url.lower()
    return any(p in u for p in CATEGORY_URL_PATTERNS)


def infer_industry(text: str) -> str:
    t = text.lower()
    for industry, kws in INDUSTRY_KEYWORDS.items():
        if any(kw in t for kw in kws):
            return industry
    return "General"


def infer_category(url: str, text: str) -> str:
    combined = (url + " " + text).lower()
    for kw, cat in CATEGORY_MAP:
        if kw in combined:
            return cat
    return "Opportunity"


def infer_range(text: str) -> str:
    t = text.lower()
    if any(kw in t for kw in INTL_KEYWORDS):
        return "International"
    return "National"


def infer_edu(text: str, industry: str) -> str:
    t = text.lower()
    for level, kws in EDU_KEYWORDS.items():
        if any(kw in t for kw in kws):
            return level
    return "Any" if industry == "Startup" else "Bachelor"


def extract_deadline(text: str) -> str:
    patterns = [
        r"deadline[:\s]+([A-Za-z]+ \d{1,2},?\s*\d{4})",
        r"apply by[:\s]+([A-Za-z]+ \d{1,2},?\s*\d{4})",
        r"closes?[:\s]+([A-Za-z]+ \d{1,2},?\s*\d{4})",
        r"closing date[:\s]+([A-Za-z]+ \d{1,2},?\s*\d{4})",
        r"(\d{1,2} [A-Za-z]+ \d{4})",
        r"([A-Za-z]+ \d{1,2},?\s*\d{4})",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def is_expired(deadline_str: str, title: str = "") -> bool:
    today = date.today()
    if title:
        for m in PAST_YEAR_RE.finditer(title):
            if int(m.group(1)) < today.year:
                return True
    if not deadline_str or not HAS_DATEUTIL:
        return False
    try:
        d = dateutil_parse(deadline_str, fuzzy=True, dayfirst=False).date()
        return d < today
    except Exception:
        return False


def is_nigeria_excluded(text: str) -> bool:
    """Return True if the text explicitly excludes Nigeria / West Africa."""
    t = text.lower()
    return any(sig in t for sig in EXCLUSION_SIGNALS)


def org_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.replace("www.", "")
        return host.split(".")[0].title()
    except Exception:
        return ""


# ── Spider ───────────────────────────────────────────────────────────────────

class OpportunitiesSpider(scrapy.Spider):
    name = "opportunities"

    # ------------------------------------------------------------------
    # DIRECT SOURCE URLS  —  SWE-List style
    # Every URL is an official programme page, careers portal, or
    # government scholarship body. No aggregators.
    # ------------------------------------------------------------------
    # Structure: (url, label, category_hint, org_name)
    # category_hint and org_name allow us to set metadata without
    # relying solely on keyword inference when the page text is sparse.
    # ------------------------------------------------------------------

    DIRECT_SOURCES = [

        # ══════════════════════════════════════════════════════════════
        # NIGERIA — NATIONAL SCHOLARSHIPS & GRANTS
        # ══════════════════════════════════════════════════════════════
        ("https://fsb.gov.ng/",
            "Federal Scholarship Board Nigeria", "Scholarship", "Federal Scholarship Board"),
        ("https://www.tetfund.gov.ng/index.php/scholarship",
            "TETFund Scholarship", "Scholarship", "TETFund"),
        ("https://nitda.gov.ng/",
            "NITDA Digital Skills & Grants", "Grant", "NITDA"),
        ("https://boi.ng/product/",
            "Bank of Industry Funding", "Grant", "Bank of Industry"),
        ("https://www.mtnfoundation.ng/programmes/scholarship/",
            "MTN Foundation Scholarship", "Scholarship", "MTN Foundation"),
        ("https://www.accessbankplc.com/CorporateSocialResponsibility/education",
            "Access Bank Education Grant", "Grant", "Access Bank"),

        # ══════════════════════════════════════════════════════════════
        # PAN-AFRICAN SCHOLARSHIPS & FELLOWSHIPS
        # (open to Nigerians; continent-wide eligibility)
        # ══════════════════════════════════════════════════════════════
        ("https://mastercardfdn.org/all-programs/scholars-program/",
            "Mastercard Foundation Scholars Program", "Scholarship", "Mastercard Foundation"),
        ("https://www.africanleadershipacademy.org/admissions/",
            "African Leadership Academy Fellowship", "Fellowship", "ALA"),
        ("https://alueducation.com/admissions/scholarships/",
            "African Leadership University Scholarship", "Scholarship", "ALU"),
        ("https://www.tonyelumelufoundation.org/teep",
            "Tony Elumelu Entrepreneurship Programme", "Grant", "Tony Elumelu Foundation"),
        ("https://www.zindabanifoundation.org/scholarships",
            "Zindabani Foundation Scholarship", "Scholarship", "Zindabani Foundation"),
        ("https://www.opensocietyfoundations.org/grants",
            "Open Society Foundations Grants", "Grant", "Open Society Foundations"),
        ("https://www.mo.ibrahim.foundation/fellowship",
            "Mo Ibrahim Foundation Fellowship", "Fellowship", "Mo Ibrahim Foundation"),

        # ══════════════════════════════════════════════════════════════
        # INTERNATIONAL — EUROPE
        # ══════════════════════════════════════════════════════════════
        ("https://www.chevening.org/scholarships/",
            "Chevening Scholarship (UK)", "Scholarship", "Chevening / FCDO"),
        ("https://cscuk.fcdo.gov.uk/scholarships/",
            "Commonwealth Scholarship (UK)", "Scholarship", "Commonwealth Scholarship Commission"),
        ("https://www.gatescambridge.org/apply/",
            "Gates Cambridge Scholarship", "Scholarship", "Gates Cambridge Trust"),
        ("https://www.rhodeshouse.ox.ac.uk/scholarships/apply/",
            "Rhodes Scholarship", "Scholarship", "Rhodes Trust"),
        ("https://www.daad.de/en/study-and-research-in-germany/scholarships/",
            "DAAD Scholarship (Germany)", "Scholarship", "DAAD"),
        ("https://erasmus-plus.ec.europa.eu/opportunities",
            "Erasmus+ Scholarships (EU)", "Scholarship", "European Commission"),
        ("https://www.studyinholland.nl/scholarships",
            "Netherlands Scholarships (Holland)", "Scholarship", "Nuffic / Holland"),
        ("https://www.universitiesscotland.ac.uk/international/scholarships/",
            "Scotland Scholarships", "Scholarship", "Universities Scotland"),
        ("https://www.si.se/en/apply/scholarships/",
            "Swedish Institute Scholarship", "Scholarship", "Swedish Institute"),
        ("https://www.norad.no/en/front/funding/scholarships/",
            "Norwegian Government Scholarship", "Scholarship", "Norad Norway"),
        ("https://www.science-without-borders.com/",
            "Brazil Scientific Mobility Program", "Scholarship", "Science Without Borders"),

        # ══════════════════════════════════════════════════════════════
        # INTERNATIONAL — NORTH AMERICA (USA / CANADA)
        # ══════════════════════════════════════════════════════════════
        ("https://ng.usembassy.gov/education-culture/fulbright-program/",
            "Fulbright Scholarship (Nigeria)", "Scholarship", "US Embassy Abuja"),
        ("https://www.humphreyfellowship.org/application",
            "Hubert H. Humphrey Fellowship (USA)", "Fellowship", "Humphrey Fellows"),
        ("https://yalinetwork.state.gov/",
            "YALI Mandela Washington Fellowship", "Fellowship", "YALI / US Dept of State"),
        ("https://www.aauw.org/resources/programs/fellowships-grants/",
            "AAUW Fellowship (USA — Women)", "Fellowship", "AAUW"),
        ("https://www.idrc.ca/en/funding",
            "IDRC Research Grants (Canada)", "Grant", "IDRC Canada"),
        ("https://scholarships.gc.ca/schol-bours/home-accueil.aspx?lang=eng",
            "Vanier Canada Graduate Scholarship", "Scholarship", "Government of Canada"),

        # ══════════════════════════════════════════════════════════════
        # INTERNATIONAL — ASIA
        # ══════════════════════════════════════════════════════════════
        ("https://www.campuschina.org/scholarships/index.html",
            "Chinese Government Scholarship (CSC)", "Scholarship", "Chinese Ministry of Education"),
        ("https://www.studyinjapan.go.jp/en/smap_ugrad-e/",
            "MEXT Scholarship Japan", "Scholarship", "Japanese MEXT"),
        ("https://www.studyinkorea.go.kr/en/scholarships/GKS_Scholarship.do",
            "Korean Government Scholarship (KGSP)", "Scholarship", "Korean NIIED"),
        ("https://www.iccr.gov.in/scholarships",
            "Indian ICCR Scholarship", "Scholarship", "ICCR India"),
        ("https://www.adb.org/work-with-us/careers/japan-scholarship-program",
            "ADB Japan Scholarship Program", "Scholarship", "Asian Development Bank"),
        ("https://www.nus.edu.sg/oam/scholarships/scholarships-for-international-students",
            "NUS Singapore International Scholarships", "Scholarship", "NUS Singapore"),
        ("https://www.ntu.edu.sg/admissions/global/international-scholarships",
            "NTU Singapore International Scholarships", "Scholarship", "NTU Singapore"),
        ("https://www.tw.org/moststudy/",
            "Taiwan MOE Scholarship", "Scholarship", "Taiwan Ministry of Education"),

        # ══════════════════════════════════════════════════════════════
        # INTERNATIONAL — SOUTH AMERICA
        # ══════════════════════════════════════════════════════════════
        ("https://www.cnpq.br/web/guest/chamadas-publicas",
            "CNPq Research Grants (Brazil)", "Grant", "CNPq Brazil"),
        ("https://www.oea.org/en/scholarships",
            "OAS Scholarships (Latin America)", "Scholarship", "Organization of American States"),
        ("https://www.fundayacucho.gob.ve/becas/",
            "Fundayacucho Scholarship (Venezuela)", "Scholarship", "Fundayacucho"),

        # ══════════════════════════════════════════════════════════════
        # TECH INTERNSHIPS — GLOBAL COMPANIES (open to Nigerians)
        # ══════════════════════════════════════════════════════════════
        ("https://buildyourfuture.withgoogle.com/programs/step",
            "Google STEP Internship", "Internship", "Google"),
        ("https://summerofcode.withgoogle.com/",
            "Google Summer of Code", "Internship", "Google"),
        ("https://careers.microsoft.com/us/en/ur-lp-msinternships",
            "Microsoft Internship Program", "Internship", "Microsoft"),
        ("https://www.metacareers.com/careerprograms/pathways/metauniversity",
            "Meta University Internship", "Internship", "Meta"),
        ("https://www.amazon.jobs/en/landing_pages/software-development-student-programs",
            "Amazon Student Programs (SDE)", "Internship", "Amazon"),
        ("https://fellowship.mlh.io/",
            "MLH Fellowship (Open Source)", "Fellowship", "MLH"),
        ("https://outreachy.org/",
            "Outreachy Internship (Open Source)", "Internship", "Outreachy"),
        ("https://www.gsoc-africa.dev/",
            "GSoC Africa Initiative", "Internship", "GSoC Africa"),
        ("https://andela.com/ats/",
            "Andela Tech Fellows Program", "Fellowship", "Andela"),
        ("https://www.awsrestart.com/",
            "AWS re/Start (Cloud Training)", "Bootcamp", "Amazon Web Services"),
        ("https://grow.google/programs/",
            "Google Career Certificates & Programs", "Bootcamp", "Google"),
        ("https://www.futurelearn.com/programs/digital-skills-africa",
            "FutureLearn Digital Skills for Africa", "Bootcamp", "FutureLearn"),

        # ══════════════════════════════════════════════════════════════
        # STARTUP FUNDING — GLOBAL ACCELERATORS & VC PROGRAMS
        # ══════════════════════════════════════════════════════════════
        ("https://www.ycombinator.com/apply/",
            "Y Combinator (Global)", "Accelerator", "Y Combinator"),
        ("https://www.techstars.com/accelerators",
            "Techstars Accelerators (Global)", "Accelerator", "Techstars"),
        ("https://startup.google.com/programs/accelerator/africa/",
            "Google for Startups Accelerator Africa", "Accelerator", "Google"),
        ("https://developers.facebook.com/startups/",
            "Meta Startup Hub", "Accelerator", "Meta"),
        ("https://www.microsoftforstartups.com/",
            "Microsoft for Startups Founders Hub", "Accelerator", "Microsoft"),
        ("https://www.500.co/accelerators",
            "500 Global Accelerator", "Accelerator", "500 Global"),
        ("https://www.seedstars.com/programs/",
            "Seedstars Africa Programs", "Accelerator", "Seedstars"),
        ("https://villageapital.com/programs/",
            "Village Capital Programs", "Accelerator", "Village Capital"),
        ("https://www.startupgrind.com/accelerate/",
            "Startup Grind Accelerate", "Accelerator", "Startup Grind"),
        ("https://www.tef.africa/",
            "Tony Elumelu Foundation Entrepreneurship", "Grant", "TEF"),
        ("https://www.norrsken.org/africa-accelerator",
            "Norrsken Africa Accelerator", "Accelerator", "Norrsken Foundation"),
        ("https://www.vc4a.com/programs/",
            "VC4A Venture Finance Africa", "VC Funding", "VC4A"),

        # ══════════════════════════════════════════════════════════════
        # RESEARCH FELLOWSHIPS — GLOBAL
        # ══════════════════════════════════════════════════════════════
        ("https://www.africanacademyofsciences.org/funding-opportunities/",
            "AAS Research Grants", "Grant", "African Academy of Sciences"),
        ("https://www.wellcome.org/grant-funding",
            "Wellcome Trust Grant Funding", "Grant", "Wellcome Trust"),
        ("https://www.gatesfoundation.org/about/how-we-work/general-information/grant-opportunities",
            "Bill & Melinda Gates Foundation Grants", "Grant", "Gates Foundation"),
        ("https://www.hewlett.org/grants/",
            "Hewlett Foundation Grants", "Grant", "Hewlett Foundation"),
        ("https://www.worldbank.org/en/programs/scholarships",
            "World Bank Scholarships Program", "Scholarship", "World Bank"),
        ("https://www.undp.org/funding/calls-for-proposals",
            "UNDP Calls for Proposals", "Grant", "UNDP"),
        ("https://www.un.org/en/academic-impact/page/scholarships",
            "UN Academic Impact Scholarships", "Scholarship", "United Nations"),

        # ══════════════════════════════════════════════════════════════
        # LAW & POLICY FELLOWSHIPS
        # ══════════════════════════════════════════════════════════════
        ("https://www.law.columbia.edu/admissions/graduate-legal-studies/llm-funding",
            "Columbia LLM Funding (USA)", "Fellowship", "Columbia Law School"),
        ("https://www.law.ox.ac.uk/admissions/graduate/scholarships",
            "Oxford Law Scholarships (UK)", "Scholarship", "Oxford University"),
        ("https://www.amnesty.org/en/get-involved/internships/",
            "Amnesty International Internship", "Internship", "Amnesty International"),
        ("https://africanlegalaid.net/opportunities/",
            "African Legal Aid Opportunities", "Fellowship", "African Legal Aid"),

        # ══════════════════════════════════════════════════════════════
        # MEDICINE & HEALTH FELLOWSHIPS
        # ══════════════════════════════════════════════════════════════
        ("https://www.who.int/about/funding/contributor",
            "WHO Fellowship Programs", "Fellowship", "World Health Organization"),
        ("https://www.nih.gov/grants-funding",
            "NIH Research Grants (USA)", "Grant", "NIH"),
        ("https://africacdc.org/opportunities/",
            "Africa CDC Opportunities", "Fellowship", "Africa CDC"),
        ("https://www.gheli.org/fellowships",
            "GHELI Global Health Fellowship", "Fellowship", "GHELI"),

        # ══════════════════════════════════════════════════════════════
        # WOMEN & UNDERREPRESENTED GROUPS
        # ══════════════════════════════════════════════════════════════
        ("https://www.anitab.org/award-programs/",
            "AnitaB.org Awards & Programs", "Award", "AnitaB.org"),
        ("https://womentechmakers.com/scholars",
            "Google Women Techmakers Scholars", "Scholarship", "Google"),
        ("https://www.globalfundforwomen.org/apply-for-a-grant/",
            "Global Fund for Women Grant", "Grant", "Global Fund for Women"),
        ("https://www.amujere.com/",
            "Amujere Women in Tech Scholarship", "Scholarship", "Amujere"),
    ]

    MAX_PAGES = 2

    # ------------------------------------------------------------------
    # Request generation
    # ------------------------------------------------------------------

    def start_requests(self):
        for url, label, category_hint, org_name in self.DIRECT_SOURCES:
            yield scrapy.Request(
                url,
                callback=self.parse_opportunity,
                meta={
                    "label": label,
                    "category_hint": category_hint,
                    "org_name": org_name,
                    "source_url": url,
                },
                errback=self.handle_error,
            )

    def handle_error(self, failure):
        self.logger.warning(f"Request failed: {failure.request.url} — {failure.value}")

    # ------------------------------------------------------------------
    # Parse an opportunity / programme page
    # ------------------------------------------------------------------

    def parse_opportunity(self, response):
        meta          = response.meta
        label         = meta.get("label", "")
        category_hint = meta.get("category_hint", "")
        org_name      = meta.get("org_name", "") or org_from_url(response.url)
        source_url    = meta.get("source_url", response.url)

        # ── Title ──────────────────────────────────────────────────────
        title = (
            response.css(
                "h1.entry-title::text, h1.post-title::text, "
                "h1.page-title::text, h1::text"
            ).get("").strip()
            or response.css("title::text").get("").strip()
            or label  # fall back to our own label if page title is empty
        )

        if not title:
            return

        # ── Body text (for inference + deadline extraction) ────────────
        full_text = " ".join(
            response.css(
                "article p::text, .entry-content p::text, "
                ".post-content p::text, main p::text, "
                "section p::text, .content p::text"
            ).getall()
        )

        combined = title + " " + full_text + " " + label

        # ── Staleness check ────────────────────────────────────────────
        deadline_str = extract_deadline(combined)
        if is_expired(deadline_str, title):
            self.logger.debug(f"Dropped (expired): {title}")
            return

        year_in_title = PAST_YEAR_RE.search(title)
        if year_in_title and int(year_in_title.group(1)) < date.today().year:
            return

        # ── Nigeria / exclusion filter ─────────────────────────────────
        if is_nigeria_excluded(combined):
            self.logger.debug(f"Dropped (Nigeria excluded): {title}")
            return

        # ── Infer metadata ─────────────────────────────────────────────
        industry = infer_industry(combined)

        # Use category_hint from DIRECT_SOURCES metadata first; fall back to inference
        category = category_hint or infer_category(response.url, combined)

        # Build a clean one-line summary (SWE-List style: no paragraph blobs)
        # Use the meta description if available; otherwise take the first sentence.
        meta_desc = response.css(
            "meta[name='description']::attr(content), "
            "meta[property='og:description']::attr(content)"
        ).get("").strip()

        if meta_desc:
            summary = meta_desc[:200].rstrip(".")
        elif full_text:
            first_sentence = re.split(r"(?<=[.!?])\s", full_text.strip())[0]
            summary = first_sentence[:200].rstrip(".")
        else:
            summary = label  # absolute fallback

        # ── Apply link ─────────────────────────────────────────────────
        # Prefer explicit apply / application / register links on the page.
        # Fall back to the source URL from DIRECT_SOURCES.
        apply_link = (
            response.css(
                "a[href*='apply']::attr(href), "
                "a[href*='application']::attr(href), "
                "a[href*='register']::attr(href), "
                "a[href*='apply-now']::attr(href)"
            ).get("")
            or source_url
        )

        # Make relative URLs absolute
        if apply_link and not apply_link.startswith("http"):
            apply_link = response.urljoin(apply_link)

        # ── Assemble item ──────────────────────────────────────────────
        item = OpportunityItem()
        item["title"]            = title
        item["industry"]         = industry
        item["category"]         = category
        item["range"]            = infer_range(combined + " " + label)
        item["education_level"]  = infer_edu(combined, industry)
        item["organization"]     = org_name
        item["summary"]          = summary
        item["application_link"] = apply_link
        item["opening_date"]     = ""
        item["deadline"]         = deadline_str
        item["status"]           = "Open"

        yield item

        # ── Follow paginated listing pages (max 2 pages) ───────────────
        current_page = int(response.meta.get("page", 1))
        if current_page < self.MAX_PAGES:
            next_page = response.css(
                "a.next.page-numbers::attr(href), "
                "a[rel='next']::attr(href), "
                "a.next::attr(href)"
            ).get()
            if next_page and not is_category_url(next_page):
                yield response.follow(
                    next_page,
                    self.parse_opportunity,
                    meta={**meta, "page": current_page + 1},
                )
