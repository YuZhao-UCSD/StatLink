#!/usr/bin/env python3
"""
Shared high-precision entity resolution, canonical school normalization, doctoral-degree
filtering, field classification, and SQLite upsert engine for StatLink's multi-source
global ETD, national thesis, departmental alumni, ORCID/PubMed, and faculty web harvesters.
"""

import html as html_mod
import os
import re
import sqlite3
import unicodedata
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
CACHE_DIR = os.path.join(BASE_DIR, "data_raw", "global_crawl")
os.makedirs(CACHE_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "StatLinkAcademicExplorer/3.0 (https://yuzhao-ucsd.github.io/StatLink/; mailto:statlink.research@gmail.com)",
    "Accept": "application/json, application/xml, text/xml, text/html, */*",
}

# ---------------------------------------------------------------------------
# Strict Doctoral-Only & Statistical Field Classifiers
# ---------------------------------------------------------------------------

MASTERS_OR_UNDERGRAD_RE = re.compile(
    r"\b(master(?:'s|\s+of|\s+in|\s+degree|\s+thesis)?|m\.?\s*s\.?(?:\s|$|,)|m\.?\s*a\.?(?:\s|$|,)|"
    r"m\.?\s*sc\.?|m\.?\s*stat\.?|m\.?\s*p\.?\s*h\.?|m\.?\s*s\.?\s*p\.?|m\.?\s*eng\.?|m\.?\s*b\.?\s*a\.?|"
    r"m\.?\s*f\.?\s*a\.?|m\.?\s*phil\.?|m\.?\s*res\.?|s\.?\s*m\.?(?:\s|$|,)|b\.?\s*s\.?(?:\s|$|,)|"
    r"b\.?\s*a\.?(?:\s|$|,)|b\.?\s*sc\.?|bachelor|undergraduate|honors\s+thesis|senior\s+thesis|"
    r"capstone|practicum|m[eé]moire\s+de\s+master|dipl[oô]me|licentiate|kandidatuppsats|magisteruppsats|"
    r"examensarbete|bachelorarbeit|masterarbeit|diplomarbeit)\b",
    re.I,
)

DOCTORAL_POS_RE = re.compile(
    r"\b(ph\.?\s*d\.?|doctor\s+of\s+philosophy|doctoral|dissertation|sc\.?\s*d\.?|d\.?\s*phil\.?|"
    r"dr\.?\s*p\.?\s*h\.?|dr\.?\s*sc\.?|dr\.?\s*rer\.?\s*nat\.?|doctorat|th[eè]se\s+de\s+doctorat|"
    r"doktorsavhandling|proefschrift|doctor\s+of\s+science)\b",
    re.I,
)

CORE_BIOSTAT_RE = re.compile(
    r"\b(biostatist\w*|biometr\w*|statistical\s+genetics|genetic\s+statistics|clinical\s+trials?|"
    r"survival\s+analysis|proportional\s+hazards|epidemiolog\w*\s+methods|causal\s+mediation|"
    r"longitudinal\s+data|competing\s+risks|biomarker\w*|genomic\w*\s+statist\w*|pharmacometr\w*)\b",
    re.I,
)

CORE_PROB_RE = re.compile(
    r"\b(probabilit\w*|stochastic\s+process\w*|stochastic\s+calculus|stochastic\s+differential|"
    r"stochastic\s+control|stochastic\s+analys\w*|markov\s+chain\w*|markov\s+process\w*|"
    r"martingale\w*|brownian\s+motion|l[eé]vy\s+process\w*|percolation|random\s+matri\w*|"
    r"random\s+walk\w*|random\s+graph\w*|interacting\s+particle|large\s+deviations|"
    r"queueing|queuing|branching\s+process\w*|ergodic\s+theory|gaussian\s+free\s+field|"
    r"optimal\s+transport|stochastik|probabilit[eé]s)\b",
    re.I,
)

CORE_STAT_DEPT_RE = re.compile(
    r"\b(statist\w*|biostatist\w*|probabilit\w*|stochastik|statistique|biostatistique|"
    r"statistical\s+science\w*|statistics\s+and\s+data\s+science|statistics\s+and\s+applied\s+probability|"
    r"statistics\s+and\s+probability|statistics\s+and\s+actuarial|biometry)\b",
    re.I,
)

CORE_STAT_TITLE_RE = re.compile(
    r"\b(statist\w*|biostat\w*|probab\w*|stochastic\w*|bayesian|nonparametric|semiparametric|"
    r"causal\s+inference|propensity\s+score|instrumental\s+variable\w*|treatment\s+effect\w*|"
    r"high[\-\s]dimensional|false\s+discovery|multiple\s+testing|conformal\s+prediction|"
    r"minimax|lasso|penalized\s+regression|quantile\s+regression|mixed[\-\s]effects|"
    r"generalized\s+linear|functional\s+data|spatial\s+statist\w*|time\s+series|garch|arima|"
    r"survival\s+analysis|cox\s+model|kaplan[\-\s]meier|missing\s+data|multiple\s+imputation|"
    r"monte\s+carlo|mcmc|variational\s+inference|bootstrap|empirical\s+bayes|hierarchical\s+bayes|"
    r"gaussian\s+process\w*|graphical\s+model\w*|latent\s+variable\w*|factor\s+model\w*|"
    r"change[\-\s]?point|changepoint|experimental\s+design|optimal\s+design|survey\s+sampling|"
    r"small\s+area\s+estimation|uncertainty\s+quantification|density\s+estimation|kernel\s+smoothing|"
    r"asymptotic\s+theory|m[\-\s]estimat\w*|maximum\s+likelihood|em\s+algorithm|expectation[\-\s]maximization|"
    r"markov|martingale|brownian|random\s+matri\w*|econometr\w*|psychometr\w*)\b",
    re.I,
)

RELATED_QUANT_RE = re.compile(
    r"\b(data\s+science|machine\s+learning|econometr\w*|psychometr\w*|actuar\w*|operations\s+research|"
    r"quantitative\s+finance|mathematical\s+finance|financial\s+mathematics|bioinformatics|"
    r"computational\s+biology|applied\s+mathematics|reinforcement\s+learning|bandit\w*|deep\s+learning)\b",
    re.I,
)

NON_STAT_NOISE_DEPT_RE = re.compile(
    r"\b(english|history|music|theatre|theater|art\s+history|literature|philosophy|religion|theology|"
    r"nursing|social\s+work|education(?:al)?\s+leadership|curriculum|counseling|kinesiology|"
    r"architecture|urban\s+planning|journalism|communication\s+studies|anthropology|archaeology|"
    r"french|spanish|german|classics|film\s+studies|veterinary|dentistry|pharmacy\s+practice|"
    r"soil\s+science|plant\s+pathology|horticulture|forestry|animal\s+science|food\s+science|"
    r"mechanical\s+engineering|civil\s+engineering|chemical\s+engineering|aerospace\s+engineering|"
    r"materials\s+science|geology|earth\s+sciences|chemistry|biochemistry|microbiology|immunology|"
    r"pharmacology|anatomy|physiology|sociology|political\s+science|psychology|law)\b",
    re.I,
)


def is_doctoral_degree(degree_text: str, title: str = "", require_positive: bool = False) -> bool:
    """Return True only if record is a doctoral dissertation (rejects Master's/Bachelor's/Honors)."""
    dt = (degree_text or "").strip()
    tl = (title or "").strip()
    if MASTERS_OR_UNDERGRAD_RE.search(dt) and not DOCTORAL_POS_RE.search(dt):
        return False
    if MASTERS_OR_UNDERGRAD_RE.search(tl) and not DOCTORAL_POS_RE.search(dt):
        return False
    if require_positive:
        return bool(DOCTORAL_POS_RE.search(dt))
    return True


def classify_stat_field(dept_or_subj: str, title: str, advisor_is_core: bool = False):
    """
    Classify a PhD dissertation into ('Statistics'|'Biostatistics'|'Probability'|'Related Quantitative'|None).
    Returns None if the dissertation is clearly outside Statistics/Biostatistics/Probability/Related Quantitative.
    """
    ds = (dept_or_subj or "").strip()
    tl = (title or "").strip()
    combined = f"{ds} {tl}"

    # Strong department/discipline signal
    is_stat_dept = bool(CORE_STAT_DEPT_RE.search(ds))
    is_noise_dept = bool(NON_STAT_NOISE_DEPT_RE.search(ds)) and not is_stat_dept

    if is_noise_dept:
        # Only keep a non-stat department thesis if the advisor is Core Stat AND title is clearly statistical
        if not (advisor_is_core and CORE_STAT_TITLE_RE.search(tl)):
            return None

    if CORE_BIOSTAT_RE.search(ds):
        return "Biostatistics"
    if re.search(r"\b(probabilit\w*|stochastic\w*|stochastik|probabilit[eé]s)\b", ds, re.I) and not re.search(r"\bstatist", ds, re.I):
        return "Probability"
    if is_stat_dept:
        if CORE_BIOSTAT_RE.search(tl):
            return "Biostatistics"
        if CORE_PROB_RE.search(tl) and not re.search(r"\b(statist\w*|estimat\w*|regression|bayesian|causal|inference)\b", tl, re.I):
            return "Probability"
        return "Statistics"

    # Title-level classification when department isn't explicitly "Statistics"
    if CORE_BIOSTAT_RE.search(tl):
        return "Biostatistics"
    if CORE_PROB_RE.search(tl):
        return "Probability"
    if CORE_STAT_TITLE_RE.search(tl):
        return "Statistics"
    if advisor_is_core and RELATED_QUANT_RE.search(combined):
        return "Related Quantitative"
    if RELATED_QUANT_RE.search(ds) and not is_noise_dept:
        return "Related Quantitative"
    return None


# ---------------------------------------------------------------------------
# Name Cleaning & Normalization
# ---------------------------------------------------------------------------

def fold_ascii(text: str) -> str:
    if not text:
        return ""
    text = html_mod.unescape(str(text))
    text = (
        text.replace("ø", "o").replace("Ø", "O")
        .replace("ł", "l").replace("Ł", "L")
        .replace("ß", "ss").replace("đ", "d").replace("Đ", "D")
        .replace("æ", "ae").replace("Æ", "AE")
        .replace("œ", "oe").replace("Œ", "OE")
    )
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


DEGREE_TAIL_RE = re.compile(
    r",?\s*(?:Ph\.?\s*D\.?|Sc\.?\s*D\.?|D\.?\s*Phil\.?|Dr\.?\s*P\.?\s*H\.?|M\.?\s*D\.?|F\.?R\.?S\.?)\b.*$",
    re.I,
)
ROLE_PAREN_RE = re.compile(
    r"\s*\((?:advisor|co-advisor|chair|co-chair|supervisor|director|main adviser|committee|thesis|phd|ph\.d\.|dr\.|prof\.|[\d\-\s]{4,})\s*[^)]*\)",
    re.I,
)
DATES_TAIL_RE = re.compile(r",?\s*\b(?:18|19|20)\d{2}\s*[-–]?\s*(?:(?:18|19|20)\d{2})?\s*$")
BAD_NAME_WORDS = (
    "university", "department", "institute", "college", "school of", "library",
    "committee", "unknown", "anonymous", "faculty", "program", "graduate",
    "center for", "centre for", "division of", "laboratory", "hospital",
    "statistics", "biostatistics", "mathematics", "dissertation", "thesis",
    "http", "www.", "@", "editor", "publisher", "proquest", "copyright",
)


def clean_person_name(raw: str) -> str:
    if not raw:
        return ""
    s = html_mod.unescape(str(raw)).strip()
    s = re.sub(r"<[^>]+>", " ", s)
    s = ROLE_PAREN_RE.sub("", s)
    s = DEGREE_TAIL_RE.sub("", s)
    s = DATES_TAIL_RE.sub("", s)
    s = re.sub(r"\b(?:Prof\.?|Professor|Dr\.?|Assoc\.?\s*Prof\.?|Asst\.?\s*Prof\.?|Sir|Dame)\s+", "", s, flags=re.I)
    s = re.sub(r"\s*\[[^\]]*\]\s*", " ", s)
    s = s.strip(" ,.;:-•*|/")
    if not s:
        return ""

    # Convert "Last, First Middle" -> "First Middle Last"
    if "," in s:
        parts = [p.strip(" .") for p in s.split(",") if p.strip(" .")]
        if len(parts) == 2:
            last, first = parts[0], parts[1]
            if first.lower() in ("jr", "sr", "ii", "iii", "iv"):
                s = f"{last} {first}."
            else:
                s = f"{first} {last}"
        elif len(parts) == 3 and parts[1].lower().rstrip(".") in ("jr", "sr", "ii", "iii", "iv"):
            s = f"{parts[2]} {parts[0]} {parts[1]}"
        elif len(parts) >= 2:
            s = f"{parts[1]} {parts[0]}"

    s = re.sub(r"\s+", " ", s).strip(" ,.;:-")
    # Fix ALL-CAPS or all-lowercase names
    if s.isupper() or s.islower():
        s = " ".join(w.capitalize() if len(w) > 1 else w.upper() for w in s.split())
    sl = s.lower()
    if any(w in sl for w in BAD_NAME_WORDS):
        return ""
    if re.search(r"\d", s):
        return ""
    toks = [t for t in s.split() if len(t.strip(".")) >= 1]
    if len(toks) < 2 or len(toks) > 6 or len(s) < 4 or len(s) > 72:
        return ""
    # Reject single-letter last names (like "John S.")
    if len(toks[-1].strip(".")) < 2:
        return ""
    return s


def split_advisor_field(raw_val: str):
    if not raw_val:
        return []
    txt = html_mod.unescape(str(raw_val)).strip()
    if not txt:
        return []
    # Split on newline, semicolon, pipe, or " and " (when not "Last, First and Last, First" ambiguity)
    if "\n" in txt:
        chunks = txt.splitlines()
    elif ";" in txt:
        chunks = txt.split(";")
    elif "|" in txt:
        chunks = txt.split("|")
    elif " and " in txt:
        chunks = re.split(r"\s+and\s+", txt, flags=re.I)
    elif " & " in txt:
        chunks = txt.split(" & ")
    else:
        chunks = [txt]
    out = []
    seen = set()
    for c in chunks:
        nm = clean_person_name(c)
        if nm:
            nk = norm_key(nm)
            if nk and nk not in seen:
                seen.add(nk)
                out.append(nm)
    return out


def clean_title(raw: str) -> str:
    if not raw:
        return ""
    s = html_mod.unescape(str(raw))
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:450]


def norm_key(name: str) -> str:
    s = re.sub(r"\([^)]*\)", " ", str(name or ""))
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", " ", fold_ascii(s).lower())
    s = re.sub(r"[^a-z0-9\s\-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def first_last_sig(name: str):
    nk = norm_key(name)
    toks = [t.strip("-") for t in nk.split() if t.strip("-")]
    if len(toks) < 2:
        return "", ""
    return toks[0], toks[-1]


# ---------------------------------------------------------------------------
# Canonical School Normalization & High-Precision Database Upserter
# ---------------------------------------------------------------------------

MANUAL_SCHOOL_ALIASES = {
    "eth zurich": "ETH Zürich",
    "eth zürich": "ETH Zürich",
    "eidgenossische technische hochschule zurich": "ETH Zürich",
    "swiss federal institute of technology zurich": "ETH Zürich",
    "epfl": "École Polytechnique Fédérale de Lausanne",
    "ecole polytechnique federale de lausanne": "École Polytechnique Fédérale de Lausanne",
    "mit": "Massachusetts Institute of Technology",
    "uc berkeley": "University of California, Berkeley",
    "university of california berkeley": "University of California, Berkeley",
    "ucla": "University of California, Los Angeles",
    "ucsd": "University of California, San Diego",
    "uc davis": "University of California, Davis",
    "ucsb": "University of California, Santa Barbara",
    "uc irvine": "University of California, Irvine",
    "ucsc": "University of California, Santa Cruz",
    "ucsf": "University of California, San Francisco",
    "uc riverside": "University of California, Riverside",
    "uc merced": "University of California, Merced",
    "cmu": "Carnegie Mellon University",
    "upenn": "University of Pennsylvania",
    "wharton": "University of Pennsylvania",
    "unc": "University of North Carolina at Chapel Hill",
    "unc chapel hill": "University of North Carolina at Chapel Hill",
    "university of north carolina": "University of North Carolina at Chapel Hill",
    "ncsu": "North Carolina State University",
    "nc state": "North Carolina State University",
    "north carolina state": "North Carolina State University",
    "uw": "University of Washington",
    "uw madison": "University of Wisconsin-Madison",
    "university of wisconsin madison": "University of Wisconsin-Madison",
    "university of wisconsin": "University of Wisconsin-Madison",
    "uiuc": "University of Illinois Urbana-Champaign",
    "university of illinois at urbana champaign": "University of Illinois Urbana-Champaign",
    "university of illinois urbana champaign": "University of Illinois Urbana-Champaign",
    "ut austin": "University of Texas at Austin",
    "the university of texas at austin": "University of Texas at Austin",
    "tamu": "Texas A&M University",
    "texas a and m university": "Texas A&M University",
    "texas a&m": "Texas A&M University",
    "osu": "Ohio State University",
    "the ohio state university": "Ohio State University",
    "psu": "Pennsylvania State University",
    "penn state": "Pennsylvania State University",
    "penn state university": "Pennsylvania State University",
    "the pennsylvania state university": "Pennsylvania State University",
    "jhu": "Johns Hopkins University",
    "the johns hopkins university": "Johns Hopkins University",
    "johns hopkins bloomberg school of public health": "Johns Hopkins University",
    "harvard": "Harvard University",
    "harvard t h chan school of public health": "Harvard University",
    "stanford": "Stanford University",
    "cornell": "Cornell University",
    "columbia": "Columbia University",
    "columbia university in the city of new york": "Columbia University",
    "uchicago": "University of Chicago",
    "the university of chicago": "University of Chicago",
    "umich": "University of Michigan",
    "university of michigan ann arbor": "University of Michigan",
    "duke": "Duke University",
    "yale": "Yale University",
    "princeton": "Princeton University",
    "nyu": "New York University",
    "usc": "University of Southern California",
    "rutgers": "Rutgers University",
    "rutgers the state university of new jersey": "Rutgers University",
    "rutgers university new brunswick": "Rutgers University",
    "pitt": "University of Pittsburgh",
    "university of maryland college park": "University of Maryland",
    "georgia tech": "Georgia Institute of Technology",
    "purdue": "Purdue University",
    "purdue university west lafayette": "Purdue University",
    "umn": "University of Minnesota",
    "university of minnesota twin cities": "University of Minnesota",
    "iowa state": "Iowa State University",
    "university of iowa": "University of Iowa",
    "virginia tech": "Virginia Tech",
    "virginia polytechnic institute and state university": "Virginia Tech",
    "uconn": "University of Connecticut",
    "washu": "Washington University in St. Louis",
    "wustl": "Washington University in St. Louis",
    "fsu": "Florida State University",
    "msu": "Michigan State University",
    "uf": "University of Florida",
    "bu": "Boston University",
    "lse": "London School of Economics",
    "london school of economics and political science": "London School of Economics",
    "ucl": "University College London",
    "oxford": "University of Oxford",
    "cambridge": "University of Cambridge",
    "warwick": "University of Warwick",
    "imperial": "Imperial College London",
    "imperial college of science technology and medicine": "Imperial College London",
    "toronto": "University of Toronto",
    "waterloo": "University of Waterloo",
    "ubc": "University of British Columbia",
    "the university of british columbia": "University of British Columbia",
    "mcgill": "McGill University",
    "ku leuven": "KU Leuven",
    "katholieke universiteit leuven": "KU Leuven",
    "universite paris saclay": "Université Paris-Saclay",
    "paris saclay": "Université Paris-Saclay",
    "sorbonne universite": "Sorbonne Université",
    "universite paris cite": "Université Paris Cité",
    "universite de paris": "Université Paris Cité",
    "universite psl": "Université Paris sciences et lettres",
    "psl": "Université Paris sciences et lettres",
    "institut polytechnique de paris": "Institut Polytechnique de Paris",
    "universite toulouse iii paul sabatier": "Université Paul Sabatier - Toulouse III",
    "universite de bordeaux": "Université de Bordeaux",
    "universite grenoble alpes": "Université Grenoble Alpes",
    "universite de lyon": "Université de Lyon",
    "universite claude bernard lyon 1": "Université Claude Bernard Lyon 1",
    "universite de rennes 1": "Université de Rennes 1",
    "universite de lille": "Université de Lille",
    "universite de strasbourg": "Université de Strasbourg",
    "universite de montpellier": "Université de Montpellier",
    "universite cote d azur": "Université Côte d'Azur",
    "universite aix marseille": "Aix-Marseille Université",
    "aix marseille universite": "Aix-Marseille Université",
    "kth royal institute of technology": "KTH Royal Institute of Technology",
    "stockholm university": "Stockholm University",
    "uppsala university": "Uppsala University",
    "lund university": "Lund University",
    "chalmers university of technology": "Chalmers University of Technology",
    "university of gothenburg": "University of Gothenburg",
    "linkoping university": "Linköping University",
    "umea university": "Umeå University",
    "karolinska institutet": "Karolinska Institutet",
    "university of melbourne": "University of Melbourne",
    "the university of melbourne": "University of Melbourne",
    "australian national university": "Australian National University",
    "the australian national university": "Australian National University",
    "monash university": "Monash University",
    "university of sydney": "University of Sydney",
    "university of new south wales": "University of New South Wales",
    "university of queensland": "University of Queensland",
    "national university of singapore": "National University of Singapore",
    "nanyang technological university": "Nanyang Technological University",
    "university of hong kong": "University of Hong Kong",
    "the university of hong kong": "University of Hong Kong",
    "university of arizona": "University of Arizona",
    "the university of arizona": "University of Arizona",
    "university of oklahoma": "University of Oklahoma",
    "university of kansas": "University of Kansas",
    "university of kentucky": "University of Kentucky",
    "university of south carolina": "University of South Carolina",
    "university of nebraska lincoln": "University of Nebraska-Lincoln",
    "university of utah": "University of Utah",
    "university of rochester": "University of Rochester",
    "case western reserve university": "Case Western Reserve University",
    "stony brook university": "Stony Brook University",
    "clemson university": "Clemson University",
    "southern methodist university": "Southern Methodist University",
    "louisiana state university": "Louisiana State University",
    "wayne state university": "Wayne State University",
    "bowling green state university": "Bowling Green State University",
    "university of delaware": "University of Delaware",
    "mcmaster university": "McMaster University",
    "queen s university": "Queen's University",
    "york university": "York University",
    "dalhousie university": "Dalhousie University",
    "university of calgary": "University of Calgary",
    "university of ottawa": "University of Ottawa",
    "university of manitoba": "University of Manitoba",
    "university of victoria": "University of Victoria",
    "university of alberta": "University of Alberta",
    "university of edinburgh": "University of Edinburgh",
    "the university of edinburgh": "University of Edinburgh",
    "university of glasgow": "University of Glasgow",
    "university of southampton": "University of Southampton",
    "university of sheffield": "University of Sheffield",
    "university of leeds": "University of Leeds",
    "university of york": "University of York",
    "university of birmingham": "University of Birmingham",
    "lancaster university": "Lancaster University",
    "university of bristol": "University of Bristol",
    "university of helsinki": "University of Helsinki",
    "aalto university": "Aalto University",
    "utrecht university": "Utrecht University",
    "universiteit utrecht": "Utrecht University",
    "university of copenhagen": "University of Copenhagen",
    "aarhus university": "Aarhus University",
    "brown university": "Brown University",
}


class StatLinkIngestor:
    """
    Loads the live SQLite people/edges/schools indices into memory and performs
    high-precision entity resolution, homonym-guarded advisor matching, and batch
    upserts with zero duplicate creation.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, timeout=60)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.cur = self.conn.cursor()

        # Build canonical school alias dictionary from schools table
        self.school_norm_map = {}
        self.school_country_map = {}
        for sch, nsch, cntry, aliases in self.cur.execute(
            "SELECT school, COALESCE(norm_school,''), COALESCE(country,''), COALESCE(aliases,'') FROM schools"
        ):
            key = norm_key(nsch or sch)
            if key:
                self.school_norm_map[key] = sch
            key2 = norm_key(sch)
            if key2:
                self.school_norm_map[key2] = sch
            if cntry:
                self.school_country_map[sch] = cntry
            for al in aliases.split("|"):
                ak = norm_key(al)
                if ak and ak not in self.school_norm_map:
                    self.school_norm_map[ak] = sch
        for k, v in MANUAL_SCHOOL_ALIASES.items():
            # Map to existing DB canonical name if present, else keep v
            vk = norm_key(v)
            canon = self.school_norm_map.get(vk, v)
            self.school_norm_map[norm_key(k)] = canon
            self.school_norm_map[vk] = canon

        # Load people lookup maps
        self.by_id = {}
        self.by_norm = defaultdict(list)
        self.by_sig = defaultdict(list)
        self.by_orcid = {}
        for row in self.cur.execute("""
            SELECT id, name, norm_name, COALESCE(search_aliases,''), COALESCE(school,''),
                   COALESCE(country,''), year, COALESCE(thesis,''), COALESCE(primary_field,''),
                   is_core_stat, is_broad_stat, COALESCE(current_affiliation,''),
                   COALESCE(orcid,''), COALESCE(homepage,''), COALESCE(sources,''),
                   student_count, descendant_count
            FROM people
        """):
            rec = {
                "id": row[0], "name": row[1], "norm_name": row[2], "aliases": row[3],
                "school": row[4], "country": row[5], "year": row[6], "thesis": row[7],
                "primary_field": row[8], "is_core_stat": row[9], "is_broad_stat": row[10],
                "current_affiliation": row[11], "orcid": row[12], "homepage": row[13],
                "sources": row[14], "student_count": row[15], "descendant_count": row[16],
            }
            self._index_rec(rec)

        # Advisor-at-school affiliation map (verified professors advising at each university!)
        self.advisor_schools = defaultdict(set)  # person_id -> set(school)
        for aid, sch in self.cur.execute("SELECT person_id, school FROM advisor_schools"):
            if sch:
                self.advisor_schools[aid].add(sch)
        for rec in self.by_id.values():
            if rec["current_affiliation"]:
                self.advisor_schools[rec["id"]].add(rec["current_affiliation"])

        self.existing_edges = set(self.cur.execute("SELECT advisor_id, student_id FROM edges").fetchall())
        max_id = self.cur.execute("SELECT MAX(id) FROM people").fetchone()[0] or 4_000_000
        self.next_id = max(4_500_000, max_id + 1000)

        self.stats = {
            "records_seen": 0,
            "new_students": 0,
            "enriched_students": 0,
            "new_advisors": 0,
            "new_edges": 0,
            "skipped_masters": 0,
            "skipped_nonstat": 0,
            "skipped_homonym": 0,
        }

    def _index_rec(self, rec):
        pid = rec["id"]
        self.by_id[pid] = rec
        nk = rec["norm_name"] or norm_key(rec["name"])
        if nk:
            self.by_norm[nk].append(rec)
        sig = first_last_sig(rec["name"])
        if sig[0] and sig[1]:
            self.by_sig[sig].append(rec)
        if rec["aliases"]:
            for al in re.split(r"[|;,]", rec["aliases"]):
                ank = norm_key(al)
                if ank and ank != nk:
                    self.by_norm[ank].append(rec)
                asig = first_last_sig(al)
                if asig[0] and asig[1] and asig != sig:
                    self.by_sig[asig].append(rec)
        if rec["orcid"]:
            self.by_orcid[rec["orcid"]] = rec

    def canonical_school(self, raw_school: str, default_country: str = ""):
        if not raw_school:
            return "", default_country
        s = html_mod.unescape(str(raw_school)).strip()
        s = re.sub(r"\s+", " ", s)
        nk = norm_key(s)
        if nk in self.school_norm_map:
            canon = self.school_norm_map[nk]
            return canon, self.school_country_map.get(canon, default_country)
        # Strip leading "the "
        nk2 = re.sub(r"^the\s+", "", nk)
        if nk2 in self.school_norm_map:
            canon = self.school_norm_map[nk2]
            return canon, self.school_country_map.get(canon, default_country)
        return s, default_country

    def find_advisor_match(self, adv_name: str, school: str, student_year):
        """
        Strict homonym-safe advisor resolver:
        - NEVER links to a scholar whose PhD year is within <= 3 years of student_year
          (or > 75 years earlier).
        - Strongly prefers faculty affiliated with `school` (via advisor_schools, current_affiliation, or school).
        - Never absorbs an unrelated cross-school homonym when multiple candidates exist.
        """
        nk = norm_key(adv_name)
        sig = first_last_sig(adv_name)
        cands_raw = list(self.by_norm.get(nk, ()))
        if sig[0] and sig[1]:
            seen_ids = {c["id"] for c in cands_raw}
            for c in self.by_sig.get(sig, ()):
                if c["id"] not in seen_ids:
                    cands_raw.append(c)
                    seen_ids.add(c["id"])

        if not cands_raw:
            return None

        best = None
        best_sc = -1
        valid_cands = []
        for c in cands_raw:
            # Strict chronological guard!
            if student_year and c["year"]:
                gap = student_year - c["year"]
                if gap <= 3 or gap > 75:
                    continue
            valid_cands.append(c)

        for c in valid_cands:
            sc = 0
            if norm_key(c["name"]) == nk:
                sc += 40
            elif sig[0] and sig[1] and first_last_sig(c["name"]) == sig:
                sc += 28
            at_school = bool(
                school and (
                    school in self.advisor_schools.get(c["id"], ())
                    or c["current_affiliation"] == school
                    or c["school"] == school
                )
            )
            if at_school:
                sc += 65
            if c["is_core_stat"]:
                sc += 25
            elif c["is_broad_stat"]:
                sc += 12
            if c["student_count"] > 0:
                sc += min(20, 5 + c["student_count"])
            if student_year and c["year"]:
                gap = student_year - c["year"]
                if 5 <= gap <= 50:
                    sc += 20
            # Unique match bonus ONLY if it's broad_stat OR at the same school
            if len(valid_cands) == 1 and (at_school or c["is_core_stat"]):
                sc += 25
            if sc > best_sc:
                best_sc = sc
                best = c

        # Require strong confidence: >= 85 (or >= 75 if at_school) so cross-school homonyms never collide!
        if best:
            at_school = bool(
                school and (
                    school in self.advisor_schools.get(best["id"], ())
                    or best["current_affiliation"] == school
                    or best["school"] == school
                )
            )
            min_thresh = 72 if at_school else (90 if len(valid_cands) == 1 else 105)
            if best_sc >= min_thresh:
                return best
        return None

    def resolve_or_create_student(
        self, name: str, school: str, country: str, year, thesis: str,
        field: str, source_label: str, orcid: str = "", homepage: str = "",
        advisor_ids=None,
    ):
        # 1. Exact ORCID match
        if orcid and orcid in self.by_orcid:
            rec = self.by_orcid[orcid]
            self._enrich_existing(rec, school, year, thesis, field, source_label, orcid, homepage)
            return rec, False

        nk = norm_key(name)
        sig = first_last_sig(name)
        cands = list(self.by_norm.get(nk, ()))
        if sig[0] and sig[1]:
            seen = {c["id"] for c in cands}
            for c in self.by_sig.get(sig, ()):
                if c["id"] not in seen:
                    cands.append(c)
                    seen.add(c["id"])

        best = None
        best_sc = -1
        for c in cands:
            sc = 0
            if norm_key(c["name"]) == nk:
                sc += 45
            elif sig[0] and sig[1] and first_last_sig(c["name"]) == sig:
                sc += 30
            if school and c["school"] == school:
                sc += 55
            elif school and c["school"] and c["school"] != school:
                # Different PhD university -> strong penalty unless advisor also matches!
                sc -= 35
            if year and c["year"]:
                diff = abs(year - c["year"])
                if diff == 0:
                    sc += 50
                elif diff <= 2:
                    sc += 38
                elif diff <= 4:
                    sc += 10
                else:
                    sc -= 75
            if advisor_ids:
                for aid in advisor_ids:
                    if (aid, c["id"]) in self.existing_edges:
                        sc += 65
                        break
            if sc > best_sc:
                best_sc = sc
                best = c

        if best and best_sc >= 80:
            self._enrich_existing(best, school, year, thesis, field, source_label, orcid, homepage)
            self.stats["enriched_students"] += 1
            return best, False

        # Create net-new student record
        uid = self.next_id
        self.next_id += 1
        is_core = 1 if field in ("Statistics", "Biostatistics", "Probability") else 0
        is_broad = 1 if (is_core or field == "Related Quantitative") else 0
        rec = {
            "id": uid, "name": name, "norm_name": nk, "aliases": "",
            "school": school, "country": country, "year": year, "thesis": thesis,
            "primary_field": field, "is_core_stat": is_core, "is_broad_stat": is_broad,
            "current_affiliation": "", "orcid": orcid, "homepage": homepage,
            "sources": source_label, "student_count": 0, "descendant_count": 0,
        }
        self.cur.execute(
            """
            INSERT INTO people (
                id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence,
                classification_reason, current_affiliation, research_areas, mgp_id, aft_pid,
                wikidata_qid, orcid, homepage, proquest_id, sources, advisor_count, student_count,
                descendant_count, stat_descendant_count
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,0)
            """,
            (
                uid, name, nk, "", school, "", country, year, thesis,
                None, field, is_core, is_broad, 0.92 if is_core else 0.78,
                f"Harvested from {source_label}", "", "", None, None,
                "", orcid, homepage, "", source_label,
            ),
        )
        self._index_rec(rec)
        self.stats["new_students"] += 1
        return rec, True

    def resolve_or_create_advisor(self, adv_name: str, school: str, country: str, student_year, field: str, source_label: str):
        matched = self.find_advisor_match(adv_name, school, student_year)
        if matched:
            if school and school not in self.advisor_schools[matched["id"]]:
                self.advisor_schools[matched["id"]].add(school)
                self.cur.execute(
                    "INSERT OR IGNORE INTO advisor_schools (person_id, school, advised_at_school) VALUES (?, ?, 1)",
                    (matched["id"], school),
                )
            return matched, False

        # Create net-new faculty/advisor record (affiliated with `school`)
        uid = self.next_id
        self.next_id += 1
        nk = norm_key(adv_name)
        is_core = 1 if field in ("Statistics", "Biostatistics", "Probability") else 0
        is_broad = 1 if (is_core or field == "Related Quantitative") else 0
        rec = {
            "id": uid, "name": adv_name, "norm_name": nk, "aliases": "",
            "school": "", "country": country, "year": None, "thesis": "",
            "primary_field": field, "is_core_stat": is_core, "is_broad_stat": is_broad,
            "current_affiliation": school, "orcid": "", "homepage": "",
            "sources": source_label, "student_count": 0, "descendant_count": 0,
        }
        self.cur.execute(
            """
            INSERT INTO people (
                id, name, norm_name, search_aliases, school, raw_school, country, year, thesis,
                mgp_subject, primary_field, is_core_stat, is_broad_stat, field_confidence,
                classification_reason, current_affiliation, research_areas, mgp_id, aft_pid,
                wikidata_qid, orcid, homepage, proquest_id, sources, advisor_count, student_count,
                descendant_count, stat_descendant_count
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,0)
            """,
            (
                uid, adv_name, nk, "", "", "", country, None, "",
                None, field, is_core, is_broad, 0.85 if is_core else 0.72,
                f"PhD advisor harvested from {source_label}", school, "", None, None,
                "", "", "", "", source_label,
            ),
        )
        self._index_rec(rec)
        if school:
            self.advisor_schools[uid].add(school)
            self.cur.execute(
                "INSERT OR IGNORE INTO advisor_schools (person_id, school, advised_at_school) VALUES (?, ?, 1)",
                (uid, school),
            )
        self.stats["new_advisors"] += 1
        return rec, True

    def _enrich_existing(self, rec, school, year, thesis, field, source_label, orcid, homepage):
        srcs = [s.strip() for s in rec["sources"].split(",") if s.strip()]
        if source_label not in srcs:
            srcs.append(source_label)
            rec["sources"] = ",".join(srcs)
        upd_year = rec["year"] or year
        upd_sch = rec["school"] or school
        upd_th = rec["thesis"] or thesis
        upd_orc = rec["orcid"] or orcid
        upd_hp = rec["homepage"] or homepage
        # Promote field to Core Stat if previously "Mathematics & Other" / "Related Quantitative" and new source is Core Stat
        upd_field = rec["primary_field"]
        upd_core = rec["is_core_stat"]
        upd_broad = rec["is_broad_stat"]
        if not upd_core and field in ("Statistics", "Biostatistics", "Probability"):
            upd_field = field
            upd_core = 1
            upd_broad = 1
        self.cur.execute(
            """
            UPDATE people SET
                year = COALESCE(year, ?),
                school = CASE WHEN school = '' OR school IS NULL THEN ? ELSE school END,
                thesis = CASE WHEN thesis = '' OR thesis IS NULL THEN ? ELSE thesis END,
                orcid = CASE WHEN orcid = '' OR orcid IS NULL THEN ? ELSE orcid END,
                homepage = CASE WHEN homepage = '' OR homepage IS NULL THEN ? ELSE homepage END,
                primary_field = ?,
                is_core_stat = ?,
                is_broad_stat = ?,
                sources = ?
            WHERE id = ?
            """,
            (upd_year, upd_sch, upd_th, upd_orc, upd_hp, upd_field, upd_core, upd_broad, rec["sources"], rec["id"]),
        )
        rec["year"] = upd_year
        rec["school"] = upd_sch
        rec["thesis"] = upd_th
        rec["orcid"] = upd_orc
        rec["homepage"] = upd_hp
        rec["primary_field"] = upd_field
        rec["is_core_stat"] = upd_core
        rec["is_broad_stat"] = upd_broad
        if upd_orc:
            self.by_orcid[upd_orc] = rec

    def ingest_record(self, rec_dict: dict, allow_without_advisor: bool = False):
        """
        Ingest a standardized harvested PhD record dict:
          student, advisors (list[str]), school, country, year, thesis, subjects,
          degree, source, orcid, homepage, known_advisor_id (optional int).
        """
        self.stats["records_seen"] += 1
        student_name = clean_person_name(rec_dict.get("student") or "")
        if not student_name:
            return False

        title = clean_title(rec_dict.get("thesis") or "")
        degree = rec_dict.get("degree") or ""
        if not is_doctoral_degree(degree, title, require_positive=False):
            self.stats["skipped_masters"] += 1
            return False

        yr = rec_dict.get("year")
        if yr is not None:
            try:
                yr = int(yr)
                if yr < 1850 or yr > 2027:
                    yr = None
            except Exception:
                yr = None

        school, country = self.canonical_school(rec_dict.get("school") or "", rec_dict.get("country") or "")
        source_label = rec_dict.get("source") or "Global ETD"
        subjects = rec_dict.get("subjects") or ""
        orcid = (rec_dict.get("orcid") or "").strip()
        homepage = (rec_dict.get("homepage") or "").strip()

        # Check advisors first (lets us know if advisor is Core Stat for borderline interdisciplinary titles!)
        raw_advs = rec_dict.get("advisors") or []
        known_adv_id = rec_dict.get("known_advisor_id")
        clean_advs = []
        seen_ank = set()
        stu_nk = norm_key(student_name)
        for a in raw_advs:
            for ca in split_advisor_field(a):
                ank = norm_key(ca)
                if ank and ank != stu_nk and ank not in seen_ank:
                    seen_ank.add(ank)
                    clean_advs.append(ca)

        if not clean_advs and not known_adv_id and not allow_without_advisor:
            return False

        matched_advs = []
        if known_adv_id and known_adv_id in self.by_id:
            matched_advs.append(self.by_id[known_adv_id])
        else:
            for anm in clean_advs[:3]:
                m = self.find_advisor_match(anm, school, yr)
                if m:
                    matched_advs.append(m)

        adv_is_core = any(m["is_core_stat"] for m in matched_advs)
        field = rec_dict.get("force_field") or classify_stat_field(subjects, title, advisor_is_core=adv_is_core)
        if not field:
            self.stats["skipped_nonstat"] += 1
            return False

        # Resolve or create advisors (only create net-new advisor when field is Core Stat or school is known)
        final_advs = []
        if known_adv_id and known_adv_id in self.by_id:
            final_advs.append(self.by_id[known_adv_id])
        else:
            for anm in clean_advs[:3]:
                adv_rec, _ = self.resolve_or_create_advisor(anm, school, country, yr, field, source_label)
                final_advs.append(adv_rec)

        adv_ids = [a["id"] for a in final_advs]
        stu_rec, _ = self.resolve_or_create_student(
            student_name, school, country, yr, title, field, source_label,
            orcid=orcid, homepage=homepage, advisor_ids=adv_ids,
        )

        # Insert directed edges (with cycle/chronology guard)
        for adv_rec in final_advs:
            aid, sid = adv_rec["id"], stu_rec["id"]
            if aid == sid or (aid, sid) in self.existing_edges or (sid, aid) in self.existing_edges:
                continue
            if stu_rec["year"] and adv_rec["year"]:
                gap = stu_rec["year"] - adv_rec["year"]
                if gap <= 3 or gap > 75:
                    self.stats["skipped_homonym"] += 1
                    continue
            self.cur.execute(
                "INSERT OR IGNORE INTO edges (advisor_id, student_id, source) VALUES (?, ?, ?)",
                (aid, sid, source_label),
            )
            self.existing_edges.add((aid, sid))
            adv_rec["student_count"] += 1
            self.stats["new_edges"] += 1
            if school and school not in self.advisor_schools[aid]:
                self.advisor_schools[aid].add(school)
                self.cur.execute(
                    "INSERT OR IGNORE INTO advisor_schools (person_id, school, advised_at_school) VALUES (?, ?, 1)",
                    (aid, school),
                )
        return True

    def commit_and_close(self):
        self.conn.commit()
        self.conn.close()
