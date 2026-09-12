"""Fluid / thermophysical property corpus via CoolProp (issue #100).

A real ``f(T, P)`` source for the thermal and CFD screening oracles so they stop
relying on hardcoded or caller-supplied fluid constants. CoolProp's Helmholtz-
energy equations of state (REFPROP-class) return density ρ, dynamic viscosity μ,
isobaric specific heat cₚ and thermal conductivity k — and the derived Prandtl
number Pr = μ·cₚ/k — for air, water, refrigerants, gases and oils as functions of
temperature and pressure.

License: CoolProp is BSD-3-Clause and a pure wheel, so it is imported **in-process**
(an opt-in ``fluids`` pip extra — see ``pyproject.toml``), exactly like the optiland
optics extra; there is no copyleft subprocess boundary. CoolProp is cited as the
data ``source`` on every return.

Degradation contract — *nothing hard-breaks* when the extra is absent:
  - For fluids that have a built-in constant fallback (air, water) the call still
    returns usable numbers with ``fidelity="constant_fallback"`` and a warning, so
    the wired thermal/CFD paths keep working without CoolProp.
  - For any other fluid the call returns ``{ok: False, reason, install}`` so the
    caller can degrade explicitly.

Return conventions mirror the rest of the corpus (``fidelity``, ``valid_range_ok``,
``source``, ``warnings``). SI units throughout:
  density [kg/m³], viscosity [Pa·s], cp [J/kg·K], conductivity [W/m·K],
  prandtl [-], kinematic_viscosity [m²/s].
"""
from __future__ import annotations

from ankusdrive import install_kind as _install_kind

# The plain-venv form; _install_hint() rewrites it for pipx / uv / the Claude
# Desktop extension, where a shell `pip install` lands in another Python (#347).
_INSTALL_HINT = "pip install 'ankusdrive[fluids]'  (CoolProp, BSD-3, in-process)"


def _install_hint() -> str:
    return _install_kind.adapt(_INSTALL_HINT)


try:  # in-process import, like optiland — no GPL/copyleft boundary
    import CoolProp  # noqa: F401
    from CoolProp.CoolProp import PropsSI as _PropsSI

    _HAVE_COOLPROP = True
    _COOLPROP_VERSION = getattr(CoolProp, "__version__", "?")
except Exception:  # pragma: no cover - exercised on hosts without the extra
    _HAVE_COOLPROP = False
    _COOLPROP_VERSION = None

    def _PropsSI(*_a, **_k):  # type: ignore
        raise RuntimeError("CoolProp not installed")


# Friendly aliases -> canonical CoolProp fluid names. CoolProp's own names also
# work (they pass through), so this only smooths over casing and common spellings.
_ALIASES = {
    "air": "Air",
    "water": "Water",
    "h2o": "Water",
    "steam": "Water",
    "nitrogen": "Nitrogen",
    "n2": "Nitrogen",
    "oxygen": "Oxygen",
    "o2": "Oxygen",
    "co2": "CO2",
    "carbondioxide": "CO2",
    "carbon-dioxide": "CO2",
    "hydrogen": "Hydrogen",
    "h2": "Hydrogen",
    "helium": "Helium",
    "argon": "Argon",
    "methane": "Methane",
    "ammonia": "Ammonia",
    "nh3": "Ammonia",
    "ethanol": "Ethanol",
    "methanol": "Methanol",
    "r134a": "R134a",
    "r410a": "R410A",
    "r1234yf": "R1234yf",
    "r744": "CO2",
    "r717": "Ammonia",
}

# Constant fallbacks (≈20 °C, 1 atm) used ONLY when CoolProp is absent, so the
# wired thermal/CFD paths still return numbers. Values are the same handbook
# constants the callers used before this module existed.
_FALLBACK = {
    "Air": {
        "density": 1.204, "viscosity": 1.813e-5, "cp": 1006.0,
        "conductivity": 0.0257, "prandtl": 0.71,
    },
    "Water": {
        "density": 998.2, "viscosity": 1.002e-3, "cp": 4182.0,
        "conductivity": 0.598, "prandtl": 7.01,
    },
}


def _canonical(name: str) -> str:
    """Map a friendly fluid name to CoolProp's canonical spelling (pass-through
    for names CoolProp already knows)."""
    key = str(name).strip().lower().replace(" ", "")
    return _ALIASES.get(key, str(name).strip())


def available() -> bool:
    """True when the CoolProp extra is importable in-process."""
    return _HAVE_COOLPROP


def fluid_props(name: str, T_K: float, P_Pa: float = 101325.0) -> dict:
    """Thermophysical properties of ``name`` at temperature ``T_K`` and pressure
    ``P_Pa`` from CoolProp's equation of state.

    Returns ``{ok, fluid, coolprop_name, T_K, P_Pa, density, viscosity, cp,
    conductivity, prandtl, kinematic_viscosity, fidelity, valid_range_ok, source,
    warnings, coolprop_available}`` (SI units). ``fidelity`` is ``"equation_of_state"``
    when CoolProp resolved the state, ``"constant_fallback"`` when it served a
    built-in ≈20 °C constant because CoolProp is absent. Outside the fluid's valid
    (T, P) envelope the nearest in-range state is evaluated and ``valid_range_ok``
    is False with the reason in ``warnings``.

    When CoolProp is absent and ``name`` has no constant fallback the result is
    ``{ok: False, reason, install, coolprop_available: False}`` so callers degrade
    cleanly.

    Raises ValueError on a non-positive temperature/pressure or an unknown fluid
    (when CoolProp is present)."""
    if T_K <= 0:
        raise ValueError("T_K must be > 0 (absolute temperature)")
    if P_Pa <= 0:
        raise ValueError("P_Pa must be > 0")
    cp_name = _canonical(name)

    if not _HAVE_COOLPROP:
        fb = _FALLBACK.get(cp_name)
        if fb is None:
            return {
                "ok": False,
                "fluid": name,
                "coolprop_name": cp_name,
                "reason": f"CoolProp not installed and no constant fallback for {name!r}",
                "install": _install_hint(),
                "coolprop_available": False,
            }
        rho = fb["density"]
        return {
            "ok": True,
            "fluid": name,
            "coolprop_name": cp_name,
            "T_K": round(float(T_K), 4),
            "P_Pa": round(float(P_Pa), 4),
            "density": rho,
            "viscosity": fb["viscosity"],
            "cp": fb["cp"],
            "conductivity": fb["conductivity"],
            "prandtl": fb["prandtl"],
            "kinematic_viscosity": fb["viscosity"] / rho,
            "fidelity": "constant_fallback",
            "valid_range_ok": True,
            "source": "constant fallback (≈20 °C, 1 atm) — CoolProp not installed",
            "warnings": [
                f"CoolProp absent: returning ≈20 °C constants for {cp_name}, "
                "not f(T,P). " + _install_hint()
            ],
            "coolprop_available": False,
        }

    # CoolProp present: resolve the valid (T, P) envelope, then evaluate.
    warnings: list[str] = []
    valid_range_ok = True
    try:
        t_min = _PropsSI("Tmin", cp_name)
        t_max = _PropsSI("Tmax", cp_name)
    except Exception as exc:
        raise ValueError(f"unknown fluid {name!r} (CoolProp: {exc})") from exc

    t_query = float(T_K)
    if not (t_min <= t_query <= t_max):
        valid_range_ok = False
        t_query = min(max(t_query, t_min), t_max)
        warnings.append(
            f"T={T_K:.2f} K outside {cp_name} valid range "
            f"[{t_min:.1f}, {t_max:.1f}] K — clamped to {t_query:.1f} K")

    def _prop(key: str):
        return float(_PropsSI(key, "T", t_query, "P", float(P_Pa), cp_name))

    try:
        rho = _prop("D")
        mu = _prop("V")
        cp = _prop("C")
        k = _prop("L")
    except ValueError as exc:
        # (T, P) landed in a two-phase / unsupported region — flag and fall back
        # to a saturated-liquid estimate at the clamped T.
        valid_range_ok = False
        warnings.append(f"(T,P) not single-phase for {cp_name}: {exc}")
        try:
            rho = float(_PropsSI("D", "T", t_query, "Q", 0, cp_name))
            mu = float(_PropsSI("V", "T", t_query, "Q", 0, cp_name))
            cp = float(_PropsSI("C", "T", t_query, "Q", 0, cp_name))
            k = float(_PropsSI("L", "T", t_query, "Q", 0, cp_name))
        except Exception as exc2:
            return {
                "ok": False,
                "fluid": name,
                "coolprop_name": cp_name,
                "reason": f"CoolProp could not resolve state: {exc2}",
                "install": _install_hint(),
                "valid_range_ok": False,
                "warnings": warnings,
                "coolprop_available": True,
            }

    prandtl = mu * cp / k if k > 0 else float("inf")

    return {
        "ok": True,
        "fluid": name,
        "coolprop_name": cp_name,
        "T_K": round(float(T_K), 4),
        "P_Pa": round(float(P_Pa), 4),
        "density": rho,
        "viscosity": mu,
        "cp": cp,
        "conductivity": k,
        "prandtl": prandtl,
        "kinematic_viscosity": mu / rho if rho > 0 else float("inf"),
        "fidelity": "equation_of_state",
        "valid_range_ok": valid_range_ok,
        "source": f"CoolProp {_COOLPROP_VERSION} (Helmholtz EOS, BSD-3, in-process)",
        "warnings": warnings,
        "coolprop_available": True,
    }
