#!/usr/bin/env python3
"""
MSC 2020 + Collaborative Fine-Tuned 2D Dissertation Topic Landscape Builder.

Maps all Core Statistics, Biostatistics & Probability PhD dissertations in StatLink onto
an authoritative taxonomy grounded in the AMS Mathematics Subject Classification 2020
(Section 62: Statistics & Section 60: Probability Theory and Stochastic Processes,
https://mathscinet.ams.org/mathscinet/msc/pdfs/classifications2020.pdf) plus modern
Statistical ML, Biostatistics, and Mathematical Finance extensions—eliminating all
non-mathematical/non-statistical bubble labels (e.g. "public", "team").

Key algorithmic upgrades:
  1. Base Encoder: Language-invariant `paraphrase-multilingual-MiniLM-L12-v2` (bottom 6
     layers frozen to preserve 50+-language alignment; top 6 layers + pooler fine-tuned).
  2. Multi-Label Weak Supervision: Dissertations spanning multiple MSC 2020 concepts
     (e.g. "Optimal Experimental Design for Causal Inference") receive ALL matching labels
     simultaneously via multi-label additive-margin cosine loss + macro-domain head.
  3. Data-Driven Collaborative Signal (zero hand-coded pairs):
     - Within-dissertation multi-concept co-occurrence (PPMI + cosine profile similarity)
     - Academic lineage collaborative co-occurrence (shared PhD advisor & advisor-student
       dissertation pairs from `edges` in SQLite)
     Used BOTH as a prototype-geometry regularizer during GPU fine-tuning AND blended
     with fine-tuned semantic cosine distance before 2D t-SNE projection!
"""

import itertools
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.cluster import KMeans
from sklearn.manifold import TSNE
from transformers import AutoModel, AutoTokenizer

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
OUT_JSON = os.path.join(BASE_DIR, "data", "dissertation_topic_map.json")
LOCAL_BASE_MODEL = os.path.join(BASE_DIR, "data_raw", "hf_multilingual_minilm")
FINETUNED_DIR = os.path.join(BASE_DIR, "data_raw", "stat_minilm_msc2020_finetuned")

MACRO_DOMAINS = [
    "Bayesian & Causal Inference",
    "Stochastic Processes & Probability",
    "Clinical Biostatistics",
    "ML & High-Dimensional Stats",
    "Time Series & Econometrics",
]

CLUSTER_PALETTE = [
    "#3b5bdb",  # Indigo  — Bayesian & Causal Inference
    "#0f9d58",  # Emerald — Stochastic Processes & Probability
    "#d97706",  # Amber   — Clinical Biostatistics
    "#8b5cf6",  # Violet  — ML & High-Dimensional Stats
    "#e11d48",  # Rose    — Time Series & Econometrics
]

# ---------------------------------------------------------------------------
# Official AMS MSC 2020 (Section 62 Statistics + Section 60 Probability)
# + Modern ML / Biostatistics / Finance Extensions
# Format: (label, msc_code, macro_domain_idx, regex_pattern)
# ---------------------------------------------------------------------------
MSC2020_TAXONOMY = [
    # --- Macro 0: Bayesian, Causal, Design & Core Statistical Inference (MSC 62A-62L) ---
    ("causal inference", "62D20", 0,
     r"\b(causal\s+infer\w*|causal\s+effect\w*|causal\s+mediat\w*|causal\s+discover\w*|counterfactual\w*|propensity\s+score\w*|instrumental\s+variable\w*|average\s+treatment\s+effect|treatment\s+effect\w*|observational\s+stud\w*|potential\s+outcome\w*|difference[\-\s]in[\-\s]differences|synthetic\s+control\w*|confounding|unmeasured\s+confound\w*|mediation\s+analysis|direct\s+and\s+indirect\s+effect\w*)\b"),
    ("experimental design", "62K05", 0,
     r"\b(experimental\s+design\w*|design\s+of\s+experiment\w*|optimal\s+design\w*|d[\-\s]optimal|a[\-\s]optimal|factorial\s+design\w*|fractional\s+factorial|block\s+design\w*|incomplete\s+block|latin\s+square|response\s+surface\w*|computer\s+experiment\w*|latin\s+hypercube|randomized\s+experiment\w*|a/b\s+test\w*|crossover\s+design\w*|split[\-\s]plot)\b"),
    ("bayesian inference", "62F15", 0,
     r"\b(bayesian\s+infer\w*|bayesian\s+estimat\w*|bayesian\s+model\w*|bayesian\s+analysis|bayesian\s+method\w*|hierarchical\s+bayes\w*|posterior\s+distribution\w*|posterior\s+contract\w*|prior\s+distribution\w*|bayes\s+factor\w*|subjective\s+probabilit\w*|inf[eé]rence\s+bay[eé]sienne|bayesian\s+approach)\b"),
    ("bayesian nonparametrics", "62G05", 0,
     r"\b(bayesian\s+nonparametric\w*|dirichlet\s+process\w*|chinese\s+restaurant\s+process|indian\s+buffet\s+process|pitman[\-\s]yor|polya\s+tree|stick[\-\s]breaking|nonparametric\s+bayes\w*)\b"),
    ("empirical bayes", "62C12", 0,
     r"\b(empirical\s+bayes\w*|compound\s+decision|robbins\s+method|james[\-\s]stein|stein\s+estimat\w*|shrinkage\s+prior\w*|horseshoe\s+prior)\b"),
    ("decision theory & minimax", "62C20", 0,
     r"\b(decision\s+theor\w*|minimax\s+(?:rate|risk|estimat|optim|bound|test)|admissib\w*|inadmissib\w*|bayes\s+risk|loss\s+function\w*|complete\s+class|statistical\s+decision)\b"),
    ("hypothesis testing", "62F03", 0,
     r"\b(hypothesis\s+test\w*|likelihood\s+ratio\s+test\w*|goodness[\-\s]of[\-\s]fit|chi[\-\s]square\w*\s+test|permutation\s+test\w*|rank\s+test\w*|wald\s+test|score\s+test|two[\-\s]sample\s+test\w*|equivalence\s+test\w*|homogeneity\s+test\w*|test\s+of\s+independence)\b"),
    ("multiple testing & fdr", "62J15", 0,
     r"\b(multiple\s+testing|multiple\s+comparison\w*|false\s+discovery\s+rate|fdr\s+control|benjamini[\-\s]hochberg|family[\-\s]wise\s+error|post[\-\s]selection\s+infer\w*|selective\s+infer\w*|knockoff\w*|large[\-\s]scale\s+testing)\b"),
    ("parametric estimation", "62F10", 0,
     r"\b(maximum\s+likelihood|point\s+estimat\w*|m[\-\s]estimat\w*|estimating\s+equation\w*|generalized\s+method\s+of\s+moments|method\s+of\s+moments|cram[eé]r[\-\s]rao|fisher\s+information|sufficient\s+statistic\w*|exponential\s+famil\w*|minimum\s+variance\s+unbiased)\b"),
    ("asymptotic theory", "62E20", 0,
     r"\b(asymptotic\s+(?:theor|distribut|propert|efficienc|normalit|expansion|infer|behavior)|edgeworth\s+expansion|saddlepoint\s+approximat\w*|local\s+asymptotic\s+normality|contiguity|le\s+cam|u[\-\s]statistic\w*|empirical\s+process\w*|donsker)\b"),
    ("nonparametric statistics", "62G05", 0,
     r"\b(nonparametric\s+(?:estimat|infer|test|method|model|statist)|non[\-\s]parametric\s+(?:estimat|infer|test|method)|kernel\s+smooth\w*|local\s+polynomial|wavelet\s+(?:estimat|smooth|shrinkage|threshold)|spline\s+smooth\w*|smoothing\s+spline\w*|nadaraya[\-\s]watson|isotonic\s+regression|shape[\-\s]constrained)\b"),
    ("density estimation", "62G07", 0,
     r"\b(density\s+estimat\w*|kernel\s+density|deconvolution\s+density|log[\-\s]concave\s+density|mixture\s+density|bandwidth\s+selection|histogram\s+estimat\w*)\b"),
    ("semiparametric inference", "62G05", 0,
     r"\b(semiparametric|semi[\-\s]parametric|efficient\s+influence\s+function|targeted\s+maximum\s+likelihood|tmle|doubly\s+robust|double\s+machine\s+learning|nuisance\s+parameter\w*|profile\s+likelihood|single[\-\s]index\s+model\w*|partially\s+linear\s+model\w*)\b"),
    ("quantile regression", "62G08", 0,
     r"\b(quantile\s+regression|conditional\s+quantile\w*|expectile\s+regression|quantile\s+estimat\w*|quantile\s+process\w*|extremal\s+quantile\w*)\b"),
    ("bootstrap & resampling", "62F40", 0,
     r"\b(bootstrap\w*|jackknife|resampling\s+method\w*|subsampling|bagging|wild\s+bootstrap|block\s+bootstrap|empirical\s+likelihood)\b"),
    ("robust statistics", "62F35", 0,
     r"\b(robust\s+(?:statist|estimat|regression|infer|test|method|procedure)|outlier\s+detect\w*|breakdown\s+point|influence\s+function\w*|trimmed\s+mean|huber\s+loss|contaminated\s+data|heavy[\-\s]tailed\s+estimat\w*)\b"),
    ("generalized linear models", "62J12", 0,
     r"\b(generalized\s+linear\s+model\w*|logistic\s+regression|poisson\s+regression|probit\s+model\w*|quasi[\-\s]likelihood|overdispersion|negative\s+binomial|zero[\-\s]inflated|categorical\s+data|contingency\s+table\w*|log[\-\s]linear\s+model\w*|multinomial\s+logit|generalized\s+additive\s+model\w*)\b"),
    ("linear & mixed models", "62J05", 0,
     r"\b(linear\s+model\w*|linear\s+regression|mixed[\-\s]effects?\s+model\w*|linear\s+mixed\s+model\w*|variance\s+component\w*|random\s+effects?\s+model\w*|analysis\s+of\s+variance|anova|ancova|multilevel\s+model\w*|hierarchical\s+linear\s+model\w*|errors[\-\s]in[\-\s]variables|measurement\s+error)\b"),
    ("survey sampling", "62D05", 0,
     r"\b(survey\s+sampl\w*|sample\s+survey\w*|finite\s+population|small\s+area\s+estimat\w*|stratified\s+sampl\w*|cluster\s+sampl\w*|complex\s+survey|horvitz[\-\s]thompson|capture[\-\s]recapture|nonprobability\s+sample\w*|survey\s+nonresponse)\b"),
    ("missing data & imputation", "62D10", 0,
     r"\b(missing\s+data|multiple\s+imputat\w*|imputation\s+method\w*|missing\s+not\s+at\s+random|mnar|mar\s+missing|incomplete\s+data|em\s+algorithm|expectation[\-\s]maximization|data\s+augmentation|nonignorable\s+missing\w*)\b"),
    ("sequential analysis", "62L10", 0,
     r"\b(sequential\s+(?:analys|test|estimat|design|method|sampl|detect|procedur)|group\s+sequential|optimal\s+stopping|sprt|wald\s+sequential|adaptive\s+sampl\w*|quickest\s+detection|cusum)\b"),
    ("monte carlo & mcmc", "65C05", 0,
     r"\b(monte\s+carlo|markov\s+chain\s+monte\s+carlo|mcmc|metropolis[\-\s]hastings|gibbs\s+sampl\w*|hamiltonian\s+monte\s+carlo|langevin\s+monte\s+carlo|importance\s+sampl\w*|sequential\s+monte\s+carlo|particle\s+filter\w*|approximate\s+bayesian\s+computat\w*|abc\s+method|quasi[\-\s]monte\s+carlo|reversible\s+jump)\b"),
    ("variational inference", "62F15", 0,
     r"\b(variational\s+(?:infer|bayes|approximat|method|autoencoder)|mean[\-\s]field\s+variational|laplace\s+approximat\w*|expectation\s+propagation|inla|stochastic\s+variational)\b"),
    ("conformal prediction & uq", "62G15", 0,
     r"\b(conformal\s+predict\w*|conformal\s+infer\w*|prediction\s+interval\w*|confidence\s+(?:band|region|interval|set)\w*|tolerance\s+interval\w*|uncertainty\s+quantificat\w*|calibrat\w*\s+predict\w*|distribution[\-\s]free\s+predict\w*)\b"),
    ("order statistics & extremes", "62G32", 0,
     r"\b(extreme\s+value\w*|tail\s+index|tail\s+infer\w*|peaks\s+over\s+threshold|generalized\s+pareto|order\s+statistic\w*|record\s+value\w*|max[\-\s]stable|heavy[\-\s]tail\w*)\b"),
    ("information geometry & theory", "62B11", 0,
     r"\b(information\s+geometr\w*|kullback[\-\s]leibler|f[\-\s]divergence|shannon\s+entropy|mutual\s+information|minimum\s+description\s+length|fisher[\-\s]rao|renyi\s+divergence|statistical\s+manifold\w*|scoring\s+rule\w*)\b"),
    ("algebraic & directional stats", "62R01", 0,
     r"\b(algebraic\s+statist\w*|directional\s+data|circular\s+data|spherical\s+data|statistics\s+on\s+manifold\w*|riemannian\s+manifold\w*|shape\s+analysis|procrustes|fr[eé]chet\s+mean|metric\s+space\w*\s+statist\w*|topological\s+data\s+analysis|persistent\s+homology)\b"),
    ("psychometrics & item response", "62P15", 0,
     r"\b(psychometr\w*|item\s+response\s+theor\w*|rasch\s+model\w*|cognitive\s+diagnos\w*|structural\s+equation\s+model\w*|latent\s+class\s+model\w*|computerized\s+adaptive\s+testing|educational\s+measurement)\b"),
    ("quality control & reliability", "62N05", 0,
     r"\b(reliability\s+(?:analys|estimat|theor|model|life|test|system)|life\s+testing|accelerated\s+life|quality\s+control|control\s+chart\w*|statistical\s+process\s+control|degradation\s+model\w*|warranty\s+data|weibull\s+distribut\w*)\b"),

    # --- Macro 1: Probability Theory & Stochastic Processes (MSC 60A-60L) ---
    ("stochastic differential equations", "60H10", 1,
     r"\b(stochastic\s+differential\s+equation\w*|sde\b|sdes\b|stochastic\s+partial\s+differential|spde\b|spdes\b|backward\s+stochastic\s+differential|bsde\b|it[oô]\s+calculus|stochastic\s+integral\w*|malliavin\s+calculus|fokker[\-\s]planck|stochastic\s+heat\s+equation|stochastic\s+navier[\-\s]stokes|équations?\s+différentielles?\s+stochastiques?)\b"),
    ("stochastic control & games", "60H30", 1,
     r"\b(stochastic\s+control|optimal\s+stochastic\s+control|mean[\-\s]field\s+game\w*|hamilton[\-\s]jacobi[\-\s]bellman|hjb\s+equation|stochastic\s+differential\s+game\w*|singular\s+control|impulse\s+control|ergodic\s+control|mckean[\-\s]vlasov)\b"),
    ("markov processes & chains", "60J10", 1,
     r"\b(markov\s+chain\w*|markov\s+process\w*|hidden\s+markov|hmm\b|markov\s+decision\s+process\w*|mixing\s+time\w*|cutoff\s+phenomenon|reversible\s+markov|semi[\-\s]markov|markov\s+renewal|feller\s+process\w*|dirichlet\s+form\w*|cha[iî]nes?\s+de\s+markov)\b"),
    ("brownian motion & diffusions", "60J65", 1,
     r"\b(brownian\s+motion|fractional\s+brownian|diffusion\s+process\w*|reflecting\s+brownian|local\s+time\w*|schramm[\-\s]loewner|sle\b|excursion\s+theor\w*|mouvement\s+brownien|ornstein[\-\s]uhlenbeck|bessel\s+process\w*)\b"),
    ("levy & jump processes", "60G51", 1,
     r"\b(l[eé]vy\s+process\w*|jump\s+process\w*|jump[\-\s]diffusion|stable\s+process\w*|infinitely\s+divisible|processus\s+de\s+l[eé]vy|subordinator\w*|compound\s+poisson|piecewise\s+deterministic\s+markov)\b"),
    ("martingales & stochastic calculus", "60G44", 1,
     r"\b(martingale\w*|semimartingale\w*|supermartingale\w*|submartingale\w*|doob\s+inequalit\w*|predictable\s+representation|filtration\w*\s+enlargement|optional\s+decomposition)\b"),
    ("limit theorems & large deviations", "60F10", 1,
     r"\b(large\s+deviation\w*|central\s+limit\s+theorem\w*|functional\s+limit\s+theorem\w*|invariance\s+principle\w*|moderate\s+deviation\w*|stein(?:'s)?\s+method|berry[\-\s]esseen|concentration\s+inequalit\w*|strong\s+law\s+of\s+large|law\s+of\s+the\s+iterated\s+logarithm|grandes?\s+d[eé]viations?)\b"),
    ("random matrices", "60B20", 1,
     r"\b(random\s+matri(?:x|ces)|wigner\s+matri\w*|wishart\s+matri\w*|eigenvalue\w*\s+distribut\w*|free\s+probabilit\w*|tracy[\-\s]widom|marchenko[\-\s]pastur|determinantal\s+point\s+process\w*|coulomb\s+gas|beta[\-\s]ensemble|matrices?\s+al[eé]atoires?)\b"),
    ("random walks & environments", "60G50", 1,
     r"\b(random\s+walk\w*|self[\-\s]avoiding\s+walk|random\s+environment\w*|marche\w*\s+al[eé]atoire\w*|first[\-\s]passage\s+percolation|last[\-\s]passage\s+percolation|ballistic\s+random\s+walk)\b"),
    ("percolation & interacting particles", "60K35", 1,
     r"\b(percolation|interacting\s+particle\s+system\w*|ising\s+model|potts\s+model|spin\s+glass\w*|exclusion\s+process|voter\s+model|contact\s+process|hydrodynamic\s+limit\w*|kpz\s+universality|gibbs\s+measure\w*|statistical\s+mechanics|random\s+cluster\s+model)\b"),
    ("branching & coalescent processes", "60J80", 1,
     r"\b(branching\s+process\w*|galton[\-\s]watson|superprocess\w*|coalescent\s+process\w*|kingman\s+coalescent|birth[\-\s]and[\-\s]death|fleming[\-\s]viot|processus\s+de\s+branchement|crump[\-\s]mode[\-\s]jagers)\b"),
    ("point processes & stochastic geometry", "60G55", 1,
     r"\b(point\s+process\w*|hawkes\s+process\w*|cox\s+process\w*|poisson\s+point\s+process|stochastic\s+geometr\w*|random\s+tessellation\w*|voronoi|boolean\s+model|random\s+set\w*|random\s+measure\w*|palm\s+theor\w*)\b"),
    ("gaussian processes & random fields", "60G15", 1,
     r"\b(gaussian\s+process\w*|gaussian\s+random\s+field\w*|gaussian\s+free\s+field|random\s+field\w*|stationary\s+gaussian|kriging|champ\w*\s+al[eé]atoire\w*|processus\s+gaussien\w*)\b"),
    ("random graphs & combinatorial prob", "60C05", 1,
     r"\b(random\s+graph\w*|erd[oö]s[\-\s]r[eé]nyi|random\s+tree\w*|random\s+permutation\w*|combinatorial\s+probabilit\w*|graphe\w*\s+al[eé]atoire\w*|preferential\s+attachment|random\s+hypergraph\w*|giant\s+component|stochastic\s+block\s+model)\b"),
    ("queueing & renewal theory", "60K25", 1,
     r"\b(queueing|queuing|queue\s+network\w*|heavy\s+traffic|fluid\s+limit\w*|renewal\s+theor\w*|renewal\s+process\w*|pollaczek|m/g/1|g/g/1|call\s+center\s+queue|service\s+system\w*|file\s+d'attente)\b"),
    ("rough paths & stochastic analysis", "60L20", 1,
     r"\b(rough\s+path\w*|regularity\s+structure\w*|paracontrolled|signature\s+method\w*|stochastic\s+analysis|white\s+noise\s+analysis|ergodic\s+theor\w*|dynamical\s+system\w*\s+random|stochastic\s+approximation)\b"),
    ("probability distributions & copulas", "60E05", 1,
     r"\b(copula\w*|characteristic\s+function\w*|stochastic\s+order\w*|probability\s+distribut\w*|multivariate\s+distribut\w*|skew[\-\s]normal|elliptical\s+distribut\w*|exchangeabilit\w*|de\s+finetti|moment\s+problem|probability\s+inequalit\w*)\b"),
    ("optimal transport & wasserstein", "60B10", 1,
     r"\b(optimal\s+transport|wasserstein\s+(?:distance|metric|barycenter|gradient|space)|monge[\-\s]kantorovich|sinkhorn|schr[oö]dinger\s+bridge|transport\s+optimal|convergence\s+of\s+probability\s+measures)\b"),

    # --- Macro 2: Clinical Biostatistics & Biomedical Statistics (MSC 62N + 62P10) ---
    ("survival analysis", "62N01", 2,
     r"\b(survival\s+analys\w*|survival\s+data|survival\s+model\w*|censored\s+data|right[\-\s]censored|interval[\-\s]censored|proportional\s+hazards|cox\s+(?:model|regression|proportional)|kaplan[\-\s]meier|competing\s+risks?|recurrent\s+event\w*|cure\s+(?:rate\s+)?model\w*|accelerated\s+failure\s+time|frailty\s+model\w*|hazard\s+function\w*|time[\-\s]to[\-\s]event|analyse\s+de\s+survie)\b"),
    ("clinical trials & adaptive designs", "62P10", 2,
     r"\b(clinical\s+trial\w*|phase\s+(?:i|ii|iii|1|2|3)\s+trial|dose[\-\s]finding|dose[\-\s]escalation|adaptive\s+trial\w*|adaptive\s+design\w*|response[\-\s]adaptive|basket\s+trial|umbrella\s+trial|platform\s+trial|bioequivalence|interim\s+analys\w*|biomarker[\-\s]guided|randomized\s+controlled\s+trial\w*|essai\w*\s+clinique\w*)\b"),
    ("longitudinal & joint modeling", "62N02", 2,
     r"\b(longitudinal\s+data|longitudinal\s+stud\w*|repeated\s+measure\w*|joint\s+model\w*\s+(?:of|for)\s+longitudinal|generalized\s+estimating\s+equation\w*|gee\b|informative\s+dropout|dynamic\s+prediction|growth\s+curve\s+model\w*)\b"),
    ("statistical genetics & genomics", "62P10", 2,
     r"\b(statistical\s+genetic\w*|genome[\-\s]wide|gwas\b|gene\s+expression|rna[\-\s]seq|single[\-\s]cell|scrna|eqtl|polygenic\s+risk|mendelian\s+randomiz\w*|microarray|epigenetic\w*|methylation|microbiome|metagenomic\w*|phylogenetic\w*|haplotype|linkage\s+disequilibrium|bioinformatic\w*|multi[\-\s]omics|proteomic\w*)\b"),
    ("neuroimaging & biomedical imaging", "62H35", 2,
     r"\b(neuroimag\w*|fmri\b|functional\s+mri|brain\s+(?:imaging|connectivity|network|data)|electroencephalogra\w*|eeg\b|medical\s+imag\w*|radiomic\w*|image\s+segmentation|image\s+reconstruction|pet\s+imaging)\b"),
    ("epidemiology & infectious disease", "62P10", 2,
     r"\b(epidemiolog\w*|infectious\s+disease\w*|epidemic\s+model\w*|sir\s+model|seir\s+model|case[\-\s]control|cohort\s+stud\w*|disease\s+mapping|covid[\-\s]19|transmission\s+dynamic\w*|reproduction\s+number|vaccine\s+efficacy|pharmacovigilance|public\s+health\s+surveillance)\b"),
    ("precision medicine & dtr", "62P10", 2,
     r"\b(precision\s+medicine|personalized\s+medicine|individualized\s+treatment\s+rule\w*|dynamic\s+treatment\s+regim\w*|optimal\s+treatment\s+regim\w*|heterogeneous\s+treatment\s+effect\w*|subgroup\s+identificat\w*|electronic\s+health\s+record\w*|ehr\s+data)\b"),
    ("meta-analysis & diagnostic tests", "62P10", 2,
     r"\b(meta[\-\s]analys\w*|network\s+meta[\-\s]analys\w*|publication\s+bias|roc\s+curve\w*|receiver\s+operating|diagnostic\s+test\w*|diagnostic\s+accurac\w*|biomarker\s+evaluat\w*|sensitivity\s+and\s+specificity|pharmacokinetic\w*|pharmacometric\w*)\b"),

    # --- Macro 3: Machine Learning, High-Dimensional & Modern Data Science (MSC 62H/62J07/62R/68T) ---
    ("high-dimensional statistics", "62H12", 3,
     r"\b(high[\-\s]dimensional|high\s+dimension\w*|large\s+p\s+small\s+n|ultra[\-\s]high\s+dimensional|feature\s+screening|sure\s+independence\s+screening|covariance\s+matrix\s+estimat\w*|precision\s+matrix\s+estimat\w*|debias\w*\s+lasso|grande\s+dimension)\b"),
    ("shrinkage, lasso & variable selection", "62J07", 3,
     r"\b(lasso\b|ridge\s+regression|elastic\s+net|penalized\s+(?:regression|likelihood|estimat)|variable\s+selection|model\s+selection|feature\s+selection|sparse\s+(?:regression|estimat|recovery|model|signal)|compressed\s+sensing|l1\s+regulariz\w*|scad\b|group\s+lasso|best\s+subset\s+selection|bic\b|aic\b)\b"),
    ("probabilistic graphical models", "62H22", 3,
     r"\b(graphical\s+model\w*|gaussian\s+graphical|bayesian\s+network\w*|markov\s+random\s+field\w*|directed\s+acyclic\s+graph\w*|dag\s+learning|structure\s+learning|ising\s+graphical|conditional\s+independence\s+graph)\b"),
    ("network analysis & community detection", "62H30", 3,
     r"\b(network\s+(?:analys|model|data|infer|statist)|social\s+network\w*|community\s+detect\w*|graphon\w*|exponential\s+random\s+graph|ergm\b|latent\s+space\s+network|dynamic\s+network\w*|graph\s+matching|spectral\s+clustering)\b"),
    ("functional data analysis", "62R10", 3,
     r"\b(functional\s+data|functional\s+regression|functional\s+principal\s+component|fpca\b|curve\s+data|functional\s+time\s+series|functional\s+linear\s+model|donn[eé]es?\s+fonctionnelles?)\b"),
    ("classification & clustering", "62H30", 3,
     r"\b(classification|cluster\s+analys\w*|clustering\s+(?:algorithm|method|model)|mixture\s+model\w*|gaussian\s+mixture|discriminant\s+analys\w*|support\s+vector\s+machine\w*|svm\b|random\s+forest\w*|gradient\s+boost\w*|decision\s+tree\w*|cart\b|unsupervised\s+learning|semi[\-\s]supervised\s+learning|model[\-\s]based\s+clustering)\b"),
    ("factor models, pca & matrix completion", "62H25", 3,
     r"\b(principal\s+component\w*|pca\b|sparse\s+pca|factor\s+analys\w*|factor\s+model\w*|latent\s+factor\w*|matrix\s+completion|low[\-\s]rank\s+matrix|tensor\s+(?:decomposit|regress|complet|factor|pca)|dimension\s+reduct\w*|sufficient\s+dimension\s+reduct\w*|canonical\s+correlat\w*|independent\s+component\w*)\b"),
    ("statistical machine learning", "62R07", 3,
     r"\b(machine\s+learning|statistical\s+learning|kernel\s+method\w*|reproducing\s+kernel\s+hilbert|rkhs\b|rademacher\s+complexity|pac[\-\s]bayes|generalization\s+bound\w*|online\s+learning|transfer\s+learning|domain\s+adaptation|federated\s+learning|distributed\s+(?:learning|statist|estimat)|active\s+learning|meta[\-\s]learning|apprentissage\s+statistique)\b"),
    ("deep learning & neural networks", "68T07", 3,
     r"\b(deep\s+learning|neural\s+network\w*|deep\s+neural|convolutional\s+neural|transformer\w*\s+(?:model|architect|attention)|generative\s+adversarial|gan\b|diffusion\s+model\w*\s+(?:generat|score)|score[\-\s]based\s+generat\w*|large\s+language\s+model\w*|overparameteriz\w*|neural\s+tangent\s+kernel|representation\s+learning|r[eé]seaux?\s+de\s+neurones?)\b"),
    ("reinforcement learning & bandits", "68T05", 3,
     r"\b(reinforcement\s+learning|multi[\-\s]armed\s+bandit\w*|contextual\s+bandit\w*|bandit\s+(?:problem|algorithm)|q[\-\s]learning|policy\s+gradient|policy\s+evaluation|off[\-\s]policy|regret\s+(?:bound|minimiz)|thompson\s+sampl\w*|exploration[\-\s]exploitation)\b"),
    ("stochastic optimization", "90C15", 3,
     r"\b(stochastic\s+gradient|sgd\b|stochastic\s+optimiz\w*|convex\s+optimiz\w*|non[\-\s]convex\s+optimiz\w*|admm\b|proximal\s+gradient|coordinate\s+descent|minimax\s+optimiz\w*|zeroth[\-\s]order\s+optimiz\w*|mirror\s+descent)\b"),
    ("differential privacy & fairness", "68P27", 3,
     r"\b(differential\s+privac\w*|differentially\s+private|statistical\s+privac\w*|algorithmic\s+fairness|fair\s+machine\s+learning|adversarial\s+robustness|data\s+privac\w*|disclosure\s+limitation|synthetic\s+data\s+privac\w*)\b"),
    ("natural language & topic models", "68T50", 3,
     r"\b(topic\s+model\w*|latent\s+dirichlet\s+allocation|text\s+mining|natural\s+language\s+processing|nlp\b|speech\s+recognition|information\s+retrieval|recommender\s+system\w*|collaborative\s+filtering)\b"),

    # --- Macro 4: Time Series, Spatial Statistics & Econometrics / Finance (MSC 62M + 62P05/62P20 + 91G) ---
    ("time series analysis", "62M10", 4,
     r"\b(time\s+series|autoregressi\w*|arma\b|arima\b|var\s+model|vector\s+autoregress\w*|nonstationary\s+time|cointegrat\w*|unit\s+root|long\s+memory\s+time|seasonality|forecasting\s+time|s[eé]ries?\s+temporelles?|zeitreihen)\b"),
    ("spectral & wavelet time series", "62M15", 4,
     r"\b(spectral\s+(?:analys|densit|estimat)\w*|periodogram|fourier\s+analys\w*\s+time|frequency\s+domain|whittle\s+likelihood|locally\s+stationary|time[\-\s]frequency)\b"),
    ("state-space & filtering", "62M20", 4,
     r"\b(state[\-\s]space\s+model\w*|kalman\s+filter\w*|nonlinear\s+filtering|data\s+assimilation|ensemble\s+kalman|dynamic\s+linear\s+model\w*|signal\s+processing\s+statist\w*|filtering\s+and\s+smoothing)\b"),
    ("change-point detection", "62M10", 4,
     r"\b(change[\-\s]?point\w*|changepoint\w*|structural\s+break\w*|regime[\-\s]switching|anomaly\s+detect\w*|rupture\s+de\s+mod[eè]le|segmentation\s+of\s+time\s+series)\b"),
    ("spatial & spatio-temporal stats", "62M30", 4,
     r"\b(spatial\s+(?:statist|data|process|model|econometr|correlat|regress|predict|sampl|point)|spatio[\-\s]temporal|space[\-\s]time\s+(?:model|statist|data)|geostatist\w*|variogram|spatial\s+confounding|environmental\s+statist\w*|climate\s+(?:model|statist|extremes))\b"),
    ("econometrics & panel data", "62P20", 4,
     r"\b(econometr\w*|panel\s+data|longitudinal\s+econometr|dynamic\s+panel|microeconometr\w*|macroeconometr\w*|nonparametric\s+econometr\w*|endogeneit\w*|weak\s+instrument\w*|demand\s+estimat\w*|discrete\s+choice\s+model\w*|spatial\s+econometr\w*|[eé]conom[eé]trie)\b"),
    ("financial econometrics & volatility", "62P05", 4,
     r"\b(garch\b|arch\s+model|stochastic\s+volatilit\w*|realized\s+volatilit\w*|high[\-\s]frequency\s+financ\w*|financial\s+time\s+series|financial\s+econometr\w*|jump\s+detect\w*\s+financ\w*|market\s+microstructure|asset\s+pricing|value[\-\s]at[\-\s]risk|systemic\s+risk)\b"),
    ("mathematical finance & derivatives", "91G20", 4,
     r"\b(option\s+pricing|black[\-\s]scholes|mathematical\s+finance|quantitative\s+finance|portfolio\s+optimiz\w*|credit\s+risk|interest\s+rate\s+model\w*|hedging\s+strateg\w*|arbitrage|incomplete\s+market\w*|term\s+structure|default\s+risk|finance\s+math[eé]matique)\b"),
    ("actuarial science & risk theory", "62P05", 4,
     r"\b(actuarial|ruin\s+(?:probabilit|theor)|insurance\s+(?:risk|loss|pricing|claim)|risk\s+theor\w*|credibility\s+theor\w*|mortality\s+(?:model|forecast|table)|longevity\s+risk|reinsurance|pension\s+fund|actuariat)\b"),
]

NUM_CONCEPTS = len(MSC2020_TAXONOMY)
COMPILED_TAXONOMY = [
    (lbl, msc, midx, re.compile(pat, re.I))
    for lbl, msc, midx, pat in MSC2020_TAXONOMY
]
CONCEPT_TO_MACRO = [x[2] for x in MSC2020_TAXONOMY]


def mean_pool_norm(last_hidden: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    mask = attention_mask.unsqueeze(-1).type_as(last_hidden)
    summed = (last_hidden * mask).sum(dim=1)
    counts = mask.sum(dim=1).clamp(min=1e-9)
    return F.normalize((summed / counts).float(), p=2, dim=1)


def compute_collaborative_affinity(doc_labels, pids, edges):
    """
    Build a 100% data-driven collaborative concept affinity matrix S_collab in [0, 1]^(C x C)
    combining:
      (1) Within-dissertation multi-concept co-occurrence (PPMI-weighted cosine similarity)
      (2) Academic lineage co-occurrence (students sharing the same PhD advisor, and
          advisor -> student dissertation concept pairs from `edges`).
    """
    C = NUM_CONCEPTS
    # 1. Within-dissertation co-occurrence
    title_cooc = np.zeros((C, C), dtype=np.float64)
    concept_freq = np.zeros(C, dtype=np.float64)
    for lbls in doc_labels:
        for c in lbls:
            concept_freq[c] += 1.0
        if len(lbls) >= 2:
            for a, b in itertools.combinations(lbls, 2):
                title_cooc[a, b] += 1.0
                title_cooc[b, a] += 1.0

    # PPMI normalization on title co-occurrence
    total_docs = max(1.0, float(sum(1 for l in doc_labels if l)))
    ppmi_title = np.zeros((C, C), dtype=np.float64)
    for i in range(C):
        for j in range(C):
            if i == j or title_cooc[i, j] < 2.0:
                continue
            p_ij = title_cooc[i, j] / total_docs
            p_i = max(1.0, concept_freq[i]) / total_docs
            p_j = max(1.0, concept_freq[j]) / total_docs
            val = math.log(p_ij / (p_i * p_j) + 1e-12)
            if val > 0:
                # Dampen tiny counts smoothly
                damp = title_cooc[i, j] / (title_cooc[i, j] + 5.0)
                ppmi_title[i, j] = val * damp
    if ppmi_title.max() > 0:
        ppmi_title /= ppmi_title.max()

    # 2. Academic lineage (advisor-group & advisor-student) co-occurrence
    pid_to_lbls = {pid: lbls for pid, lbls in zip(pids, doc_labels) if lbls}
    adv_to_students = defaultdict(list)
    lineage_cooc = np.zeros((C, C), dtype=np.float64)
    for aid, sid in edges:
        if sid in pid_to_lbls:
            adv_to_students[aid].append(sid)
        # Direct advisor -> student concept transfer
        if aid in pid_to_lbls and sid in pid_to_lbls:
            for ca in pid_to_lbls[aid]:
                for cs in pid_to_lbls[sid]:
                    if ca != cs:
                        lineage_cooc[ca, cs] += 1.0
                        lineage_cooc[cs, ca] += 1.0
    # Sibling students advised by the same professor (collaborative filtering!)
    for aid, stus in adv_to_students.items():
        if len(stus) < 2 or len(stus) > 60:
            continue
        # Weight inversely by group size so huge labs don't dominate
        wt = 1.0 / math.sqrt(len(stus))
        c_counts = Counter()
        for sid in stus:
            for c in pid_to_lbls[sid]:
                c_counts[c] += 1
        c_list = list(c_counts.keys())
        for a, b in itertools.combinations(c_list, 2):
            val = wt * min(c_counts[a], c_counts[b])
            lineage_cooc[a, b] += val
            lineage_cooc[b, a] += val

    # Cosine-normalize lineage co-occurrence matrix
    row_norms = np.linalg.norm(lineage_cooc, axis=1, keepdims=True) + 1e-9
    lin_normed = lineage_cooc / row_norms
    # Direct normalized co-occurrence strength
    lin_diag = np.sqrt(np.outer(lineage_cooc.sum(axis=1) + 1e-6, lineage_cooc.sum(axis=0) + 1e-6))
    lin_aff = lineage_cooc / lin_diag
    np.fill_diagonal(lin_aff, 0.0)
    if lin_aff.max() > 0:
        lin_aff /= lin_aff.max()
    # Combine direct lineage affinity + 2nd-order cosine neighborhood similarity
    lin_cos = lin_normed @ lin_normed.T
    np.fill_diagonal(lin_cos, 0.0)
    S_lineage = np.clip(0.65 * lin_aff + 0.35 * lin_cos, 0.0, 1.0)

    # Fuse within-dissertation PPMI + academic lineage collaborative signal
    S_collab = np.clip(0.62 * ppmi_title + 0.38 * S_lineage, 0.0, 1.0)
    np.fill_diagonal(S_collab, 1.0)
    return S_collab, title_cooc, lineage_cooc


def finetune_and_encode(texts, doc_labels, S_collab, batch_size=256, max_epochs=25):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model_src = LOCAL_BASE_MODEL if os.path.exists(os.path.join(LOCAL_BASE_MODEL, "config.json")) else "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    print(f"Loading base multilingual model from {model_src} on {device}...")
    tokenizer = AutoTokenizer.from_pretrained(model_src)
    model = AutoModel.from_pretrained(model_src).to(device)

    # Build stratified multi-label train/val dataset (all docs matching >= 1 MSC 2020 concept)
    rng = random.Random(42)
    by_primary = defaultdict(list)
    for idx, lbls in enumerate(doc_labels):
        if lbls:
            # Stratify by rarest matched concept so smaller MSC classes get full representation
            by_primary[lbls[0]].append((texts[idx], lbls))

    train_items, val_items = [], []
    for cid, items in sorted(by_primary.items()):
        rng.shuffle(items)
        items = items[:900]
        n_val = max(2, int(len(items) * 0.10)) if len(items) >= 12 else 0
        val_items.extend(items[:n_val])
        train_items.extend(items[n_val:])

    print(
        f"Fine-tuning top 6 transformer layers with Multi-Label MSC 2020 Margin + Collaborative Regularizer "
        f"on {len(train_items):,} train / {len(val_items):,} val dissertations across {NUM_CONCEPTS} MSC 2020 concepts..."
    )

    # Freeze embeddings + bottom 6 layers (preserves 50+-language alignment); unfreeze top 6 + pooler
    for p in model.parameters():
        p.requires_grad = False
    for layer in model.encoder.layer[-6:]:
        for p in layer.parameters():
            p.requires_grad = True
    if hasattr(model, "pooler") and model.pooler is not None:
        for p in model.pooler.parameters():
            p.requires_grad = True

    prototypes_fine = nn.Parameter(torch.randn(NUM_CONCEPTS, 384, device=device))
    prototypes_macro = nn.Parameter(torch.randn(len(MACRO_DOMAINS), 384, device=device))
    nn.init.xavier_uniform_(prototypes_fine)
    nn.init.xavier_uniform_(prototypes_macro)

    # Collaborative affinity tensor for prototype smoothness regularization
    S_collab_t = torch.tensor(S_collab, dtype=torch.float32, device=device)
    # Keep top-8 strongest collaborative neighbors per concept (removes noise)
    S_reg = S_collab_t.clone()
    S_reg.fill_diagonal_(0.0)
    topk_vals, topk_idx = torch.topk(S_reg, k=min(8, NUM_CONCEPTS - 1), dim=1)
    S_mask = torch.zeros_like(S_reg)
    S_mask.scatter_(1, topk_idx, topk_vals)
    S_mask = 0.5 * (S_mask + S_mask.T)
    S_mask_sum = S_mask.sum().clamp(min=1e-6)

    trainable_params = [p for p in model.parameters() if p.requires_grad] + [prototypes_fine, prototypes_macro]
    opt = torch.optim.AdamW(trainable_params, lr=5e-5, weight_decay=0.015)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max_epochs, eta_min=1e-6)
    scaler = torch.cuda.amp.GradScaler(enabled=(device == "cuda"))

    ft_bs = 128
    t_ft = time.time()
    best_val_loss = float("inf")
    best_state = None
    best_pf = None
    no_improve = 0
    patience = 4
    min_delta = 0.001
    arc_margin = 0.10
    lambda_macro = 0.65
    lambda_collab = 0.35

    def build_targets(batch_items):
        B = len(batch_items)
        y_fine = torch.zeros((B, NUM_CONCEPTS), dtype=torch.float32, device=device)
        y_macro = torch.zeros((B, len(MACRO_DOMAINS)), dtype=torch.float32, device=device)
        for bi, (_, lbls) in enumerate(batch_items):
            w_f = 1.0 / len(lbls)
            macros = set()
            for c in lbls:
                y_fine[bi, c] = w_f
                macros.add(CONCEPT_TO_MACRO[c])
            w_m = 1.0 / len(macros)
            for m in macros:
                y_macro[bi, m] = w_m
        return y_fine, y_macro

    for epoch in range(max_epochs):
        model.train()
        rng.shuffle(train_items)
        tot_loss = 0.0
        hits_f = 0
        seen = 0

        for i in range(0, len(train_items), ft_bs):
            batch = train_items[i : i + ft_bs]
            b_texts = [x[0] for x in batch]
            y_f, y_m = build_targets(batch)
            pos_f = (y_f > 0).float()
            pos_m = (y_m > 0).float()
            enc = tokenizer(b_texts, padding=True, truncation=True, max_length=64, return_tensors="pt").to(device)
            opt.zero_grad(set_to_none=True)
            with torch.cuda.amp.autocast(enabled=(device == "cuda")):
                out = model(**enc)
                emb = mean_pool_norm(out.last_hidden_state, enc["attention_mask"])
                pf_norm = F.normalize(prototypes_fine.float(), p=2, dim=1)
                pm_norm = F.normalize(prototypes_macro.float(), p=2, dim=1)
                cos_f = emb @ pf_norm.T
                cos_m = emb @ pm_norm.T

                # Multi-label soft-target cosine margin cross-entropy:
                # pulls embedding simultaneously toward ALL matching MSC 2020 concepts!
                logits_f = (cos_f - arc_margin * pos_f) * 18.0
                logits_m = (cos_m - (arc_margin * 0.75) * pos_m) * 14.0
                # Label smoothing 0.03
                y_f_s = y_f * 0.97 + 0.03 / NUM_CONCEPTS
                y_m_s = y_m * 0.97 + 0.03 / len(MACRO_DOMAINS)
                loss_f = -(y_f_s * F.log_softmax(logits_f, dim=1)).sum(dim=1).mean()
                loss_m = -(y_m_s * F.log_softmax(logits_m, dim=1)).sum(dim=1).mean()

                # Collaborative prototype smoothness regularizer:
                # pulls frequently co-occurring MSC prototypes closer on the unit hypersphere!
                proto_sim = pf_norm @ pf_norm.T
                loss_collab = (S_mask * (1.0 - proto_sim)).sum() / S_mask_sum

                loss = loss_f + lambda_macro * loss_m + lambda_collab * loss_collab

            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
            tot_loss += float(loss.item()) * len(batch)
            pred_f = cos_f.argmax(dim=1)
            hits_f += int(pos_f[torch.arange(len(batch), device=device), pred_f].sum().item())
            seen += len(batch)
        scheduler.step()

        # Validation (margin-free multi-label soft cross-entropy & hit@1 accuracy)
        model.eval()
        val_loss = 0.0
        val_hits_f = 0
        val_hits_m = 0
        val_seen = 0
        with torch.no_grad():
            pf_norm = F.normalize(prototypes_fine.float(), p=2, dim=1)
            pm_norm = F.normalize(prototypes_macro.float(), p=2, dim=1)
            for i in range(0, len(val_items), ft_bs):
                batch = val_items[i : i + ft_bs]
                b_texts = [x[0] for x in batch]
                y_f, y_m = build_targets(batch)
                pos_f = (y_f > 0).float()
                pos_m = (y_m > 0).float()
                enc = tokenizer(b_texts, padding=True, truncation=True, max_length=64, return_tensors="pt").to(device)
                with torch.cuda.amp.autocast(enabled=(device == "cuda")):
                    out = model(**enc)
                    emb = mean_pool_norm(out.last_hidden_state, enc["attention_mask"])
                    logits_f = (emb @ pf_norm.T) * 18.0
                    logits_m = (emb @ pm_norm.T) * 14.0
                    vf = -(y_f * F.log_softmax(logits_f, dim=1)).sum(dim=1).mean()
                    vm = -(y_m * F.log_softmax(logits_m, dim=1)).sum(dim=1).mean()
                    vloss = vf + lambda_macro * vm
                val_loss += float(vloss.item()) * len(batch)
                val_hits_f += int(pos_f[torch.arange(len(batch), device=device), logits_f.argmax(dim=1)].sum().item())
                val_hits_m += int(pos_m[torch.arange(len(batch), device=device), logits_m.argmax(dim=1)].sum().item())
                val_seen += len(batch)

        tr_l = tot_loss / seen
        tr_a = hits_f / seen
        va_l = val_loss / val_seen
        va_a = val_hits_f / val_seen
        va_ma = val_hits_m / val_seen
        lr_now = opt.param_groups[0]["lr"]
        mark = ""
        if va_l < best_val_loss - min_delta:
            best_val_loss = va_l
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            best_pf = F.normalize(prototypes_fine.detach().float(), p=2, dim=1).cpu().numpy()
            no_improve = 0
            mark = " *best*"
        else:
            no_improve += 1
        print(
            f"  Epoch {epoch + 1:2d}/{max_epochs}: train_loss={tr_l:.4f} acc={tr_a:.3f} | "
            f"val_loss={va_l:.4f} msc_acc={va_a:.3f} macro_acc={va_ma:.3f} | lr={lr_now:.1e} ({time.time() - t_ft:.1f}s){mark}"
        )
        if no_improve >= patience:
            print(f"  Early stopping at epoch {epoch + 1}: val_loss converged near minimum ({best_val_loss:.4f}).")
            break

    if best_state is not None:
        model.load_state_dict({k: v.to(device) for k, v in best_state.items()})

    os.makedirs(FINETUNED_DIR, exist_ok=True)
    model.save_pretrained(FINETUNED_DIR)
    tokenizer.save_pretrained(FINETUNED_DIR)

    # Encode ALL dissertations in FP16
    model.eval()
    if device == "cuda":
        model = model.half()
    all_embs = np.empty((len(texts), 384), dtype=np.float32)
    t0 = time.time()
    with torch.no_grad():
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            enc = tokenizer(batch, padding=True, truncation=True, max_length=72, return_tensors="pt").to(device)
            out = model(**enc)
            emb = mean_pool_norm(out.last_hidden_state, enc["attention_mask"])
            all_embs[i : i + len(batch)] = emb.cpu().numpy()
            if (i // batch_size) % 50 == 0:
                print(f"  Embedded {min(len(texts), i + len(batch)):,}/{len(texts):,} dissertations ({time.time() - t0:.1f}s)...")
    print(f"Finished encoding {len(texts):,} dissertations in {time.time() - t0:.1f}s!")
    return all_embs, best_pf


def main():
    t_start = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, name, school, year, primary_field, thesis, descendant_count, student_count
        FROM people
        WHERE is_core_stat = 1 AND thesis IS NOT NULL AND LENGTH(TRIM(thesis)) >= 12
    """).fetchall()
    edges = conn.execute("SELECT advisor_id, student_id FROM edges").fetchall()
    conn.close()

    pids = [r["id"] for r in rows]
    texts = [r["thesis"].strip() for r in rows]
    print(f"Loaded {len(rows):,} Core Stat/Biostat/Prob dissertations from SQLite.")

    # 1. Multi-label MSC 2020 matching (every dissertation can match multiple concepts!)
    doc_labels = []
    multi_cnt = 0
    matched_cnt = 0
    for txt in texts:
        hits = []
        for cid, (_, _, _, rx) in enumerate(COMPILED_TAXONOMY):
            if rx.search(txt):
                hits.append(cid)
        if hits:
            matched_cnt += 1
            if len(hits) >= 2:
                multi_cnt += 1
        doc_labels.append(hits)
    print(
        f"Matched {matched_cnt:,}/{len(rows):,} dissertations ({100.0 * matched_cnt / len(rows):.1f}%) "
        f"to {NUM_CONCEPTS} MSC 2020 + modern statistical classifications ({multi_cnt:,} multi-label dissertations!)."
    )

    # 2. Compute empirical collaborative signal (within-dissertation co-occurrence + advisor-group lineage)
    S_collab, title_cooc, lineage_cooc = compute_collaborative_affinity(doc_labels, pids, edges)

    # Print top collaborative pairs to verify data-driven signal (e.g. experimental design <-> causal inference!)
    pair_list = []
    for i in range(NUM_CONCEPTS):
        for j in range(i + 1, NUM_CONCEPTS):
            if S_collab[i, j] > 0.05:
                pair_list.append((S_collab[i, j], int(title_cooc[i, j]), int(lineage_cooc[i, j]), MSC2020_TAXONOMY[i][0], MSC2020_TAXONOMY[j][0]))
    pair_list.sort(reverse=True)
    print("Top 12 empirical collaborative concept pairs (title_cooc + advisor_lineage):")
    for sc, tc, lc, a, b in pair_list[:12]:
        print(f"  {a:32s} <-> {b:32s} | collab={sc:.3f} (titles={tc:3d}, lineage={lc:3d})")
    idx_ci = next(i for i, x in enumerate(MSC2020_TAXONOMY) if x[0] == "causal inference")
    idx_ed = next(i for i, x in enumerate(MSC2020_TAXONOMY) if x[0] == "experimental design")
    print(f"  Check causal inference <-> experimental design: collab={S_collab[idx_ci, idx_ed]:.3f} (titles={int(title_cooc[idx_ci, idx_ed])}, lineage={int(lineage_cooc[idx_ci, idx_ed])})")

    # 3. GPU Fine-tune & encode all dissertations
    all_embs, prototypes_fine = finetune_and_encode(texts, doc_labels, S_collab, batch_size=256, max_epochs=25)

    # 4. Assign EVERY dissertation to its best MSC 2020 / modern statistical classification
    #    (using fine-tuned cosine similarity to learned prototypes, with a gentle +0.18 prior
    #     for concepts explicitly matched in the dissertation title)
    sims = all_embs @ prototypes_fine.T  # (N, NUM_CONCEPTS)
    for idx, lbls in enumerate(doc_labels):
        if lbls:
            for c in lbls:
                sims[idx, c] += 0.18
    assigned = sims.argmax(axis=1)

    concept_docs = defaultdict(list)
    for idx, cid in enumerate(assigned):
        concept_docs[int(cid)].append(idx)

    # Keep all concepts with >= 15 dissertations (all 100% genuine MSC 2020 / statistical concepts!)
    valid_cids = [c for c in range(NUM_CONCEPTS) if len(concept_docs[c]) >= 15]
    print(f"\nActive MSC 2020 + modern statistical classifications (>=15 dissertations): {len(valid_cids)}/{NUM_CONCEPTS}")

    # Compute empirical centroids of assigned dissertations in fine-tuned 384-D space
    centroids = []
    sizes = []
    for cid in valid_cids:
        idxs = concept_docs[cid]
        c_emb = all_embs[idxs].mean(axis=0)
        c_emb = c_emb / (np.linalg.norm(c_emb) + 1e-9)
        centroids.append(c_emb)
        sizes.append(len(idxs))
    centroids = np.vstack(centroids)
    sizes_arr = np.array(sizes, dtype=np.float64)

    # 5. Collaborative + Semantic 2D Projection via cosine t-SNE (ZERO hand-coded pairs!)
    cos_sim = np.clip(centroids @ centroids.T, -1.0, 1.0)
    D_sem = np.clip(1.0 - cos_sim, 0.0, 2.0)

    S_sub = S_collab[np.ix_(valid_cids, valid_cids)]
    D_collab = np.clip(1.0 - S_sub, 0.0, 1.0)

    # Blend 68% fine-tuned semantic distance + 32% empirical collaborative distance
    D_fused = 0.68 * D_sem + 0.32 * D_collab
    np.fill_diagonal(D_fused, 0.0)

    perp = min(22, max(8, len(valid_cids) // 4))
    tsne = TSNE(
        n_components=2,
        perplexity=perp,
        early_exaggeration=14.0,
        metric="precomputed",
        init="random",
        learning_rate=180.0,
        n_iter=2500,
        random_state=42,
    )
    coords = tsne.fit_transform(D_fused)

    # PCA-align horizontally into 1280 x 560 widescreen viewBox
    coords_c = coords - coords.mean(axis=0, keepdims=True)
    _, _, vt = np.linalg.svd(coords_c, full_matrices=False)
    coords_rot = coords_c @ vt.T
    xs = 90.0 + (coords_rot[:, 0] - coords_rot[:, 0].min()) / (coords_rot[:, 0].max() - coords_rot[:, 0].min() + 1e-9) * 1100.0
    ys = 58.0 + (coords_rot[:, 1] - coords_rot[:, 1].min()) / (coords_rot[:, 1].max() - coords_rot[:, 1].min() + 1e-9) * 444.0

    # Gently pull the 5 MSC macro-domain neighborhoods closer so domains touch & overlap seamlessly
    gcx, gcy = 640.0, 280.0
    macro_assign = [CONCEPT_TO_MACRO[c] for c in valid_cids]
    for m_id in range(len(MACRO_DOMAINS)):
        m_idxs = [i for i, m in enumerate(macro_assign) if m == m_id]
        if not m_idxs:
            continue
        w = np.sqrt(sizes_arr[m_idxs])
        cx = float(np.average(xs[m_idxs], weights=w))
        cy = float(np.average(ys[m_idxs], weights=w))
        ncx = gcx + (cx - gcx) * 0.68
        ncy = gcy + (cy - gcy) * 0.68
        for i in m_idxs:
            xs[i] = ncx + (xs[i] - cx) * 0.88
            ys[i] = ncy + (ys[i] - cy) * 0.88

    # Re-normalize into clean canvas bounds
    xs = 82.0 + (xs - xs.min()) / (xs.max() - xs.min() + 1e-9) * 1116.0
    ys = 52.0 + (ys - ys.min()) / (ys.max() - ys.min() + 1e-9) * 456.0

    # Bubble radii (proportional to sqrt(count)) + gentle overlap relaxation (allow slight touching, prevent text collision)
    max_sz = float(sizes_arr.max())
    min_sz = float(sizes_arr.min())
    radii = np.array([
        11.0 + 27.0 * ((math.sqrt(sz) - math.sqrt(min_sz)) / (math.sqrt(max_sz) - math.sqrt(min_sz) + 1e-9))
        for sz in sizes_arr
    ])

    for _ in range(90):
        for i in range(len(valid_cids)):
            for j in range(i + 1, len(valid_cids)):
                dx = xs[j] - xs[i]
                dy = ys[j] - ys[i]
                dist = math.hypot(dx, dy) + 1e-6
                min_d = (radii[i] + radii[j]) * 0.88 + 4.0
                if dist < min_d:
                    push = (min_d - dist) * 0.35
                    ux, uy = dx / dist, dy / dist
                    xs[i] -= ux * push
                    ys[i] -= uy * push
                    xs[j] += ux * push
                    ys[j] += uy * push
        xs = np.clip(xs, 68.0, 1212.0)
        ys = np.clip(ys, 46.0, 514.0)

    # Build final JSON payload
    clusters_out = []
    for m_id, m_name in enumerate(MACRO_DOMAINS):
        m_idxs = [i for i, m in enumerate(macro_assign) if m == m_id]
        if not m_idxs:
            continue
        w = np.sqrt(sizes_arr[m_idxs])
        cx = float(np.average(xs[m_idxs], weights=w))
        cy = float(np.average(ys[m_idxs], weights=w))
        tot_c = int(sum(sizes_arr[i] for i in m_idxs))
        top_c = [MSC2020_TAXONOMY[valid_cids[i]][0] for i in sorted(m_idxs, key=lambda i: -sizes_arr[i])[:6]]
        print(f"  Domain {m_id}: {m_name:34s} ({len(m_idxs):2d} MSC concepts, {tot_c:6,d} dissertations) | top: {top_c[:5]}")
        clusters_out.append({
            "id": m_id,
            "name": m_name,
            "color": CLUSTER_PALETTE[m_id],
            "count": tot_c,
            "cx": round(cx, 1),
            "cy": round(cy, 1),
        })

    topics_out = []
    for pos, cid in enumerate(valid_cids):
        lbl, msc_code, m_id, _ = MSC2020_TAXONOMY[cid]
        idxs = concept_docs[cid]
        # Sort representative dissertations by similarity to concept prototype + scholar impact + recency
        idxs_sorted = sorted(
            idxs,
            key=lambda i: (sims[i, cid] * 2.5 + math.log1p(rows[i]["descendant_count"] + rows[i]["student_count"] * 3) * 0.15 + (0.08 if (rows[i]["year"] or 0) >= 2015 else 0.0)),
            reverse=True,
        )
        samples = []
        for si in idxs_sorted[:8]:
            r = rows[si]
            samples.append({
                "id": r["id"],
                "name": r["name"],
                "school": r["school"] or "",
                "year": r["year"],
                "thesis": r["thesis"].strip()[:220],
                "field": r["primary_field"],
            })

        # Top 4 empirically linked collaborative neighbor concepts (for tooltip/drawer!)
        collab_neighbors = []
        for opos, ocid in enumerate(valid_cids):
            if ocid != cid and S_collab[cid, ocid] > 0.04:
                collab_neighbors.append((float(S_collab[cid, ocid]), MSC2020_TAXONOMY[ocid][0]))
        collab_neighbors.sort(reverse=True)
        top_terms = [f"MSC {msc_code}"] + [nm for _, nm in collab_neighbors[:4]]

        yrs = [rows[i]["year"] for i in idxs if rows[i]["year"] and 1900 <= rows[i]["year"] <= 2026]
        med_yr = int(np.median(yrs)) if yrs else None

        topics_out.append({
            "id": pos,
            "label": lbl,
            "msc_code": msc_code,
            "count": len(idxs),
            "x": round(float(xs[pos]), 1),
            "y": round(float(ys[pos]), 1),
            "r": round(float(radii[pos]), 1),
            "cluster_id": m_id,
            "cluster_name": MACRO_DOMAINS[m_id],
            "color": CLUSTER_PALETTE[m_id],
            "median_year": med_yr,
            "top_terms": top_terms,
            "samples": samples,
        })

    topics_out.sort(key=lambda t: -t["count"])

    payload = {
        "model": "MSC-2020 + Collaborative Fine-Tuned Multilingual MiniLM-L12-v2",
        "total_dissertations": len(rows),
        "num_topics": len(topics_out),
        "viewBox": [1280, 560],
        "clusters": clusters_out,
        "topics": topics_out,
    }

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO meta_stats (key, value_json) VALUES ('dissertation_topic_map', ?)",
        (json.dumps(payload, ensure_ascii=False),),
    )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    sz_kb = os.path.getsize(OUT_JSON) / 1024.0
    print(f"\nSaved {OUT_JSON} ({sz_kb:.1f} KB) + meta_stats['dissertation_topic_map'] in {time.time() - t_start:.1f}s!")


if __name__ == "__main__":
    main()
