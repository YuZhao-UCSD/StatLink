#!/usr/bin/env python3
"""
Builds the Comprehensive Global Directory of Universities with PhD-Granting Departments in
Statistics, Biostatistics, Probability, Actuarial Science, and Quantitative Data Science
(covering ASA / IPEDS CIP 27.05 & 26.1102 US doctoral universities + top international programs).
"""

import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(BASE_DIR, "data", "stat_departments_master.json")

# Format: (Canonical University, Country, [Department Names], [Known Faculty/Dept Seed URLs])
MASTER_SCHOOLS = [
    # --- United States: Universities with BOTH Statistics & Biostatistics (or Major Dual Hubs) ---
    ("Harvard University", "United States", ["Department of Statistics", "Department of Biostatistics (HSPH)"], [
        "https://statistics.fas.harvard.edu/people", "https://hsph.harvard.edu/department/biostatistics/people/"
    ]),
    ("Stanford University", "United States", ["Department of Statistics", "Department of Biomedical Data Science"], [
        "https://statistics.stanford.edu/people/faculty", "https://dbds.stanford.edu/people/faculty.html"
    ]),
    ("University of California, Berkeley", "United States", ["Department of Statistics", "Division of Biostatistics"], [
        "https://statistics.berkeley.edu/people/faculty", "https://publichealth.berkeley.edu/academics/biostatistics/faculty"
    ]),
    ("University of Washington", "United States", ["Department of Statistics", "Department of Biostatistics"], [
        "https://stat.uw.edu/about-us/people/faculty", "https://www.biostat.washington.edu/people/faculty"
    ]),
    ("University of Chicago", "United States", ["Department of Statistics", "Department of Public Health Sciences (Biostatistics)"], [
        "https://stat.uchicago.edu/people/#faculty"
    ]),
    ("Johns Hopkins University", "United States", ["Department of Biostatistics (Bloomberg SPH)", "Department of Applied Mathematics and Statistics"], [
        "https://publichealth.jhu.edu/departments/biostatistics/people/faculty", "https://engineering.jhu.edu/ams/faculty/"
    ]),
    ("University of Michigan", "United States", ["Department of Statistics", "Department of Biostatistics"], [
        "https://lsa.umich.edu/stats/people/faculty.html", "https://sph.umich.edu/biostat/faculty/"
    ]),
    ("University of North Carolina at Chapel Hill", "United States", ["Department of Statistics and Operations Research", "Department of Biostatistics (Gillings SPH)"], [
        "https://stor.unc.edu/faculty-directory/", "https://sph.unc.edu/bios/biostatistics-faculty-and-staff/"
    ]),
    ("North Carolina State University", "United States", ["Department of Statistics"], [
        "https://statistics.sciences.ncsu.edu/people/faculty/"
    ]),
    ("Columbia University", "United States", ["Department of Statistics", "Department of Biostatistics (Mailman SPH)"], [
        "https://stat.columbia.edu/department-directory/faculty-and-lecturers/", "https://www.publichealth.columbia.edu/academics/departments/biostatistics/directory"
    ]),
    ("University of Pennsylvania", "United States", ["Department of Statistics and Data Science (Wharton)", "Department of Biostatistics, Epidemiology and Informatics"], [
        "https://statistics.wharton.upenn.edu/faculty/faculty-list/", "https://www.med.upenn.edu/cceb/biostatistics-faculty.html"
    ]),
    ("Carnegie Mellon University", "United States", ["Department of Statistics & Data Science"], [
        "https://www.cmu.edu/dietrich/statistics-datascience/faculty/index.html"
    ]),
    ("Duke University", "United States", ["Department of Statistical Science", "Department of Biostatistics and Bioinformatics"], [
        "https://stat.duke.edu/people/appointed-faculty", "https://biostat.duke.edu/faculty"
    ]),
    ("Cornell University", "United States", ["Department of Statistics and Data Science", "Division of Biostatistics"], [
        "https://stat.cornell.edu/people/faculty"
    ]),
    ("University of Wisconsin-Madison", "United States", ["Department of Statistics", "Department of Biostatistics and Medical Informatics"], [
        "https://stat.wisc.edu/people-faculty/", "https://www.biostat.wisc.edu/People-Faculty"
    ]),
    ("University of Minnesota", "United States", ["School of Statistics", "Division of Biostatistics & Health Data Science"], [
        "https://cla.umn.edu/statistics/people/faculty", "https://www.sph.umn.edu/academics/divisions/biostatistics/directory/"
    ]),
    ("University of California, San Diego", "United States", ["Halıcıoğlu Data Science Institute", "Division of Biostatistics and Bioinformatics", "Department of Mathematics (Statistics)"], [
        "https://datascience.ucsd.edu/about/people/faculty/", "https://ph.ucsd.edu/biostat/faculty/", "https://math.ucsd.edu/people/faculty"
    ]),
    ("University of California, Los Angeles", "United States", ["Department of Statistics and Data Science", "Department of Biostatistics (Fielding SPH)"], [
        "https://statistics.ucla.edu/people/faculty/", "https://ph.ucla.edu/about/departments/department-biostatistics/faculty"
    ]),
    ("University of California, Davis", "United States", ["Department of Statistics", "Graduate Group in Biostatistics"], [
        "https://statistics.ucdavis.edu/people/faculty"
    ]),
    ("University of California, Santa Barbara", "United States", ["Department of Statistics and Applied Probability"], [
        "https://www.pstat.ucsb.edu/people/academic"
    ]),
    ("University of California, Irvine", "United States", ["Department of Statistics"], [
        "https://www.stat.uci.edu/faculty/"
    ]),
    ("University of California, Santa Cruz", "United States", ["Department of Statistics"], [
        "https://statistics.ucsc.edu/people/faculty.html"
    ]),
    ("University of California, Riverside", "United States", ["Department of Statistics"], [
        "https://statistics.ucr.edu/faculty"
    ]),
    ("University of California, San Francisco", "United States", ["Department of Epidemiology and Biostatistics"], [
        "https://epibiostat.ucsf.edu/faculty"
    ]),
    ("Iowa State University", "United States", ["Department of Statistics"], [
        "https://www.stat.iastate.edu/people/faculty"
    ]),
    ("Purdue University", "United States", ["Department of Statistics"], [
        "https://www.stat.purdue.edu/people/faculty/index.html"
    ]),
    ("Texas A&M University", "United States", ["Department of Statistics"], [
        "https://stat.tamu.edu/people/faculty/"
    ]),
    ("Ohio State University", "United States", ["Department of Statistics", "Division of Biostatistics"], [
        "https://stat.osu.edu/people/faculty"
    ]),
    ("Pennsylvania State University", "United States", ["Department of Statistics"], [
        "https://science.psu.edu/stat/people/faculty"
    ]),
    ("University of Illinois Urbana-Champaign", "United States", ["Department of Statistics"], [
        "https://stat.illinois.edu/directory/faculty"
    ]),
    ("Virginia Tech", "United States", ["Department of Statistics"], [
        "https://www.stat.vt.edu/people/stat-faculty.html"
    ]),
    ("Rutgers University", "United States", ["Department of Statistics", "Department of Biostatistics and Epidemiology"], [
        "https://statistics.rutgers.edu/people-pages/faculty"
    ]),
    ("Princeton University", "United States", ["Department of Operations Research and Financial Engineering (Statistics)", "Center for Statistics and Machine Learning"], [
        "https://orfe.princeton.edu/people/faculty", "https://csml.princeton.edu/people/faculty"
    ]),
    ("Massachusetts Institute of Technology", "United States", ["Statistics and Data Science Center (IDSS)", "Department of Mathematics (Statistics & Probability)"], [
        "https://stat.mit.edu/people/faculty/", "https://math.mit.edu/directory/faculty/"
    ]),
    ("California Institute of Technology", "United States", ["Department of Computing and Mathematical Sciences"], [
        "https://www.cms.caltech.edu/people"
    ]),
    ("Yale University", "United States", ["Department of Statistics and Data Science", "Department of Biostatistics (YSPH)"], [
        "https://statistics.yale.edu/people/faculty", "https://ysph.yale.edu/public-health-research-and-practice/department-research/biostatistics/people/"
    ]),
    ("Brown University", "United States", ["Department of Biostatistics", "Division of Applied Mathematics"], [
        "https://biostatistics.sph.brown.edu/people/faculty", "https://appliedmath.brown.edu/people/faculty"
    ]),
    ("New York University", "United States", ["Tech/Stern Statistics Group", "Courant Institute (Probability & Statistics)", "Division of Biostatistics"], [
        "https://www.stern.nyu.edu/experience-stern/about/departments-centers-initiatives/academic-departments/top-stats/faculty"
    ]),
    ("Northwestern University", "United States", ["Department of Statistics and Data Science", "Division of Biostatistics"], [
        "https://statistics.northwestern.edu/people/faculty/"
    ]),
    ("Emory University", "United States", ["Department of Biostatistics and Bioinformatics (Rollins SPH)"], [
        "https://sph.emory.edu/departments/bios/faculty/index.html"
    ]),
    ("University of Pittsburgh", "United States", ["Department of Statistics", "Department of Biostatistics"], [
        "https://www.stat.pitt.edu/people/faculty", "https://www.sph.pitt.edu/biostatistics/directory"
    ]),
    ("Boston University", "United States", ["Department of Mathematics and Statistics", "Department of Biostatistics (BUSPH)"], [
        "https://www.bu.edu/math/people/faculty/", "https://www.bu.edu/sph/about/departments/biostatistics/faculty-and-staff/"
    ]),
    ("Rice University", "United States", ["Department of Statistics"], [
        "https://statistics.rice.edu/people/faculty"
    ]),
    ("University of Florida", "United States", ["Department of Statistics", "Department of Biostatistics"], [
        "https://stat.ufl.edu/people/faculty/", "https://biostat.ufl.edu/about/people/faculty/"
    ]),
    ("Florida State University", "United States", ["Department of Statistics"], [
        "https://stat.fsu.edu/people/faculty"
    ]),
    ("University of Connecticut", "United States", ["Department of Statistics"], [
        "https://stat.uconn.edu/faculty/"
    ]),
    ("University of Georgia", "United States", ["Department of Statistics", "Department of Epidemiology & Biostatistics"], [
        "https://www.stat.uga.edu/directory/faculty"
    ]),
    ("University of Iowa", "United States", ["Department of Statistics and Actuarial Science", "Department of Biostatistics"], [
        "https://stat.uiowa.edu/people/faculty", "https://www.public-health.uiowa.edu/biostat-faculty-list/"
    ]),
    ("Indiana University", "United States", ["Department of Statistics", "Department of Biostatistics and Health Data Science"], [
        "https://stat.indiana.edu/about/faculty/index.html"
    ]),
    ("Michigan State University", "United States", ["Department of Statistics and Probability"], [
        "https://stt.natsci.msu.edu/directory/faculty/"
    ]),
    ("University of Texas at Austin", "United States", ["Department of Statistics and Data Sciences"], [
        "https://stat.utexas.edu/directory/faculty"
    ]),
    ("University of Texas Health Science Center at Houston", "United States", ["Department of Biostatistics and Data Science (UTHealth / MD Anderson)"], []),
    ("Vanderbilt University", "United States", ["Department of Biostatistics"], [
        "https://www.vumc.org/biostatistics/person-type/faculty"
    ]),
    ("University of Rochester", "United States", ["Department of Biostatistics and Computational Biology", "Department of Statistics"], [
        "https://www.urmc.rochester.edu/biostatistics/people/faculty.aspx"
    ]),
    ("Washington University in St. Louis", "United States", ["Department of Statistics and Data Science", "Division of Biostatistics"], [
        "https://sds.wustl.edu/people"
    ]),
    ("University of Maryland", "United States", ["Statistics Program (Dept of Mathematics)", "Joint Program in Survey Methodology"], []),
    ("Colorado State University", "United States", ["Department of Statistics"], ["https://statistics.colostate.edu/people/"]),
    ("University of Colorado Anschutz Medical Campus", "United States", ["Department of Biostatistics and Informatics"], []),
    ("University of Colorado Boulder", "United States", ["Department of Applied Mathematics (Statistics)"], []),
    ("Oregon State University", "United States", ["Department of Statistics"], ["https://stat.oregonstate.edu/people/faculty"]),
    ("University of Missouri", "United States", ["Department of Statistics"], ["https://statistics.missouri.edu/people/faculty"]),
    ("Kansas State University", "United States", ["Department of Statistics"], ["https://www.k-state.edu/stats/about/people/faculty.html"]),
    ("Oklahoma State University", "United States", ["Department of Statistics"], []),
    ("University of Arizona", "United States", ["Statistics and Data Science GIDP", "Department of Epidemiology and Biostatistics"], []),
    ("Arizona State University", "United States", ["School of Mathematical and Statistical Sciences"], []),
    ("University of New Mexico", "United States", ["Department of Mathematics and Statistics"], []),
    ("University of Utah", "United States", ["Division of Biostatistics", "Department of Mathematics"], []),
    ("Utah State University", "United States", ["Department of Mathematics and Statistics"], []),
    ("Brigham Young University", "United States", ["Department of Statistics"], []),
    ("Montana State University", "United States", ["Department of Mathematical Sciences (Statistics)"], []),
    ("University of Nebraska-Lincoln", "United States", ["Department of Statistics"], ["https://statistics.unl.edu/faculty-person-type/faculty"]),
    ("North Dakota State University", "United States", ["Department of Statistics"], []),
    ("South Dakota State University", "United States", ["Department of Mathematics and Statistics"], []),
    ("Baylor University", "United States", ["Department of Statistical Science"], ["https://statistics.artsandsciences.baylor.edu/people/faculty"]),
    ("Southern Methodist University", "United States", ["Department of Statistical Science"], ["https://www.smu.edu/dedman/academics/departments/statistics/people/faculty"]),
    ("George Mason University", "United States", ["Department of Statistics"], ["https://statistics.gmu.edu/people/faculty"]),
    ("George Washington University", "United States", ["Department of Statistics", "Department of Biostatistics and Bioinformatics"], ["https://statistics.columbian.gwu.edu/faculty"]),
    ("American University", "United States", ["Department of Mathematics and Statistics"], []),
    ("Temple University", "United States", ["Department of Statistics, Operations, and Data Science"], []),
    ("Drexel University", "United States", ["Department of Epidemiology and Biostatistics"], []),
    ("University of Southern California", "United States", ["Department of Data Sciences and Operations", "Division of Biostatistics"], []),
    ("University of Alabama at Birmingham", "United States", ["Department of Biostatistics"], []),
    ("University of Alabama", "United States", ["Applied Statistics Program"], []),
    ("Auburn University", "United States", ["Department of Mathematics and Statistics"], []),
    ("Louisiana State University", "United States", ["Department of Experimental Statistics"], []),
    ("Tulane University", "United States", ["Department of Biostatistics and Data Science"], []),
    ("University of Kentucky", "United States", ["Dr. Bing Zhang Department of Statistics", "Department of Biostatistics"], ["https://stat.as.uky.edu/users/faculty"]),
    ("University of Louisville", "United States", ["Department of Bioinformatics and Biostatistics"], []),
    ("University of South Carolina", "United States", ["Department of Statistics", "Department of Epidemiology and Biostatistics"], ["https://sc.edu/study/colleges_schools/artsandsciences/statistics/our_people/index.php"]),
    ("Medical University of South Carolina", "United States", ["Department of Public Health Sciences (Biostatistics)"], []),
    ("Clemson University", "United States", ["School of Mathematical and Statistical Sciences"], []),
    ("Virginia Commonwealth University", "United States", ["Department of Biostatistics"], []),
    ("University of Virginia", "United States", ["Department of Statistics"], ["https://statistics.as.virginia.edu/faculty"]),
    ("University of Massachusetts Amherst", "United States", ["Department of Mathematics and Statistics", "Department of Biostatistics and Epidemiology"], []),
    ("University at Buffalo (SUNY)", "United States", ["Department of Biostatistics"], ["https://publichealth.buffalo.edu/biostatistics/faculty-and-staff/faculty-directory.html"]),
    ("Stony Brook University", "United States", ["Department of Applied Mathematics and Statistics"], []),
    ("University at Albany (SUNY)", "United States", ["Department of Epidemiology and Biostatistics"], []),
    ("Binghamton University (SUNY)", "United States", ["Department of Mathematical Sciences (Statistics)"], []),
    ("University of Illinois at Chicago", "United States", ["Department of Mathematics, Statistics, and Computer Science", "Division of Epidemiology and Biostatistics"], []),
    ("Northern Illinois University", "United States", ["Department of Mathematical Sciences (Statistics)"], []),
    ("Case Western Reserve University", "United States", ["Department of Population and Quantitative Health Sciences (Biostatistics)", "Department of Mathematics, Applied Mathematics, and Statistics"], []),
    ("University of Cincinnati", "United States", ["Division of Biostatistics and Bioinformatics"], []),
    ("Bowling Green State University", "United States", ["Department of Mathematics and Statistics"], []),
    ("Miami University", "United States", ["Department of Statistics"], []),
    ("Western Michigan University", "United States", ["Department of Statistics"], []),
    ("Central Michigan University", "United States", ["Department of Statistics, Actuarial and Data Sciences"], []),
    ("Wayne State University", "United States", ["Department of Mathematics (Statistics)"], []),
    ("Medical College of Wisconsin", "United States", ["Division of Biostatistics"], []),
    ("University of Notre Dame", "United States", ["Department of Applied and Computational Mathematics and Statistics"], ["https://acms.nd.edu/people/faculty/"]),
    ("Worcester Polytechnic Institute", "United States", ["Department of Mathematical Sciences (Statistics)"], []),
    ("University of Maryland, Baltimore County", "United States", ["Department of Mathematics and Statistics"], []),
    ("University of Delaware", "United States", ["Department of Applied Economics and Statistics"], []),
    ("Georgia Institute of Technology", "United States", ["H. Milton Stewart School of Industrial and Systems Engineering (Statistics)"], []),
    ("Georgia State University", "United States", ["Department of Mathematics and Statistics", "Department of Population Health Sciences (Biostatistics)"], []),
    ("University of Central Florida", "United States", ["Department of Statistics and Data Science"], []),
    ("University of South Florida", "United States", ["College of Public Health (Biostatistics)"], []),
    ("University of Miami", "United States", ["Division of Biostatistics"], []),

    # --- Canada ---
    ("University of Toronto", "Canada", ["Department of Statistical Sciences", "Division of Biostatistics (Dalla Lana SPH)"], ["https://www.statistics.utoronto.ca/people/directories/all-faculty"]),
    ("University of Waterloo", "Canada", ["Department of Statistics and Actuarial Science"], ["https://uwaterloo.ca/statistics-and-actuarial-science/profiles"]),
    ("University of British Columbia", "Canada", ["Department of Statistics"], ["https://www.stat.ubc.ca/users/faculty"]),
    ("McGill University", "Canada", ["Department of Mathematics and Statistics", "Department of Epidemiology, Biostatistics and Occupational Health"], ["https://www.mcgill.ca/epi-biostat-occh/faculty-0"]),
    ("Simon Fraser University", "Canada", ["Department of Statistics and Actuarial Science"], ["https://www.sfu.ca/stat-actsci/faculty-and-staff/faculty.html"]),
    ("Western University", "Canada", ["Department of Statistical and Actuarial Sciences"], []),
    ("University of Alberta", "Canada", ["Department of Mathematical and Statistical Sciences"], []),
    ("Université de Montréal", "Canada", ["Département de mathématiques et de statistique"], []),
    ("Queen's University", "Canada", ["Department of Mathematics and Statistics"], []),
    ("University of Ottawa", "Canada", ["Department of Mathematics and Statistics"], []),
    ("University of Calgary", "Canada", ["Department of Mathematics and Statistics"], []),
    ("Université Laval", "Canada", ["Département de mathématiques et de statistique"], []),
    ("University of Manitoba", "Canada", ["Department of Statistics"], []),
    ("Carleton University", "Canada", ["School of Mathematics and Statistics"], []),

    # --- United Kingdom & Ireland ---
    ("University of Oxford", "United Kingdom", ["Department of Statistics"], ["https://www.stats.ox.ac.uk/people/academic_staff"]),
    ("University of Cambridge", "United Kingdom", ["Statistical Laboratory (DPMMS)"], ["https://www.statslab.cam.ac.uk/people"]),
    ("Imperial College London", "United Kingdom", ["Statistics Section (Dept of Mathematics)", "School of Public Health (Biostatistics)"], ["https://www.imperial.ac.uk/statistics/staff/"]),
    ("London School of Economics and Political Science", "United Kingdom", ["Department of Statistics"], ["https://www.lse.ac.uk/statistics/people"]),
    ("University College London", "United Kingdom", ["Department of Statistical Science"], ["https://www.ucl.ac.uk/statistics/people/academic-and-teaching-staff"]),
    ("London School of Hygiene & Tropical Medicine", "United Kingdom", ["Department of Medical Statistics"], []),
    ("University of Warwick", "United Kingdom", ["Department of Statistics"], ["https://warwick.ac.uk/fac/sci/statistics/staff/academic-research/"]),
    ("University of Edinburgh", "United Kingdom", ["Statistics Group (School of Mathematics)"], []),
    ("University of Bristol", "United Kingdom", ["Institute for Statistical Science"], []),
    ("Lancaster University", "United Kingdom", ["Department of Mathematics and Statistics", "CHICAS (Biostatistics)"], []),
    ("University of Bath", "United Kingdom", ["Department of Mathematical Sciences (Statistics & Probability)"], []),
    ("University of Manchester", "United Kingdom", ["Probability and Statistics Group", "Centre for Biostatistics"], []),
    ("University of Glasgow", "United Kingdom", ["School of Mathematics and Statistics"], []),
    ("University of Nottingham", "United Kingdom", ["Statistics and Probability Group"], []),
    ("University of Southampton", "United Kingdom", [" Southampton Statistical Sciences Research Institute"], []),
    ("Durham University", "United Kingdom", ["Department of Mathematical Sciences (Statistics)"], []),
    ("University of St Andrews", "United Kingdom", ["School of Mathematics and Statistics (CREEM)"], []),
    ("Trinity College Dublin", "Ireland", ["Discipline of Statistics and Information Systems"], []),

    # --- Continental Europe ---
    ("ETH Zürich", "Switzerland", ["Seminar for Statistics (SfS)", "Probability Group"], ["https://stat.ethz.ch/people.html"]),
    ("EPFL", "Switzerland", ["Institute of Mathematics (Chair of Statistics / Probability)"], []),
    ("Sorbonne Université", "France", ["LPSM (Laboratoire de Probabilités, Statistique et Modélisation)"], []),
    ("Université Paris-Saclay", "France", ["Laboratoire de Mathématiques d'Orsay (Probabilités & Statistique)", "ENSAE / CREST"], []),
    ("École Normale Supérieure", "France", ["Département de Mathématiques et Applications"], []),
    ("École Polytechnique", "France", ["CMAP (Probabilités & Statistique)"], []),
    ("Université de Toulouse", "France", ["Institut de Mathématiques de Toulouse (Statistique & Probabilités)"], []),
    ("Leiden University", "Netherlands", ["Mathematical Institute (Statistics & Probability)", "Medical Statistics"], []),
    ("University of Amsterdam", "Netherlands", ["Korteweg-de Vries Institute (Stochastics)"], []),
    ("Delft University of Technology", "Netherlands", ["Delft Institute of Applied Mathematics (Statistics & Probability)"], []),
    ("Eindhoven University of Technology", "Netherlands", ["Stochastics Section"], []),
    ("KU Leuven", "Belgium", ["Leuven Statistics Research Centre (LStat)"], []),
    ("UCLouvain", "Belgium", ["Institute of Statistics, Biostatistics and Actuarial Sciences (ISBA)"], []),
    ("University of Copenhagen", "Denmark", ["Department of Mathematical Sciences (Statistics & Probability)", "Section of Biostatistics"], []),
    ("Aarhus University", "Denmark", ["Department of Mathematics (Stochastics)"], []),
    ("Stockholm University", "Sweden", ["Department of Mathematics (Mathematical Statistics)"], []),
    ("KTH Royal Institute of Technology", "Sweden", ["Division of Mathematical Statistics"], []),
    ("University of Oslo", "Norway", ["Statistics and Data Science Group"], []),
    ("University of Helsinki", "Finland", ["Statistics and Probability Group"], []),
    ("Technical University of Munich", "Germany", ["Chair of Mathematical Statistics"], []),
    ("Ludwig-Maximilians-Universität München", "Germany", ["Department of Statistics"], []),
    ("University of Bonn", "Germany", ["Institute for Applied Mathematics (Probability & Stochastics)"], []),
    ("Ruprecht-Karls-Universität Heidelberg", "Germany", ["Institute for Applied Mathematics (Stochastics)"], []),
    ("Georg-August-Universität Göttingen", "Germany", ["Institute for Mathematical Stochastics"], []),
    ("Bocconi University", "Italy", ["Department of Decision Sciences (Statistics)"], []),
    ("Università degli Studi di Padova", "Italy", ["Department of Statistical Sciences"], []),
    ("Universidad Carlos III de Madrid", "Spain", ["Department of Statistics"], []),

    # --- Asia-Pacific, Israel & Latin America ---
    ("Indian Statistical Institute", "India", ["Stat-Math Unit & Applied Statistics Unit (Kolkata, Delhi, Bangalore)"], []),
    ("National University of Singapore", "Singapore", ["Department of Statistics and Data Science"], ["https://www.stat.nus.edu.sg/people/faculty-members/"]),
    ("Nanyang Technological University", "Singapore", ["Division of Mathematical Sciences (Statistics)"], []),
    ("University of Hong Kong", "Hong Kong", ["Department of Statistics and Actuarial Science"], ["https://saasweb.hku.hk/people/academic.php"]),
    ("Hong Kong University of Science and Technology", "Hong Kong", ["Department of ISOM (Statistics) & Mathematics"], []),
    ("Chinese University of Hong Kong", "Hong Kong", ["Department of Statistics"], ["https://www.sta.cuhk.edu.hk/peoples/academic-staff/"]),
    ("Peking University", "China", ["School of Mathematical Sciences (Dept of Probability & Statistics)", "Center for Statistical Science"], []),
    ("Tsinghua University", "China", ["Department of Statistics and Data Science"], []),
    ("University of Science and Technology of China", "China", ["Department of Statistics and Finance"], []),
    ("Fudan University", "China", ["Department of Statistics and Data Science"], []),
    ("Renmin University of China", "China", ["School of Statistics", "Institute of Statistics and Big Data"], []),
    ("Shanghai Jiao Tong University", "China", ["Department of Statistics"], []),
    ("Zhejiang University", "China", ["School of Mathematical Sciences (Statistics)"], []),
    ("National Taiwan University", "Taiwan", ["Institute of Statistical Science / Math"], []),
    ("University of Tokyo", "Japan", ["Mathematical Informatics / Statistics"], []),
    ("Institute of Statistical Mathematics", "Japan", ["Department of Statistical Methodology"], []),
    ("Seoul National University", "South Korea", ["Department of Statistics"], []),
    ("KAIST", "South Korea", ["Department of Mathematical Sciences (Probability & Statistics)"], []),
    ("Yonsei University", "South Korea", ["Department of Statistics and Data Science"], []),
    ("Australian National University", "Australia", ["Research School of Finance, Actuarial Studies and Statistics"], []),
    ("University of Melbourne", "Australia", ["School of Mathematics and Statistics"], []),
    ("University of Sydney", "Australia", ["School of Mathematics and Statistics"], []),
    ("University of New South Wales", "Australia", ["School of Mathematics and Statistics"], []),
    ("Monash University", "Australia", ["Department of Econometrics and Business Statistics"], []),
    ("Technion – Israel Institute of Technology", "Israel", ["Faculty of Data and Decision Sciences (Statistics)"], []),
    ("Tel Aviv University", "Israel", ["Department of Statistics and Operations Research"], []),
    ("Hebrew University of Jerusalem", "Israel", ["Department of Statistics and Data Science"], []),
    ("Weizmann Institute of Science", "Israel", ["Department of Computer Science and Applied Mathematics"], []),
    ("IMPA", "Brazil", ["Probability and Statistics"], []),
    ("Universidade de São Paulo", "Brazil", ["Department of Statistics (IME-USP)"], []),
    ("Pontificia Universidad Católica de Chile", "Chile", ["Department of Statistics"], []),
]


def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    structured = []
    for school, country, depts, urls in MASTER_SCHOOLS:
        structured.append({
            "school": school,
            "country": country,
            "departments": depts,
            "seed_urls": urls,
        })
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(structured, f, indent=2, ensure_ascii=False)
    print(f"Saved {len(structured)} global Statistics/Biostatistics/Probability PhD institutions to {OUT_PATH}")


if __name__ == "__main__":
    main()
