#!/usr/bin/env python3
"""
Statistics-Fine-Tuned Multilingual LLM Embedding & 2D Topic Clustering Builder for StatLink.

Pipeline:
  1. Extracts all Core Statistics, Biostatistics & Probability PhD dissertations (~65,800).
  2. Fine-tunes the top 4 transformer layers of `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
     (freezing the lower 8 layers to preserve 50+-language cross-lingual invariance!) using scaled-cosine
     supervised contrastive classification over 30 canonical Statistics/Biostatistics/Probability subfields
     (e.g. unifying `observational study`, `propensity score`, `instrumental variable`, `treatment effect`,
     `counterfactual`, `mediation` -> `causal inference`; `lasso`, `sparse`, `penalized` -> `high-dimensional`).
  3. Encodes all ~65,800 dissertations on CUDA GPU (RTX 3060) with the fine-tuned model.
  4. Clusters into 5 macro research domains & deduplicated 100% English c-TF-IDF topics, projects raw
     384-dim centroids to 2D via cosine t-SNE, and saves `data/dissertation_topic_map.json` +
     `meta_stats['dissertation_topic_map']`.
"""

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
from sklearn.cluster import KMeans, MiniBatchKMeans
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.manifold import TSNE
from transformers import AutoModel, AutoTokenizer

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
OUT_JSON = os.path.join(BASE_DIR, "data", "dissertation_topic_map.json")
BASE_MODEL_DIR = os.path.join(BASE_DIR, "data_raw", "hf_multilingual_minilm")
FINETUNED_DIR = os.path.join(BASE_DIR, "data_raw", "hf_stat_multilingual_minilm")

MODEL_NAME = "stat-finetuned-multilingual-MiniLM-L12-v2"
BASE_HF_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
N_TOPICS = 240
N_CLUSTERS = 5

CLUSTER_PALETTE = [
    "#ef4444",  # Coral Red
    "#3b5bdb",  # Royal Indigo/Blue
    "#10b981",  # Emerald Green
    "#8b5cf6",  # Violet
    "#f59e0b",  # Warm Amber
]

# 30 Canonical Statistics, Biostatistics & Probability Subfields for Domain Fine-Tuning
STAT_TAXONOMY = [
    ("causal inference", r"\b(causal\w*|observational\s+stud\w*|propensity\s+score\w*|instrumental\s+variable\w*|counterfactual\w*|treatment\s+effect\w*|potential\s+outcome\w*|doubly\s+robust|unmeasured\s+confound\w*|confounding|mediation\s+analy\w*|difference[\s\-]in[\s\-]difference\w*|matching\s+method\w*|dynamic\s+treatment|precision\s+medicine|directed\s+acyclic|do[\s\-]calculus|synthetic\s+control)\b"),
    ("bayesian inference", r"\b(bayesian|bayes\b|posterior|prior\s+distribut\w*|dirichlet\s+process\w*|variational\s+inference|empirical\s+bayes|gibbs\s+sampl\w*|hamiltonian\s+monte\s+carlo|mcmc|markov\s+chain\s+monte\s+carlo|approximate\s+bayesian|reversible\s+jump|credible\s+interval)\b"),
    ("survival analysis", r"\b(survival\s+analy\w*|survival\s+model\w*|survival\s+data|proportional\s+hazard\w*|cox\s+model|kaplan[\s\-]meier|competing\s+risk\w*|censored\s+data|right[\s\-]censor\w*|interval[\s\-]censor\w*|recurrent\s+event\w*|frailty\s+model\w*|cure\s+model\w*|accelerated\s+failure|time[\s\-]to[\s\-]event|hazard\s+function)\b"),
    ("clinical trials", r"\b(clinical\s+trial\w*|dose[\s\-]finding|phase\s+[i123]+|adaptive\s+design\w*|group\s+sequential|randomized\s+trial\w*|randomization|interim\s+analy\w*|biomarker[\s\-]guided|basket\s+trial|platform\s+trial|non[\s\-]inferiority|bioequivalence|bioassay)\b"),
    ("high-dimensional statistics", r"\b(high[\s\-]dimensional|lasso\b|sparse\s+\w+|sparsity|variable\s+selection|feature\s+selection|penalized\s+\w+|regulariz\w+|shrinkage\s+estimat\w*|compressed\s+sensing|matrix\s+completion|graphical\s+lasso|debiased|knockoff\w*|ridge\s+regression|elastic\s+net)\b"),
    ("time series", r"\b(time\s+series|autoregress\w*|arma\b|arima\b|garch\b|state[\s\-]space|kalman\s+filter|cointegrat\w*|spectral\s+density|periodogram|nonstationary|unit\s+root|volatility\s+model\w*|forecasting|seasonal\s+\w+|var\s+model)\b"),
    ("spatial statistics", r"\b(spatial\s+stat\w*|spatial\s+data|spatial\s+model\w*|spatio[\s\-]temporal|kriging|geostatistic\w*|gaussian\s+random\s+field\w*|point\s+process\w*|areal\s+data|spatial\s+autocorrelation|disease\s+mapping|variogram)\b"),
    ("nonparametric statistics", r"\b(nonparametric|semiparametric|kernel\s+smooth\w*|kernel\s+density|density\s+estimation|spline\w*|wavelet\w*|local\s+polynomial|rank\s+test\w*|u[\s\-]statistic\w*|empirical\s+likelihood|minimax\s+rate\w*|adaptive\s+estimation)\b"),
    ("longitudinal data", r"\b(longitudinal\s+data|longitudinal\s+stud\w*|repeated\s+measure\w*|mixed[\s\-]effect\w*|linear\s+mixed\s+model\w*|generalized\s+estimating\s+equation\w*|gee\b|joint\s+model\w*|growth\s+curve\w*|panel\s+data|clustered\s+data)\b"),
    ("functional data", r"\b(functional\s+data|functional\s+regression|functional\s+principal|curve\s+data|hilbert\s+space\s+valued|fda\b|shape\s+analy\w*|manifold[\s\-]valued|object\s+oriented\s+data)\b"),
    ("missing data", r"\b(missing\s+data|imputation|multiple\s+imputation|missing\s+at\s+random|nonignorable|incomplete\s+data|measurement\s+error|errors[\s\-]in[\s\-]variables|misclassification|em\s+algorithm)\b"),
    ("multiple testing", r"\b(multiple\s+testing|false\s+discovery\s+rate|fdr\b|multiple\s+comparison\w*|post[\s\-]selection|selective\s+inference|conformal\s+prediction|family[\s\-]wise\s+error|simultaneous\s+inference|hypothesis\s+testing|goodness[\s\-]of[\s\-]fit)\b"),
    ("experimental design", r"\b(experimental\s+design\w*|optimal\s+design\w*|factorial\s+design\w*|fractional\s+factorial|block\s+design\w*|latin\s+square|response\s+surface|computer\s+experiment\w*|latin\s+hypercube|split[\s\-]plot|crossover\s+design|anova\b)\b"),
    ("survey sampling", r"\b(survey\s+sampl\w*|finite\s+population|complex\s+survey|small\s+area\s+estimation|stratified\s+sampl\w*|capture[\s\-]recapture|nonresponse|sampling\s+design|probability\s+sampling|horvitz[\s\-]thompson)\b"),
    ("statistical genetics", r"\b(genome[\s\-]wide|gwas\b|genetic\s+association|gene\s+expression|microarray\w*|rna[\s\-]seq|single[\s\-]cell|qtl\b|eqtl\b|phylogenetic\w*|microbiome|metagenomic\w*|genomic\w*|proteomic\w*|epigenetic\w*|linkage\s+analy\w*|sequencing\s+data|bioinformatic\w*)\b"),
    ("machine learning", r"\b(machine\s+learning|deep\s+learning|neural\s+network\w*|support\s+vector|random\s+forest\w*|gradient\s+boosting|boosting|kernel\s+method\w*|rkhs\b|classification|clustering|unsupervised\s+learning|transfer\s+learning|federated\s+learning|generative\s+model\w*|diffusion\s+model\w*|transformer\w*)\b"),
    ("reinforcement learning", r"\b(reinforcement\s+learning|multi[\s\-]armed\s+bandit\w*|contextual\s+bandit\w*|markov\s+decision\s+process\w*|online\s+learning|regret\s+bound\w*|policy\s+evaluation|off[\s\-]policy|sequential\s+decision)\b"),
    ("network analysis", r"\b(network\s+analy\w*|social\s+network\w*|stochastic\s+block\s+model\w*|community\s+detection|random\s+graph\w*|graphical\s+model\w*|graphon\w*|network\s+data|exponential\s+random\s+graph|latent\s+space\s+model)\b"),
    ("change-point analysis", r"\b(change[\s\-]?point\w*|structural\s+break\w*|anomaly\s+detection|outlier\s+detection|sequential\s+detection|cusum\b|quickest\s+detection|regime\s+switching|segmentation)\b"),
    ("extreme value", r"\b(extreme\s+value\w*|tail\s+index|heavy[\s\-]tail\w*|peaks\s+over\s+threshold|generalized\s+pareto|max[\s\-]stable|rare\s+event\w*|exceedance\w*|tail\s+dependence)\b"),
    ("copulas & dependence", r"\b(copula\w*|vine\s+copula|rank\s+correlation|dependence\s+structur\w*|multivariate\s+dependence|concordance|archimedean\s+copula)\b"),
    ("financial econometrics", r"\b(econometric\w*|option\s+pricing|portfolio\s+optim\w*|asset\s+pricing|stochastic\s+volatility|credit\s+risk|financial\s+market\w*|derivative\s+pricing|black[\s\-]scholes|interest\s+rate\s+model|high[\s\-]frequency\s+trading|value[\s\-]at[\s\-]risk)\b"),
    ("actuarial science", r"\b(actuarial|insurance|ruin\s+probabilit\w*|claim\s+reserving|mortality\s+model\w*|life\s+table\w*|credibility\s+theory|reinsurance|risk\s+theory|loss\s+model\w*)\b"),
    ("reliability & quality", r"\b(reliability\s+analy\w*|reliability\s+est\w*|system\s+reliability|quality\s+control|control\s+chart\w*|statistical\s+process\s+control|degradation\s+model\w*|accelerated\s+life|warranty\s+data|maintenance\s+optim\w*)\b"),
    ("markov processes", r"\b(markov\s+chain\w*|markov\s+process\w*|hidden\s+markov|mixing\s+time\w*|ergodic\w*|stationary\s+distribut\w*|invariant\s+measure\w*|renewal\s+process\w*|branching\s+process\w*|interacting\s+particle)\b"),
    ("stochastic differential equations", r"\b(stochastic\s+differential|sde\b|spde\b|brownian\s+motion|ito\s+calculus|malliavin|diffusion\s+process\w*|levy\s+process\w*|martingale\w*|fokker[\s\-]planck|stochastic\s+integral|fractional\s+brownian)\b"),
    ("random matrices", r"\b(random\s+matri\w*|spectral\s+distribut\w*|eigenvalue\s+distribut\w*|wishart\s+matri\w*|marchenko[\s\-]pastur|tracy[\s\-]widom|free\s+probability|wigner\s+matri\w*|spiked\s+covariance)\b"),
    ("limit theorems", r"\b(central\s+limit|large\s+deviation\w*|moderate\s+deviation\w*|law\s+of\s+large\s+numbers|invariance\s+principle|empirical\s+process\w*|concentration\s+inequalit\w*|stein.?s\s+method|weak\s+convergence|asymptotic\s+normalit\w*)\b"),
    ("queueing theory", r"\b(queueing|queuing|queue\s+length|heavy\s+traffic|fluid\s+limit|service\s+system\w*|polling\s+system|birth[\s\-]death\s+process|scheduling\s+polic\w*)\b"),
    ("stochastic control", r"\b(stochastic\s+control|optimal\s+control|optimal\s+stopping|mean[\s\-]field\s+game\w*|hamilton[\s\-]jacobi|dynamic\s+programming|differential\s+game\w*|stochastic\s+optimization)\b"),
    ("quantile & robust regression", r"\b(quantile\s+regression|robust\s+regression|robust\s+estimat\w*|m[\s\-]estimat\w*|huber\s+loss|breakdown\s+point|trimmed\s+mean|median\s+regression|expectile)\b"),
    ("neuroimaging & biostatistics", r"\b(fmri\b|neuroimaging|brain\s+imaging|eeg\b|medical\s+imaging|epidemiolog\w*|infectious\s+disease\w*|epidemic\s+model\w*|public\s+health|environmental\s+health|air\s+pollution|diagnostic\s+test\w*|roc\s+curve)\b"),
    ("bootstrap & resampling", r"\b(bootstrap\w*|jackknife|resampling|permutation\s+test\w*|subsampling|cross[\s\-]validation|bagging)\b"),
    ("differential privacy", r"\b(differential\s+privac\w*|differentially\s+private|data\s+privac\w*|algorithmic\s+fairness|privacy[\s\-]preserving)\b"),
    ("optimal transport", r"\b(optimal\s+transport|wasserstein|earth\s+mover|sinkhorn|barycenter\w*)\b"),
]

MACRO_DOMAINS = [
    "Statistical Inference",
    "Clinical Biostatistics",
    "ML & High-Dimensional",
    "Stochastic Processes",
    "Time & Finance",
]

# Maps each of the 35 canonical subfields to its parent macro domain (0..4) for hierarchical two-level loss
SUBFIELD_TO_MACRO = {
    "causal inference": 0,
    "bayesian inference": 0,
    "nonparametric statistics": 0,
    "multiple testing": 0,
    "experimental design": 0,
    "survey sampling": 0,
    "quantile & robust regression": 0,
    "bootstrap & resampling": 0,
    "survival analysis": 1,
    "clinical trials": 1,
    "longitudinal data": 1,
    "missing data": 1,
    "statistical genetics": 1,
    "neuroimaging & biostatistics": 1,
    "high-dimensional statistics": 2,
    "machine learning": 2,
    "reinforcement learning": 2,
    "network analysis": 2,
    "functional data": 2,
    "differential privacy": 2,
    "optimal transport": 2,
    "markov processes": 3,
    "stochastic differential equations": 3,
    "random matrices": 3,
    "limit theorems": 3,
    "queueing theory": 3,
    "stochastic control": 3,
    "time series": 4,
    "spatial statistics": 4,
    "change-point analysis": 4,
    "extreme value": 4,
    "copulas & dependence": 4,
    "financial econometrics": 4,
    "actuarial science": 4,
    "reliability & quality": 4,
}

TAXONOMY_COMPILED = [(name, re.compile(pat, re.I)) for name, pat in STAT_TAXONOMY]
FINE_TO_MACRO_IDX = [SUBFIELD_TO_MACRO[name] for name, _ in STAT_TAXONOMY]

STRICT_FILLER_WORDS = {
    "statistical", "statistics", "statistic", "analysis", "analyses", "method", "methods",
    "methodology", "model", "models", "modeling", "modelling", "data", "using", "based",
    "approach", "approaches", "study", "studies", "theory", "theoretical", "problem", "problems",
    "contribution", "contributions", "some", "certain", "new", "novel", "application", "applications",
    "properties", "property", "thesis", "dissertation", "via", "under", "high", "low", "non",
    "multi", "two", "three", "one", "large", "small", "case", "cases", "class", "classes",
    "type", "types", "system", "systems", "function", "functions", "variable", "variables",
    "parameter", "parameters", "effect", "effects", "factor", "factors", "rate", "rates",
    "level", "levels", "order", "orders", "framework", "essay", "essays", "topics", "aspects",
    "issues", "selected", "investigations", "investigation", "evaluation", "comparison",
    "comparative", "assessment", "different", "various", "related", "associated", "general",
    "generalized", "modern", "advanced", "improved", "enhanced", "unified", "flexible",
    "research", "use", "used", "with", "from", "into", "over", "for", "and", "the", "that",
    "this", "their", "its", "are", "was", "were", "have", "has", "can", "may", "part",
    "estimation", "estimator", "estimators", "inference", "process", "processes", "distribution", "distributions",
    "test", "tests", "testing", "sample", "samples", "time", "space", "point", "points",
    "set", "sets", "measure", "measures", "probability", "mathematical", "random", "linear",
    "matrix", "matrices", "field", "fields", "graph", "graphs", "tree", "trees", "network",
    "networks", "risk", "risks", "health", "clinical", "genetic", "spatial", "dynamic", "optimal",
    "robust", "adaptive", "sequential", "dependent", "independent", "mixed", "joint",
    "local", "global", "finite", "infinite", "discrete", "continuous", "complex", "structured",
    "selection", "series", "missing", "design", "designs", "hypotheses", "hypothesis",
    "treatment", "treatments", "exposure", "stationary", "images", "image", "signal", "signals",
    "market", "markets", "financial", "finance", "credit", "pricing", "extreme", "extremes",
    "regression", "variance", "covariance", "correlation", "classification", "clustering",
    "prediction", "forecasting", "learning", "optimization", "optimisation", "control",
    "location", "population", "biological", "association", "spaces", "operators",
}

BAD_PHRASE_WORDS = {
    "some", "certain", "contributions", "essays", "topics", "selected", "aspects", "issues",
    "using", "based", "via", "under", "new", "novel", "study", "studies", "thesis", "dissertation",
}

NON_ENGLISH_STOP_RE = re.compile(
    r"\b(?:der|die|das|und|von|zur|zum|mit|fuer|fur|ueber|uber|eine|einer|einem|eines|unter|bei|nach|aus|nicht|durch|zwischen|untersuchungen|stochastische|differentialgleichungen|wahrscheinlichkeit|"
    r"des|les|une|pour|dans|par|avec|sur|aux|entre|vers|sans|etude|analyse|theorie|methodes|modeles|statistiques|probabilites|processus|stochastiques|systemes|approche|territoires|optimisation|optimale|"
    r"los|las|del|una|para|con|por|sobre|entre|modelos|estadisticos|analisis|procesos|teoria|metodos|"
    r"della|delle|degli|nella|sul|sulla|modelli|analisi|processi|teoria|"
    r"van|het|een|voor|met|naar|onderzoek)\b",
    re.I,
)


def is_english_title(title: str) -> bool:
    if not title:
        return False
    non_ascii = sum(1 for ch in title if ord(ch) > 127)
    if non_ascii >= 2 or (non_ascii >= 1 and len(title) < 35):
        return False
    if len(NON_ENGLISH_STOP_RE.findall(title)) >= 1:
        return False
    return True


def classify_stat_taxonomy(text: str):
    """Returns class index (0..len(STAT_TAXONOMY)-1) if text clearly belongs to a canonical stat subfield, else -1."""
    for idx, (_, rx) in enumerate(TAXONOMY_COMPILED):
        if rx.search(text):
            return idx
    return -1


def mean_pool_norm(token_embs: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    mask = attention_mask.unsqueeze(-1).expand(token_embs.size()).float()
    pooled = torch.sum(token_embs.float() * mask, dim=1) / torch.clamp(mask.sum(dim=1), min=1e-9)
    return F.normalize(pooled, p=2, dim=1)


def finetune_and_encode_gpu(texts: list, labels: list, batch_size: int = 256) -> np.ndarray:
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model_src = BASE_MODEL_DIR if os.path.exists(os.path.join(BASE_MODEL_DIR, "model.safetensors")) else BASE_HF_NAME
    print(f"Loading base multilingual model from {model_src} on {device}...")
    tokenizer = AutoTokenizer.from_pretrained(model_src)
    model = AutoModel.from_pretrained(model_src).to(device)

    # Build balanced Statistics-Taxonomy fine-tuning set with stratified 90/10 train/val split (up to 1,400/class)
    by_cls = defaultdict(list)
    for idx, lab in enumerate(labels):
        if lab >= 0:
            by_cls[lab].append(texts[idx])
    train_pairs = []
    val_pairs = []
    rng = random.Random(42)
    for cid, c_texts in by_cls.items():
        rng.shuffle(c_texts)
        take = c_texts[:1400]
        n_val = max(3, int(len(take) * 0.10))
        val_take = take[:n_val]
        tr_take = take[n_val:]
        cname = STAT_TAXONOMY[cid][0]
        for _ in range(min(30, max(6, len(tr_take) // 25))):
            train_pairs.append((cname, cid))
        for t in tr_take:
            train_pairs.append((t, cid))
        for t in val_take:
            val_pairs.append((t, cid))
    rng.shuffle(train_pairs)
    num_classes = len(STAT_TAXONOMY)
    max_epochs = 25
    print(f"Fine-tuning top 6 transformer layers with ArcFace hierarchical cosine margin on {len(train_pairs):,} train / {len(val_pairs):,} val dissertations across {num_classes} subfields (up to {max_epochs} epochs)...")

    # Freeze embeddings + bottom 6 layers to preserve 50+-language cross-lingual invariance; unfreeze top 6 for deeper stat geometry!
    for param in model.parameters():
        param.requires_grad = False
    for layer in model.encoder.layer[-6:]:
        for param in layer.parameters():
            param.requires_grad = True
    if hasattr(model, "pooler") and model.pooler is not None:
        for param in model.pooler.parameters():
            param.requires_grad = True

    prototypes_fine = nn.Parameter(torch.randn(num_classes, 384, device=device))
    prototypes_macro = nn.Parameter(torch.randn(len(MACRO_DOMAINS), 384, device=device))
    nn.init.xavier_uniform_(prototypes_fine)
    nn.init.xavier_uniform_(prototypes_macro)
    fine_to_macro_t = torch.tensor(FINE_TO_MACRO_IDX, dtype=torch.long, device=device)

    trainable_params = [p for p in model.parameters() if p.requires_grad] + [prototypes_fine, prototypes_macro]
    opt = torch.optim.AdamW(trainable_params, lr=4.5e-5, weight_decay=0.015)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max_epochs, eta_min=1e-6)
    scaler = torch.cuda.amp.GradScaler(enabled=(device == "cuda"))

    ft_bs = 128
    t_ft = time.time()
    best_val_loss = float("inf")
    best_state = None
    no_improve = 0
    patience = 4
    min_delta = 0.001
    lambda_macro = 0.70
    arc_margin = 0.12  # Additive cosine margin pulls same-subfield dissertations into tighter, more compact clusters!

    for epoch in range(max_epochs):
        model.train()
        rng.shuffle(train_pairs)
        tot_loss = 0.0
        correct = 0
        macro_correct = 0
        seen = 0
        for i in range(0, len(train_pairs), ft_bs):
            batch = train_pairs[i : i + ft_bs]
            b_texts = [x[0] for x in batch]
            b_y = torch.tensor([x[1] for x in batch], dtype=torch.long, device=device)
            b_m = fine_to_macro_t[b_y]
            enc = tokenizer(b_texts, padding=True, truncation=True, max_length=64, return_tensors="pt").to(device)
            opt.zero_grad(set_to_none=True)
            with torch.cuda.amp.autocast(enabled=(device == "cuda")):
                out = model(**enc)
                emb = mean_pool_norm(out.last_hidden_state, enc["attention_mask"])
                pf_norm = F.normalize(prototypes_fine.float(), p=2, dim=1)
                pm_norm = F.normalize(prototypes_macro.float(), p=2, dim=1)
                cos_f = emb @ pf_norm.T
                cos_m = emb @ pm_norm.T
                # CosFace/ArcFace-style additive margin on target class for tight cluster compactness
                cos_f_m = cos_f.clone()
                cos_f_m[torch.arange(len(b_y), device=device), b_y] -= arc_margin
                cos_m_m = cos_m.clone()
                cos_m_m[torch.arange(len(b_m), device=device), b_m] -= (arc_margin * 0.75)
                loss = F.cross_entropy(cos_f_m * 18.0, b_y, label_smoothing=0.03) + lambda_macro * F.cross_entropy(cos_m_m * 14.0, b_m, label_smoothing=0.03)
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
            tot_loss += float(loss.item()) * len(batch)
            correct += int((cos_f.argmax(dim=1) == b_y).sum().item())
            macro_correct += int((cos_m.argmax(dim=1) == b_m).sum().item())
            seen += len(batch)
        scheduler.step()

        # Evaluate on held-out validation split (margin-free logits)
        model.eval()
        val_loss = 0.0
        val_correct = 0
        val_macro_correct = 0
        val_seen = 0
        with torch.no_grad():
            pf_norm = F.normalize(prototypes_fine.float(), p=2, dim=1)
            pm_norm = F.normalize(prototypes_macro.float(), p=2, dim=1)
            for i in range(0, len(val_pairs), ft_bs):
                batch = val_pairs[i : i + ft_bs]
                b_texts = [x[0] for x in batch]
                b_y = torch.tensor([x[1] for x in batch], dtype=torch.long, device=device)
                b_m = fine_to_macro_t[b_y]
                enc = tokenizer(b_texts, padding=True, truncation=True, max_length=64, return_tensors="pt").to(device)
                with torch.cuda.amp.autocast(enabled=(device == "cuda")):
                    out = model(**enc)
                    emb = mean_pool_norm(out.last_hidden_state, enc["attention_mask"])
                    logits_f = (emb @ pf_norm.T) * 18.0
                    logits_m = (emb @ pm_norm.T) * 14.0
                    vloss = F.cross_entropy(logits_f, b_y) + lambda_macro * F.cross_entropy(logits_m, b_m)
                val_loss += float(vloss.item()) * len(batch)
                val_correct += int((logits_f.argmax(dim=1) == b_y).sum().item())
                val_macro_correct += int((logits_m.argmax(dim=1) == b_m).sum().item())
                val_seen += len(batch)

        tr_l = tot_loss / seen
        tr_a = correct / seen
        va_l = val_loss / val_seen
        va_a = val_correct / val_seen
        va_ma = val_macro_correct / val_seen
        lr_now = opt.param_groups[0]["lr"]
        mark = ""
        if va_l < best_val_loss - min_delta:
            best_val_loss = va_l
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            no_improve = 0
            mark = " *best*"
        else:
            no_improve += 1
        print(
            f"  Epoch {epoch + 1:2d}/{max_epochs}: train_loss={tr_l:.4f} acc={tr_a:.3f} | "
            f"val_loss={va_l:.4f} fine_acc={va_a:.3f} macro_acc={va_ma:.3f} | lr={lr_now:.1e} ({time.time() - t_ft:.1f}s){mark}"
        )
        if no_improve >= patience:
            print(f"  Early stopping at epoch {epoch + 1}: val_loss converged near minimum ({best_val_loss:.4f}).")
            break

    if best_state is not None:
        model.load_state_dict({k: v.to(device) for k, v in best_state.items()})

    # Save fine-tuned model locally
    os.makedirs(FINETUNED_DIR, exist_ok=True)
    model.save_pretrained(FINETUNED_DIR)
    tokenizer.save_pretrained(FINETUNED_DIR)

    # Encode ALL dissertations with the fine-tuned statistics-aware multilingual model
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


def clean_phrase(p: str) -> str:
    return re.sub(r"\s+", " ", p).strip()


def stem_word(w: str) -> str:
    w = w.lower().strip("-")
    if len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("ics") and len(w) > 5:
        return w[:-1]
    if w.endswith("es") and len(w) > 4 and not w.endswith(("series", "bayes", "processes")):
        return w[:-2]
    if w.endswith("s") and len(w) > 3 and not w.endswith(("ss", "us", "is", "series", "bayes", "chaos")):
        return w[:-1]
    return w


def stem_phrase(p: str) -> str:
    return " ".join(stem_word(w) for w in re.findall(r"[a-z0-9]+", p.lower()))


def labels_overlap_semantically(p1: str, p2: str) -> bool:
    s1, s2 = stem_phrase(p1), stem_phrase(p2)
    if s1 == s2:
        return True
    t1, t2 = set(s1.split()), set(s2.split())
    if not t1 or not t2:
        return False
    return t1.issubset(t2) or t2.issubset(t1)


def is_good_label(phrase: str) -> bool:
    words = phrase.split()
    if not words or len(phrase) < 4 or not phrase.isascii():
        return False
    if NON_ENGLISH_STOP_RE.search(phrase):
        return False
    if any(w in BAD_PHRASE_WORDS for w in words):
        return False
    if len(words) == 1 and words[0] in STRICT_FILLER_WORDS:
        return False
    if len(words) >= 2 and all(w in STRICT_FILLER_WORDS for w in words):
        iconic = {name for name, _ in STAT_TAXONOMY} | {
            "time series", "random matrices", "random graphs", "clinical trials",
            "experimental design", "spatial statistics", "statistical inference",
            "bayesian inference", "causal inference", "survival analysis", "missing data",
            "change point", "functional data", "longitudinal data", "graphical models",
            "machine learning", "deep learning", "reinforcement learning", "extreme value",
            "optimal control", "stochastic control", "stochastic processes", "markov chains",
            "brownian motion", "monte carlo", "empirical processes", "quality control",
            "queueing theory", "large deviations", "hypothesis testing", "multiple testing",
            "variable selection", "model selection", "feature selection", "high dimensional",
            "matrix completion", "factor models", "mixed models", "linear models",
            "nonparametric regression", "quantile regression", "logistic regression",
            "point processes", "branching processes", "diffusion processes",
            "financial mathematics", "statistical genetics", "differential privacy",
            "measurement error", "dose finding", "network analysis", "community detection",
            "random fields", "gaussian processes", "dirichlet processes", "levy processes",
            "empirical bayes", "variational inference", "sequential analysis", "group sequential",
            "adaptive designs", "optimal design", "sampling theory", "survey sampling",
            "robust statistics", "order statistics", "rank tests", "goodness of fit",
            "kernel smoothing", "density estimation", "spectral analysis", "panel data",
            "state space", "kalman filter", "particle filter", "copula models",
            "credit risk", "option pricing", "stochastic volatility", "actuarial science",
            "reliability analysis", "competing risks", "recurrent events", "cure models",
            "joint modeling", "causal mediation", "propensity score", "instrumental variables",
            "treatment effects", "dynamic treatment", "precision medicine", "gene expression",
            "genome wide", "single cell", "brain imaging", "infectious disease",
            "epidemic models", "spatio temporal", "optimal transport", "observational studies",
        }
        if phrase not in iconic:
            return False
    if words[0] == words[-1] and len(words) > 1:
        return False
    return True


def infer_macro_cluster_name(top_topic_labels: list) -> str:
    blob = " ".join(top_topic_labels[:10]).lower()
    if any(k in blob for k in ("markov", "stochastic differential", "limit theorem", "brownian", "random matrices", "diffusion", "percolation")):
        return "Stochastic Processes"
    if any(k in blob for k in ("econometric", "actuarial", "insurance", "option pricing", "financial", "risk management", "portfolio", "queueing", "reliability")):
        return "Financial Econometrics"
    if any(k in blob for k in ("survival", "clinical trials", "causal", "longitudinal", "biostat")):
        return "Clinical Biostatistics"
    if any(k in blob for k in ("machine learning", "sequencing", "genomic", "genetics", "neuroimaging", "cancer", "network", "spatial")):
        return "ML & Genomics"
    if any(k in blob for k in ("bayesian", "high-dimensional", "nonparametric", "multiple testing", "hypothesis", "missing data", "experimental", "time series")):
        return "Statistical Inference"
    return "Applied Theory"


def main():
    t_all = time.time()
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row

    rows = db.execute("""
        SELECT id, name, school, year, primary_field, thesis,
               COALESCE(research_areas, '') AS research_areas,
               student_count, descendant_count
        FROM people
        WHERE is_core_stat = 1 AND LENGTH(TRIM(COALESCE(thesis, ''))) >= 6
    """).fetchall()
    print(f"Loaded {len(rows):,} Core Stat/Biostat/Prob dissertations from SQLite.")

    texts = []
    titles_only = []
    tax_labels = []
    for r in rows:
        th = re.sub(r"\s+", " ", (r["thesis"] or "").strip())
        ra = re.sub(r"\s+", " ", (r["research_areas"] or "").strip())
        titles_only.append(th)
        full_txt = f"{th} ({ra[:80]})" if (ra and ra.lower() not in th.lower()) else th
        texts.append(full_txt)
        tax_labels.append(classify_stat_taxonomy(full_txt))

    labeled_cnt = sum(1 for x in tax_labels if x >= 0)
    print(f"Matched {labeled_cnt:,}/{len(rows):,} dissertations ({100.0 * labeled_cnt / len(rows):.1f}%) to 35 canonical Statistics subfields.")

    # 1. Fine-tune multilingual MiniLM on Statistics taxonomy & encode all 65,793 dissertations
    embs = finetune_and_encode_gpu(texts, tax_labels, batch_size=256)

    # 2. Fine-grained clustering (320 micro-topics)
    print(f"Clustering {len(rows):,} fine-tuned embeddings into {N_TOPICS} semantic topics...")
    km_topics = MiniBatchKMeans(n_clusters=N_TOPICS, batch_size=4096, n_init=5, max_iter=150, random_state=42)
    topic_assign = km_topics.fit_predict(embs)

    topic_members = defaultdict(list)
    for idx, tid in enumerate(topic_assign):
        topic_members[int(tid)].append(idx)

    valid_tids = sorted([t for t, m in topic_members.items() if len(m) >= 15])
    print(f"Initial valid topics (>=15 dissertations): {len(valid_tids)}")

    # Stage A: Merge near-identical centroids (cos >= 0.925 in fine-tuned space)
    clusters_members = [list(topic_members[t]) for t in valid_tids]
    clusters_members.sort(key=len, reverse=True)
    merged_members = []
    merged_centroids = []
    for mem in clusters_members:
        c = embs[mem].mean(axis=0)
        c /= (np.linalg.norm(c) + 1e-12)
        if merged_centroids:
            sims = np.dot(np.vstack(merged_centroids), c)
            best_j = int(np.argmax(sims))
            if sims[best_j] >= 0.925:
                merged_members[best_j].extend(mem)
                nc = embs[merged_members[best_j]].mean(axis=0)
                nc /= (np.linalg.norm(nc) + 1e-12)
                merged_centroids[best_j] = nc
                continue
        merged_members.append(mem)
        merged_centroids.append(c)
    print(f"After Stage A centroid merge (cos>=0.925): {len(merged_members)} topics")

    # 3. 100% English c-TF-IDF + Canonical Statistics Taxonomy Label Promotion + Stage B Dedup
    en_flags = [is_english_title(t) for t in titles_only]
    topic_docs = []
    for mem in merged_members:
        en_titles = [titles_only[i] for i in mem if en_flags[i]]
        topic_docs.append(" ".join(en_titles) if len(en_titles) >= 3 else (" ".join(en_titles) if en_titles else " "))

    vec = TfidfVectorizer(
        ngram_range=(1, 3),
        min_df=1,
        max_df=0.65,
        stop_words="english",
        token_pattern=r"\b[a-zA-Z][a-zA-Z\-]{2,}\b",
        max_features=30000,
    )
    tfidf_mat = vec.fit_transform(topic_docs)
    vocab = np.array(vec.get_feature_names_out() if hasattr(vec, "get_feature_names_out") else vec.get_feature_names())

    raw_cands_list = []
    dominant_tax = []
    for row_i, mem in enumerate(merged_members):
        scores = tfidf_mat[row_i].toarray().ravel()
        top_idx = np.argsort(scores)[::-1][:50]
        cands = []
        for vi in top_idx:
            if scores[vi] <= 0:
                break
            ph = clean_phrase(vocab[vi])
            if is_good_label(ph):
                w_bonus = 1.40 if len(ph.split()) == 2 else (1.02 if len(ph.split()) == 3 else 0.90)
                cands.append((scores[vi] * w_bonus, ph))
        cands.sort(reverse=True)

        # Check if a canonical Statistics subfield dominates this cluster (>=48% of dissertations!)
        cls_cnt = Counter(tax_labels[i] for i in mem if tax_labels[i] >= 0)
        dom_name = None
        if cls_cnt:
            best_cid, best_n = cls_cnt.most_common(1)[0]
            if best_n / len(mem) >= 0.48:
                dom_name = STAT_TAXONOMY[best_cid][0]
        dominant_tax.append(dom_name)
        raw_cands_list.append(cands)

    final_members = []
    final_centroids = []
    final_labels = []
    final_top_terms = []
    used_stems = set()
    used_single_stems = set()
    canonical_to_idx = {}

    for idx in range(len(merged_members)):
        mem = merged_members[idx]
        c = merged_centroids[idx]
        cands = raw_cands_list[idx]
        dom_name = dominant_tax[idx]

        # If this sub-bubble is dominated by the EXACT SAME canonical subfield as an existing bubble
        # (e.g. "treatment effects" + "observational studies" -> "causal inference"), MERGE them into ONE unified bubble!
        if dom_name and dom_name in canonical_to_idx:
            j = canonical_to_idx[dom_name]
            final_members[j].extend(mem)
            nc = embs[final_members[j]].mean(axis=0)
            nc /= (np.linalg.norm(nc) + 1e-12)
            final_centroids[j] = nc
            continue

        # Promote canonical Statistics label onto the primary bubble of that subfield!
        if dom_name:
            top_ph = dom_name
            cands = [(999.0, dom_name)] + cands
        else:
            top_ph = cands[0][1] if cands else ""

        merged_into = -1
        if top_ph and final_labels:
            sims = np.dot(np.vstack(final_centroids), c)
            for j, existing_lbl in enumerate(final_labels):
                if stem_phrase(top_ph) == stem_phrase(existing_lbl):
                    merged_into = j
                    break
                if sims[j] >= 0.85 and labels_overlap_semantically(top_ph, existing_lbl):
                    merged_into = j
                    break

        if merged_into >= 0:
            final_members[merged_into].extend(mem)
            nc = embs[final_members[merged_into]].mean(axis=0)
            nc /= (np.linalg.norm(nc) + 1e-12)
            final_centroids[merged_into] = nc
            continue

        chosen = None
        for _, ph in cands:
            st = stem_phrase(ph)
            if st in used_stems:
                continue
            if len(st.split()) == 1 and st in used_single_stems:
                continue
            if any(labels_overlap_semantically(ph, el) for el in final_labels):
                continue
            chosen = ph
            break
        if not chosen:
            for _, ph in cands:
                if stem_phrase(ph) not in used_stems:
                    chosen = ph
                    break
        if not chosen:
            continue

        if dom_name and chosen == dom_name:
            canonical_to_idx[dom_name] = len(final_members)
        st = stem_phrase(chosen)
        used_stems.add(st)
        for tok in st.split():
            if len(tok) >= 4:
                used_single_stems.add(tok)
        final_members.append(mem)
        final_centroids.append(c)
        final_labels.append(chosen)
        final_top_terms.append([ph for _, ph in cands[:6]])

    print(f"After Stage B dedup & canonical stat promotion: {len(final_members)} clean 100%-English topics!")

    valid_tids = list(range(len(final_members)))
    topic_members = {i: final_members[i] for i in valid_tids}
    centroids = np.vstack(final_centroids)
    topic_sizes = np.array([len(m) for m in final_members], dtype=np.float32)
    topic_labels = final_labels
    topic_top_terms = final_top_terms

    # 4. Raw Unblended 2D Cosine t-SNE Projection (with statistical cousin affinity e.g. causal inference <-> experimental design)
    print("Projecting fine-tuned 384-dim centroids to 2D via cosine t-SNE...")
    cos_dist = np.clip(1.0 - (centroids @ centroids.T), 0.0, 2.0)
    np.fill_diagonal(cos_dist, 0.0)
    cousin_pairs = [
        ("causal inference", "experimental design"),
        ("survival analysis", "clinical trials"),
        ("high-dimensional statistics", "machine learning"),
        ("time series", "financial econometrics"),
    ]
    lbl_to_i = {lbl: idx for idx, lbl in enumerate(topic_labels)}
    for a, b in cousin_pairs:
        if a in lbl_to_i and b in lbl_to_i:
            ia, ib = lbl_to_i[a], lbl_to_i[b]
            cos_dist[ia, ib] = min(cos_dist[ia, ib], 0.14)
            cos_dist[ib, ia] = cos_dist[ia, ib]

    tsne = TSNE(
        n_components=2,
        perplexity=32,
        early_exaggeration=18.0,
        metric="precomputed",
        init="random",
        learning_rate=200.0,
        n_iter=2000,
        random_state=42,
    )
    coords = tsne.fit_transform(cos_dist)

    # PCA-align the 2D cloud so its principal semantic axis runs horizontally across wide screens (1280 x 560)
    coords_centered = coords - coords.mean(axis=0, keepdims=True)
    _, _, vt = np.linalg.svd(coords_centered, full_matrices=False)
    coords_rot = coords_centered @ vt.T
    xs = coords_rot[:, 0]
    ys = coords_rot[:, 1]
    xs = 80.0 + (xs - xs.min()) / (xs.max() - xs.min() + 1e-9) * 1120.0
    ys = 52.0 + (ys - ys.min()) / (ys.max() - ys.min() + 1e-9) * 456.0

    # 5. Spatially contiguous 2D-manifold macro-clustering so every color forms one cohesive neighborhood
    pts_2d = np.column_stack([xs, ys])
    pts_2d_s = (pts_2d - pts_2d.mean(axis=0)) / (pts_2d.std(axis=0) + 1e-9)
    km_macro = KMeans(n_clusters=N_CLUSTERS, n_init=30, random_state=42)
    macro_assign = km_macro.fit_predict(pts_2d_s, sample_weight=np.sqrt(topic_sizes))

    macro_names = {}
    used_macro_names = set()
    fallback_pool = ["ML & Causal", "Time & Finance", "Clinical Biostatistics", "Bayesian & Stochastic", "High-Dimensional Stats"]
    for cid in range(N_CLUSTERS):
        t_idxs = sorted([i for i in range(len(valid_tids)) if macro_assign[i] == cid], key=lambda i: -topic_sizes[i])
        top_lbls = [topic_labels[i] for i in t_idxs]
        nm = infer_macro_cluster_name(top_lbls)
        if nm in used_macro_names:
            for fb in fallback_pool:
                if fb not in used_macro_names:
                    nm = fb
                    break
        used_macro_names.add(nm)
        macro_names[cid] = nm
        print(f"  Cluster {cid}: {nm} ({len(t_idxs)} topics, {int(sum(topic_sizes[i] for i in t_idxs)):,} dissertations) | top: {top_lbls[:6]}")

    # Pull the 5 cluster centroids closer together (0.52x inter-cluster, 0.84x intra-cluster) so domains naturally touch & overlap into one unified continent
    gcx, gcy = 640.0, 280.0
    for cid in range(N_CLUSTERS):
        t_idxs = [i for i in range(len(valid_tids)) if macro_assign[i] == cid]
        weights = np.array([topic_sizes[i] for i in t_idxs], dtype=np.float64)
        old_cx = float(np.average([xs[i] for i in t_idxs], weights=weights))
        old_cy = float(np.average([ys[i] for i in t_idxs], weights=weights))
        new_cx = gcx + 0.52 * (old_cx - gcx)
        new_cy = gcy + 0.52 * (old_cy - gcy)
        for i in t_idxs:
            xs[i] = new_cx + 0.84 * (xs[i] - old_cx)
            ys[i] = new_cy + 0.84 * (ys[i] - old_cy)

    max_sz = float(topic_sizes.max())
    min_sz = float(topic_sizes.min())
    radii = np.array([
        3.8 + 32.5 * (((float(sz) - min_sz) / (max_sz - min_sz + 1e-9)) ** 0.68)
        for sz in topic_sizes
    ])

    clusters_out = []
    for cid in range(N_CLUSTERS):
        t_idxs = [i for i in range(len(valid_tids)) if macro_assign[i] == cid]
        weights = np.array([topic_sizes[i] for i in t_idxs], dtype=np.float64)
        cx = float(np.average([xs[i] for i in t_idxs], weights=weights))
        cy = float(np.average([ys[i] for i in t_idxs], weights=weights))
        dists = sorted([math.hypot(xs[i] - cx, ys[i] - cy) + radii[i] for i in t_idxs])
        hull_r = float(dists[min(len(dists) - 1, int(len(dists) * 0.88))]) + 14.0
        t_idxs_sorted = sorted(t_idxs, key=lambda i: -topic_sizes[i])
        clusters_out.append({
            "id": cid,
            "name": macro_names[cid],
            "color": CLUSTER_PALETTE[cid % len(CLUSTER_PALETTE)],
            "cx": round(cx, 1),
            "cy": round(cy, 1),
            "r": round(hull_r, 1),
            "count": int(sum(topic_sizes[i] for i in t_idxs)),
            "topic_count": len(t_idxs),
            "top_terms": [topic_labels[i] for i in t_idxs_sorted[:6]],
        })

    topics_out = []
    for i, tid in enumerate(valid_tids):
        m_idxs = topic_members[tid]
        cent = centroids[i]
        sims = embs[m_idxs] @ cent
        years = [rows[idx]["year"] for idx in m_idxs if rows[idx]["year"] is not None and 1850 <= rows[idx]["year"] <= 2026]
        field_cnt = Counter(rows[idx]["primary_field"] for idx in m_idxs)

        scored_reps = []
        for local_k, global_idx in enumerate(m_idxs):
            r = rows[global_idx]
            sim = float(sims[local_k])
            imp = 0.015 * math.log1p((r["descendant_count"] or 0) + (r["student_count"] or 0) * 2)
            scored_reps.append((sim + imp, global_idx))
        scored_reps.sort(reverse=True)

        samples = []
        seen_names = set()
        for _, g_idx in scored_reps:
            r = rows[g_idx]
            if r["name"] in seen_names:
                continue
            seen_names.add(r["name"])
            samples.append({
                "id": r["id"],
                "name": r["name"],
                "school": r["school"] or "",
                "year": r["year"],
                "primary_field": r["primary_field"],
                "thesis": (r["thesis"] or "")[:180],
            })
            if len(samples) >= 6:
                break

        topics_out.append({
            "id": i,
            "label": topic_labels[i],
            "cluster_id": int(macro_assign[i]),
            "x": round(float(xs[i]), 1),
            "y": round(float(ys[i]), 1),
            "r": round(float(radii[i]), 1),
            "count": len(m_idxs),
            "year_min": min(years) if years else None,
            "year_max": max(years) if years else None,
            "year_median": int(np.median(years)) if years else None,
            "fields": dict(field_cnt),
            "top_terms": topic_top_terms[i],
            "samples": samples,
        })

    payload = {
        "total_dissertations": len(rows),
        "clustered_dissertations": int(sum(t["count"] for t in topics_out)),
        "embedding_model": MODEL_NAME,
        "num_clusters": len(clusters_out),
        "num_topics": len(topics_out),
        "width": 1280,
        "height": 560,
        "clusters": clusters_out,
        "topics": topics_out,
    }

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    db.execute(
        "INSERT OR REPLACE INTO meta_stats (key, value_json) VALUES ('dissertation_topic_map', ?)",
        (json.dumps(payload, ensure_ascii=False),),
    )
    db.commit()
    db.close()

    sz_kb = os.path.getsize(OUT_JSON) / 1024.0
    print(f"\nSaved {OUT_JSON} ({sz_kb:.1f} KB) + meta_stats['dissertation_topic_map'] in {time.time() - t_all:.1f}s!")


if __name__ == "__main__":
    main()
