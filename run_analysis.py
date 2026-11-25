"""
Brick-driven energy diagnostics for a hospital OR suite and pharmacy cleanroom in Bern.
Finds AHUs and critical rooms via SPARQL, checks operation against good practice
(SWKI VA105-01 for medical rooms, EU GMP Annex 1 for the pharmacy cleanroom, SIA 382/1),
and quantifies an energy-saving measure stack.
"""
import os
from dataclasses import replace
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["svg.fonttype"] = "none"

from model_and_energy import build_graph, BRICK, H, AHU, bern_climate, run_ahu, totals

os.makedirs("model", exist_ok=True); os.makedirs("results", exist_ok=True)
g = build_graph(); g.serialize("model/bern_hospital.ttl", format="turtle")
oat = bern_climate()

base = {
    "OR": AHU("AHU-OR", airflow_occ_m3h=6 * 9000, airflow_unocc_frac=1.0, schedule="or", sfp_kw_per_m3s=2.5, hr_eff=0.45),
    "PH": AHU("AHU-PH", airflow_occ_m3h=4000, airflow_unocc_frac=1.0, schedule="pharmacy", sfp_kw_per_m3s=2.5, hr_eff=0.45),
}

# SPARQL: which AHUs serve which critical rooms
Q = """
SELECT ?ahu ?room ?type WHERE {
  ?ahu a brick:AHU ; brick:feeds ?zone .
  ?zone brick:hasPart ?room . ?room a brick:Medical_Room ; h:roomType ?type .
}"""
served = list(g.query(Q, initNs={"brick": BRICK, "h": H}))
print(f"Brick model: {len(g)} triples; {len(served)} critical rooms found via SPARQL")

# Baseline trends and findings
findings = []
for key, a in base.items():
    df = run_ahu(a, oat)
    occ_share = df.occupied.mean()
    if a.airflow_unocc_frac >= 0.99:
        findings.append((a.name, "Full airflow 24/7",
                         f"Rooms are in use only {occ_share:.0%} of hours, but airflow never reduces",
                         "Occupancy-based setback with maintained overpressure"))
    if a.hr_eff < 0.6:
        findings.append((a.name, "Low heat recovery", f"Run-around coil efficiency {a.hr_eff:.0%}",
                         "High-efficiency run-around coil (about 70 %), no air mixing"))
    if a.sfp_kw_per_m3s > 2.0:
        findings.append((a.name, "High specific fan power", f"SFP {a.sfp_kw_per_m3s} kW per m3/s",
                         "EC fans, lower-pressure-drop pre-filters, pressure reset"))
findings.append(("Both AHUs", "Humidification in mild cold weather",
                 "Steam humidification starts below 5 C outdoor temperature (approximate model)",
                 "Dew-point based humidity control where the hygiene concept allows a wider band"))
pd.DataFrame(findings, columns=["AHU", "Finding", "Evidence", "Measure"]).to_csv("results/findings.csv", index=False)

# Measure stack
def stack(mod):
    return {k: mod(k, a) for k, a in base.items()}

steps = [
    ("Baseline", stack(lambda k, a: a)),
    ("M1 Occupancy setback (OR 50 %, pharmacy 60 %)",
     stack(lambda k, a: replace(a, airflow_unocc_frac=0.5 if k == "OR" else 0.6))),
    ("M2 + Heat recovery 70 %",
     stack(lambda k, a: replace(a, airflow_unocc_frac=0.5 if k == "OR" else 0.6, hr_eff=0.70))),
    ("M3 + EC fans, SFP 1.8",
     stack(lambda k, a: replace(a, airflow_unocc_frac=0.5 if k == "OR" else 0.6, hr_eff=0.70, sfp_kw_per_m3s=1.8))),
    ("M4 + Dew-point humidity control",
     stack(lambda k, a: replace(a, airflow_unocc_frac=0.5 if k == "OR" else 0.6, hr_eff=0.70, sfp_kw_per_m3s=1.8,
                                humidify_below_c=2.0))),
]
rows = []
for name, ahus in steps:
    tot = {"Electricity_MWh": 0, "Heat_MWh": 0, "Cost_kCHF": 0}
    for a in ahus.values():
        for k, v in totals(run_ahu(a, oat)).items(): tot[k] += v
    rows.append({"Step": name, "Electricity MWh/a": round(tot["Electricity_MWh"]), "Heat MWh/a": round(tot["Heat_MWh"]),
                 "Energy cost kCHF/a": round(tot["Cost_kCHF"])})
res = pd.DataFrame(rows)
b = res.iloc[0]
res["Total energy MWh/a"] = res["Electricity MWh/a"] + res["Heat MWh/a"]
res["Saving vs baseline"] = (100 * (1 - res["Total energy MWh/a"] / res.iloc[0]["Total energy MWh/a"])).round(0).astype(int).astype(str) + " %"
res.to_csv("results/measures.csv", index=False)
print("\nFindings:"); [print(" -", f[0], "|", f[1], "|", f[2]) for f in findings]
print("\nMeasures:"); print(res.to_string(index=False))

labels = ["Baseline", "M1", "M1+M2", "M1-M3", "M1-M4"]
plt.figure(figsize=(9, 4))
plt.bar([i - 0.2 for i in range(5)], res["Electricity MWh/a"], 0.4, label="Electricity", color="#3f6596")
plt.bar([i + 0.2 for i in range(5)], res["Heat MWh/a"], 0.4, label="Heat", color="#c0763a")
plt.xticks(range(5), labels); plt.ylabel("MWh per year")
plt.title("Hospital OR suite and pharmacy cleanroom: HVAC energy by measure")
plt.legend(); plt.tight_layout(); plt.savefig("results/01_measure_stack.svg"); plt.close()

d = run_ahu(base["OR"], oat).iloc[24 * 14: 24 * 21]
d2 = run_ahu(steps[1][1]["OR"], oat).iloc[24 * 14: 24 * 21]
plt.figure(figsize=(9, 3.5))
plt.plot(d.airflow_m3h.values, label="Baseline: 24/7 full airflow", color="#9a9a9a")
plt.plot(d2.airflow_m3h.values, label="Occupancy setback", color="#1f3b63")
plt.ylabel("OR suite airflow (m3/h)"); plt.xlabel("Hour of a typical week")
plt.title("Operating room airflow: one week"); plt.legend(); plt.tight_layout()
plt.savefig("results/02_or_airflow_week.svg"); plt.close()
