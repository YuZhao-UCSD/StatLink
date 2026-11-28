#!/usr/bin/env python3
"""
MSC 2020 + Collaborative Fine-Tuned 2D Dissertation Topic Landscape Builder.

Combines:
  1. Official AMS MSC 2020 (Section 62 Statistics + Section 60 Probability) + modern
     ML / Biostatistics / Finance fine-grained statistical sub-classifications (~185-200
     lush, overlapping, 100%-mathematical/statistical bubbles — zero "public"/"team" noise).
  2. Multi-Label Soft-Target Additive-Margin Cosine Fine-Tuning of top 6 layers of
     language-invariant `paraphrase-multilingual-MiniLM-L12-v2` (bottom 6 frozen).
  3. Data-Driven Collaborative Signal (zero hand-coded pairs):
     - Within-dissertation multi-concept co-occurrence (PPMI)
     - Academic lineage collaborative co-occurrence (shared PhD advisor & advisor-student
       dissertation pairs from `edges` in SQLite)
     Used BOTH as a prototype-geometry regularizer during GPU training AND blended
     (68% fine-tuned semantic + 32% collaborative) before cosine t-SNE projection!
  4. Full lush AI-lab visual schema (`width: 1280`, `height: 560`, ambient Gaussian cluster
     auras with `cx, cy, r, color`, `year_min`, `year_max`, `year_median`, `msc_code`,
     and empirically linked collaborative neighbors in `top_terms`).

Pass `--reuse-finetuned` to reuse the already-trained checkpoint in
`data_raw/stat_minilm_msc2020_finetuned` (~35s rebuild without retraining).
"""

import argparse
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
    "Statistical Inference & Theory",
    "Stochastic Processes & Probability",
    "Clinical Biostatistics",
    "ML & High-Dimensional Stats",
    "Time Series & Econometrics",
]

CLUSTER_PALETTE = [
    "#3b5bdb",  # Indigo  — Statistical Inference & Theory
    "#0f9d58",  # Emerald — Stochastic Processes & Probability
    "#d97706",  # Amber   — Clinical Biostatistics
    "#8b5cf6",  # Violet  — ML & High-Dimensional Stats
    "#e11d48",  # Rose    — Time Series & Econometrics
]

# ---------------------------------------------------------------------------
# Fine-Grained Authoritative MSC 2020 (Section 62 Statistics + Section 60 Probability)
# + Modern Statistical ML / Biostatistics / Finance Sub-Classifications (~175 concepts)
# Format: (label, msc_code, macro_domain_idx, regex_pattern)
# ---------------------------------------------------------------------------
MSC2020_FINE_TAXONOMY = [
    # === Macro 0: Bayesian, Causal, Experimental Design & Core Inference (MSC 62A-62L) ===
    ("causal inference", "62D20", 0, r"\b(causal\s+infer\w*|causal\s+effect\w*|potential\s+outcome\w*|counterfactual\w*)\b"),
    ("propensity score methods", "62D20", 0, r"\b(propensity\s+score\w*|inverse\s+probability\s+weight\w*|matching\s+estimator\w*|observational\s+stud\w*)\b"),
    ("instrumental variables", "62D20", 0, r"\b(instrumental\s+variable\w*|mendelian\s+randomiz\w*|weak\s+instrument\w*|two[\-\s]stage\s+least\s+squares)\b"),
    ("causal mediation", "62D20", 0, r"\b(causal\s+mediat\w*|mediation\s+analysis|direct\s+and\s+indirect\s+effect\w*|natural\s+direct\s+effect)\b"),
    ("causal discovery & dags", "62D20", 0, r"\b(causal\s+discover\w*|causal\s+graph\w*|structural\s+causal\s+model\w*|unmeasured\s+confound\w*|sensitivity\s+analysis\s+confound\w*)\b"),
    ("treatment effects & did", "62D20", 0, r"\b(treatment\s+effect\w*|average\s+treatment\s+effect|difference[\-\s]in[\-\s]differences|synthetic\s+control\w*|regression\s+discontinuit\w*)\b"),
    ("experimental design", "62K05", 0, r"\b(experimental\s+design\w*|design\s+of\s+experiment\w*|randomized\s+experiment\w*|a/b\s+test\w*)\b"),
    ("optimal design", "62K05", 0, r"\b(optimal\s+design\w*|d[\-\s]optimal|a[\-\s]optimal|e[\-\s]optimal|c[\-\s]optimal|minimax\s+design\w*|bayesian\s+optimal\s+design)\b"),
    ("factorial & block designs", "62K15", 0, r"\b(factorial\s+design\w*|fractional\s+factorial|block\s+design\w*|incomplete\s+block|latin\s+square|split[\-\s]plot|orthogonal\s+array\w*)\b"),
    ("computer experiments", "62K20", 0, r"\b(computer\s+experiment\w*|latin\s+hypercube|response\s+surface\w*|surrogate\s+model\w*|emulat\w*\s+computer)\b"),
    ("bayesian inference", "62F15", 0, r"\b(bayesian\s+infer\w*|bayesian\s+estimat\w*|bayesian\s+analysis|bayesian\s+method\w*|inf[eé]rence\s+bay[eé]sienne)\b"),
    ("hierarchical bayes", "62F15", 0, r"\b(hierarchical\s+bayes\w*|bayesian\s+hierarchical|multilevel\s+bayes\w*|bayes\s+factor\w*|bayesian\s+model\s+averaging)\b"),
    ("posterior asymptotics", "62F15", 0, r"\b(posterior\s+contract\w*|posterior\s+consistenc\w*|bernstein[\-\s]von\s+mises|objective\s+prior\w*|reference\s+prior\w*|jeffreys\s+prior)\b"),
    ("bayesian nonparametrics", "62G05", 0, r"\b(bayesian\s+nonparametric\w*|nonparametric\s+bayes\w*|polya\s+tree|stick[\-\s]breaking|indian\s+buffet)\b"),
    ("dirichlet processes", "62G05", 0, r"\b(dirichlet\s+process\w*|pitman[\-\s]yor|chinese\s+restaurant\s+process|hierarchical\s+dirichlet)\b"),
    ("empirical bayes", "62C12", 0, r"\b(empirical\s+bayes\w*|compound\s+decision|robbins\s+method|james[\-\s]stein|stein\s+estimat\w*|horseshoe\s+prior)\b"),
    ("decision theory", "62C05", 0, r"\b(decision\s+theor\w*|statistical\s+decision|admissib\w*|inadmissib\w*|bayes\s+risk|complete\s+class|loss\s+function\w*)\b"),
    ("minimax theory", "62C20", 0, r"\b(minimax\s+(?:rate|risk|estimat|optim|bound|test|adapt)|optimal\s+rate\w*\s+of\s+convergence)\b"),
    ("hypothesis testing", "62F03", 0, r"\b(hypothesis\s+test\w*|likelihood\s+ratio\s+test\w*|wald\s+test|score\s+test|two[\-\s]sample\s+test\w*|equivalence\s+test\w*)\b"),
    ("goodness of fit", "62F03", 0, r"\b(goodness[\-\s]of[\-\s]fit|chi[\-\s]square\w*\s+test|kolmogorov[\-\s]smirnov|cram[eé]r[\-\s]von\s+mises|anderson[\-\s]darling|normality\s+test)\b"),
    ("rank & permutation tests", "62G10", 0, r"\b(permutation\s+test\w*|rank\s+test\w*|signed[\-\s]rank|wilcoxon|mann[\-\s]whitney|randomization\s+test\w*|nonparametric\s+test\w*)\b"),
    ("multiple testing & fdr", "62J15", 0, r"\b(multiple\s+testing|multiple\s+comparison\w*|false\s+discovery\s+rate|fdr\s+control|benjamini[\-\s]hochberg|family[\-\s]wise\s+error|large[\-\s]scale\s+testing)\b"),
    ("selective inference & knockoffs", "62J15", 0, r"\b(post[\-\s]selection\s+infer\w*|selective\s+infer\w*|knockoff\w*|model[\-\s]x\s+knockoff|data\s+carving)\b"),
    ("maximum likelihood & m-estimation", "62F10", 0, r"\b(maximum\s+likelihood|m[\-\s]estimat\w*|point\s+estimat\w*|estimating\s+equation\w*|method\s+of\s+moments|cram[eé]r[\-\s]rao|fisher\s+information)\b"),
    ("exponential families", "62E10", 0, r"\b(exponential\s+famil\w*|sufficient\s+statistic\w*|ancillary\s+statistic|curved\s+exponential|natural\s+exponential)\b"),
    ("asymptotic theory", "62E20", 0, r"\b(asymptotic\s+(?:theor|distribut|propert|efficienc|normalit|expansion|infer)|edgeworth\s+expansion|saddlepoint\s+approximat\w*|le\s+cam|contiguity)\b"),
    ("empirical processes & u-stats", "62G20", 0, r"\b(empirical\s+process\w*|u[\-\s]statistic\w*|v[\-\s]statistic\w*|donsker|glivenko[\-\s]cantelli|empirical\s+distribution\s+function)\b"),
    ("nonparametric regression", "62G08", 0, r"\b(nonparametric\s+regression|non[\-\s]parametric\s+regression|local\s+polynomial|nadaraya[\-\s]watson|kernel\s+regression|additive\s+model\w*)\b"),
    ("kernel & spline smoothing", "62G08", 0, r"\b(kernel\s+smooth\w*|spline\s+smooth\w*|smoothing\s+spline\w*|penalized\s+spline\w*|b[\-\s]spline\w*|bandwidth\s+selection)\b"),
    ("wavelet estimation", "62G05", 0, r"\b(wavelet\s+(?:estimat|smooth|shrinkage|threshold|denois|analys)|besov\s+space|multiresolution\s+analys\w*)\b"),
    ("shape-constrained inference", "62G05", 0, r"\b(isotonic\s+regression|shape[\-\s]constrained|monotone\s+regression|log[\-\s]concave|convex\s+regression|order[\-\s]restricted\s+infer\w*)\b"),
    ("density estimation", "62G07", 0, r"\b(density\s+estimat\w*|kernel\s+density|deconvolution\s+density|mixture\s+density|histogram\s+estimat\w*)\b"),
    ("semiparametric inference", "62G05", 0, r"\b(semiparametric|semi[\-\s]parametric|efficient\s+influence\s+function|nuisance\s+parameter\w*|profile\s+likelihood|single[\-\s]index\s+model\w*|partially\s+linear)\b"),
    ("doubly robust & tmle", "62G05", 0, r"\b(doubly\s+robust|targeted\s+maximum\s+likelihood|tmle|double\s+machine\s+learning|debiased\s+machine\s+learning|cross[\-\s]fitting)\b"),
    ("quantile regression", "62G08", 0, r"\b(quantile\s+regression|conditional\s+quantile\w*|expectile\s+regression|quantile\s+estimat\w*|quantile\s+process\w*)\b"),
    ("bootstrap & resampling", "62F40", 0, r"\b(bootstrap\w*|jackknife|resampling\s+method\w*|subsampling|wild\s+bootstrap|block\s+bootstrap)\b"),
    ("empirical likelihood", "62G09", 0, r"\b(empirical\s+likelihood|generalized\s+empirical\s+likelihood|exponential\s+tilting|jackknife\s+empirical\s+likelihood)\b"),
    ("robust statistics", "62F35", 0, r"\b(robust\s+(?:statist|estimat|regression|infer|test|method)|breakdown\s+point|influence\s+function\w*|huber\s+loss|outlier\s+detect\w*|trimmed\s+mean)\b"),
    ("generalized linear models", "62J12", 0, r"\b(generalized\s+linear\s+model\w*|logistic\s+regression|poisson\s+regression|probit\s+model\w*|quasi[\-\s]likelihood|overdispersion|zero[\-\s]inflated)\b"),
    ("categorical & contingency tables", "62H17", 0, r"\b(categorical\s+data|contingency\s+table\w*|log[\-\s]linear\s+model\w*|multinomial\s+logit|ordinal\s+regression|correspondence\s+analysis)\b"),
    ("linear regression & anova", "62J05", 0, r"\b(linear\s+regression|linear\s+model\w*|analysis\s+of\s+variance|anova|ancova|least\s+squares\s+estimat\w*|regression\s+diagnostic\w*)\b"),
    ("mixed-effects models", "62J05", 0, r"\b(mixed[\-\s]effects?\s+model\w*|linear\s+mixed\s+model\w*|generalized\s+linear\s+mixed|variance\s+component\w*|random\s+effects?\s+model\w*|multilevel\s+model\w*)\b"),
    ("measurement error models", "62J05", 0, r"\b(measurement\s+error|errors[\-\s]in[\-\s]variables|misclassification|simex|regression\s+calibration)\b"),
    ("survey sampling", "62D05", 0, r"\b(survey\s+sampl\w*|sample\s+survey\w*|finite\s+population|stratified\s+sampl\w*|complex\s+survey|horvitz[\-\s]thompson|capture[\-\s]recapture|nonprobability\s+sample\w*)\b"),
    ("small area estimation", "62D05", 0, r"\b(small\s+area\s+estimat\w*|fay[\-\s]herriot|domain\s+estimat\w*|survey\s+nonresponse)\b"),
    ("missing data & imputation", "62D10", 0, r"\b(missing\s+data|multiple\s+imputat\w*|imputation\s+method\w*|missing\s+not\s+at\s+random|mnar|incomplete\s+data|nonignorable\s+missing\w*)\b"),
    ("em algorithm", "62F10", 0, r"\b(em\s+algorithm|expectation[\-\s]maximization|data\s+augmentation|mm\s+algorithm|majorization[\-\s]minimization)\b"),
    ("sequential analysis", "62L10", 0, r"\b(sequential\s+(?:analys|test|estimat|design|method|sampl|detect)|group\s+sequential|sprt|wald\s+sequential|quickest\s+detection)\b"),
    ("optimal stopping", "62L15", 0, r"\b(optimal\s+stopping|stopping\s+time\w*|secretary\s+problem|dynkin\s+game|free\s+boundary\s+stopping)\b"),
    ("monte carlo simulation", "65C05", 0, r"\b(monte\s+carlo\s+(?:simulat|integrat|method|estimat)|importance\s+sampl\w*|quasi[\-\s]monte\s+carlo|multilevel\s+monte\s+carlo|variance\s+reduction)\b"),
    ("mcmc & gibbs sampling", "65C05", 0, r"\b(markov\s+chain\s+monte\s+carlo|mcmc|metropolis[\-\s]hastings|gibbs\s+sampl\w*|reversible\s+jump|slice\s+sampl\w*)\b"),
    ("hamiltonian & langevin mc", "65C05", 0, r"\b(hamiltonian\s+monte\s+carlo|hmc\b|langevin\s+monte\s+carlo|mala\b|unadjusted\s+langevin|stochastic\s+gradient\s+mcmc)\b"),
    ("sequential monte carlo & abc", "65C05", 0, r"\b(sequential\s+monte\s+carlo|particle\s+filter\w*|particle\s+mcmc|approximate\s+bayesian\s+computat\w*|abc\s+method|likelihood[\-\s]free\s+infer\w*)\b"),
    ("variational inference", "62F15", 0, r"\b(variational\s+(?:infer|bayes|approximat|method)|mean[\-\s]field\s+variational|laplace\s+approximat\w*|expectation\s+propagation|inla\b)\b"),
    ("conformal prediction", "62G15", 0, r"\b(conformal\s+predict\w*|conformal\s+infer\w*|distribution[\-\s]free\s+predict\w*|split\s+conformal|prediction\s+set\w*)\b"),
    ("confidence regions & uq", "62F25", 0, r"\b(confidence\s+(?:band|region|interval|set)\w*|prediction\s+interval\w*|tolerance\s+interval\w*|uncertainty\s+quantificat\w*|calibrat\w*\s+predict\w*)\b"),
    ("extreme value theory", "62G32", 0, r"\b(extreme\s+value\w*|tail\s+index|tail\s+infer\w*|peaks\s+over\s+threshold|generalized\s+pareto|max[\-\s]stable|heavy[\-\s]tail\w*)\b"),
    ("order statistics & records", "62G30", 0, r"\b(order\s+statistic\w*|record\s+value\w*|l[\-\s]statistic\w*|spacings\s+statistic\w*|ranked\s+set\s+sampl\w*)\b"),
    ("information theory & geometry", "62B11", 0, r"\b(information\s+geometr\w*|kullback[\-\s]leibler|f[\-\s]divergence|shannon\s+entropy|mutual\s+information|minimum\s+description\s+length|fisher[\-\s]rao|scoring\s+rule\w*)\b"),
    ("algebraic statistics", "62R01", 0, r"\b(algebraic\s+statist\w*|toric\s+model\w*|markov\s+basis|gr[oö]bner\s+basis\s+statist)\b"),
    ("directional & manifold stats", "62R30", 0, r"\b(directional\s+data|circular\s+data|spherical\s+data|statistics\s+on\s+manifold\w*|riemannian\s+manifold\w*|shape\s+analysis|procrustes|fr[eé]chet\s+mean)\b"),
    ("topological data analysis", "62R40", 0, r"\b(topological\s+data\s+analysis|persistent\s+homology|persistence\s+diagram\w*|betti\s+number\w*\s+statist|simplicial\s+complex\s+statist)\b"),
    ("psychometrics & irt", "62P15", 0, r"\b(psychometr\w*|item\s+response\s+theor\w*|rasch\s+model\w*|cognitive\s+diagnos\w*|computerized\s+adaptive\s+testing|educational\s+measurement)\b"),
    ("structural equation & latent models", "62H25", 0, r"\b(structural\s+equation\s+model\w*|latent\s+class\s+model\w*|latent\s+variable\s+model\w*|latent\s+trait\s+model\w*)\b"),
    ("reliability & life testing", "62N05", 0, r"\b(reliability\s+(?:analys|estimat|theor|model|life|test|system)|life\s+testing|accelerated\s+life|degradation\s+model\w*|weibull\s+distribut\w*)\b"),
    ("statistical quality control", "62P30", 0, r"\b(quality\s+control|control\s+chart\w*|statistical\s+process\s+control|cusum\s+chart|ewma\s+chart|acceptance\s+sampl\w*|six\s+sigma)\b"),

    # === Macro 1: Probability Theory & Stochastic Processes (MSC 60A-60L) ===
    ("stochastic differential equations", "60H10", 1, r"\b(stochastic\s+differential\s+equation\w*|sde\b|sdes\b|it[oô]\s+calculus|stochastic\s+integral\w*|fokker[\-\s]planck|[eé]quations?\s+diff[eé]rentielles?\s+stochastiques?)\b"),
    ("stochastic pdes", "60H15", 1, r"\b(stochastic\s+partial\s+differential|spde\b|spdes\b|stochastic\s+heat\s+equation|stochastic\s+navier[\-\s]stokes|stochastic\s+wave\s+equation|singular\s+spde)\b"),
    ("bsdes & malliavin calculus", "60H07", 1, r"\b(backward\s+stochastic\s+differential|bsde\b|bsdes\b|malliavin\s+calculus|malliavin\s+derivative|wiener\s+chaos)\b"),
    ("stochastic control", "60H30", 1, r"\b(stochastic\s+control|optimal\s+stochastic\s+control|hamilton[\-\s]jacobi[\-\s]bellman|hjb\s+equation|singular\s+control|impulse\s+control|ergodic\s+control)\b"),
    ("mean-field games & mckean-vlasov", "60H30", 1, r"\b(mean[\-\s]field\s+game\w*|mckean[\-\s]vlasov|propagation\s+of\s+chaos|stochastic\s+differential\s+game\w*)\b"),
    ("markov chains & mixing times", "60J10", 1, r"\b(markov\s+chain\w*|mixing\s+time\w*|cutoff\s+phenomenon|reversible\s+markov|cha[iî]nes?\s+de\s+markov)\b"),
    ("markov processes & dirichlet forms", "60J25", 1, r"\b(markov\s+process\w*|feller\s+process\w*|dirichlet\s+form\w*|semi[\-\s]markov|markov\s+renewal|markov\s+semigroup)\b"),
    ("hidden markov models", "62M05", 1, r"\b(hidden\s+markov|hmm\b|hmms\b|baum[\-\s]welch|viterbi\s+algorithm|switching\s+markov)\b"),
    ("brownian motion", "60J65", 1, r"\b(brownian\s+motion|fractional\s+brownian|reflecting\s+brownian|local\s+time\w*|excursion\s+theor\w*|mouvement\s+brownien)\b"),
    ("diffusion processes", "60J60", 1, r"\b(diffusion\s+process\w*|ornstein[\-\s]uhlenbeck|bessel\s+process\w*|ergodic\s+diffusion|degenerate\s+diffusion)\b"),
    ("schramm-loewner evolution", "60J67", 1, r"\b(schramm[\-\s]loewner|loewner\s+evolution|sle\b|conformal\s+invariance\s+probabilit|gaussian\s+free\s+field|liouville\s+quantum\s+gravity)\b"),
    ("levy & jump processes", "60G51", 1, r"\b(l[eé]vy\s+process\w*|jump\s+process\w*|jump[\-\s]diffusion|stable\s+process\w*|infinitely\s+divisible|subordinator\w*|processus\s+de\s+l[eé]vy)\b"),
    ("martingales", "60G44", 1, r"\b(martingale\w*|semimartingale\w*|supermartingale\w*|submartingale\w*|doob\s+inequalit\w*|predictable\s+representation)\b"),
    ("central limit & stein's method", "60F05", 1, r"\b(central\s+limit\s+theorem\w*|functional\s+limit\s+theorem\w*|invariance\s+principle\w*|stein(?:'s)?\s+method|berry[\-\s]esseen|concentration\s+inequalit\w*)\b"),
    ("large deviations", "60F10", 1, r"\b(large\s+deviation\w*|moderate\s+deviation\w*|freidlin[\-\s]wentzell|varadhan|grandes?\s+d[eé]viations?)\b"),
    ("random matrices", "60B20", 1, r"\b(random\s+matri(?:x|ces)|wigner\s+matri\w*|wishart\s+matri\w*|tracy[\-\s]widom|marchenko[\-\s]pastur|beta[\-\s]ensemble|matrices?\s+al[eé]atoires?)\b"),
    ("free probability & dpp", "60B20", 1, r"\b(free\s+probabilit\w*|determinantal\s+point\s+process\w*|coulomb\s+gas|eigenvalue\w*\s+universality)\b"),
    ("random walks", "60G50", 1, r"\b(random\s+walk\w*|self[\-\s]avoiding\s+walk|random\s+environment\w*|marche\w*\s+al[eé]atoire\w*|first[\-\s]passage\s+percolation|last[\-\s]passage\s+percolation)\b"),
    ("percolation theory", "60K35", 1, r"\b(percolation|bootstrap\s+percolation|continuum\s+percolation|random\s+cluster\s+model|fk[\-\s]percolation)\b"),
    ("interacting particle systems", "60K35", 1, r"\b(interacting\s+particle\s+system\w*|exclusion\s+process|voter\s+model|contact\s+process|hydrodynamic\s+limit\w*|kpz\s+universality|zero[\-\s]range\s+process)\b"),
    ("ising, spin glass & gibbs", "60K35", 1, r"\b(ising\s+model|potts\s+model|spin\s+glass\w*|gibbs\s+measure\w*|statistical\s+mechanics\s+probabilit|sherrington[\-\s]kirkpatrick)\b"),
    ("branching processes", "60J80", 1, r"\b(branching\s+process\w*|galton[\-\s]watson|superprocess\w*|birth[\-\s]and[\-\s]death|processus\s+de\s+branchement|branching\s+brownian)\b"),
    ("coalescent processes", "60J90", 1, r"\b(coalescent\s+process\w*|kingman\s+coalescent|fleming[\-\s]viot|lambda[\-\s]coalescent|population\s+genetics\s+diffusion)\b"),
    ("point & hawkes processes", "60G55", 1, r"\b(point\s+process\w*|hawkes\s+process\w*|cox\s+process\w*|poisson\s+point\s+process|random\s+measure\w*|palm\s+theor\w*)\b"),
    ("stochastic geometry", "60D05", 1, r"\b(stochastic\s+geometr\w*|random\s+tessellation\w*|voronoi|boolean\s+model|random\s+set\w*|random\s+polytope\w*|integral\s+geometry)\b"),
    ("gaussian processes & fields", "60G15", 1, r"\b(gaussian\s+process\w*|gaussian\s+random\s+field\w*|random\s+field\w*|stationary\s+gaussian|processus\s+gaussien\w*|champ\w*\s+al[eé]atoire\w*)\b"),
    ("random graphs & trees", "60C05", 1, r"\b(random\s+graph\w*|erd[oö]s[\-\s]r[eé]nyi|random\s+tree\w*|random\s+permutation\w*|combinatorial\s+probabilit\w*|graphe\w*\s+al[eé]atoire\w*|preferential\s+attachment)\b"),
    ("queueing theory", "60K25", 1, r"\b(queueing|queuing|queue\s+network\w*|heavy\s+traffic|fluid\s+limit\w*|m/g/1|g/g/1|file\s+d'attente)\b"),
    ("renewal theory", "60K05", 1, r"\b(renewal\s+theor\w*|renewal\s+process\w*|alternating\s+renewal|regeneration\s+process\w*)\b"),
    ("rough paths & regularity", "60L20", 1, r"\b(rough\s+path\w*|regularity\s+structure\w*|paracontrolled|signature\s+method\w*|white\s+noise\s+analysis)\b"),
    ("ergodic theory & random dynamics", "60F15", 1, r"\b(ergodic\s+theor\w*|random\s+dynamical\s+system\w*|lyapunov\s+exponent\w*|strong\s+law\s+of\s+large|stochastic\s+approximation)\b"),
    ("copulas & dependence", "62H05", 1, r"\b(copula\w*|vine\s+copula|archimedean\s+copula|tail\s+dependence|measures\s+of\s+association|kendall(?:'s)?\s+tau)\b"),
    ("distribution theory & inequalities", "60E05", 1, r"\b(characteristic\s+function\w*|stochastic\s+order\w*|probability\s+distribut\w*|skew[\-\s]normal|exchangeabilit\w*|de\s+finetti|probability\s+inequalit\w*)\b"),
    ("optimal transport & wasserstein", "60B10", 1, r"\b(optimal\s+transport|wasserstein\s+(?:distance|metric|barycenter|gradient|space)|monge[\-\s]kantorovich|sinkhorn|schr[oö]dinger\s+bridge|transport\s+optimal)\b"),

    # === Macro 2: Clinical Biostatistics & Biomedical Statistics (MSC 62N + 62P10) ===
    ("survival analysis", "62N01", 2, r"\b(survival\s+analys\w*|survival\s+data|survival\s+model\w*|time[\-\s]to[\-\s]event|hazard\s+function\w*|analyse\s+de\s+survie)\b"),
    ("cox proportional hazards", "62N02", 2, r"\b(proportional\s+hazards|cox\s+(?:model|regression|proportional)|kaplan[\-\s]meier|accelerated\s+failure\s+time|additive\s+hazards)\b"),
    ("censored data models", "62N01", 2, r"\b(censored\s+data|right[\-\s]censored|interval[\-\s]censored|left[\-\s]truncated|current\s+status\s+data|informative\s+censoring)\b"),
    ("competing risks & recurrent events", "62N02", 2, r"\b(competing\s+risks?|recurrent\s+event\w*|multi[\-\s]state\s+model\w*|cumulative\s+incidence)\b"),
    ("cure & frailty models", "62N02", 2, r"\b(cure\s+(?:rate\s+)?model\w*|mixture\s+cure|frailty\s+model\w*|shared\s+frailty)\b"),
    ("clinical trials", "62P10", 2, r"\b(clinical\s+trial\w*|randomized\s+controlled\s+trial\w*|phase\s+(?:ii|iii|2|3)\s+trial|interim\s+analys\w*|bioequivalence|essai\w*\s+clinique\w*)\b"),
    ("adaptive & platform trials", "62P10", 2, r"\b(adaptive\s+trial\w*|adaptive\s+design\w*|response[\-\s]adaptive|basket\s+trial|umbrella\s+trial|platform\s+trial|biomarker[\-\s]guided)\b"),
    ("dose-finding & phase i trials", "62P10", 2, r"\b(dose[\-\s]finding|dose[\-\s]escalation|phase\s+(?:i|1)\s+trial|continual\s+reassessment|maximum\s+tolerated\s+dose|pharmacokinetic\w*|pharmacometric\w*)\b"),
    ("longitudinal data analysis", "62N02", 2, r"\b(longitudinal\s+data|longitudinal\s+stud\w*|repeated\s+measure\w*|generalized\s+estimating\s+equation\w*|gee\b|growth\s+curve\s+model\w*)\b"),
    ("joint modeling", "62N02", 2, r"\b(joint\s+model\w*\s+(?:of|for)\s+longitudinal|joint\s+longitudinal|informative\s+dropout|dynamic\s+prediction\s+survival)\b"),
    ("statistical genetics & gwas", "62P10", 2, r"\b(statistical\s+genetic\w*|genome[\-\s]wide|gwas\b|polygenic\s+risk|haplotype|linkage\s+disequilibrium|eqtl|rare\s+variant\w*)\b"),
    ("genomics & gene expression", "62P10", 2, r"\b(gene\s+expression|rna[\-\s]seq|microarray|epigenetic\w*|methylation|multi[\-\s]omics|proteomic\w*|bioinformatic\w*)\b"),
    ("single-cell & microbiome stats", "62P10", 2, r"\b(single[\-\s]cell|scrna|microbiome|metagenomic\w*|compositional\s+data\s+microbiome|spatial\s+transcriptomic\w*)\b"),
    ("phylogenetics & coalescent bio", "92D15", 2, r"\b(phylogenetic\w*|evolutionary\s+tree\w*|molecular\s+evolution|phylodynamic\w*)\b"),
    ("neuroimaging & fmri", "62H35", 2, r"\b(neuroimag\w*|fmri\b|functional\s+mri|brain\s+(?:imaging|connectivity|network)|eeg\b|electroencephalogra\w*)\b"),
    ("medical image analysis", "62H35", 2, r"\b(medical\s+imag\w*|radiomic\w*|image\s+segmentation|image\s+reconstruction|pet\s+imaging|statistical\s+image\s+analys\w*)\b"),
    ("epidemiology & case-control", "62P10", 2, r"\b(epidemiolog\w*|case[\-\s]control|cohort\s+stud\w*|disease\s+mapping|environmental\s+epidemiol\w*)\b"),
    ("infectious disease & epidemics", "62P10", 2, r"\b(infectious\s+disease\w*|epidemic\s+model\w*|sir\s+model|seir\s+model|covid[\-\s]19|transmission\s+dynamic\w*|reproduction\s+number|vaccine\s+efficacy)\b"),
    ("precision medicine & dtr", "62P10", 2, r"\b(precision\s+medicine|personalized\s+medicine|individualized\s+treatment\s+rule\w*|dynamic\s+treatment\s+regim\w*|optimal\s+treatment\s+regim\w*|subgroup\s+identificat\w*)\b"),
    ("electronic health records", "62P10", 2, r"\b(electronic\s+health\s+record\w*|ehr\s+data|pharmacovigilance|health\s+informatics|clinical\s+prediction\s+model\w*)\b"),
    ("meta-analysis", "62P10", 2, r"\b(meta[\-\s]analys\w*|network\s+meta[\-\s]analys\w*|publication\s+bias|systematic\s+review\s+statist|evidence\s+synthesis)\b"),
    ("diagnostic tests & roc", "62P10", 2, r"\b(roc\s+curve\w*|receiver\s+operating|diagnostic\s+test\w*|diagnostic\s+accurac\w*|biomarker\s+evaluat\w*|sensitivity\s+and\s+specificity)\b"),

    # === Macro 3: Machine Learning, High-Dimensional & Data Science (MSC 62H/62J07/62R/68T) ===
    ("high-dimensional statistics", "62H12", 3, r"\b(high[\-\s]dimensional|high\s+dimension\w*|large\s+p\s+small\s+n|ultra[\-\s]high\s+dimensional|grande\s+dimension)\b"),
    ("covariance & precision matrices", "62H12", 3, r"\b(covariance\s+matrix\s+estimat\w*|precision\s+matrix\s+estimat\w*|large\s+covariance|banded\s+covariance|glasso|graphical\s+lasso)\b"),
    ("lasso & sparse regression", "62J07", 3, r"\b(lasso\b|ridge\s+regression|elastic\s+net|penalized\s+(?:regression|likelihood|estimat)|sparse\s+(?:regression|estimat|recovery)|l1\s+regulariz\w*|scad\b|group\s+lasso)\b"),
    ("variable & model selection", "62J07", 3, r"\b(variable\s+selection|model\s+selection|feature\s+selection|feature\s+screening|sure\s+independence\s+screening|best\s+subset|bic\b|aic\b)\b"),
    ("compressed sensing", "94A20", 3, r"\b(compressed\s+sensing|compressive\s+sensing|sparse\s+signal\s+recovery|phase\s+retrieval|restricted\s+isometry)\b"),
    ("probabilistic graphical models", "62H22", 3, r"\b(graphical\s+model\w*|gaussian\s+graphical|bayesian\s+network\w*|markov\s+random\s+field\w*|structure\s+learning|dag\s+learning)\b"),
    ("network analysis & graphons", "62H30", 3, r"\b(network\s+(?:analys|model|data|infer|statist)|social\s+network\w*|graphon\w*|exponential\s+random\s+graph|ergm\b|latent\s+space\s+network|dynamic\s+network\w*)\b"),
    ("community detection & sbm", "62H30", 3, r"\b(community\s+detect\w*|stochastic\s+block\s+model|spectral\s+clustering|graph\s+matching|graph\s+partitioning)\b"),
    ("functional data analysis", "62R10", 3, r"\b(functional\s+data|functional\s+regression|functional\s+principal\s+component|fpca\b|curve\s+data|functional\s+time\s+series|donn[eé]es?\s+fonctionnelles?)\b"),
    ("classification & discriminant", "62H30", 3, r"\b(classification\s+(?:method|rule|algorithm|model)|discriminant\s+analys\w*|support\s+vector\s+machine\w*|svm\b|naive\s+bayes|k[\-\s]nearest\s+neighbor)\b"),
    ("clustering & mixture models", "62H30", 3, r"\b(cluster\s+analys\w*|clustering\s+(?:algorithm|method|model)|mixture\s+model\w*|gaussian\s+mixture|finite\s+mixture|model[\-\s]based\s+clustering|unsupervised\s+learning)\b"),
    ("random forests & boosting", "62H30", 3, r"\b(random\s+forest\w*|gradient\s+boost\w*|adaboost|decision\s+tree\w*|cart\b|ensemble\s+learning|bart\b|bayesian\s+additive\s+regression\s+trees)\b"),
    ("pca & dimension reduction", "62H25", 3, r"\b(principal\s+component\w*|pca\b|sparse\s+pca|dimension\s+reduct\w*|sufficient\s+dimension\s+reduct\w*|canonical\s+correlat\w*|independent\s+component\w*)\b"),
    ("factor models", "62H25", 3, r"\b(factor\s+analys\w*|factor\s+model\w*|latent\s+factor\w*|dynamic\s+factor\s+model\w*|approximate\s+factor)\b"),
    ("matrix completion & tensors", "62H25", 3, r"\b(matrix\s+completion|low[\-\s]rank\s+matrix|tensor\s+(?:decomposit|regress|complet|factor|pca)|nuclear\s+norm)\b"),
    ("statistical learning theory", "62R07", 3, r"\b(statistical\s+learning|rademacher\s+complexity|pac[\-\s]bayes|generalization\s+bound\w*|vc\s+dimension|empirical\s+risk\s+minimiz\w*|apprentissage\s+statistique)\b"),
    ("kernel methods & rkhs", "62R07", 3, r"\b(kernel\s+method\w*|reproducing\s+kernel\s+hilbert|rkhs\b|kernel\s+ridge|maximum\s+mean\s+discrepancy|mmd\b|random\s+fourier\s+feature\w*)\b"),
    ("transfer & federated learning", "62R07", 3, r"\b(transfer\s+learning|domain\s+adaptation|federated\s+learning|distributed\s+(?:learning|statist|estimat)|multi[\-\s]task\s+learning|meta[\-\s]learning)\b"),
    ("online & active learning", "68T05", 3, r"\b(online\s+learning|active\s+learning|semi[\-\s]supervised\s+learning|streaming\s+data\s+statist|regret\s+minimiz\w*)\b"),
    ("deep learning & neural nets", "68T07", 3, r"\b(deep\s+learning|neural\s+network\w*|deep\s+neural|convolutional\s+neural|overparameteriz\w*|neural\s+tangent\s+kernel|r[eé]seaux?\s+de\s+neurones?)\b"),
    ("generative & diffusion models", "68T07", 3, r"\b(generative\s+adversarial|gan\b|diffusion\s+model\w*\s+(?:generat|score)|score[\-\s]based\s+generat\w*|normalizing\s+flow\w*|variational\s+autoencoder|large\s+language\s+model\w*|transformer\w*\s+(?:model|attention))\b"),
    ("reinforcement learning", "68T05", 3, r"\b(reinforcement\s+learning|q[\-\s]learning|policy\s+gradient|policy\s+evaluation|off[\-\s]policy|markov\s+decision\s+process\w*)\b"),
    ("multi-armed bandits", "68T05", 3, r"\b(multi[\-\s]armed\s+bandit\w*|contextual\s+bandit\w*|bandit\s+(?:problem|algorithm)|thompson\s+sampl\w*|upper\s+confidence\s+bound)\b"),
    ("stochastic optimization", "90C15", 3, r"\b(stochastic\s+gradient|sgd\b|stochastic\s+optimiz\w*|convex\s+optimiz\w*|non[\-\s]convex\s+optimiz\w*|admm\b|proximal\s+gradient|coordinate\s+descent|mirror\s+descent)\b"),
    ("differential privacy & fairness", "68P27", 3, r"\b(differential\s+privac\w*|differentially\s+private|statistical\s+privac\w*|algorithmic\s+fairness|fair\s+machine\s+learning|adversarial\s+robustness|disclosure\s+limitation)\b"),
    ("topic models & nlp", "68T50", 3, r"\b(topic\s+model\w*|latent\s+dirichlet\s+allocation|text\s+mining|natural\s+language\s+processing|nlp\b|recommender\s+system\w*|collaborative\s+filtering)\b"),

    # === Macro 4: Time Series, Spatial Statistics & Econometrics / Finance (MSC 62M + 62P05/62P20 + 91G) ===
    ("time series analysis", "62M10", 4, r"\b(time\s+series|autoregressi\w*|arma\b|arima\b|var\s+model|vector\s+autoregress\w*|forecasting\s+time|s[eé]ries?\s+temporelles?|zeitreihen)\b"),
    ("nonstationary & cointegration", "62M10", 4, r"\b(nonstationary\s+time|cointegrat\w*|unit\s+root|long\s+memory\s+time|fractional\s+integrat\w*|locally\s+stationary)\b"),
    ("spectral time series", "62M15", 4, r"\b(spectral\s+(?:analys|densit|estimat)\w*|periodogram|frequency\s+domain|whittle\s+likelihood|time[\-\s]frequency)\b"),
    ("state-space & kalman filtering", "62M20", 4, r"\b(state[\-\s]space\s+model\w*|kalman\s+filter\w*|nonlinear\s+filtering|data\s+assimilation|ensemble\s+kalman|dynamic\s+linear\s+model\w*)\b"),
    ("change-point detection", "62M10", 4, r"\b(change[\-\s]?point\w*|changepoint\w*|structural\s+break\w*|regime[\-\s]switching|anomaly\s+detect\w*|rupture\s+de\s+mod[eè]le)\b"),
    ("spatial statistics", "62M30", 4, r"\b(spatial\s+(?:statist|data|process|model|correlat|regress|predict|sampl|point)|geostatist\w*|kriging|variogram|spatial\s+confounding)\b"),
    ("spatio-temporal & climate stats", "62M30", 4, r"\b(spatio[\-\s]temporal|space[\-\s]time\s+(?:model|statist|data)|environmental\s+statist\w*|climate\s+(?:model|statist|extremes))\b"),
    ("econometrics", "62P20", 4, r"\b(econometr\w*|microeconometr\w*|macroeconometr\w*|nonparametric\s+econometr\w*|endogeneit\w*|discrete\s+choice\s+model\w*|spatial\s+econometr\w*|[eé]conom[eé]trie)\b"),
    ("panel data analysis", "62P20", 4, r"\b(panel\s+data|dynamic\s+panel|longitudinal\s+econometr|fixed\s+effects\s+panel|interactive\s+fixed\s+effects)\b"),
    ("financial econometrics & garch", "62P05", 4, r"\b(garch\b|arch\s+model|financial\s+econometr\w*|financial\s+time\s+series|high[\-\s]frequency\s+financ\w*|market\s+microstructure)\b"),
    ("stochastic volatility", "91G20", 4, r"\b(stochastic\s+volatilit\w*|realized\s+volatilit\w*|rough\s+volatilit\w*|jump\s+detect\w*\s+financ\w*|heston\s+model)\b"),
    ("option pricing & derivatives", "91G20", 4, r"\b(option\s+pricing|black[\-\s]scholes|mathematical\s+finance|quantitative\s+finance|hedging\s+strateg\w*|arbitrage|incomplete\s+market\w*|finance\s+math[eé]matique)\b"),
    ("portfolio & credit risk", "91G40", 4, r"\b(portfolio\s+optimiz\w*|credit\s+risk|interest\s+rate\s+model\w*|term\s+structure|default\s+risk|asset\s+pricing|value[\-\s]at[\-\s]risk|systemic\s+risk)\b"),
    ("actuarial science & ruin theory", "62P05", 4, r"\b(actuarial|ruin\s+(?:probabilit|theor)|insurance\s+(?:risk|loss|pricing|claim)|credibility\s+theor\w*|mortality\s+(?:model|forecast)|longevity\s+risk|reinsurance|actuariat)\b"),
]

NUM_CONCEPTS = len(MSC2020_FINE_TAXONOMY)
COMPILED_TAXONOMY = [
    (lbl, msc, midx, re.compile(pat, re.I))
    for lbl, msc, midx, pat in MSC2020_FINE_TAXONOMY
]
CONCEPT_TO_MACRO = [x[2] for x in MSC2020_FINE_TAXONOMY]


def mean_pool_norm(last_hidden: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    mask = attention_mask.unsqueeze(-1).type_as(last_hidden)
    summed = (last_hidden * mask).sum(dim=1)
    counts = mask.sum(dim=1).clamp(min=1e-9)
    return F.normalize((summed / counts).float(), p=2, dim=1)


def compute_collaborative_affinity(doc_labels, pids, edges):
    """
    100% data-driven collaborative concept affinity matrix S_collab in [0, 1]^(C x C):
      (1) Within-dissertation multi-concept co-occurrence (PPMI)
      (2) Academic lineage co-occurrence (students sharing the same PhD advisor + advisor->student edges).
    """
    C = NUM_CONCEPTS
    title_cooc = np.zeros((C, C), dtype=np.float64)
    concept_freq = np.zeros(C, dtype=np.float64)
    for lbls in doc_labels:
        for c in lbls:
            concept_freq[c] += 1.0
        if len(lbls) >= 2:
            for a, b in itertools.combinations(lbls, 2):
                title_cooc[a, b] += 1.0
                title_cooc[b, a] += 1.0

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
                damp = title_cooc[i, j] / (title_cooc[i, j] + 4.0)
                ppmi_title[i, j] = val * damp
    if ppmi_title.max() > 0:
        ppmi_title /= ppmi_title.max()

    pid_to_lbls = {pid: lbls for pid, lbls in zip(pids, doc_labels) if lbls}
    adv_to_students = defaultdict(list)
    lineage_cooc = np.zeros((C, C), dtype=np.float64)
    for aid, sid in edges:
        if sid in pid_to_lbls:
            adv_to_students[aid].append(sid)
        if aid in pid_to_lbls and sid in pid_to_lbls:
            for ca in pid_to_lbls[aid]:
                for cs in pid_to_lbls[sid]:
                    if ca != cs:
                        lineage_cooc[ca, cs] += 1.0
                        lineage_cooc[cs, ca] += 1.0
    for aid, stus in adv_to_students.items():
        if len(stus) < 2 or len(stus) > 60:
            continue
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

    row_norms = np.linalg.norm(lineage_cooc, axis=1, keepdims=True) + 1e-9
    lin_normed = lineage_cooc / row_norms
    lin_diag = np.sqrt(np.outer(lineage_cooc.sum(axis=1) + 1e-6, lineage_cooc.sum(axis=0) + 1e-6))
    lin_aff = lineage_cooc / lin_diag
    np.fill_diagonal(lin_aff, 0.0)
    if lin_aff.max() > 0:
        lin_aff /= lin_aff.max()
    lin_cos = lin_normed @ lin_normed.T
    np.fill_diagonal(lin_cos, 0.0)
    S_lineage = np.clip(0.65 * lin_aff + 0.35 * lin_cos, 0.0, 1.0)

    S_collab = np.clip(0.62 * ppmi_title + 0.38 * S_lineage, 0.0, 1.0)
    np.fill_diagonal(S_collab, 1.0)
    return S_collab, title_cooc, lineage_cooc


def encode_all(texts, model_dir, batch_size=256):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Encoding {len(texts):,} dissertations using fine-tuned model from {model_dir} on {device}...")
    tokenizer = AutoTokenizer.from_pretrained(model_dir)
    model = AutoModel.from_pretrained(model_dir).to(device)
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
    return all_embs


def finetune_and_encode(texts, doc_labels, S_collab, batch_size=256, max_epochs=25):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model_src = LOCAL_BASE_MODEL if os.path.exists(os.path.join(LOCAL_BASE_MODEL, "config.json")) else "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    print(f"Loading base multilingual model from {model_src} on {device}...")
    tokenizer = AutoTokenizer.from_pretrained(model_src)
    model = AutoModel.from_pretrained(model_src).to(device)

    rng = random.Random(42)
    by_primary = defaultdict(list)
    for idx, lbls in enumerate(doc_labels):
        if lbls:
            by_primary[lbls[0]].append((texts[idx], lbls))

    train_items, val_items = [], []
    for cid, items in sorted(by_primary.items()):
        rng.shuffle(items)
        items = items[:750]
        n_val = max(2, int(len(items) * 0.10)) if len(items) >= 12 else 0
        val_items.extend(items[:n_val])
        train_items.extend(items[n_val:])

    print(
        f"Fine-tuning top 6 transformer layers with Multi-Label MSC 2020 Margin + Collaborative Regularizer "
        f"on {len(train_items):,} train / {len(val_items):,} val dissertations across {NUM_CONCEPTS} fine MSC 2020 concepts..."
    )

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

    S_collab_t = torch.tensor(S_collab, dtype=torch.float32, device=device)
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
                logits_f = (cos_f - arc_margin * pos_f) * 18.0
                logits_m = (cos_m - (arc_margin * 0.75) * pos_m) * 14.0
                y_f_s = y_f * 0.97 + 0.03 / NUM_CONCEPTS
                y_m_s = y_m * 0.97 + 0.03 / len(MACRO_DOMAINS)
                loss_f = -(y_f_s * F.log_softmax(logits_f, dim=1)).sum(dim=1).mean()
                loss_m = -(y_m_s * F.log_softmax(logits_m, dim=1)).sum(dim=1).mean()
                proto_sim = pf_norm @ pf_norm.T
                loss_collab = (S_mask * (1.0 - proto_sim)).sum() / S_mask_sum
                loss = loss_f + lambda_macro * loss_m + lambda_collab * loss_collab
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
            tot_loss += float(loss.item()) * len(batch)
            hits_f += int(pos_f[torch.arange(len(batch), device=device), cos_f.argmax(dim=1)].sum().item())
            seen += len(batch)
        scheduler.step()

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
                    vloss = -(y_f * F.log_softmax(logits_f, dim=1)).sum(dim=1).mean() + lambda_macro * (-(y_m * F.log_softmax(logits_m, dim=1)).sum(dim=1).mean())
                val_loss += float(vloss.item()) * len(batch)
                val_hits_f += int(pos_f[torch.arange(len(batch), device=device), logits_f.argmax(dim=1)].sum().item())
                val_hits_m += int(pos_m[torch.arange(len(batch), device=device), logits_m.argmax(dim=1)].sum().item())
                val_seen += len(batch)

        va_l = val_loss / val_seen
        mark = ""
        if va_l < best_val_loss - min_delta:
            best_val_loss = va_l
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            no_improve = 0
            mark = " *best*"
        else:
            no_improve += 1
        print(
            f"  Epoch {epoch + 1:2d}/{max_epochs}: train_loss={tot_loss/seen:.4f} acc={hits_f/seen:.3f} | "
            f"val_loss={va_l:.4f} msc_acc={val_hits_f/val_seen:.3f} macro_acc={val_hits_m/val_seen:.3f} ({time.time() - t_ft:.1f}s){mark}"
        )
        if no_improve >= patience:
            print(f"  Early stopping at epoch {epoch + 1}: val_loss converged near minimum ({best_val_loss:.4f}).")
            break

    if best_state is not None:
        model.load_state_dict({k: v.to(device) for k, v in best_state.items()})
    os.makedirs(FINETUNED_DIR, exist_ok=True)
    model.save_pretrained(FINETUNED_DIR)
    tokenizer.save_pretrained(FINETUNED_DIR)
    return encode_all(texts, FINETUNED_DIR, batch_size=batch_size)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reuse-finetuned", action="store_true", help="Reuse saved fine-tuned MSC 2020 checkpoint")
    args = parser.parse_args()

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

    # 1. Multi-label fine MSC 2020 matching across all ~162 fine-grained mathematical/statistical classifications
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
        f"to {NUM_CONCEPTS} fine MSC 2020 + modern statistical classifications ({multi_cnt:,} multi-label)."
    )

    # 2. Data-driven collaborative affinity matrix (within-dissertation PPMI + shared-advisor lineage)
    S_collab, title_cooc, lineage_cooc = compute_collaborative_affinity(doc_labels, pids, edges)

    # 3. Encode all dissertations (either reuse already fine-tuned MSC 2020 checkpoint or train fresh)
    if args.reuse_finetuned and os.path.exists(os.path.join(FINETUNED_DIR, "config.json")):
        all_embs = encode_all(texts, FINETUNED_DIR, batch_size=256)
    else:
        all_embs = finetune_and_encode(texts, doc_labels, S_collab, batch_size=256, max_epochs=25)

    # 4. Build empirical unit prototypes for every fine MSC 2020 class from its matched dissertations
    #    (fallback to encoding the class name itself if a niche MSC class has < 5 direct regex hits)
    proto_list = []
    tokenizer_p = AutoTokenizer.from_pretrained(FINETUNED_DIR)
    model_p = AutoModel.from_pretrained(FINETUNED_DIR).to("cuda" if torch.cuda.is_available() else "cpu")
    model_p.eval()
    class_matched_idxs = defaultdict(list)
    for idx, lbls in enumerate(doc_labels):
        for c in lbls:
            class_matched_idxs[c].append(idx)

    for cid, (lbl, msc, midx, _) in enumerate(MSC2020_FINE_TAXONOMY):
        m_idxs = class_matched_idxs[cid]
        if len(m_idxs) >= 5:
            p_vec = all_embs[m_idxs].mean(axis=0)
            p_vec = p_vec / (np.linalg.norm(p_vec) + 1e-9)
        else:
            with torch.no_grad():
                enc = tokenizer_p([f"{lbl} ({MACRO_DOMAINS[midx]})"], return_tensors="pt").to(model_p.device)
                p_vec = mean_pool_norm(model_p(**enc).last_hidden_state, enc["attention_mask"]).cpu().numpy()[0]
        proto_list.append(p_vec)
    prototypes_fine = np.vstack(proto_list).astype(np.float32)

    # Assign EVERY dissertation to its best fine MSC 2020 classification
    sims = all_embs @ prototypes_fine.T  # (N, NUM_CONCEPTS)
    for idx, lbls in enumerate(doc_labels):
        if lbls:
            for c in lbls:
                sims[idx, c] += 0.22
    assigned = sims.argmax(axis=1)

    concept_docs = defaultdict(list)
    for idx, cid in enumerate(assigned):
        concept_docs[int(cid)].append(idx)

    valid_cids = [c for c in range(NUM_CONCEPTS) if len(concept_docs[c]) >= 15]
    print(f"Active fine MSC 2020 + modern statistical bubbles (>=15 dissertations): {len(valid_cids)}/{NUM_CONCEPTS}")

    # Compute empirical 384-D centroids & sizes
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
    D_fused = 0.68 * D_sem + 0.32 * D_collab
    np.fill_diagonal(D_fused, 0.0)

    tsne = TSNE(
        n_components=2,
        perplexity=28,
        early_exaggeration=16.0,
        metric="precomputed",
        init="random",
        learning_rate=200.0,
        n_iter=2500,
        random_state=42,
    )
    coords = tsne.fit_transform(D_fused)

    # PCA-align horizontally into 1280 x 560 widescreen viewBox
    W, H = 1280, 560
    coords_c = coords - coords.mean(axis=0, keepdims=True)
    _, _, vt = np.linalg.svd(coords_c, full_matrices=False)
    coords_rot = coords_c @ vt.T
    xs = 80.0 + (coords_rot[:, 0] - coords_rot[:, 0].min()) / (coords_rot[:, 0].max() - coords_rot[:, 0].min() + 1e-9) * 1120.0
    ys = 52.0 + (coords_rot[:, 1] - coords_rot[:, 1].min()) / (coords_rot[:, 1].max() - coords_rot[:, 1].min() + 1e-9) * 456.0

    # Spatially contiguous 5-domain macro assignment (matches the old lush overlapping ambient-aura aesthetic!)
    pts_2d = np.column_stack([xs, ys])
    pts_2d_s = (pts_2d - pts_2d.mean(axis=0)) / (pts_2d.std(axis=0) + 1e-9)
    km_macro = KMeans(n_clusters=len(MACRO_DOMAINS), n_init=30, random_state=42)
    spatial_cluster = km_macro.fit_predict(pts_2d_s, sample_weight=np.sqrt(sizes_arr))

    # Optimal 1-to-1 alignment between spatial clusters and the 5 MSC macro domains
    score_mat = {}
    for sc_id in range(len(MACRO_DOMAINS)):
        m_idxs = [i for i in range(len(valid_cids)) if spatial_cluster[i] == sc_id]
        for dom_id in range(len(MACRO_DOMAINS)):
            score_mat[(sc_id, dom_id)] = sum(sizes_arr[i] for i in m_idxs if CONCEPT_TO_MACRO[valid_cids[i]] == dom_id)
    best_perm = max(itertools.permutations(range(len(MACRO_DOMAINS))), key=lambda p: sum(score_mat[(sc, p[sc])] for sc in range(len(MACRO_DOMAINS))))
    sc_to_dom = {sc: best_perm[sc] for sc in range(len(MACRO_DOMAINS))}
    macro_assign = [sc_to_dom[spatial_cluster[i]] for i in range(len(valid_cids))]

    # Pull the 5 spatial clusters into one cohesive, naturally overlapping continent (exact old 0.56x / 0.85x geometry!)
    gcx, gcy = W * 0.5, H * 0.5
    for dom_id in range(len(MACRO_DOMAINS)):
        m_idxs = [i for i, m in enumerate(macro_assign) if m == dom_id]
        if not m_idxs:
            continue
        w = np.sqrt(sizes_arr[m_idxs])
        cx = float(np.average(xs[m_idxs], weights=w))
        cy = float(np.average(ys[m_idxs], weights=w))
        ncx = gcx + (cx - gcx) * 0.56
        ncy = gcy + (cy - gcy) * 0.56
        for i in m_idxs:
            xs[i] = ncx + (xs[i] - cx) * 0.85
            ys[i] = ncy + (ys[i] - cy) * 0.85

    xs = 72.0 + (xs - xs.min()) / (xs.max() - xs.min() + 1e-9) * (W - 144.0)
    ys = 46.0 + (ys - ys.min()) / (ys.max() - ys.min() + 1e-9) * (H - 92.0)

    # Gentle bubble anti-collision (matching app.js concave bubble radii 4.5px -> 25px)
    max_sz, min_sz = float(sizes_arr.max()), float(sizes_arr.min())
    radii = np.array([
        4.5 + 21.0 * math.pow(max(0.0, (sz - min_sz) / (max_sz - min_sz + 1e-9)), 0.62)
        for sz in sizes_arr
    ])
    for _ in range(70):
        for i in range(len(valid_cids)):
            for j in range(i + 1, len(valid_cids)):
                dx, dy = xs[j] - xs[i], ys[j] - ys[i]
                dist = math.hypot(dx, dy) + 1e-6
                min_d = (radii[i] + radii[j]) * 0.92 + 2.5
                if dist < min_d:
                    push = (min_d - dist) * 0.30
                    ux, uy = dx / dist, dy / dist
                    xs[i] -= ux * push
                    ys[i] -= uy * push
                    xs[j] += ux * push
                    ys[j] += uy * push
        xs = np.clip(xs, 48.0, W - 48.0)
        ys = np.clip(ys, 38.0, H - 38.0)

    # Compute ambient aura circle (cx, cy, r) for each of the 5 macro domains!
    clusters_out = []
    for dom_id, dom_name in enumerate(MACRO_DOMAINS):
        m_idxs = [i for i, m in enumerate(macro_assign) if m == dom_id]
        if not m_idxs:
            continue
        w = np.sqrt(sizes_arr[m_idxs])
        cx = float(np.average(xs[m_idxs], weights=w))
        cy = float(np.average(ys[m_idxs], weights=w))
        # Ambient Gaussian aura radius covering ~82nd percentile of cluster spread
        dists = sorted(math.hypot(xs[i] - cx, ys[i] - cy) + radii[i] for i in m_idxs)
        aura_r = float(dists[int(0.82 * (len(dists) - 1))]) + 26.0
        tot_c = int(sum(sizes_arr[i] for i in m_idxs))
        top_c = [MSC2020_FINE_TAXONOMY[valid_cids[i]][0] for i in sorted(m_idxs, key=lambda i: -sizes_arr[i])[:5]]
        print(f"  Domain {dom_id}: {dom_name:34s} ({len(m_idxs):2d} bubbles, {tot_c:6,d} diss, aura_r={aura_r:.0f}) | top: {top_c}")
        clusters_out.append({
            "id": dom_id,
            "name": dom_name,
            "color": CLUSTER_PALETTE[dom_id],
            "count": tot_c,
            "cx": round(cx, 1),
            "cy": round(cy, 1),
            "r": round(aura_r, 1),
        })

    topics_out = []
    for pos, cid in enumerate(valid_cids):
        lbl, msc_code, _, _ = MSC2020_FINE_TAXONOMY[cid]
        dom_id = macro_assign[pos]
        idxs = concept_docs[cid]
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

        collab_neighbors = []
        for opos, ocid in enumerate(valid_cids):
            if ocid != cid and S_collab[cid, ocid] > 0.03:
                collab_neighbors.append((float(S_collab[cid, ocid]), MSC2020_FINE_TAXONOMY[ocid][0]))
        collab_neighbors.sort(reverse=True)
        top_terms = [f"MSC {msc_code}"] + [nm for _, nm in collab_neighbors[:4]]

        yrs = [rows[i]["year"] for i in idxs if rows[i]["year"] and 1900 <= rows[i]["year"] <= 2026]
        yr_min = int(min(yrs)) if yrs else None
        yr_max = int(max(yrs)) if yrs else None
        yr_med = int(np.median(yrs)) if yrs else None

        topics_out.append({
            "id": pos,
            "label": lbl,
            "msc_code": msc_code,
            "count": len(idxs),
            "x": round(float(xs[pos]), 1),
            "y": round(float(ys[pos]), 1),
            "cluster_id": dom_id,
            "cluster_name": MACRO_DOMAINS[dom_id],
            "color": CLUSTER_PALETTE[dom_id],
            "year_min": yr_min,
            "year_max": yr_max,
            "year_median": yr_med,
            "top_terms": top_terms,
            "samples": samples,
        })

    topics_out.sort(key=lambda t: -t["count"])

    payload = {
        "model": "MSC-2020 + Collaborative Fine-Tuned Multilingual MiniLM-L12-v2",
        "width": W,
        "height": H,
        "total_dissertations": len(rows),
        "num_topics": len(topics_out),
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
    print(f"\nSaved {OUT_JSON} ({sz_kb:.1f} KB, {len(topics_out)} fine MSC 2020 bubbles, {W}x{H} + ambient auras) in {time.time() - t_start:.1f}s!")


if __name__ == "__main__":
    main()
