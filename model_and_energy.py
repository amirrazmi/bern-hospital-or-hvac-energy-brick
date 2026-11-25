"""
Brick model and hourly HVAC energy model for a representative hospital in Bern:
- Operating room (OR) suite: 6 ORs with laminar/low-turbulence displacement ceilings, served by AHU-OR
- Hospital pharmacy cleanroom (aseptic preparation, GMP grade C background), served by AHU-PH

All site data are representative assumptions for a concept case study.
"""
from dataclasses import dataclass
import numpy as np
import pandas as pd
from rdflib import Graph, Namespace, Literal, RDF, RDFS

BRICK = Namespace("https://brickschema.org/schema/Brick#")
BLDG = Namespace("urn:bern-hospital#")
H = Namespace("urn:bern-hospital-props#")

RHO, CP = 1.2, 1005.0
BERN_MONTHLY_MEAN = [-0.5, 0.8, 4.8, 8.6, 13.0, 16.4, 18.5, 17.9, 13.9, 9.4, 4.0, 0.7]
HOURS = [744, 672, 744, 720, 744, 720, 744, 744, 720, 744, 720, 744]


def bern_climate(seed=7, amp=4.5, sd=2.0):
    """Synthetic hourly outdoor temperature (replace with MeteoSwiss Bern-Zollikofen data)."""
    rng = np.random.default_rng(seed)
    out = []
    for m, h in zip(BERN_MONTHLY_MEAN, HOURS):
        k = np.arange(h)
        daily = amp * np.sin(2 * np.pi * ((k % 24) - 11) / 24)
        noise = np.convolve(rng.normal(0, sd, h + 48), np.ones(48) / np.sqrt(48), "same")[:h]
        out.append(m + daily + noise)
    return np.concatenate(out)


# ---------------------------------------------------------------- Brick model
def build_graph() -> Graph:
    g = Graph(); g.bind("brick", BRICK); g.bind("bldg", BLDG); g.bind("h", H)

    def add(n, cls, label):
        g.add((n, RDF.type, BRICK[cls])); g.add((n, RDFS.label, Literal(label))); return n

    def pt(parent, name, cls, col):
        p = add(BLDG[name], cls, name.replace("_", " ")); g.add((parent, BRICK.hasPoint, p))
        g.add((p, H.timeseries, Literal(col))); return p

    site = add(BLDG.Hospital_Bern, "Building", "Hospital, Bern region (representative case)")
    pt(site, "Outside_Air_Temp", "Outside_Air_Temperature_Sensor", "oat_c")

    for code, rooms, label in (("OR", [f"OR_{i}" for i in range(1, 7)], "AHU-OR, operating room suite"),
                               ("PH", ["Pharmacy_Cleanroom"], "AHU-PH, hospital pharmacy cleanroom")):
        ahu = add(BLDG[f"AHU_{code}"], "AHU", label); g.add((site, BRICK.hasPart, ahu))
        sf = add(BLDG[f"AHU_{code}_Supply_Fan"], "Supply_Fan", f"{label} supply fan")
        ef = add(BLDG[f"AHU_{code}_Exhaust_Fan"], "Exhaust_Fan", f"{label} exhaust fan")
        hr = add(BLDG[f"AHU_{code}_Heat_Recovery"], "Heat_Exchanger", f"{label} run-around coil heat recovery")
        hu = add(BLDG[f"AHU_{code}_Humidifier"], "Humidifier", f"{label} steam humidifier")
        ff = add(BLDG[f"AHU_{code}_Final_Filter"], "Final_Filter", f"{label} terminal HEPA filters")
        for part in (sf, ef, hr, hu, ff): g.add((ahu, BRICK.hasPart, part))
        pt(ahu, f"AHU_{code}_Supply_Air_Flow", "Supply_Air_Flow_Sensor", f"{code.lower()}_airflow_m3h")
        pt(ahu, f"AHU_{code}_Supply_Air_Temp", "Supply_Air_Temperature_Sensor", f"{code.lower()}_sat_c")
        pt(ahu, f"AHU_{code}_Mode", "Occupied_Mode_Status", f"{code.lower()}_occupied")
        pt(hr, f"AHU_{code}_HR_Efficiency", "Thermal_Power_Sensor", f"{code.lower()}_hr_eff")
        for r in rooms:
            room = add(BLDG[r], "Medical_Room", r.replace("_", " "))
            g.add((room, H.roomType, Literal("operating room" if code == "OR" else "pharmacy cleanroom GMP grade C")))
            zone = add(BLDG[f"Zone_{r}"], "HVAC_Zone", f"Zone {r}"); g.add((zone, BRICK.hasPart, room))
            g.add((ahu, BRICK.feeds, zone))
            pt(room, f"{r}_Pressure_Diff", "Air_Differential_Pressure_Sensor", f"{r.lower()}_dp_pa")
            pt(room, f"{r}_Temp", "Zone_Air_Temperature_Sensor", f"{r.lower()}_t_c")
            pt(room, f"{r}_Humidity", "Zone_Air_Humidity_Sensor", f"{r.lower()}_rh")
            pt(room, f"{r}_Occupancy", "Occupancy_Sensor", f"{r.lower()}_occ")
    return g


# ---------------------------------------------------------------- energy model
@dataclass
class AHU:
    name: str
    airflow_occ_m3h: float          # airflow in use
    airflow_unocc_frac: float       # airflow in setback as share of in-use airflow (1.0 = no setback)
    schedule: str                   # "or" = weekdays 07-17, "pharmacy" = weekdays 06-18
    sfp_kw_per_m3s: float           # specific fan power, supply + exhaust
    hr_eff: float                   # heat recovery efficiency
    t_supply: float = 20.0
    t_extract: float = 23.0
    humidify_below_c: float = 5.0   # steam humidification needed when colder than this (approx.)
    humid_kwh_per_m3_k: float = 0.00045  # approx. steam energy per m3 per K below threshold


def occupied(schedule, n):
    hour = np.arange(n); hod = hour % 24; dow = (hour // 24) % 7
    start, end = (7, 17) if schedule == "or" else (6, 18)
    return (dow < 5) & (hod >= start) & (hod < end)


def run_ahu(a: AHU, oat, chiller_cop=4.0):
    occ = occupied(a.schedule, len(oat))
    flow = np.where(occ, a.airflow_occ_m3h, a.airflow_occ_m3h * a.airflow_unocc_frac)   # m3/h
    m3s = flow / 3600
    fan_kw = m3s * a.sfp_kw_per_m3s * np.where(occ, 1.0, a.airflow_unocc_frac ** 2)   # fan power falls ~ with flow^3; SFP x flow already linear
    t_after_hr = oat + a.hr_eff * (a.t_extract - oat)
    heat_kw = np.where(t_after_hr < a.t_supply, RHO * CP * m3s * (a.t_supply - t_after_hr) / 1000, 0.0)
    cool_kw = np.where(oat > a.t_supply + 2, RHO * CP * m3s * (oat - a.t_supply) / 1000, 0.0)
    humid_kw = np.where(oat < a.humidify_below_c, flow * a.humid_kwh_per_m3_k * (a.humidify_below_c - oat), 0.0)
    return pd.DataFrame({"occupied": occ, "airflow_m3h": flow, "fan_kw": fan_kw, "heat_kw": heat_kw,
                         "cool_el_kw": cool_kw / chiller_cop, "humid_kw": humid_kw})


def totals(df, el_price=0.20, heat_price=0.12):
    el = (df.fan_kw + df.cool_el_kw + df.humid_kw).sum() / 1000
    heat = df.heat_kw.sum() / 1000
    return {"Electricity_MWh": el, "Heat_MWh": heat, "Cost_kCHF": el * el_price + heat * heat_price}
