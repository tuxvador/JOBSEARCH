"""Starting profiles for search criteria. Users copy one, then edit freely."""
import copy

_REMOTE_COUNTRIES = ["france", "europe", "eu", "emea", "worldwide", "anywhere"]
_COMMON_EXCLUDE = ["alternance", "apprenti*", "apprentissage", "stage", "stagiaire", "intern",
                   "internship", "benevolat"]
_REMOTE = {"name": "Télétravail", "weight": 5, "scope": "location", "gate": "",
           "terms": ["remote", "hybrid*", "teletravail", "full remote"]}
_SC_TERMS = ["supply chain", "supply-chain", "scm", "logisti*", "approvision*", "appro",
             "planif*", "planner", "s&op", "demand planning", "demand planner", "flux", "achat*",
             "acheteu*", "procurement", "purchas*", "sourcing", "stock*", "inventory", "transport*",
             "distribution", "entrepot*", "warehouse", "adv", "administration des ventes",
             "order to cash"]
_SC_OPERATIVE = ["cariste", "preparateur*", "preparatrice*", "manutention*", "magasinier*",
                 "chauffeur*", "livreur*", "agent de quai", "conducteur*", "interimaire"]

PENTEST = {
    "keywords": ["pentester", "pentest", "test d'intrusion", "red team",
                 "penetration tester", "sécurité offensive"],
    "sources": ["jooble", "careerjet", "remoteok", "himalayas"],
    "ft_departements": "",
    "adzuna_where": "",
    "lba_romes": "",
    "contracts": ["CDI", "Freelance"],
    "max_age_days": 31,
    "salary_floor": 55000,
    "remote_countries": list(_REMOTE_COUNTRIES),
    "categories": [
        {"name": "Pentest / offensif", "weight": 30, "scope": "all", "gate": "signal",
         "terms": ["pentest*", "penetration test*", "test d'intrusion", "tests d'intrusion",
                   "ethical hacker", "red team*", "offensi*"]},
        {"name": "Séniorité", "weight": 15, "scope": "title", "gate": "",
         "terms": ["senior", "confirm*", "expert", "lead", "manager"]},
        {"name": "Cyber / sécurité", "weight": 10, "scope": "title", "gate": "signal",
         "terms": ["cyber*", "security", "securite", "cyberdefense"]},
        {"name": "Rennes / Bretagne", "weight": 10, "scope": "location", "gate": "lieu",
         "terms": ["rennes", "bretagne", "brittany", "ille-et-vilaine", "cesson*",
                   "saint-jacques*", "bruz"]},
        {"name": "Télétravail", "weight": 5, "scope": "location", "gate": "lieu",
         "terms": ["remote", "hybrid*", "teletravail", "emea", "european union",
                   "european economic area", "worldwide", "anywhere"]},
    ],
    "exclude": _COMMON_EXCLUDE + ["formateur", "formatrice", "enseignant*", "chercheur*",
                                  "biostatisticien*", "soc analyst", "analyste soc",
                                  "analyste cybersoc", "analyse soc", "soc", "grc", "compliance",
                                  r"re:\brisk\b.*\bmanager\b", r"re:administrat\w*.*\bsiem\b"],
    "penalty_weight": -20,
    "penalty": [r"re:\badmin\w*.*\breseau\b", "xsoar", "risque*", "risk", "devops", "socle"],
    "min_score": 20,
    "top_n": 10,
}

SUPPLY_CHAIN = {
    "keywords": ["supply chain", "responsable supply chain", "approvisionneur", "demand planner",
                 "responsable logistique", "acheteur", "planification"],
    "sources": ["jooble", "careerjet", "himalayas"],
    "ft_departements": "",
    "adzuna_where": "",
    "lba_romes": "",
    "contracts": ["CDI"],
    "max_age_days": 31,
    "salary_floor": 0,
    "remote_countries": list(_REMOTE_COUNTRIES),
    "categories": [
        {"name": "Supply chain / logistique", "weight": 30, "scope": "title", "gate": "signal",
         "terms": list(_SC_TERMS)},
        {"name": "Séniorité / encadrement", "weight": 15, "scope": "title", "gate": "",
         "terms": ["senior", "confirm*", "expert", "lead", "manager", "responsable", "head",
                   "directeur", "directrice", "chef", "coordinat*"]},
        {"name": "Conseil / projet / ERP", "weight": 5, "scope": "title", "gate": "",
         "terms": ["consultant*", "projet", "project", "sap", "erp", "amelioration continue", "lean"]},
        dict(_REMOTE),
    ],
    "exclude": _COMMON_EXCLUDE + _SC_OPERATIVE,
    "penalty_weight": -20,
    "penalty": ["assistant*", "operateur*", "operatrice*", "technicien*", "agent"],
    "min_score": 30,
    "top_n": 15,
}

# Work-study (alternance): the contract becomes a requirement instead of an
# exclusion, and experienced-hire wording is penalised.
SUPPLY_CHAIN_ALTERNANCE = {
    "keywords": ["supply chain", "logistique", "approvisionnement", "achats", "demand planning",
                 "transport", "ADV"],
    "sources": ["labonnealternance", "jooble", "careerjet"],
    "ft_departements": "",
    "adzuna_where": "",
    "lba_romes": "",  # empty on purpose: see search.src_labonnealternance
    "contracts": ["Alternance"],
    "max_age_days": 31,
    "salary_floor": 0,
    "remote_countries": list(_REMOTE_COUNTRIES),
    "categories": [
        {"name": "Supply chain / logistique", "weight": 30, "scope": "title", "gate": "signal",
         "terms": list(_SC_TERMS)},
        {"name": "Alternance", "weight": 20, "scope": "all", "gate": "contrat",
         "terms": ["alternance", "alternant*", "apprenti*", "apprentissage",
                   "contrat pro*", "professionnalisation", "work-study", "work study",
                   "apprenticeship", "alternating"]},
        {"name": "Conseil / projet / ERP", "weight": 5, "scope": "title", "gate": "",
         "terms": ["projet", "project", "sap", "erp", "amelioration continue", "lean"]},
        dict(_REMOTE),
    ],
    "exclude": ["stage", "stagiaire", "intern", "internship", "benevolat"] + _SC_OPERATIVE,
    "penalty_weight": -20,
    "penalty": ["senior", "confirm*", "expert", r"re:\b\d+\s*ans d.experience",
                "operateur*", "operatrice*", "technicien*"],
    "min_score": 40,
    "top_n": 20,
}

BLANK = {
    "keywords": [],
    "sources": ["jooble", "careerjet"],
    "ft_departements": "",
    "adzuna_where": "",
    "lba_romes": "",
    "contracts": ["CDI"],
    "max_age_days": 31,
    "salary_floor": 0,
    "remote_countries": list(_REMOTE_COUNTRIES),
    "categories": [
        {"name": "Métier recherché", "weight": 30, "scope": "title", "gate": "signal", "terms": []},
        {"name": "Séniorité", "weight": 15, "scope": "title", "gate": "",
         "terms": ["senior", "confirm*", "expert", "lead", "manager", "responsable"]},
        dict(_REMOTE),
    ],
    "exclude": list(_COMMON_EXCLUDE),
    "penalty_weight": -20,
    "penalty": [],
    "min_score": 0,
    "top_n": 15,
}

# key -> (French label, criteria). Labels are translated in the UI.
PRESETS = {
    "blank": ("Vierge (à compléter)", BLANK),
    "supply_chain": ("Supply chain / logistique", SUPPLY_CHAIN),
    "supply_chain_alternance": ("Alternance supply chain", SUPPLY_CHAIN_ALTERNANCE),
    "pentest": ("Pentest / cybersécurité", PENTEST),
}
DEFAULT_PRESET = "blank"


def get(preset=DEFAULT_PRESET):
    return copy.deepcopy(PRESETS.get(preset, PRESETS[DEFAULT_PRESET])[1])
